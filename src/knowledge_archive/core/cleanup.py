"""Reviewable, snapshot-bound removal of archive entries, never evidence erasure."""

from copy import deepcopy
import hashlib
import json

from .model import validate
from .store import atomic_json, load_json, utcnow


def _token(plan):
    return hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _record_dependencies(record):
    refs = {
        block["record_id"]
        for block in record["blocks"]
        if block["kind"] == "quote"
    }
    if record.get("reply_to"):
        refs.add(record["reply_to"])
    return refs


def _closure(records, roots):
    found, todo = set(), list(roots)
    while todo:
        rid = todo.pop()
        if rid in found or rid not in records:
            continue
        found.add(rid)
        todo.extend(_record_dependencies(records[rid]) - found)
    return found


def _asset_ids(records):
    return {
        block[key]
        for record in records
        for block in record["blocks"]
        if block["kind"] in ("image", "video")
        for key in ("asset", "poster")
        if block.get(key)
    }


def _prepare(store, data, expected, request):
    if not isinstance(request, dict) or set(request) - {"entries", "synthesis", "notes"}:
        raise ValueError("Cleanup request accepts only entries, synthesis, notes")
    choices = request.get("entries")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Cleanup request needs a nonempty entries list")
    selected = {}
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {"entry_id", "reason"}:
            raise ValueError("Each cleanup entry needs exactly entry_id and reason")
        eid, reason = choice["entry_id"], choice["reason"]
        if not isinstance(eid, str) or eid not in data["entries"]:
            raise ValueError(f"Unknown cleanup entry: {eid}")
        if eid in selected:
            raise ValueError(f"Duplicate cleanup entry: {eid}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Cleanup reason must be nonempty: {eid}")
        selected[eid] = reason
    result = deepcopy(data)
    details, roots = [], set()
    for eid, reason in selected.items():
        entry = result["entries"].pop(eid)
        record = data["records"][entry["record_id"]]
        roots.update([entry["record_id"], *entry["members"], *entry.get("context", [])])
        details.append({
            "entry_id": eid, "record_id": entry["record_id"],
            "source": record["source"], "source_id": record["source_id"],
            "title": record["title"], "url": record["url"], "reason": reason,
        })
    # A source-level exclusion cannot coexist with an unselected alias entry.
    excluded = {(item["source"], item["source_id"]) for item in details}
    for eid, entry in result["entries"].items():
        record = result["records"][entry["record_id"]]
        if (record["source"], record["source_id"]) in excluded:
            raise ValueError(f"Retained entry identifies a selected source: {eid}")

    repairs = {"synthesis": {}, "notes": {}}
    for field in repairs:
        replacements = request.get(field, {})
        if not isinstance(replacements, dict):
            raise ValueError(f"Cleanup {field} must be an object")
        for key, value in replacements.items():
            if field == "synthesis":
                if key not in data["synthesis"]:
                    raise ValueError(f"Unknown synthesis to replace: {key}")
                before = data["synthesis"][key]
                result["synthesis"][key] = value
            else:
                entry = result["entries"].get(key)
                if not entry or entry.get("label") is None:
                    raise ValueError(f"Unknown labeled retained entry: {key}")
                before = entry["label"].get("note", "")
                entry["label"]["note"] = value
            repairs[field][key] = {"before": before, "after": value}

    candidates = _closure(data["records"], roots)
    retained_roots = set(data["records"]) - candidates
    for entry in result["entries"].values():
        retained_roots.update([entry["record_id"], *entry["members"], *entry.get("context", [])])
    retained = _closure(data["records"], retained_roots)
    removed_records = sorted(candidates - retained)
    removed_assets = sorted(
        _asset_ids(data["records"][rid] for rid in removed_records)
        - _asset_ids(data["records"][rid] for rid in retained)
    )
    for rid in removed_records:
        del result["records"][rid]
    for aid in removed_assets:
        del result["assets"][aid]
    # Broken citations require explicit replacements, never an inferred rewrite.
    validate(result, store)
    plan = {
        "plan_version": 1,
        "archive_root": str(store.root),
        "archive_sha256": expected,
        "request": deepcopy(request),
        "entries": details,
        "remove_records": removed_records,
        "retain_shared_records": sorted(candidates & retained),
        "remove_asset_metadata": removed_assets,
        "retain_media_files": [data["assets"][aid]["file"] for aid in removed_assets],
        "reference_replacements": repairs,
        "retention": {
            "scope": "Remove selected entries and their unshared content from raw/archive.json; not disk erasure.",
            "evidence": "All immutable evidence is retained.",
            "media": "All physical media files are retained, including newly unreferenced files.",
            "backup": "A complete pre-apply archive snapshot and this plan are retained in .archive/cleanup/<approval_token>/.",
            "generated_views": "Existing generated views and old generations remain until build; build does not erase old generations.",
            "sync": "Source identities and reasons remain in deleted_entries to prevent entry reintroduction by sync/ingest. Shared content may still be captured as context.",
        },
        "next_steps": ["Run loom build to publish the changed active view; report any build failure."],
    }
    plan["approval_token"] = _token(plan)
    return plan, result


def preview(store, request):
    data, expected = store.snapshot()
    validate(data, store)
    return _prepare(store, data, expected, request)[0]


def apply(store, plan, approval):
    """The token binds reviewed bytes; the calling Agent obtains human approval."""
    if not isinstance(plan, dict) or approval != plan.get("approval_token"):
        raise ValueError("Approval token must match the reviewed cleanup plan")
    unsigned = {key: value for key, value in plan.items() if key != "approval_token"}
    if _token(unsigned) != approval:
        raise ValueError("Cleanup plan was edited; generate and review a new preview")
    with store.writer():
        data, expected = store.snapshot()
        validate(data, store)
        if plan.get("archive_root") != str(store.root) or plan.get("archive_sha256") != expected:
            raise ValueError("Cleanup plan is stale or belongs to another archive; preview again")
        fresh, result = _prepare(store, data, expected, plan["request"])
        if fresh != plan:
            raise ValueError("Cleanup impact changed; generate and review a new preview")
        deleted = result.setdefault("deleted_entries", {})
        deleted_at = utcnow()
        for item in plan["entries"]:
            deleted[item["entry_id"]] = {
                key: item[key] for key in ("source", "source_id", "record_id", "reason")
            } | {"deleted_at": deleted_at}
        validate(result, store)
        backup = store.runtime / "cleanup" / approval
        if not backup.resolve().is_relative_to(store.root):
            raise ValueError("Cleanup backup path escapes archive root")
        for name, value in (("archive.json", data), ("plan.json", plan)):
            path = backup / name
            if path.exists() or path.is_symlink():
                if path.is_symlink() or load_json(path) != value:
                    raise ValueError(f"Existing cleanup backup differs: {name}")
            else:
                atomic_json(path, value)
        store.save(result, expected)
    return {
        "status": "ok", "removed_entries": [item["entry_id"] for item in plan["entries"]],
        "removed_records": plan["remove_records"],
        "removed_asset_metadata": plan["remove_asset_metadata"],
        "backup": str(backup), "retention": plan["retention"],
        "next_steps": plan["next_steps"],
    }

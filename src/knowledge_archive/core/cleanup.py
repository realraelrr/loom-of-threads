"""Preview and remove selected archive entries, never erase source evidence."""

from copy import deepcopy
import uuid

from .model import validate
from .store import atomic_json, utcnow


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


def _prepare(store, data, request):
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
    plan = {
        "archive_root": str(store.root),
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
            "backup": "A complete pre-apply archive snapshot and this request are retained in .archive/cleanup/<operation_id>/.",
            "generated_views": "The fixed reading view is updated by build; immutable source evidence is retained.",
            "sync": "Source identities and reasons remain in deleted_entries to prevent entry reintroduction by sync/ingest. Shared content may still be captured as context.",
        },
        "next_steps": ["Run loom build to publish the changed active view; report any build failure."],
    }
    return plan, result


def preview(store, request):
    data = store.load()
    validate(data, store)
    impact, result = _prepare(store, data, request)
    try:
        validate(result, store)
    except ValueError as error:
        impact["blocking_errors"] = str(error).splitlines()
    else:
        impact["blocking_errors"] = []
    return impact


def apply(store, request):
    """The host confirms the requested entries and repairs before this command."""
    with store.writer():
        data, expected = store.snapshot()
        validate(data, store)
        impact, result = _prepare(store, data, request)
        deleted = result.setdefault("deleted_entries", {})
        deleted_at = utcnow()
        for item in impact["entries"]:
            deleted[item["entry_id"]] = {
                key: item[key] for key in ("source", "source_id", "record_id", "reason")
            } | {"deleted_at": deleted_at}
        validate(result, store)
        backup = store.runtime / "cleanup" / uuid.uuid4().hex
        if not backup.resolve().is_relative_to(store.root):
            raise ValueError("Cleanup backup path escapes archive root")
        atomic_json(backup / "archive.json", data)
        atomic_json(backup / "request.json", request)
        store.save(result, expected)
    return {
        "status": "ok", "removed_entries": [item["entry_id"] for item in impact["entries"]],
        "removed_records": impact["remove_records"],
        "removed_asset_metadata": impact["remove_asset_metadata"],
        "backup": str(backup), "retention": impact["retention"],
        "next_steps": impact["next_steps"],
    }

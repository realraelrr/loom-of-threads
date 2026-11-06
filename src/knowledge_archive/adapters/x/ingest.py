"""Validate immutable observations, select related content, and commit atomically."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import re
from .content import from_capture, iso, referenced_ids, reference_scope
from .threads import ThreadGraph
from ...core.model import validate, timestamp
from ...core.store import unique_object


def _capture(capture_dir):
    """Parse, hash and preserve the same bytes, even if the caller resumes capture."""
    capture_dir = Path(capture_dir).resolve()
    report_path = capture_dir / "report.json"
    if not report_path.resolve().is_relative_to(capture_dir):
        raise ValueError("Report path escapes capture directory")
    payloads = {"report.json": report_path.read_bytes()}
    report = json.loads(payloads["report.json"], object_pairs_hook=unique_object)
    if report.get("schema_version") != 1 or report.get("status") not in (
        "observed",
        "partial",
    ):
        raise ValueError(
            "Only observed/partial successful capture reports may be ingested"
        )
    selected = report["selected_id"]
    depth = report.get("reference_depth", 1)
    if type(depth) is not int or depth != 1:
        raise ValueError("Unsupported capture reference_depth")
    if (
        not isinstance(selected, str)
        or not selected.isascii()
        or not selected.isdigit()
    ):
        raise ValueError("Selected X ID must be a decimal string")
    if (
        not isinstance(report.get("observation_files"), list)
        or not report["observation_files"]
    ):
        raise ValueError("Capture report must name observation files")
    chosen, ranks, record_issues, seen_visible, global_issues = {}, {}, {}, set(), []
    all_sources = []
    for name in report["observation_files"]:
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or "\\" in name
            or name in payloads
        ):
            raise ValueError("Observation names must be unique relative filenames")
        path = capture_dir / name
        if not path.resolve().is_relative_to(capture_dir):
            raise ValueError("Observation path escapes capture directory")
        payloads[name] = path.read_bytes()
        obs = json.loads(payloads[name], object_pairs_hook=unique_object)
        if obs.get("schema_version") != 1 or obs.get("page_state") != "content":
            raise ValueError("Unsupported or non-content observation")
        url = urlsplit(obs["page_url"])
        if (
            url.scheme != "https"
            or url.hostname != "x.com"
            or url.username
            or url.password
            or not re.fullmatch(r"/[^/]+/status/[0-9]+/?", url.path)
        ):
            raise ValueError("Not an X post observation")
        observed_at = iso(obs["observed_at"])
        if (
            not isinstance(obs.get("records"), list)
            or not isinstance(obs.get("visible_ids"), list)
            or not isinstance(obs.get("issues"), list)
        ):
            raise ValueError("Malformed observation records/visibility/issues")
        ids = [source["id"] for source in obs["records"]]
        if len(ids) != len(set(ids)) or not set(obs["visible_ids"]) <= set(ids):
            raise ValueError("Duplicate records or visible IDs without records")
        seen_visible.update(obs["visible_ids"])
        per_record = {}
        for issue in obs["issues"]:
            if not isinstance(issue.get("code"), str):
                raise ValueError("Extraction issue must contain a code")
            if issue.get("record_id") is None:
                global_issues.append(issue["code"])
            else:
                per_record.setdefault(issue["record_id"], []).append(issue["code"])
        all_sources.extend(obs["records"])
        for source in obs["records"]:
            tid = source["id"]
            source_issues = per_record.get(tid, [])
            rank = (
                not source_issues,
                tid in obs["visible_ids"],
                timestamp(observed_at),
            )
            if tid not in chosen or rank >= ranks[tid]:
                chosen[tid], ranks[tid] = source, rank
                record_issues[tid] = source_issues
    ThreadGraph(
        all_sources
    )  # Reject contradictory source relationships before ranking payloads.
    if selected not in seen_visible:
        raise ValueError("Selected post was never observed as a visible post")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                name: hashlib.sha256(content).hexdigest()
                for name, content in payloads.items()
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return report, chosen, ranks, record_issues, global_issues, payloads, fingerprint


def _save_evidence(store, fingerprint, payloads):
    parent = store.root / "raw/evidence/captures"
    if not parent.resolve().is_relative_to(store.root):
        raise ValueError("Evidence directory escapes archive root")
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / fingerprint
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("Evidence destination is not an ordinary directory")
        if {p.name for p in destination.iterdir()} != set(payloads):
            raise ValueError(
                "Existing evidence directory is incomplete or has unexpected files"
            )
        for name, content in payloads.items():
            path = destination / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ValueError(f"Existing evidence bytes differ: {name}")
        return
    # The canonical evidence directory appears only after every source is written.
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=parent) as staging:
        staging = Path(staging)
        for name, content in payloads.items():
            path = staging / name
            path.write_bytes(content)
            if path.read_bytes() != content:
                raise OSError(f"Evidence verification failed: {name}")
        os.rename(staging, destination)


def ingest(store, capture_dir, collection="likes"):
    if collection not in ("likes", "reposts", "bookmarks"):
        raise ValueError("Unsupported X collection")
    report, chosen, ranks, source_issues, global_issues, payloads, fingerprint = (
        _capture(capture_dir)
    )
    selected = report["selected_id"]
    graph = ThreadGraph(chosen.values())
    thread = graph.resolve(selected)
    roots = set(thread.node_ids) | set(thread.context_ids)
    included, scope = reference_scope(chosen, roots)
    needed = included & chosen.keys()
    missing_references = [
        f"{tid}:missing_reference:{reference}"
        for tid in sorted(roots)
        for reference in referenced_ids(chosen[tid])
        if reference not in chosen
    ]
    selected_rid = "x:" + selected
    with store.writer():
        data, expected = store.snapshot()
        if any(
            deleted["source"] == "x" and deleted["source_id"] == selected
            for deleted in data.get("deleted_entries", {}).values()
        ):
            raise ValueError("Selected X source is suppressed by a local entry deletion")
        # Preserve legacy citation IDs only when they identify this exact source.
        matching = [
            eid for eid, e in data["entries"].items() if e["record_id"] == selected_rid
        ]
        if len(matching) > 1:
            raise ValueError(
                "Multiple entries identify the same X source; resolve explicitly"
            )
        eid = matching[0] if matching else selected_rid
        if eid in data["entries"] and data["entries"][eid]["record_id"] != selected_rid:
            raise ValueError("Entry namespace collision")
        entry = data["entries"].get(eid)
        collections = list(
            dict.fromkeys(
                [*(entry.get("collections", []) if entry else []), collection]
            )
        )
        selected_time = ranks[selected][2]
        existing_selected = data["records"].get(selected_rid)
        stale_selected = bool(
            existing_selected
            and existing_selected.get("observed_at")
            and selected_time < timestamp(existing_selected["observed_at"])
        )
        unresolved = [*global_issues, *missing_references]
        if stale_selected and entry is None:
            needed = {selected}
            unresolved.append("older_selection_observation")
        merged_records = 0
        for tid in sorted(needed):
            rid = "x:" + tid
            source = chosen[tid]
            candidate_assets = deepcopy(data["assets"])
            candidate = from_capture(
                source, candidate_assets, source_issues.get(tid, [])
            )
            existing = data["records"].get(rid)
            if (
                existing
                and existing["author"].get("id")
                and source["author"].get("id")
                and existing["author"]["id"] != source["author"]["id"]
            ):
                raise ValueError("Stable source ID changed author identity")
            if (
                existing
                and existing.get("observed_at")
                and ranks[tid][2] < timestamp(existing["observed_at"])
            ):
                continue
            if (
                existing
                and (existing["coverage"] == "observed" or existing.get("reply_to"))
                and existing.get("reply_to") != candidate.get("reply_to")
                and candidate["coverage"] != "partial"
            ):
                raise ValueError("Confirmed reply relationship changed")
            if candidate["coverage"] == "partial":
                unresolved.extend(f"{tid}:{issue}" for issue in candidate["issues"])
                if existing:
                    continue
            candidate["observed_at"] = ranks[tid][2].isoformat().replace("+00:00", "Z")
            candidate["evidence"] = f"raw/evidence/captures/{fingerprint}"
            merged_records += candidate != existing
            data["records"][rid] = candidate
            data["assets"] = candidate_assets
        if stale_selected and entry is not None:
            # Record observations have independent clocks. The older selection
            # cannot revise entry membership or clear its newer capture gaps.
            entry["collections"] = collections
            validate(data, store)
            _save_evidence(store, fingerprint, payloads)
            store.save(data, expected)
            return {
                "entry": eid,
                "ignored_stale": True,
                "merged_records": merged_records,
                "capture_state": entry["capture_state"],
            }
        members = (
            [selected_rid]
            if stale_selected
            else ["x:" + tid for tid in thread.node_ids]
        )
        previous = entry["members"] if entry else []
        missing_previous = [rid for rid in previous if rid not in members]
        members.extend(missing_previous)
        partial = (
            report["status"] == "partial"
            or unresolved
            or missing_previous
            or thread.missing_parent_ids
            or thread.unresolved_author_ids
            or any(data["records"][rid]["coverage"] == "partial" for rid in members)
        )
        new_entry = {
            "record_id": selected_rid,
            "members": members,
            "context": [] if stale_selected else ["x:" + t for t in thread.context_ids],
            "collections": collections,
            "label": entry.get("label") if entry else None,
            "capture_state": "partial" if partial else "observed",
            "capture_scope": scope,
            "last_captured_at": selected_time.isoformat().replace("+00:00", "Z"),
            "capture_issues": {
                "missing_parents": list(thread.missing_parent_ids),
                "unobserved_previous_members": missing_previous,
                "pending_pages": report.get("pending_page_ids", []),
                "content": sorted(set(unresolved)),
            },
        }
        if entry:
            data["entries"][eid] = new_entry
        else:
            data["entries"] = {eid: new_entry, **data["entries"]}
        validate(data, store)
        _save_evidence(store, fingerprint, payloads)
        store.save(data, expected)
        return {
            "entry": eid,
            "members": len(members),
            "capture_state": new_entry["capture_state"],
            "needs_label": new_entry["label"] is None,
        }

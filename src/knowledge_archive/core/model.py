"""Validate source data at ingestion/build boundaries, not at every access."""

from datetime import datetime
import re
from urllib.parse import urlsplit

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
CITATION = re.compile(r"\[#([^\]]+)\]")
TTL = {"evergreen", "tactical", "ephemeral", "leisure"}
STATUSES = {"active", "expired", "duplicate"}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return dt


def web_url(value):
    if not isinstance(value, str):
        raise ValueError("Content URL must be a string")
    p = urlsplit(value)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError(f"Invalid public content URL: {value!r}")
    return value


def validate(data, store=None):
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)
        return condition

    require(data.get("schema_version") == 1, "Unsupported schema_version")
    for field in ["records", "entries", "assets", "synthesis"]:
        if not isinstance(data.get(field), dict):
            raise ValueError(f"{field} must be an object")
    deleted = data.get("deleted_entries", {})
    if not isinstance(deleted, dict):
        raise ValueError("deleted_entries must be an object")
    deleted_sources = set()
    for eid, item in deleted.items():
        if not isinstance(eid, str) or not ID.fullmatch(eid) or not isinstance(item, dict):
            raise ValueError("Invalid deleted entry")
        for field in ("source", "source_id", "record_id", "reason"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Invalid deleted entry {field}: {eid}")
        require(bool(ID.fullmatch(item["record_id"])), f"Invalid deleted record ID: {eid}")
        timestamp(item.get("deleted_at"))
        require(eid not in data["entries"], f"Deleted entry still active: {eid}")
        identity = (item["source"], item["source_id"])
        deleted_sources.add(identity)
    for eid, entry in data["entries"].items():
        record = data["records"].get(entry.get("record_id"), {}) if isinstance(entry, dict) else {}
        require(
            (record.get("source"), record.get("source_id")) not in deleted_sources,
            f"Deleted source still has an entry: {eid}",
        )
    taxonomy = data.get("taxonomy")
    if not isinstance(taxonomy, list):
        raise ValueError("taxonomy must be a list")
    l1_ids, l2_ids = set(), set()
    for topic in taxonomy:
        if (
            not isinstance(topic, dict)
            or not isinstance(topic.get("id"), str)
            or not isinstance(topic.get("subtopics"), list)
        ):
            raise ValueError("Invalid topic shape")
        require(topic["id"] not in l1_ids, f"Duplicate L1: {topic['id']}")
        require(bool(ID.fullmatch(topic["id"])), "Invalid L1 ID")
        l1_ids.add(topic["id"])
        require(
            isinstance(topic["name"], str) and bool(topic["name"]), "Missing topic name"
        )
        for sub in topic["subtopics"]:
            if (
                not isinstance(sub, dict)
                or not isinstance(sub.get("id"), str)
                or not isinstance(sub.get("name"), str)
                or not sub["name"]
            ):
                raise ValueError("Invalid subtopic shape")
            require(sub["id"] not in l2_ids, f"Duplicate L2: {sub['id']}")
            require(bool(ID.fullmatch(sub["id"])), "Invalid L2 ID")
            l2_ids.add(sub["id"])
    filenames = {}
    for aid, asset in data["assets"].items():
        if not isinstance(asset, dict) or not isinstance(asset.get("file"), str):
            raise ValueError(f"Invalid asset shape: {aid}")
        require(bool(ID.fullmatch(aid)), f"Invalid asset ID: {aid}")
        web_url(asset["url"])
        require(asset["kind"] in ("image", "video"), f"Unsupported asset kind: {aid}")
        if store:
            store.asset_path(asset)
        require(
            asset["file"] not in filenames or filenames[asset["file"]] == aid,
            "Two assets use one filename",
        )
        filenames[asset["file"]] = aid
        if asset.get("sha256"):
            require(
                isinstance(asset["sha256"], str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])),
                f"Invalid digest: {aid}",
            )
            require(
                type(asset.get("size")) is int and asset["size"] > 0,
                f"Invalid asset size: {aid}",
            )
    for rid, record in data["records"].items():
        if not isinstance(record, dict):
            raise ValueError(f"Invalid record shape: {rid}")
        for field in ("source", "source_id", "title"):
            require(isinstance(record.get(field), str), f"Invalid {field}: {rid}")
        require(bool(ID.fullmatch(rid)), f"Invalid record ID: {rid}")
        web_url(record["url"])
        if record["created_at"] is not None:
            timestamp(record["created_at"])
        if not isinstance(record.get("author"), dict) or not isinstance(
            record.get("blocks"), list
        ):
            raise ValueError(f"Invalid author/blocks: {rid}")
        for key in ("id", "name", "handle"):
            require(
                record["author"].get(key) is None
                or isinstance(record["author"][key], str),
                f"Invalid author {key}: {rid}",
            )
        require(
            isinstance(record.get("issues", []), list)
            and all(isinstance(i, str) for i in record.get("issues", [])),
            f"Invalid issues: {rid}",
        )
        for block in record["blocks"]:
            if not isinstance(block, dict):
                raise ValueError(f"Invalid block: {rid}")
            kind = block["kind"]
            require(
                kind in ("markdown", "image", "video", "quote", "divider"),
                f"Unknown block: {kind}",
            )
            if kind == "markdown":
                require(isinstance(block["text"], str), f"Invalid text: {rid}")
            if kind in ("image", "video"):
                require(block["asset"] in data["assets"], f"Missing asset: {rid}")
                if block["asset"] in data["assets"]:
                    require(
                        data["assets"][block["asset"]]["kind"] == kind,
                        f"Asset kind mismatch: {rid}",
                    )
                if block.get("poster"):
                    require(block["poster"] in data["assets"], f"Missing poster: {rid}")
                    if block["poster"] in data["assets"]:
                        require(
                            data["assets"][block["poster"]]["kind"] == "image",
                            f"Poster is not an image: {rid}",
                        )
            if kind == "quote":
                require(
                    isinstance(block["record_id"], str)
                    and bool(ID.fullmatch(block["record_id"])),
                    f"Invalid quote ID: {rid}",
                )
                if block.get("url"):
                    web_url(block["url"])
        require(
            record["coverage"] in ("legacy", "observed", "partial"),
            f"Invalid coverage: {rid}",
        )
    for eid, entry in data["entries"].items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("members"), list)
            or not isinstance(entry.get("context", []), list)
        ):
            raise ValueError(f"Invalid entry shape: {eid}")
        collections = entry.get("collections")
        require(
            isinstance(collections, list)
            and bool(collections)
            and all(isinstance(c, str) and bool(c) for c in collections),
            f"Invalid collections: {eid}",
        )
        require(
            entry.get("capture_state") in ("legacy", "observed", "partial"),
            f"Invalid capture_state: {eid}",
        )
        if entry.get("last_captured_at"):
            timestamp(entry["last_captured_at"])
        require(bool(ID.fullmatch(eid)), f"Invalid entry ID: {eid}")
        require(
            entry["record_id"] in data["records"], f"Missing selected record: {eid}"
        )
        require(
            data["records"].get(entry["record_id"], {}).get("created_at") is not None,
            f"Missing selected publication time: {eid}",
        )
        require(
            entry["record_id"] in entry["members"],
            f"Selected record absent from members: {eid}",
        )
        require(
            len(entry["members"]) == len(set(entry["members"])),
            f"Duplicate member: {eid}",
        )
        for rid in entry["members"] + entry.get("context", []):
            require(rid in data["records"], f"Missing member: {rid}")
        lab = entry.get("label")
        if lab is not None:
            if not isinstance(lab, dict):
                raise ValueError(f"Invalid label: {eid}")
            if not isinstance(lab.get("note", ""), str):
                raise ValueError(f"Invalid label note: {eid}")
            require(lab["l2"] in l2_ids, f"Invalid L2: {eid}")
            require(lab["ttl"] in TTL, f"Invalid TTL: {eid}")
            require(lab["status"] in STATUSES, f"Invalid status: {eid}")
            require(
                lab["status"] == "active" or bool(lab.get("note", "").strip()),
                f"Missing status reason: {eid}",
            )
            if lab.get("reviewed_at"):
                timestamp(lab["reviewed_at"])
            for target in CITATION.findall(lab.get("note", "")):
                other = data["entries"].get(target, {}).get("label")
                require(
                    other is not None and other["l2"] == lab["l2"],
                    f"Invalid note reference: {eid} -> {target}",
                )
    for sid, bullets in data["synthesis"].items():
        require(sid in l2_ids, f"Unknown synthesis category: {sid}")
        if not isinstance(bullets, list) or not all(
            isinstance(b, str) for b in bullets
        ):
            raise ValueError(f"Invalid synthesis: {sid}")
        for bullet in bullets:
            for target in CITATION.findall(bullet):
                lab = data["entries"].get(target, {}).get("label")
                require(
                    lab is not None and lab["l2"] == sid,
                    f"Invalid synthesis reference: {sid} -> {target}",
                )
    if errors:
        raise ValueError("\n".join(errors))


def pending(data):
    return {
        "need_labels": [
            eid for eid, e in data["entries"].items() if e.get("label") is None
        ],
        "need_capture": [
            eid
            for eid, e in data["entries"].items()
            if e.get("capture_state") != "observed"
        ],
        "need_media": [aid for aid, a in data["assets"].items() if not a.get("sha256")],
    }

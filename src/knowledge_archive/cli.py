"""Provider-independent archive commands; source adapters are dispatched here."""

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from .core.store import Store
from .core.model import validate, pending, timestamp
from .core.build import build
from .core.assets import fetch_assets, verify_asset


def archive_pending(store, data):
    result = pending(data)
    result["need_media"] = [
        aid for aid, asset in data["assets"].items() if not verify_asset(store, asset)
    ]
    return result


def execute(args):
    store = Store(args.root)
    if args.command == "migrate-x":
        from .adapters.x.migrate import migrate

        return migrate(store)
    if args.command == "ingest-x":
        from .adapters.x.ingest import ingest

        return ingest(store, args.capture, args.collection)
    if args.command == "build":
        return build(store)
    if args.command == "fetch":
        with store.writer():
            data, expected = store.snapshot()
            validate(data, store)
            failures = fetch_assets(store, data, store.config())
            store.save(data, expected)
            return {"failures": failures, "status": "partial" if failures else "ok"}
    if args.command == "label":
        with store.writer():
            data, expected = store.snapshot()
            old = data["entries"][args.id].get("label") or {}
            label = {**old, "l2": args.l2, "ttl": args.ttl, "status": args.status}
            if args.note is not None:
                label["note"] = args.note
            if args.reviewed_at is not None:
                label["reviewed_at"] = args.reviewed_at
            data["entries"][args.id]["label"] = label
            validate(data, store)
            store.save(data, expected)
        return {"entry": args.id, "label": label}
    if args.command == "synthesis":
        with store.writer():
            data, expected = store.snapshot()
            data["synthesis"][args.l2] = json.loads(
                Path(args.file).read_text(encoding="utf-8")
            )
            validate(data, store)
            store.save(data, expected)
        return {"l2": args.l2}
    data = store.load()
    validate(data, store)
    if args.command == "check":
        return {
            "entries": len(data["entries"]),
            "records": len(data["records"]),
            "assets": len(data["assets"]),
            "pending": archive_pending(store, data),
        }
    if args.command == "pending":
        return archive_pending(store, data)
    if args.command == "watermark":
        entries = [
            eid
            for eid, e in data["entries"].items()
            if args.collection in e["collections"]
        ]
        recent = [
            {
                "entry_id": eid,
                "source": data["records"][data["entries"][eid]["record_id"]]["source"],
                "source_id": data["records"][data["entries"][eid]["record_id"]][
                    "source_id"
                ],
            }
            for eid in entries[:20]
        ]
        return {
            "collection": args.collection,
            "recent_entry_ids": entries[:20],
            "recent_source_ids": [r["source_id"] for r in recent if r["source"] == "x"],
            "recent_entries": recent,
            "count": len(entries),
        }
    if args.command == "audit":
        now = datetime.now(timezone.utc)
        due = []
        for eid, e in data["entries"].items():
            lab = e.get("label")
            if (
                not lab
                or lab["status"] != "active"
                or lab["ttl"] not in ("ephemeral", "tactical")
            ):
                continue
            start = (
                lab.get("reviewed_at") or data["records"][e["record_id"]]["created_at"]
            )
            age = (now - timestamp(start)).days
            if age > {"ephemeral": 30, "tactical": 90}[lab["ttl"]]:
                due.append({"id": eid, "age_days": age, "ttl": lab["ttl"]})
        return {"due": due, "pending": archive_pending(store, data)}
    raise ValueError("Unsupported command")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["check", "pending", "build", "fetch", "audit", "migrate-x"]:
        sub.add_parser(name)
    p = sub.add_parser("watermark")
    p.add_argument("--collection", default="likes")
    p = sub.add_parser("ingest-x")
    p.add_argument("capture", type=Path)
    p.add_argument(
        "--collection", default="likes", choices=["likes", "reposts", "bookmarks"]
    )
    p = sub.add_parser("label")
    p.add_argument("id")
    p.add_argument("--l2", required=True)
    p.add_argument("--ttl", required=True)
    p.add_argument("--status", default="active")
    p.add_argument("--note")
    p.add_argument("--reviewed-at")
    p = sub.add_parser("synthesis")
    p.add_argument("l2")
    p.add_argument("file")
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())

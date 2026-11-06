"""Bounded account discovery and thread capture; semantic curation stays with Agent."""

import fcntl
import re
import subprocess
import time
import uuid

from .capture import CaptureError, capture_thread
from .discover import discover
from .ingest import ingest
from ...core.model import pending, validate
from ...core.store import atomic_json, load_json, utcnow


def sync(
    store,
    client,
    *,
    max_entries=20,
    max_seconds=900,
    max_pages=8,
    max_scrolls=6,
    scan_scrolls=30,
    max_total_scrolls=80,
    resume=None,
):
    if (
        min(max_entries, max_seconds, max_pages, max_total_scrolls) < 1
        or min(max_scrolls, scan_scrolls) < 0
    ):
        raise ValueError("Sync budgets must be positive; scroll budgets may be zero")
    source = store.config().get("sources", {}).get("x", {})
    profile, collections = source.get("profile"), source.get("collections")
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", profile):
        raise ValueError("Configure sources.x.profile with an X handle")
    if (
        not isinstance(collections, list)
        or not collections
        or len(set(collections)) != len(collections)
        or any(c not in ("likes", "reposts") for c in collections)
    ):
        raise ValueError("Configure unique supported sources.x.collections")
    data = store.load()
    validate(data, store)
    runtime = store.runtime / "sync"
    runtime.mkdir(parents=True, exist_ok=True)
    if not runtime.resolve().is_relative_to(store.root):
        raise ValueError("Sync runtime escapes archive root")
    with (runtime / "writer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another account sync is running") from None
        try:
            return _run(
                store,
                client,
                runtime,
                profile,
                collections,
                max_entries,
                max_seconds,
                max_pages,
                max_scrolls,
                scan_scrolls,
                max_total_scrolls,
                resume,
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run(
    store,
    client,
    runtime,
    profile,
    collections,
    max_entries,
    max_seconds,
    max_pages,
    max_scrolls,
    scan_scrolls,
    max_total_scrolls,
    resume,
):
    state_path = store.root / "raw/sync-state.json"
    if not state_path.resolve().is_relative_to(store.root):
        raise ValueError("Sync state path escapes archive root")
    legacy = runtime / "state.json"
    if not state_path.exists() and legacy.exists():
        # Old runtime state is retained as a migration backup. The new raw file
        # is authoritative from this atomic write onward, including after crash.
        atomic_json(state_path, load_json(legacy))
    state = (
        load_json(state_path)
        if state_path.exists()
        else {"schema_version": 1, "profile": profile, "pending": {}, "attempts": {}}
    )
    if state.get("schema_version") != 1 or state["profile"].lower() != profile.lower():
        raise ValueError("Sync state belongs to another account or schema")
    if resume is not None:
        if not isinstance(resume, str) or not re.fullmatch(r"[a-f0-9]{32}", resume):
            raise ValueError("Resume requires a reported sync run ID")
        run_id = resume
        run = load_json(runtime / run_id / "run.json")
        if (
            run["profile"].lower() != profile.lower()
            or run["collections"] != collections
        ):
            raise ValueError("Source configuration changed since this run")
    else:
        run_id = uuid.uuid4().hex
        run = {
            "schema_version": 1,
            "profile": profile,
            "collections": collections,
            "started_at": utcnow(),
            "discoveries": {},
            "processed": [],
            "failures": [],
        }
    folder = runtime / run_id
    folder.mkdir(exist_ok=True)
    if not folder.resolve().is_relative_to(runtime.resolve()):
        raise ValueError("Sync run directory escapes runtime")
    run_path = folder / "run.json"
    atomic_json(run_path, run)
    started = time.monotonic()
    attempts_this_run = 0
    reconciled_requests = 0
    previous_failures = len(run["failures"])
    stopped = None
    try:
        for collection in collections:
            if (
                collection in run["discoveries"]
                and run["discoveries"][collection]["status"] == "observed"
            ):
                continue
            remaining = int(max_seconds - (time.monotonic() - started))
            if remaining < 1:
                stopped = "time_budget"
                break
            data = store.load()
            known = {
                data["records"][e["record_id"]]["source_id"]
                for e in data["entries"].values()
                if collection in e["collections"]
                and data["records"][e["record_id"]]["source"] == "x"
            }
            scans = state.setdefault("scans", {})
            scan = scans.setdefault(
                collection, {"known_ids": sorted(known), "depth": 0}
            )
            # Until a scan reaches its old overlap, imported prefix IDs must not
            # become new stopping anchors or a deeper unseen tail is lost forever.
            atomic_json(state_path, state)
            evidence = store.root / "raw/evidence/discovery" / run_id
            if not evidence.resolve().is_relative_to(store.root):
                raise ValueError("Discovery evidence path escapes archive root")
            evidence.mkdir(parents=True, exist_ok=True)
            output = evidence / (collection + "-" + uuid.uuid4().hex)
            report = discover(
                client,
                profile,
                collection,
                output,
                set(scan["known_ids"]),
                max_scrolls=scan["depth"] + scan_scrolls,
                max_seconds=max(1, remaining // (len(collections) + 1)),
            )
            # Persist discoveries before marking the scan complete: a crash may
            # repeat a scan, but cannot silently discard discovered collection IDs.
            for tid in report["ids"]:
                if not isinstance(tid, str) or not tid.isascii() or not tid.isdigit():
                    raise ValueError("Discovery returned a non-decimal source ID")
                if tid not in known:
                    state["pending"][tid] = list(
                        dict.fromkeys([*state["pending"].get(tid, []), collection])
                    )
            if report["status"] == "observed":
                scans.pop(collection)
            else:
                scan["depth"] = max(
                    scan["depth"], len(report.get("observation_files", [])) - 1
                )
            atomic_json(state_path, state)
            run["discoveries"][collection] = {
                "status": report["status"],
                "stop_reason": report["stop_reason"],
                "count": len(report["ids"]),
                "path": str(output.relative_to(store.root)),
            }
            if report["status"] == "failed":
                run["failures"].append(
                    {
                        "collection": collection,
                        "error": report.get("error") or report["stop_reason"],
                        "details": report.get("failures", []),
                    }
                )
                # A broken list parser does not invalidate already known entry
                # IDs. Authentication/identity failures do affect the account.
                if report["stop_reason"] in (
                    "login_required",
                    "wrong_account",
                    "wrong_route",
                ):
                    stopped = report["stop_reason"]
            atomic_json(run_path, run)
            if stopped:
                break
        if stopped is None:
            data = store.load()
            completed = {}
            for entry in data["entries"].values():
                record = data["records"][entry["record_id"]]
                if record["source"] == "x" and entry["capture_state"] == "observed":
                    completed.setdefault(record["source_id"], set()).update(
                        entry["collections"]
                    )
            # Manual imports/backfill may finish a queued discovery after it was
            # saved. Reconcile each collection against current source truth before
            # spending capture budget; scan anchors and attempt history are separate.
            for tid, requested in list(state["pending"].items()):
                remaining = [c for c in requested if c not in completed.get(tid, set())]
                reconciled_requests += len(requested) - len(remaining)
                if remaining:
                    state["pending"][tid] = remaining
                else:
                    del state["pending"][tid]
            if reconciled_requests:
                atomic_json(state_path, state)
                run["reconciled_requests"] = (
                    run.get("reconciled_requests", 0) + reconciled_requests
                )
            candidates = {
                tid: [c for c in cols if c in collections]
                for tid, cols in state["pending"].items()
                if any(c in collections for c in cols)
            }
            for entry in data["entries"].values():
                record = data["records"][entry["record_id"]]
                if record["source"] == "x" and entry["capture_state"] != "observed":
                    cols = [c for c in entry["collections"] if c in collections]
                    if cols:
                        candidates[record["source_id"]] = list(
                            dict.fromkeys(
                                [*candidates.get(record["source_id"], []), *cols]
                            )
                        )
            # Attempt count survives new runs. A broken early item cannot starve
            # later historical entries, even when each run has a small budget.
            queue = sorted(
                candidates,
                key=lambda tid: (
                    tid != run.get("active"),
                    tid not in {f.get("id") for f in run["failures"]},
                    state["attempts"].get(tid, {}).get("count", 0),
                    state["attempts"].get(tid, {}).get("at", ""),
                ),
            )
            for tid in queue:
                remaining = int(max_seconds - (time.monotonic() - started))
                if attempts_this_run >= max_entries or remaining < 1:
                    stopped = (
                        "entry_budget"
                        if attempts_this_run >= max_entries
                        else "time_budget"
                    )
                    break
                previous = state["attempts"].get(tid, {})
                capture_dir = (
                    store.root / previous["capture"]
                    if previous.get("capture")
                    else folder / ("capture-" + tid)
                )
                if not capture_dir.resolve().is_relative_to(runtime.resolve()):
                    raise ValueError("Capture path escapes sync runtime")
                can_resume = (capture_dir / "checkpoint.json").is_file()
                if capture_dir.exists() and not can_resume:
                    capture_dir = folder / ("capture-" + tid + "-" + uuid.uuid4().hex)
                attempts_this_run += 1
                state["attempts"][tid] = {
                    "count": previous.get("count", 0) + 1,
                    "at": utcnow(),
                    "capture": str(capture_dir.relative_to(store.root)),
                    "status": "interrupted",
                }
                atomic_json(state_path, state)
                run["active"] = tid
                atomic_json(run_path, run)
                imported = False
                try:
                    report = capture_thread(
                        client,
                        tid,
                        capture_dir,
                        max_pages,
                        max_scrolls,
                        resume=can_resume,
                        max_total_scrolls=max_total_scrolls,
                        max_seconds=remaining,
                    )
                    state["attempts"][tid]["status"] = report["status"]
                    if report["status"] in ("observed", "partial"):
                        for collection in candidates[tid]:
                            ingest(store, capture_dir, collection)
                        unhandled = [
                            c
                            for c in state["pending"].get(tid, [])
                            if c not in candidates[tid]
                        ]
                        if unhandled:
                            state["pending"][tid] = unhandled
                        else:
                            state["pending"].pop(tid, None)
                        imported = True
                    else:
                        run["failures"].append(
                            {"id": tid, "error": report.get("error", "capture failed")}
                        )
                    if "login" in report.get("error", "").lower() or any(
                        v.get("stop_reason") == "login_required"
                        for v in report.get("visits", [])
                    ):
                        stopped = "login_required"
                except (
                    CaptureError,
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RuntimeError,
                    subprocess.TimeoutExpired,
                ) as error:
                    state["attempts"][tid]["status"] = "failed"
                    run["failures"].append({"id": tid, "error": str(error)})
                    if "login" in str(error).lower():
                        stopped = "login_required"
                atomic_json(state_path, state)
                if imported:
                    if tid not in run["processed"]:
                        run["processed"].append(tid)
                run.pop("active", None)
                atomic_json(run_path, run)
                if stopped:
                    break
    except (
        CaptureError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        run["failures"].append({"error": str(error)})
        stopped = "failed"
    finally:
        atomic_json(state_path, state)
        run["stop_reason"] = stopped or "queue_processed"
        run["updated_at"] = utcnow()
        atomic_json(run_path, run)
    outstanding = pending(store.load())
    active_failures = [
        f
        for i, f in enumerate(run["failures"])
        if i >= previous_failures
        or (
            f.get("id") and state["attempts"].get(f["id"], {}).get("status") == "failed"
        )
        or (
            f.get("collection")
            and run["discoveries"].get(f["collection"], {}).get("status") == "failed"
        )
    ]
    partial = bool(
        stopped
        or state["pending"]
        or outstanding["need_capture"]
        or active_failures
        or any(d["status"] != "observed" for d in run["discoveries"].values())
    )
    summary = {
        "run_id": run_id,
        "status": "partial" if partial else "ok",
        "coverage": "observed_only",
        "stop_reason": run["stop_reason"],
        "processed_this_invocation": attempts_this_run,
        "processed_total": len(run["processed"]),
        "discoveries": run["discoveries"],
        "failures": active_failures,
        "pending_discovered": len(state["pending"]),
        "reconciled_requests": reconciled_requests,
        "pending_counts": {k: len(v) for k, v in outstanding.items()},
        "needs_curation": outstanding["need_labels"],
        "report": str(run_path.relative_to(store.root)),
    }
    atomic_json(folder / "report.json", summary)
    return summary

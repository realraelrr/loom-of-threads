"""Discover explicit collection entries through a bounded visible-page scan."""

import json
import re
import subprocess
import time
import uuid
from importlib.resources import files
from pathlib import Path

from .capture import CaptureError
from ...core.store import atomic_json, digest


def collection_script(profile, collection, token, scroll=False):
    reader = (
        files(__package__).joinpath("read_collection.js").read_text(encoding="utf-8")
    )
    args = json.dumps(profile) + "," + json.dumps(collection)
    guard = (
        "if(window["
        + json.dumps(token)
        + ']!==true)throw new Error("Discovery tab ownership lost");'
    )
    movement = "window.scrollBy(0,window.innerHeight*0.75);" if scroll else ""
    return (
        "async () => {"
        + guard
        + "const read=("
        + reader
        + ");"
        + movement
        + "const deadline=Date.now()+"
        + ("2000" if scroll else "10000")
        + ";let result;"
        + "do {await new Promise(r=>setTimeout(r,250));result=read("
        + args
        + ");"
        + (
            'if(result.page_state!=="content"&&result.page_state!=="unresolved"&&result.page_state!=="unresolved_account")break;'
            if scroll
            else 'if(!["unresolved","unresolved_account"].includes(result.page_state))break;'
        )
        + "}while(Date.now()<deadline);return result;}"
    )


def discover(
    client,
    profile,
    collection,
    output,
    known_ids,
    max_scrolls=40,
    max_seconds=180,
    overlap=20,
):
    """Save evidence and return only IDs/report; never claim platform completeness.

    The overlap window counts distinct newly encountered selected IDs, not repeated
    virtualized frames. A quiet viewport is partial, never proof of collection end.
    """
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", profile):
        raise ValueError("Expected an X profile handle")
    if collection not in ("likes", "reposts"):
        raise ValueError("Supported collections: likes, reposts")
    if max_scrolls < 0 or max_seconds <= 0 or overlap < 2:
        raise ValueError("Invalid discovery budgets or overlap window")
    known = set(known_ids)
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9]+", v) for v in known):
        raise ValueError("Known IDs must be decimal strings")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    token = "__loom_discovery_" + uuid.uuid4().hex
    report = {
        "schema_version": 1,
        "profile": profile,
        "collection": collection,
        "status": "interrupted",
        "coverage": "observed_only",
        "ids": [],
        "observation_files": [],
        "observation_hashes": {},
        "failures": [],
        "stop_reason": "interrupted",
    }
    page_id = None
    seen, visible, consecutive, idle = set(), set(), 0, 0
    try:
        before = {p["id"] for p in client.call("list_pages")["pages"]}
        url = f"https://x.com/{profile}/{collection}"
        pages = client.call("new_page", url, "--background", "--timeout=25000")["pages"]
        created = [p for p in pages if p["id"] not in before]
        if len(created) != 1:
            raise CaptureError("Cannot identify newly created discovery tab")
        page_id = created[0]["id"]
        routes = ["/" + profile.lower() + "/reposts"]
        if collection == "likes":
            routes = ["/" + profile.lower() + "/likes", "/i/history/likes"]
        claim = (
            '() => {const valid=location.hostname==="x.com"&&'
            + json.dumps([*routes, "/i/flow/login"])
            + '.includes(location.pathname.replace(/\\/$/,"").toLowerCase());'
            + "if(valid)Object.defineProperty(window,"
            + json.dumps(token)
            + ",{value:true,configurable:true});return {owned:valid};}"
        )
        if client.evaluate(page_id, claim).get("owned") is not True:
            raise CaptureError("Cannot establish discovery tab ownership")
        report["status"] = "partial"
        report["stop_reason"] = "scroll_budget"
        for step in range(max_scrolls + 1):
            if time.monotonic() - started >= max_seconds:
                report["stop_reason"] = "time_budget"
                break
            obs = client.evaluate(
                page_id, collection_script(profile, collection, token, step > 0)
            )
            name = f"observation-{step:04d}.json"
            atomic_json(output / name, obs)
            report["observation_files"].append(name)
            report["observation_hashes"][name] = digest(output / name)
            if obs["page_state"] != "content":
                report["stop_reason"] = obs["page_state"]
                report["status"] = (
                    "failed"
                    if not seen
                    or obs["page_state"]
                    in ("login_required", "wrong_account", "wrong_route")
                    else "partial"
                )
                break
            if obs.get("issues"):
                report["failures"].extend(obs["issues"])
            new_visible = set(obs["visible_ids"]) - visible
            visible.update(obs["visible_ids"])
            idle = 0 if new_visible else idle + 1
            for tid in obs["selected_ids"]:
                if tid in seen:
                    continue
                seen.add(tid)
                report["ids"].append(tid)
                consecutive = consecutive + 1 if tid in known else 0
            if consecutive >= overlap and not report["failures"]:
                report["status"] = "observed"
                report["stop_reason"] = "known_overlap"
                break
            if idle >= 3:
                report["stop_reason"] = "stalled_viewport"
                break
            # Makes interruption recoverable by an outer sync runner without
            # assuming that a partly scanned collection is already complete.
            atomic_json(output / "report.json", report)
    except (
        CaptureError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        report["status"] = "failed"
        report["stop_reason"] = "error"
        report["error"] = str(error)
    finally:
        if page_id is not None:
            try:
                if client.owns_page(page_id, token):
                    client.call("close_page", str(page_id))
                else:
                    report["failures"].append("tab_ownership_lost_not_closed")
            except (
                CaptureError,
                OSError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as error:
                report["failures"].append("tab_cleanup_failed: " + str(error))
        if report["failures"] and report["status"] == "observed":
            report["status"] = "partial"
        report["failures"] = list(dict.fromkeys(report["failures"]))
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        atomic_json(output / "report.json", report)
    return report

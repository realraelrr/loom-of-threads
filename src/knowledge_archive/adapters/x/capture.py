"""Bounded, read-only thread probe using the official chrome-devtools CLI.

The CLI owns its MCP transport. This module does not implement MCP or access
Chrome profile files. Start a connected daemon before running the probe.
"""

import argparse
import fcntl
import json
import subprocess
import time
import uuid
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from .threads import ThreadGraph
from .content import reference_scope
from ...core.store import atomic_json, load_json, digest


class CaptureError(RuntimeError):
    pass


def decode_evaluation(result: dict) -> dict:
    message = result.get("message", "")
    prefix = "Script ran on page and returned:\n```json\n"
    suffix = "\n```"
    if not message.startswith(prefix) or not message.endswith(suffix):
        raise CaptureError(
            "Unexpected evaluate_script response; check the official CLI version"
        )
    return json.loads(message[len(prefix) : -len(suffix)])


class ChromeDevTools:
    def __init__(self, command: list[str]):
        self.command = command
        self.calls = 0
        self.response_bytes = 0

    def call(self, tool: str, *args: str) -> dict:
        self.calls += 1
        result = subprocess.run(
            [*self.command, tool, *args, "--output-format=json"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.response_bytes += len(result.stdout.encode("utf-8"))
        if result.returncode:
            raise CaptureError(
                f"{tool} failed: {(result.stderr or result.stdout)[-1000:]}"
            )
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            detail = ""
            if isinstance(response, list):
                for block in response:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        detail = (detail + "\n" + block["text"][:1000])[:1000]
                        if len(detail) == 1000:
                            break
            raise CaptureError(
                f"{tool}: unexpected CLI response type {type(response).__name__}"
                + (f": {detail.strip()}" if detail else "")
            )
        if response.get("error") or response.get("isError"):
            raise CaptureError(f"{tool}: {response}")
        return response

    def evaluate(self, page_id: int, script: str) -> dict:
        return decode_evaluation(
            self.call(
                "evaluate_script",
                script,
                f"--pageId={page_id}",
                "--waitForStableDom=false",
            )
        )

    def claim_page(self, page_id: int, target: str, token: str) -> bool:
        script = (
            "() => {const key="
            + json.dumps(token)
            + ";const target="
            + json.dumps(target)
            + ";"
            + (
                "const valid=location.hostname==='x.com'&&(location.pathname.endsWith('/status/'+target)||location.pathname==='/i/flow/login');"
                "if(valid)Object.defineProperty(window,key,{value:true,configurable:true});return {owned:valid};}"
            )
        )
        return self.evaluate(page_id, script).get("owned") is True

    def owns_page(self, page_id: int, token: str) -> bool:
        return (
            self.evaluate(
                page_id, "() => ({owned:window[" + json.dumps(token) + "]===true})"
            ).get("owned")
            is True
        )


def reader_script(
    target_id: str, scroll: bool = False, token: str | None = None
) -> str:
    reader = files(__package__).joinpath("read_page.js").read_text(encoding="utf-8")
    guard = (
        ""
        if token is None
        else "if(window["
        + json.dumps(token)
        + "]!==true)throw new Error('Capture tab ownership lost');"
    )
    return (
        "async () => {"
        + guard
        + " const read = ("
        + reader
        + "); const target = "
        + json.dumps(target_id)
        + ";"
        + (
            "const before=read().visible_ids.join(',');"
            "const labels=/^(Show more replies|Show additional replies|显示更多回复|显示其他回复)$/i;"
            "const button=[...document.querySelectorAll('main [role=button]')].find(e=>labels.test(e.innerText.trim())&&e.getClientRects().length);"
            "if(button)button.click();else window.scrollBy(0,window.innerHeight*0.75);"
            "const deadline=Date.now()+2000; let result; do { await new Promise(r=>setTimeout(r,250));"
            "result=read(); if(result.visible_ids.join(',')!==before||result.page_state==='login_required')break;"
            "}while(Date.now()<deadline); result.expanded_reply_control=!!button; return result;}"
            if scroll
            else "const deadline=Date.now()+10000; let result; do { result=read();"
            "if(result.visible_ids.includes(target)||result.page_state==='login_required')break;"
            "await new Promise(r=>setTimeout(r,250));}while(Date.now()<deadline); return result;}"
        )
    )


def _progress(observations: list[dict], selected_id: str, done: set[str], reference_done=()):
    """Derive current work from evidence; persisted queue is only a report."""
    best, ranks, issues_by_id, page_issues = {}, {}, {}, {}
    observed_bodies = set()
    for obs in observations:
        grouped = {}
        for issue in obs["issues"]:
            grouped.setdefault(issue["record_id"], []).append(issue)
        page_issues[obs["page_url"]] = grouped.get(None, [])
        for record in obs["records"]:
            rid = record["id"]
            issues = grouped.get(rid, [])
            rank = (not issues, rid in obs["visible_ids"], obs["observed_at"])
            if rid not in best or rank >= ranks[rid]:
                best[rid], ranks[rid], issues_by_id[rid] = record, rank, issues
                observed_bodies.discard(rid)
                if rid in obs["visible_ids"] and not issues and not grouped.get(None):
                    observed_bodies.add(rid)
    if selected_id not in best:
        return None, [selected_id], [], len(best), {
            "reference_depth": 1,
            "excluded_reference_edges": [],
        }
    thread = ThreadGraph(best.values()).resolve(selected_id)
    related = list(dict.fromkeys([*thread.node_ids, *thread.context_ids]))
    # Reuse visible, issue-free accepted bodies even from old checkpoints.
    # Only quiet-page done can certify a thread; body evidence cannot suppress
    # a visit when context/reference roles become thread or unresolved parents.
    done = done | ((set(reference_done) | observed_bodies) - set(thread.node_ids)
                   - set(thread.missing_parent_ids) - set(thread.unresolved_author_ids))
    included, scope = reference_scope(best, related)
    candidates = list(dict.fromkeys([
        *thread.missing_parent_ids, *related, *sorted(included - set(related))
    ]))
    issues = [issue for rid in candidates for issue in issues_by_id.get(rid, [])]
    issues.extend(issue for rows in page_issues.values() for issue in rows)
    needs_details = {
        issue["record_id"] for issue in issues if issue["record_id"] is not None
    }
    # Unknown identities cannot establish a self-reply edge even if the page
    # happens to stop loading additional elements.
    needs_details.update(thread.unresolved_author_ids)
    candidates.extend(
        rid for rid in thread.unresolved_author_ids if rid not in candidates
    )
    queue = [rid for rid in candidates if rid not in done or rid in needs_details]
    if any(issue["record_id"] is None for issue in issues) and selected_id not in queue:
        queue.insert(0, selected_id)
    return thread, queue, issues, len(best), scope


def capture_thread(
    client: ChromeDevTools,
    selected_id: str,
    output: Path,
    max_pages: int,
    max_scrolls: int,
    resume: bool = False,
    max_total_scrolls: int = 80,
    max_seconds: int = 900,
) -> dict:
    output = Path(output)
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
    # A scheduled run and a manual resume can otherwise overwrite each other's
    # checkpoints while both leave apparently valid observation files behind.
    with (output / ".capture.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError(
                "Another process is using this capture directory"
            ) from None
        try:
            return _capture_locked(
                client,
                selected_id,
                output,
                max_pages,
                max_scrolls,
                resume,
                max_total_scrolls,
                max_seconds,
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _capture_locked(
    client,
    selected_id,
    output,
    max_pages,
    max_scrolls,
    resume,
    max_total_scrolls,
    max_seconds,
):
    if resume:
        state = load_json(output / "checkpoint.json")
        if state["selected_id"] != selected_id:
            raise ValueError("Checkpoint belongs to a different selected post")
    else:
        state = {
            "selected_id": selected_id,
            "observations": {},
            "accepted": [],
            "done": [],
            "scrolls": {},
            "queue": [selected_id],
        }
    observations = []
    if not set(state["accepted"]) <= set(state["observations"]):
        raise ValueError("Checkpoint refers to unknown accepted observations")
    for name, sha in state["observations"].items():
        path = output / name
        if (
            path.name != name
            or not path.resolve().is_relative_to(output.resolve())
            or digest(path) != sha
        ):
            raise ValueError("Checkpoint evidence path or hash mismatch")
        if name in state["accepted"]:
            observations.append(load_json(path))
    thread, queue, issues, count, scope = _progress(
        observations, selected_id, set(state["done"]), state.get("reference_done", [])
    )
    state["queue"] = list(queue)
    visited, failures = set(), []
    page_id = None
    token = "__knowledge_archive_" + uuid.uuid4().hex
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "selected_id": selected_id,
        "coverage": "observed_only",
        "status": "interrupted",
        "visits": [],
    }
    atomic_json(output / "checkpoint.json", state)
    try:
        if queue:
            before = {p["id"] for p in client.call("list_pages")["pages"]}
            first = queue[0]
            pages = client.call(
                "new_page",
                f"https://x.com/i/status/{first}",
                "--background",
                "--timeout=25000",
            )["pages"]
            created = [p for p in pages if p["id"] not in before]
            if len(created) != 1:
                raise CaptureError("Cannot identify newly created capture tab")
            page_id = created[0]["id"]
            if not client.claim_page(page_id, first, token):
                raise CaptureError("Could not establish capture tab ownership")
        while (
            queue
            and len(visited) < max_pages
            and time.monotonic() - started < max_seconds
        ):
            target = queue.pop(0)
            if target in visited:
                continue
            if visited:
                if not client.owns_page(page_id, token):
                    raise CaptureError(
                        "Capture tab ownership lost; not navigating reused page ID"
                    )
                init = (
                    "Object.defineProperty(window,"
                    + json.dumps(token)
                    + ",{value:true,configurable:true});"
                )
                client.call(
                    "navigate_page",
                    str(page_id),
                    f"--url=https://x.com/i/status/{target}",
                    "--timeout=25000",
                    "--initScript=" + init,
                )
            visited.add(target)
            body_only = thread is not None and target not in (
                *thread.node_ids, *thread.missing_parent_ids, *thread.unresolved_author_ids
            )
            prior_scrolls = state["scrolls"].get(target, 0)
            if prior_scrolls >= max_total_scrolls and not body_only:
                report["visits"].append(
                    {
                        "target_id": target,
                        "stop_reason": "total_scroll_budget",
                        "visible_posts": 0,
                    }
                )
                continue
            limit = min(prior_scrolls + max_scrolls, max_total_scrolls)
            seen, idle = set(), 0
            stop_reason = "scroll_budget"
            for step in range(limit + 1):
                if time.monotonic() - started >= max_seconds:
                    stop_reason = "time_budget"
                    break
                obs = client.evaluate(
                    page_id, reader_script(target, scroll=step > 0, token=token)
                )
                name = "observation-" + uuid.uuid4().hex + ".json"
                atomic_json(output / name, obs)
                state["observations"][name] = digest(output / name)
                atomic_json(output / "checkpoint.json", state)
                if obs["page_state"] == "login_required":
                    raise CaptureError(
                        "X login required; saved observations remain resumable"
                    )
                route = urlparse(obs["page_url"])
                if (
                    route.scheme != "https"
                    or route.hostname not in ("x.com", "www.x.com")
                    or not route.path.rstrip("/").endswith("/status/" + target)
                ):
                    failures.append(
                        {
                            "target_id": target,
                            "reason": "unexpected_route",
                            "url": obs["page_url"],
                        }
                    )
                    raise CaptureError(
                        "Unexpected route; evidence retained but not merged"
                    )
                if obs["page_state"] != "content":
                    failures.append(
                        {
                            "target_id": target,
                            "reason": "page_not_content",
                            "state": obs["page_state"],
                        }
                    )
                    stop_reason = "page_not_content"
                    break
                state["accepted"].append(name)
                observations.append(obs)
                atomic_json(output / "checkpoint.json", state)
                if step == 0 and target not in obs["visible_ids"]:
                    failures.append(
                        {"target_id": target, "reason": "target_not_observed"}
                    )
                    stop_reason = "target_not_observed"
                    break
                ids = set(obs["visible_ids"])
                idle = idle + 1 if ids <= seen else 0
                seen.update(ids)
                state["scrolls"][target] = max(prior_scrolls, step)
                atomic_json(output / "checkpoint.json", state)
                if thread is not None and target not in thread.node_ids:
                    current, _, _, _, _ = _progress(
                        observations, selected_id, set(state["done"]),
                        state.get("reference_done", []),
                    )
                    if (
                        target in obs["visible_ids"]
                        and any(record["id"] == target for record in obs["records"])
                        and not any(issue["record_id"] in (None, target) for issue in obs["issues"])
                        and target not in (*current.node_ids, *current.missing_parent_ids,
                                           *current.unresolved_author_ids)
                    ):
                        stop_reason = (
                            "context_observed" if target in current.context_ids
                            else "direct_reference_observed"
                        )
                        break
                if (
                    step >= prior_scrolls
                    and idle >= 2
                    and not obs.get("expanded_reply_control")
                ):
                    stop_reason = "no_new_visible_nodes"
                    break
            report["visits"].append(
                {
                    "target_id": target,
                    "stop_reason": stop_reason,
                    "visible_posts": len(seen),
                }
            )
            thread, _, issues, count, scope = _progress(
                observations, selected_id, set(state["done"]), state.get("reference_done", [])
            )
            if thread is None:
                raise CaptureError(
                    "Selected post was not observed; not an empty successful thread"
                )
            if stop_reason == "no_new_visible_nodes":
                state["done"] = list(dict.fromkeys([*state["done"], target]))
            elif stop_reason in ("direct_reference_observed", "context_observed"):
                state["reference_done"] = list(dict.fromkeys([
                    *state.get("reference_done", []), target
                ]))
            thread, pending, issues, count, scope = _progress(
                observations, selected_id, set(state["done"]), state.get("reference_done", [])
            )
            state["queue"] = pending
            queue = [rid for rid in pending if rid not in visited]
            atomic_json(output / "checkpoint.json", state)
        report["status"] = (
            "partial" if state["queue"] or failures or issues else "observed"
        )
    except (
        CaptureError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        report["status"] = "failed"
        report["error"] = str(error)
    finally:
        if report["status"] in ("failed", "interrupted"):
            try:
                thread, pending, issues, count, scope = _progress(
                    observations, selected_id, set(state["done"]),
                    state.get("reference_done", []),
                )
                state["queue"] = pending
            except (ValueError, KeyError, TypeError) as error:
                # Contradictory/malformed evidence may itself have caused the
                # failure. Keep the last valid pending snapshot and original error.
                failures.append({"reason": "progress_recompute_failed", "detail": str(error)})
        if page_id is not None:
            try:
                # Browser/MCP reconnects may recycle numeric page IDs. Only
                # close a document still bearing this run's ownership marker.
                if client.owns_page(page_id, token):
                    client.call("close_page", str(page_id))
                else:
                    failures.append({"reason": "tab_ownership_lost_not_closed"})
            except (
                CaptureError,
                OSError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as error:
                failures.append({"reason": "tab_cleanup_failed", "detail": str(error)})
        if failures and report["status"] == "observed":
            report["status"] = "partial"
        if thread is not None:
            report["thread"] = thread.to_dict()
            report["observed_unique_posts"] = count
        report.update(
            {
                **scope,
                "observation_files": list(state["accepted"]),
                "pending_page_ids": state["queue"],
                "failures": failures,
                "extraction_issues": list(
                    {json.dumps(i, sort_keys=True): i for i in issues}.values()
                ),
                "tool_calls": client.calls,
                "transport_response_bytes": client.response_bytes,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
        )
        atomic_json(output / "checkpoint.json", state)
        atomic_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selected_id")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-scrolls", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-total-scrolls", type=int, default=80)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--devtools-command", nargs="+", default=["chrome-devtools"])
    args = parser.parse_args()
    if (
        not args.selected_id.isascii()
        or not args.selected_id.isdigit()
        or args.max_pages < 1
        or args.max_scrolls < 0
        or args.max_total_scrolls < 1
        or args.max_seconds < 1
    ):
        parser.error(
            "Use a decimal post ID, positive page budget, and nonnegative scroll budget"
        )
    try:
        report = capture_thread(
            ChromeDevTools(args.devtools_command),
            args.selected_id,
            args.output,
            args.max_pages,
            args.max_scrolls,
            args.resume,
            args.max_total_scrolls,
            args.max_seconds,
        )
    except (OSError, ValueError, KeyError, CaptureError) as error:
        parser.exit(1, f"Cannot create capture directory: {error}\n")
    # Only the report reaches the Agent; captured source content remains on disk.
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

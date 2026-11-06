"""Reason-based capture cooldowns; deferred work remains pending and recoverable."""

from datetime import datetime, timedelta
import hashlib
from importlib.resources import files


def extractor_revision():
    digest = hashlib.sha256()
    for name in ("read_page.js", "content.py", "threads.py"):
        digest.update(files(__package__).joinpath(name).read_bytes())
    return digest.hexdigest()


def retry_schedule(report, previous, at, revision):
    if report.get("status") == "observed":
        return {}
    reasons = [v.get("stop_reason", "") for v in report.get("visits", [])]
    reasons += [v.get("reason", "") for v in report.get("failures", [])]
    error = report.get("error", "")
    issues = report.get("extraction_issues", [])
    if "login" in error.lower() or "login_required" in reasons:
        kind, reason = "authentication", "login_required"
    elif issues:
        kind, reason = "extraction", "extraction_issues"
    elif "progress_recompute_failed" in reasons:
        kind, reason = "diagnostic", "progress_recompute_failed"
    elif any(r in ("target_not_observed", "page_not_content", "unexpected_route") for r in reasons):
        kind = "source_unresolved"
        reason = next(r for r in reasons if r in (
            "target_not_observed", "page_not_content", "unexpected_route"))
    elif any(r.endswith("budget") for r in reasons) or (
        report.get("pending_page_ids") and not error
    ):
        kind = "budget"
        reason = next((r for r in reasons if r.endswith("budget")), "page_budget")
    else:
        kind = "transient" if report.get("status") == "failed" else "incomplete"
        reason = error or "unclassified_partial"
    consecutive = (
        previous.get("consecutive_failures", 0) + 1
        if previous.get("retry_kind") == kind and previous.get("failure_reason") == reason
        else 1
    )
    delays = {
        "transient": (1, 3, 7, 14),
        "authentication": (1, 3, 7, 14),
        "budget": (7, 14, 30),
        "extraction": (30,),
        "source_unresolved": (14, 30),
        "diagnostic": (30,),
        "incomplete": (7, 14, 30),
    }[kind]
    next_retry = datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(
        days=delays[min(consecutive - 1, len(delays) - 1)]
    )
    return {
        "retry_kind": kind,
        "failure_reason": reason,
        "failure_details": issues or report.get("failures", []),
        "consecutive_failures": consecutive,
        "next_retry_at": next_retry.isoformat(timespec="seconds").replace("+00:00", "Z"),
        # Accepted JSON can be ingested again after a parser repair, but does
        # not necessarily contain the missing source fields. Review it first.
        "deferred_reason": (
            "existing_evidence_needs_review" if kind == "extraction" else kind + "_cooldown"
        ),
        "extractor_revision": revision,
    }


def is_deferred(attempt, now, revision):
    next_retry = attempt.get("next_retry_at")
    if not next_retry:
        return False
    if (attempt.get("retry_kind") == "extraction"
            and attempt.get("extractor_revision") != revision):
        return False
    return datetime.fromisoformat(next_retry.replace("Z", "+00:00")) > datetime.fromisoformat(
        now.replace("Z", "+00:00")
    )

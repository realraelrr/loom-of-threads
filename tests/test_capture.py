import json
import fcntl
from pathlib import Path
import tempfile
import unittest

from knowledge_archive.adapters.x.capture import (
    CaptureError,
    capture_thread,
    decode_evaluation,
)


def observation(ids=("100", "101"), target="101", state="content", issues=None):
    return {
        "schema_version": 1,
        "page_url": f"https://x.com/example/status/{target}",
        "page_state": state,
        "observed_at": "2026-11-05T00:00:00Z",
        "visible_ids": list(ids),
        "issues": issues or [],
        "coverage": {"tweet_elements": len(ids), "extracted_elements": len(ids)},
        "records": [
            {
                "id": id,
                "author": {"id": "10"},
                "reply_to_id": "100" if id == "101" else None,
                "reply_to_author_id": "10" if id == "101" else None,
            }
            for id in ids
        ],
    }


class Browser:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.calls, self.response_bytes = 0, 0
        self.closed = []
        self.owned = True

    def call(self, tool, *args):
        self.calls += 1
        if tool == "list_pages":
            return {"pages": [{"id": 7}]}
        if tool == "new_page":
            return {"pages": [{"id": 7}, {"id": 19}]}
        if tool == "close_page":
            self.closed.append(args[0])
        return {}

    def claim_page(self, page_id, target, token):
        self.calls += 1
        return self.owned

    def owns_page(self, page_id, token):
        self.calls += 1
        return self.owned

    def evaluate(self, page_id, script):
        self.calls += 1
        value = next(self.observations)
        if isinstance(value, Exception):
            raise value
        return value


class CaptureTests(unittest.TestCase):
    def run_capture(self, browser, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            report = capture_thread(browser, "101", destination, **kwargs)
            saved = json.loads((destination / "report.json").read_text())
            self.assertEqual(saved, report)
            for name in report["observation_files"]:
                self.assertTrue((destination / name).exists())
            return report

    def test_page_and_scroll_limits_are_reported_as_partial(self):
        browser = Browser([observation()])
        report = self.run_capture(browser, max_pages=1, max_scrolls=0)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(set(report["pending_page_ids"]), {"100", "101"})
        self.assertEqual(report["thread"]["node_ids"], ["100", "101"])
        self.assertEqual(browser.closed, ["19"])

    def test_login_failure_preserves_evidence_and_closes_only_owned_tab(self):
        login = observation(ids=(), state="login_required")
        login["page_url"] = "https://x.com/i/flow/login"
        browser = Browser([login])
        report = self.run_capture(browser, max_pages=1, max_scrolls=0)
        self.assertEqual(report["status"], "failed")
        self.assertIn("login", report["error"])
        self.assertEqual(len(report["observation_files"]), 0)
        self.assertEqual(browser.closed, ["19"])

    def test_target_id_prefix_is_not_a_matching_route(self):
        report = self.run_capture(
            Browser([observation(target="1010")]), max_pages=1, max_scrolls=0
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][0]["reason"], "unexpected_route")

    def test_interruption_keeps_previous_observations(self):
        report = self.run_capture(
            Browser([observation(), CaptureError("network interrupted")]),
            max_pages=1,
            max_scrolls=1,
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["observation_files"]), 1)

    def test_unavailable_selected_post_is_not_an_empty_success(self):
        report = self.run_capture(
            Browser([observation(ids=())]), max_pages=1, max_scrolls=0
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][0]["reason"], "target_not_observed")

    def test_resume_preserves_prior_evidence_and_continues_pending_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            first = capture_thread(Browser([observation()]), "101", destination, 1, 0)
            prior_names = first["observation_files"][:]
            second = capture_thread(
                Browser([observation(target="100")]),
                "101",
                destination,
                1,
                0,
                resume=True,
            )
            self.assertTrue(set(prior_names) <= set(second["observation_files"]))
            self.assertEqual(second["thread"]["node_ids"], ["100", "101"])
            self.assertEqual(second["status"], "partial")

    def test_resume_rejects_changed_evidence_before_browser_access(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            first = capture_thread(Browser([observation()]), "101", destination, 1, 0)
            (destination / first["observation_files"][0]).write_text("{}")
            browser = Browser([])
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                capture_thread(browser, "101", destination, 1, 1, resume=True)
            self.assertEqual(browser.calls, 0)

    def test_loading_or_error_state_is_not_treated_as_exhausted_content(self):
        good = observation(ids=("100",), target="100")
        unresolved = observation(ids=(), target="100", state="unresolved")
        with tempfile.TemporaryDirectory() as temp:
            report = capture_thread(
                Browser([good, unresolved]), "100", Path(temp) / "probe", 1, 2
            )
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["pending_page_ids"], ["100"])
            self.assertEqual(report["visits"][0]["stop_reason"], "page_not_content")
            self.assertEqual(len(report["observation_files"]), 1)

    def test_later_complete_observation_resolves_old_record_issue(self):
        warning = {
            "code": "missing_video_variants",
            "record_id": "100",
            "detail": "needs details",
        }
        early = observation(ids=("100",), target="100", issues=[warning])
        complete = observation(ids=("100",), target="100")
        with tempfile.TemporaryDirectory() as temp:
            report = capture_thread(
                Browser([early, complete, complete]), "100", Path(temp) / "probe", 1, 2
            )
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["extraction_issues"], [])
            self.assertEqual(report["pending_page_ids"], [])

    def test_resume_recomputes_queue_instead_of_trusting_stale_done_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            leaf = observation(ids=("101",), target="101")
            capture_thread(Browser([leaf]), "101", destination, 1, 0)
            checkpoint_path = destination / "checkpoint.json"
            state = json.loads(checkpoint_path.read_text())
            # A prior implementation could checkpoint this state on interruption
            # between page completion and graph/queue recomputation.
            state["done"] = ["101"]
            state["queue"] = ["101"]
            checkpoint_path.write_text(json.dumps(state))
            report = capture_thread(
                Browser([observation(target="100")]),
                "101",
                destination,
                1,
                0,
                resume=True,
            )
            self.assertEqual(report["visits"][0]["target_id"], "100")
            self.assertEqual(report["thread"]["node_ids"], ["100", "101"])

    def test_concurrent_resume_cannot_replace_another_writers_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            capture_thread(Browser([observation()]), "101", destination, 1, 0)
            old = (destination / "checkpoint.json").read_bytes()
            with (destination / ".capture.lock").open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                browser = Browser([])
                with self.assertRaisesRegex(CaptureError, "Another process"):
                    capture_thread(browser, "101", destination, 1, 0, resume=True)
                self.assertEqual(browser.calls, 0)
            self.assertEqual((destination / "checkpoint.json").read_bytes(), old)

    def test_recycled_page_id_is_not_closed(self):
        class RecycledBrowser(Browser):
            def evaluate(self, page_id, script):
                self.owned = False
                raise CaptureError("MCP reconnected and page ID was reused")

        browser = RecycledBrowser([])
        with tempfile.TemporaryDirectory() as temp:
            report = capture_thread(browser, "101", Path(temp) / "probe", 1, 0)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(browser.closed, [])
            self.assertEqual(
                report["failures"][0]["reason"], "tab_ownership_lost_not_closed"
            )

    def test_completed_resume_avoids_opening_a_new_browser_tab(self):
        complete = observation(ids=("100",), target="100")
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            capture_thread(
                Browser([complete, complete, complete]), "100", destination, 1, 2
            )
            browser = Browser([])
            report = capture_thread(browser, "100", destination, 1, 2, resume=True)
            self.assertEqual(browser.calls, 0)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["thread"]["node_ids"], ["100"])

    def test_cli_response_format_drift_fails_explicitly(self):
        with self.assertRaises(CaptureError):
            decode_evaluation({"message": "new response format"})


if __name__ == "__main__":
    unittest.main()

import json
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

from knowledge_archive.adapters.x.capture import (
    CaptureError,
    ChromeDevTools,
    _progress,
    capture_thread,
    decode_evaluation,
)

from test_ingest import article_with_tweets


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
        if isinstance(value, BaseException):
            raise value
        return value


class CaptureTests(unittest.TestCase):
    def test_cli_text_error_array_preserves_connection_diagnostic(self):
        response = [
            {"type": "text", "text": "Chrome is not running."},
            {"type": "text", "text": "DevToolsActivePort could not be read."},
        ]
        result = CompletedProcess([], 0, json.dumps(response), "")
        client = ChromeDevTools(["synthetic-cli"])
        with patch("knowledge_archive.adapters.x.capture.subprocess.run", return_value=result):
            with self.assertRaises(CaptureError) as raised:
                client.call("list_pages")
        self.assertIn("list_pages", str(raised.exception))
        self.assertIn("Chrome is not running", str(raised.exception))
        self.assertIn("DevToolsActivePort", str(raised.exception))
        self.assertEqual(client.calls, 1)

    def test_cli_unknown_response_shapes_fail_explicitly(self):
        for response in (None, True, 12, "unexpected", [], [{"type": "text", "text": 12}]):
            with self.subTest(response=response):
                result = CompletedProcess([], 0, json.dumps(response), "")
                with patch("knowledge_archive.adapters.x.capture.subprocess.run", return_value=result):
                    with self.assertRaisesRegex(CaptureError, "unexpected CLI response type"):
                        ChromeDevTools(["synthetic-cli"]).call("list_pages")

    def test_cli_error_array_diagnostic_is_bounded(self):
        response = [{"type": "text", "text": "x" * 5000}] * 10
        result = CompletedProcess([], 0, json.dumps(response), "")
        with patch("knowledge_archive.adapters.x.capture.subprocess.run", return_value=result):
            with self.assertRaises(CaptureError) as raised:
                ChromeDevTools(["synthetic-cli"]).call("list_pages")
        self.assertLessEqual(len(str(raised.exception)), 1100)

    def test_cli_object_response_is_preserved(self):
        response = {"pages": [{"id": 1}]}
        result = CompletedProcess([], 0, json.dumps(response), "")
        with patch("knowledge_archive.adapters.x.capture.subprocess.run", return_value=result):
            self.assertEqual(ChromeDevTools(["synthetic-cli"]).call("list_pages"), response)

    def run_capture(self, browser, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            report = capture_thread(browser, "101", destination, **kwargs)
            saved = json.loads((destination / "report.json").read_text())
            self.assertEqual(saved, report)
            for name in report["observation_files"]:
                self.assertTrue((destination / name).exists())
            return report

    def test_progress_follows_only_used_explicit_article_references(self):
        obs = observation(ids=("100",), target="100")
        record = obs["records"][0]
        record["article"] = article_with_tweets("200", "200")
        state = record["article"]["content_state"]
        state["entityMap"].append(
            {"key": "unused", "value": {"type": "TWEET", "data": {"tweetId": "999"}}}
        )
        state["blocks"].append({"type": "unstyled", "text": "https://x.com/i/status/888"})
        record["quoted_id"] = "300"
        _, queue, _, _, scope = _progress([obs], "100", {"100"})
        self.assertEqual(queue, ["200", "300"])
        reference = observation(ids=("200",), target="200")["records"][0]
        reference["quoted_id"] = "100"
        obs["records"].append(reference)
        _, queue, _, _, scope = _progress([obs], "100", {"100", "200", "300"})
        self.assertEqual(queue, [])

    def test_confirmed_context_finishes_without_scrolling_comments(self):
        root = observation(ids=("101",), target="101")
        root["records"][0]["reply_to_author_id"] = "20"
        context = observation(ids=("100",), target="100")
        context["records"][0]["author"]["id"] = "20"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            report = capture_thread(Browser([root, root, root, context]),
                                    "101", destination, 2, 2)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["visits"][-1]["stop_reason"], "context_observed")
            state = json.loads((destination / "checkpoint.json").read_text())
            self.assertEqual(state["reference_done"], ["100"])
            self.assertEqual(state["scrolls"]["100"], 0)
            browser = Browser([])
            self.assertEqual(capture_thread(browser, "101", destination, 2, 2, resume=True)["status"], "observed")
            self.assertEqual(browser.calls, 0)

    def test_resume_infers_context_body_from_existing_accepted_evidence(self):
        root = observation(ids=("101", "100"), target="101")
        root["records"][0]["reply_to_author_id"] = "20"
        root["records"][1]["author"]["id"] = "20"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            capture_thread(Browser([root]), "101", destination, 1, 0)
            checkpoint = destination / "checkpoint.json"
            state = json.loads(checkpoint.read_text())
            state["done"] = ["101"]
            state["queue"] = ["100"]
            state.pop("reference_done", None)
            checkpoint.write_text(json.dumps(state))
            browser = Browser([])
            report = capture_thread(browser, "101", destination, 2, 2, resume=True)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(browser.calls, 0)
            self.assertEqual(report["pending_page_ids"], [])
        root["issues"] = [{"record_id": "100", "code": "missing_body"}]
        _, queue, _, _, _ = _progress([root], "101", {"101"})
        self.assertIn("100", queue)
        root["issues"] = [{"record_id": None, "code": "unreadable_page"}]
        _, queue, _, _, _ = _progress([root], "101", {"101"})
        self.assertIn("100", queue)

    def test_unknown_parent_requires_thread_observation_if_same_author(self):
        root = observation(ids=("101",), target="101")
        parent = observation(ids=("100",), target="100")
        with tempfile.TemporaryDirectory() as temp:
            report = capture_thread(Browser([root, root, root, parent, parent, parent]),
                                    "101", Path(temp) / "probe", 2, 2)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["visits"][-1]["stop_reason"], "no_new_visible_nodes")
        _, queue, _, _, _ = _progress([root], "101", {"101"}, ["100"])
        self.assertIn("100", queue)

    def test_context_body_done_cannot_suppress_new_thread_role(self):
        obs = observation(ids=("101", "100"), target="101")
        # Body-level completion from an earlier context role does not certify
        # thread coverage when current evidence identifies an author ancestor.
        _, queue, _, _, _ = _progress([obs], "101", {"101"}, ["100"])
        self.assertIn("100", queue)
        obs["records"][1]["author"]["id"] = None
        obs["records"][0]["reply_to_author_id"] = None
        _, queue, _, _, _ = _progress([obs], "101", {"101"}, ["100"])
        self.assertIn("100", queue)  # Unresolved context is not confirmed context.

    def test_direct_reference_finishes_without_scrolling_comments(self):
        root = observation(ids=("100",), target="100")
        root["records"][0]["quoted_id"] = "200"
        direct = observation(ids=("200",), target="200")
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            browser = Browser([root, root, root, direct])
            report = capture_thread(browser, "100", destination, 2, 2)
            self.assertEqual(report["status"], "observed")
            self.assertEqual([v["stop_reason"] for v in report["visits"]], [
                "no_new_visible_nodes", "direct_reference_observed"
            ])
            state = json.loads((destination / "checkpoint.json").read_text())
            self.assertEqual(state["done"], ["100"])
            self.assertEqual(state["reference_done"], ["200"])
            self.assertEqual(state["scrolls"]["200"], 0)
            resumed = Browser([])
            self.assertEqual(capture_thread(resumed, "100", destination, 2, 2, resume=True)["status"], "observed")
            self.assertEqual(resumed.calls, 0)

    def test_direct_reference_extraction_gaps_do_not_complete(self):
        for issue_id in ("200", None):
            with self.subTest(issue_id=issue_id), tempfile.TemporaryDirectory() as temp:
                root = observation(ids=("100",), target="100")
                root["records"][0]["quoted_id"] = "200"
                direct = observation(ids=("200",), target="200", issues=[
                    {"record_id": issue_id, "code": "missing_body", "detail": "incomplete"}
                ])
                destination = Path(temp) / "probe"
                report = capture_thread(Browser([root, direct]), "100", destination, 2, 0)
                self.assertEqual(report["status"], "partial")
                self.assertNotEqual(report["visits"][-1]["stop_reason"], "direct_reference_observed")
                state = json.loads((destination / "checkpoint.json").read_text())
                self.assertNotIn("200", state.get("reference_done", []))

    def test_reference_that_becomes_thread_node_requires_thread_completion(self):
        root = observation(ids=("100",), target="100")
        root["records"][0]["quoted_id"] = "101"
        direct = observation(ids=("101",), target="101")  # Explicit self-reply to 100.
        with tempfile.TemporaryDirectory() as temp:
            report = capture_thread(Browser([root, root, root, direct, direct, direct]),
                                    "100", Path(temp) / "probe", 2, 2)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["visits"][-1]["stop_reason"], "no_new_visible_nodes")
        _, queue, _, _, _ = _progress([root, direct], "100", {"100"}, ["101"])
        self.assertIn("101", queue)  # Body-only done cannot certify thread coverage.

    def test_resume_discards_deep_queue_but_preserves_evidence(self):
        root = observation(ids=("100",), target="100")
        root["records"][0]["quoted_id"] = "200"
        direct = observation(ids=("200",), target="200")
        direct["records"][0]["quoted_id"] = "300"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            capture_thread(Browser([root, direct]), "100", destination, 2, 0)
            checkpoint = destination / "checkpoint.json"
            state = json.loads(checkpoint.read_text())
            names = list(state["accepted"])
            state["done"] = ["100", "200"]
            state["queue"] = ["300"]  # Old recursive scope checkpoint.
            checkpoint.write_text(json.dumps(state))
            browser = Browser([])
            report = capture_thread(browser, "100", destination, 2, 0, resume=True)
            self.assertEqual(browser.calls, 0)
            self.assertEqual(report["status"], "observed")
            self.assertEqual(report["pending_page_ids"], [])
            self.assertEqual(report["observation_files"], names)
            self.assertEqual(report["reference_depth"], 1)
            self.assertEqual(report["excluded_reference_edges"], [
                {"source_id": "200", "target_id": "300"}
            ])

    def test_article_reference_is_pending_when_capture_budget_ends(self):
        obs = observation(ids=("101",))
        obs["records"][0]["reply_to_id"] = None
        obs["records"][0]["reply_to_author_id"] = None
        obs["records"][0]["article"] = article_with_tweets("200")
        report = self.run_capture(Browser([obs]), max_pages=1, max_scrolls=0)
        self.assertEqual(report["status"], "partial")
        self.assertIn("200", report["pending_page_ids"])

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
        self.assertEqual(report["pending_page_ids"], ["101"])

    def test_failed_first_frame_preserves_checkpoint_pending_and_original_error(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            report = capture_thread(
                Browser([CaptureError("DevToolsActivePort unavailable")]),
                "101", destination, 1, 0,
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error"], "DevToolsActivePort unavailable")
            self.assertEqual(report["pending_page_ids"], ["101"])
            state = json.loads((destination / "checkpoint.json").read_text())
            self.assertEqual(state["queue"], ["101"])

    def test_interrupted_capture_recomputes_pending_from_accepted_evidence(self):
        root = observation(ids=("101",), target="101")
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "probe"
            with self.assertRaises(KeyboardInterrupt):
                capture_thread(Browser([root, KeyboardInterrupt()]), "101", destination, 1, 2)
            report = json.loads((destination / "report.json").read_text())
            state = json.loads((destination / "checkpoint.json").read_text())
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(set(report["pending_page_ids"]), {"100", "101"})
            self.assertEqual(state["queue"], report["pending_page_ids"])

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

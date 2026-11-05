import tempfile
import unittest
from pathlib import Path

from knowledge_archive.adapters.x.discover import discover, collection_script
from knowledge_archive.core.store import load_json, digest


def observation(ids, *, selected=None, state="content", issues=None):
    return {
        "page_state": state,
        "visible_ids": ids,
        "selected_ids": ids if selected is None else selected,
        "issues": issues or [],
    }


class Browser:
    def __init__(self, observations, owned=True):
        self.observations = iter(observations)
        self.owned = owned
        self.closed = []
        self.urls = []

    def call(self, name, *args):
        if name == "list_pages":
            return {"pages": [{"id": 1}]}
        if name == "new_page":
            self.urls.append(args[0])
            return {"pages": [{"id": 1}, {"id": 2}]}
        if name == "close_page":
            self.closed.append(args[0])
            return {}
        raise AssertionError(name)

    def evaluate(self, page_id, script):
        if "Object.defineProperty" in script:
            return {"owned": True}
        return next(self.observations)

    def owns_page(self, page_id, token):
        return self.owned


class DiscoveryTests(unittest.TestCase):
    def test_reposts_uses_dedicated_route(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = Browser([observation(["1"])])
            discover(
                browser, "example", "reposts", Path(temp) / "scan", [], max_scrolls=0
            )
            self.assertEqual(browser.urls, ["https://x.com/example/reposts"])

    def run_scan(self, browser, known=(), **kwargs):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "scan"
        return discover(browser, "example", "likes", self.output, known, **kwargs)

    def test_one_old_id_does_not_stop_and_overlapping_frames_do_not_count(self):
        browser = Browser(
            [observation(["1"]), observation(["1"]), observation(["1", "2"])]
        )
        report = self.run_scan(browser, ["1"], max_scrolls=2, overlap=2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["ids"], ["1", "2"])
        self.assertEqual(report["stop_reason"], "scroll_budget")
        self.assertEqual(browser.closed, ["2"])

    def test_contiguous_distinct_known_window_is_observed_only(self):
        report = self.run_scan(
            Browser([observation(["9", "1", "2"])]), ["1", "2"], overlap=2
        )
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["stop_reason"], "known_overlap")
        self.assertEqual(report["coverage"], "observed_only")
        for name, sha in report["observation_hashes"].items():
            self.assertEqual(digest(self.output / name), sha)
        self.assertEqual(load_json(self.output / "report.json"), report)

    def test_new_item_breaks_known_window(self):
        report = self.run_scan(
            Browser([observation(["1", "9", "2"])]),
            ["1", "2"],
            overlap=2,
            max_scrolls=0,
        )
        self.assertEqual(report["status"], "partial")

    def test_login_and_wrong_account_never_success(self):
        for state in [
            "login_required",
            "wrong_account",
            "wrong_route",
            "unresolved_account",
            "unresolved",
        ]:
            with self.subTest(state=state):
                report = self.run_scan(Browser([observation([], state=state)]))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["stop_reason"], state)

    def test_quiet_viewport_is_partial_not_end(self):
        report = self.run_scan(Browser([observation(["1"])] * 4))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["stop_reason"], "stalled_viewport")

    def test_no_selected_reposts_does_not_claim_empty_collection(self):
        report = self.run_scan(Browser([observation(["1"], selected=[])] * 4))
        self.assertEqual(report["ids"], [])
        self.assertEqual(report["status"], "partial")

    def test_extraction_issues_prevent_overlap_success(self):
        report = self.run_scan(
            Browser([observation(["1", "2"], issues=["unresolved_tweet_element"])]),
            ["1", "2"],
            overlap=2,
            max_scrolls=0,
        )
        self.assertEqual(report["status"], "partial")

    def test_reused_page_id_not_closed(self):
        browser = Browser([observation(["1", "2"])], owned=False)
        report = self.run_scan(browser, ["1", "2"], overlap=2)
        self.assertEqual(browser.closed, [])
        self.assertEqual(report["status"], "partial")

    def test_reader_guard_precedes_scroll(self):
        script = collection_script("example", "likes", "token", True)
        self.assertLess(
            script.index("Discovery tab ownership lost"),
            script.index("window.scrollBy"),
        )


if __name__ == "__main__":
    unittest.main()

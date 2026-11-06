from copy import deepcopy
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_archive.adapters.x.sync import sync
from knowledge_archive.core.store import Store, atomic_json, load_json
from test_engine import example


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "archive.toml").write_text(
            'title="Demo"\n[sources.x]\nprofile="example"\ncollections=["likes","reposts"]\n'
        )
        self.store = Store(self.root)
        data = example()
        template = data["records"].pop("demo:100")
        data["entries"] = {}
        data["synthesis"] = {}
        for tid in ("100", "101", "102"):
            record = deepcopy(template)
            record.update(
                source="x", source_id=tid, url="https://x.com/example/status/" + tid
            )
            data["records"]["x:" + tid] = record
            data["entries"][tid] = {
                "record_id": "x:" + tid,
                "members": ["x:" + tid],
                "context": [],
                "collections": ["likes"],
                "capture_state": "legacy",
                "label": {"l2": "1.1", "ttl": "evergreen", "status": "active"},
            }
        self.store.save(data)
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def discovery(self, client, profile, collection, output, known_ids, **kwargs):
        output.mkdir()
        report = {"ids": [], "status": "observed", "stop_reason": "known_overlap"}
        atomic_json(output / "report.json", report)
        return report

    def capture(self, client, tid, output, *args, **kwargs):
        self.seen.append((tid, kwargs["resume"]))
        output.mkdir(exist_ok=True)
        atomic_json(output / "checkpoint.json", {"selected_id": tid})
        report = {"selected_id": tid, "status": "partial", "visits": []}
        atomic_json(output / "report.json", report)
        return report

    def ingest(self, store, directory, collection):
        tid = load_json(directory / "report.json")["selected_id"]
        data = store.load()
        entry = data["entries"][tid]
        entry["capture_state"] = "partial"
        entry["collections"] = list(dict.fromkeys([*entry["collections"], collection]))
        store.save(data)
        return {"entry": tid}

    def run_sync(self, **kwargs):
        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery
            ),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=self.capture,
            ),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            return sync(self.store, object(), **kwargs)

    def test_bounded_runs_do_not_starve_later_history(self):
        first = self.run_sync(max_entries=1)
        second = self.run_sync(max_entries=1)
        self.assertEqual([r[0] for r in self.seen], ["100", "101"])
        self.assertEqual(first["stop_reason"], "entry_budget")
        self.assertEqual(second["pending_counts"]["need_capture"], 3)
        self.assertEqual(second["needs_curation"], [])
        self.assertFalse((self.root / "view").exists())

    def test_resume_retains_completed_work_and_skips_repeated_scans(self):
        first = self.run_sync(max_entries=1)
        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover",
                side_effect=AssertionError("must reuse scan"),
            ),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=self.capture,
            ),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            second = sync(self.store, object(), max_entries=1, resume=first["run_id"])
        self.assertEqual([r[0] for r in self.seen], ["100", "101"])
        self.assertEqual(second["processed_total"], 2)

    def test_interruption_preserves_capture_for_resume(self):
        def interrupt(*args, **kwargs):
            self.capture(*args, **kwargs)
            raise KeyboardInterrupt()

        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery
            ),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=interrupt,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                sync(self.store, object(), max_entries=1)
        run_id = next(
            p.name for p in (self.root / ".archive/sync").iterdir() if p.is_dir()
        )
        self.run_sync(max_entries=1, resume=run_id)
        self.assertEqual(self.seen[-1], ("100", True))
        # Interrupted item remains queued and its evidence is resumed on retry.
        self.run_sync(max_entries=3)
        self.assertIn(("100", True), self.seen)

    def test_resume_advances_partial_threads_without_repeating_in_same_call(self):
        first = self.run_sync(max_entries=3)
        second = self.run_sync(max_entries=3, resume=first["run_id"])
        self.assertEqual(self.seen[3:], [("100", True), ("101", True), ("102", True)])
        self.assertEqual(second["processed_total"], 3)

    def test_login_failure_stops_before_capture(self):
        def failed(*args, **kwargs):
            return {"ids": [], "status": "failed", "stop_reason": "login_required"}

        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=failed),
            patch("knowledge_archive.adapters.x.sync.capture_thread") as capture,
        ):
            report = sync(self.store, object())
        capture.assert_not_called()
        self.assertEqual(report["stop_reason"], "login_required")
        self.assertEqual(report["status"], "partial")

    def test_one_unresolved_collection_does_not_block_known_history(self):
        def discovery(client, profile, collection, *args, **kwargs):
            if collection == "reposts":
                return {
                    "ids": [],
                    "status": "failed",
                    "stop_reason": "unresolved",
                    "failures": ["unsupported_list_structure"],
                }
            return {"ids": [], "status": "observed", "stop_reason": "known_overlap"}

        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=discovery),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=self.capture,
            ),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            report = sync(self.store, object(), max_entries=1)
        self.assertEqual(self.seen, [("100", False)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["discoveries"]["reposts"]["status"], "failed")
        self.assertEqual(
            report["failures"],
            [
                {
                    "collection": "reposts",
                    "error": "unresolved",
                    "details": ["unsupported_list_structure"],
                }
            ],
        )

    def test_identity_failures_stop_all_account_work(self):
        for reason in ("wrong_account", "wrong_route"):
            with (
                self.subTest(reason=reason),
                patch(
                    "knowledge_archive.adapters.x.sync.discover",
                    return_value={"ids": [], "status": "failed", "stop_reason": reason},
                ),
                patch("knowledge_archive.adapters.x.sync.capture_thread") as capture,
            ):
                report = sync(self.store, object())
                capture.assert_not_called()
                self.assertEqual(report["stop_reason"], reason)
                self.assertEqual(report["failures"][0]["error"], reason)

    def test_observed_pending_is_reconciled_without_recapture_or_resetting_progress(
        self,
    ):
        data = self.store.load()
        for entry in data["entries"].values():
            entry.update(capture_state="observed", collections=["likes", "reposts"])
        self.store.save(data)
        # A disabled collection's already satisfied request can also be removed.
        (self.root / "archive.toml").write_text(
            '[sources.x]\nprofile="example"\ncollections=["likes"]\n'
        )
        state = {
            "schema_version": 1,
            "profile": "example",
            "pending": {tid: ["reposts"] for tid in data["entries"]},
            "attempts": {"100": {"count": 4, "status": "observed", "at": "old"}},
            "scans": {
                "likes": {"known_ids": ["old"], "depth": 17},
                "reposts": {"known_ids": ["older"], "depth": 19},
            },
        }
        atomic_json(self.root / "raw/sync-state.json", state)
        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover",
                return_value={
                    "ids": [],
                    "status": "partial",
                    "stop_reason": "scroll_budget",
                },
            ),
            patch("knowledge_archive.adapters.x.sync.capture_thread") as capture,
        ):
            report = sync(self.store, object())
        capture.assert_not_called()
        after = load_json(self.root / "raw/sync-state.json")
        self.assertEqual(after["pending"], {})
        self.assertEqual(after["attempts"], state["attempts"])
        self.assertEqual(after["scans"], state["scans"])
        self.assertEqual(self.store.load(), data)
        self.assertEqual(report["reconciled_requests"], 3)
        self.assertEqual(report["processed_this_invocation"], 0)
        self.assertEqual(report["pending_discovered"], 0)

    def test_reconciliation_keeps_missing_collection_partial_and_absent_sources(self):
        data = self.store.load()
        data["entries"]["100"]["capture_state"] = "observed"
        data["entries"]["101"]["capture_state"] = "partial"
        data["entries"]["102"]["capture_state"] = "observed"
        # Same source_id on a non-X record must not satisfy an X request.
        data["records"]["demo:102"] = data["records"].pop("x:102")
        data["records"]["demo:102"]["source"] = "demo"
        data["entries"]["102"].update(record_id="demo:102", members=["demo:102"])
        self.store.save(data)
        requested = {
            "100": ["likes", "reposts"],
            "101": ["likes", "reposts"],
            "102": ["likes"],
            "999": ["reposts"],
        }
        atomic_json(
            self.root / "raw/sync-state.json",
            {
                "schema_version": 1,
                "profile": "example",
                "pending": requested,
                "attempts": {},
            },
        )
        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery
            ),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=RuntimeError("offline"),
            ),
        ):
            report = sync(self.store, object())
        self.assertEqual(
            load_json(self.root / "raw/sync-state.json")["pending"],
            {
                "100": ["reposts"],
                "101": ["likes", "reposts"],
                "102": ["likes"],
                "999": ["reposts"],
            },
        )
        self.assertEqual(report["reconciled_requests"], 1)
        self.assertEqual(report["pending_discovered"], 4)
        self.assertEqual(self.store.load(), data)

    def test_processing_enabled_collection_retains_unfulfilled_disabled_request(self):
        (self.root / "archive.toml").write_text(
            '[sources.x]\nprofile="example"\ncollections=["likes"]\n'
        )
        atomic_json(
            self.root / "raw/sync-state.json",
            {
                "schema_version": 1,
                "profile": "example",
                "pending": {"100": ["likes", "reposts"]},
                "attempts": {},
            },
        )
        self.run_sync(max_entries=1)
        self.assertEqual(
            load_json(self.root / "raw/sync-state.json")["pending"],
            {"100": ["reposts"]},
        )
        self.assertNotIn("reposts", self.store.load()["entries"]["100"]["collections"])

    def test_pending_collection_membership_survives_budget_and_failure(self):
        def discovered(client, profile, collection, *args, **kwargs):
            return {
                "ids": ["100"] if collection == "reposts" else [],
                "status": "observed",
                "stop_reason": "known_overlap",
            }

        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=discovered),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=RuntimeError("transport unavailable"),
            ),
        ):
            report = sync(self.store, object(), max_entries=1)
        self.assertEqual(report["pending_discovered"], 1)
        self.assertEqual(len(report["failures"]), 1)
        self.run_sync(max_entries=3, resume=report["run_id"])
        self.assertIn("reposts", self.store.load()["entries"]["100"]["collections"])
        self.assertEqual(load_json(self.root / "raw/sync-state.json")["pending"], {})

    def test_partial_scan_preserves_old_baseline_across_runs_and_resume(self):
        calls = []

        def discovery(client, profile, collection, output, known, **kwargs):
            if collection == "reposts":
                calls.append((set(known), kwargs["max_scrolls"]))
                return {
                    "ids": ["100"],
                    "status": "partial",
                    "stop_reason": "scroll_budget",
                    "observation_files": ["a", "b", "c"],
                }
            return {"ids": [], "status": "observed", "stop_reason": "known_overlap"}

        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=discovery),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=self.capture,
            ),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            first = sync(self.store, object(), max_entries=1, scan_scrolls=2)
            self.assertIn("reposts", self.store.load()["entries"]["100"]["collections"])
            sync(self.store, object(), max_entries=1, scan_scrolls=2)
            sync(
                self.store,
                object(),
                max_entries=1,
                scan_scrolls=4,
                resume=first["run_id"],
            )
        self.assertEqual(calls, [(set(), 2), (set(), 4), (set(), 6)])

    def test_failed_capture_can_retry_same_run_after_login(self):
        def failed(*args, **kwargs):
            self.capture(*args, **kwargs)
            return {"status": "failed", "error": "login_required", "visits": []}

        with (
            patch(
                "knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery
            ),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread", side_effect=failed
            ),
        ):
            first = sync(self.store, object(), max_entries=1)
        self.assertEqual(first["processed_total"], 0)
        second = self.run_sync(max_entries=1, resume=first["run_id"])
        self.assertEqual(second["processed_total"], 1)
        # Historical login failure is preserved on disk but no longer active.
        self.assertFalse(second["failures"])

    def test_parallel_sync_rejected_before_browser_access(self):
        path = self.root / ".archive/sync"
        path.mkdir(parents=True)
        with (path / "writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "Another account sync"):
                self.run_sync()

    def test_budget_cooldown_preserves_pending_and_checkpoint_until_due(self):
        def bounded(*args, **kwargs):
            report = self.capture(*args, **kwargs)
            report["visits"] = [{"stop_reason": "total_scroll_budget"}]
            return report

        with (
            patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-01T00:00:00Z"),
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery),
            patch("knowledge_archive.adapters.x.sync.capture_thread", side_effect=bounded),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            first = sync(self.store, object(), max_entries=3)
        state = load_json(self.root / "raw/sync-state.json")
        self.assertEqual(first["deferred"][0]["next_retry_at"], "2026-01-08T00:00:00Z")
        self.assertEqual(first["deferred"][0]["failure_reason"], "total_scroll_budget")
        checkpoint = self.root / state["attempts"]["100"]["capture"] / "checkpoint.json"
        before = checkpoint.read_bytes()
        with patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-04T00:00:00Z"):
            cooling = self.run_sync(max_entries=3)
        self.assertEqual(cooling["processed_this_invocation"], 0)
        self.assertEqual(cooling["pending_counts"]["need_capture"], 3)
        self.assertEqual(checkpoint.read_bytes(), before)
        with patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-08T00:00:00Z"):
            due = self.run_sync(max_entries=1)
        self.assertEqual(due["processed_this_invocation"], 1)
        self.assertEqual(self.seen[-1], ("100", True))

    def test_extraction_deferred_with_recovery_date_and_parser_change_unlocks(self):
        def broken(*args, **kwargs):
            report = self.capture(*args, **kwargs)
            report["extraction_issues"] = [{"record_id": "100", "reason": "missing_text"}]
            return report

        with (
            patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-01T00:00:00Z"),
            patch("knowledge_archive.adapters.x.sync.extractor_revision", return_value="old"),
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery),
            patch("knowledge_archive.adapters.x.sync.capture_thread", side_effect=broken),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            first = sync(self.store, object(), max_entries=3)
        self.assertEqual(first["deferred"][0]["deferred_reason"], "existing_evidence_needs_review")
        self.assertEqual(first["deferred"][0]["next_retry_at"], "2026-01-31T00:00:00Z")
        with (
            patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-02T00:00:00Z"),
            patch("knowledge_archive.adapters.x.sync.extractor_revision", return_value="new"),
        ):
            second = self.run_sync(max_entries=1)
        self.assertEqual(second["processed_this_invocation"], 1)

    def test_transient_backoff_caps_and_legacy_attempt_report_is_used(self):
        from knowledge_archive.adapters.x.retry import retry_schedule

        previous = {}
        for expected in ("02", "04", "08", "15", "15"):
            previous = retry_schedule(
                {"status": "failed", "error": "transport unavailable"},
                previous, "2026-01-01T00:00:00Z", "revision",
            )
            self.assertEqual(previous["next_retry_at"], f"2026-01-{expected}T00:00:00Z")
        with patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-01T00:00:00Z"):
            self.run_sync(max_entries=3)
        state = load_json(self.root / "raw/sync-state.json")
        for tid, attempt in state["attempts"].items():
            state["attempts"][tid] = {k: attempt[k] for k in ("count", "at", "capture", "status")}
        atomic_json(self.root / "raw/sync-state.json", state)
        with patch("knowledge_archive.adapters.x.sync.utcnow", return_value="2026-01-02T00:00:00Z"):
            report = self.run_sync(max_entries=3)
        self.assertEqual(report["processed_this_invocation"], 0)
        self.assertEqual(len(report["deferred"]), 3)

    def test_new_discovery_precedes_unattempted_history_and_cooled_failures(self):
        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=self.discovery),
            patch("knowledge_archive.adapters.x.sync.capture_thread", side_effect=RuntimeError("offline")),
        ):
            sync(self.store, object(), max_entries=1)

        def discovery(client, profile, collection, *args, **kwargs):
            return {"ids": ["102"], "status": "observed", "stop_reason": "known_overlap"}

        self.discovery = discovery
        report = self.run_sync(max_entries=1)
        self.assertEqual(self.seen, [("102", False)])
        self.assertEqual(report["pending_counts"]["need_capture"], 3)
        self.assertIn("100", [item["id"] for item in report["deferred"]])

    def test_deleted_source_is_suppressed_without_replacing_scan_baseline(self):
        data = self.store.load()
        del data["entries"]["100"]
        data["deleted_entries"] = {"100": {
            "source": "x", "source_id": "100", "record_id": "x:100",
            "reason": "user removed", "deleted_at": "2026-01-01T00:00:00Z",
        }}
        self.store.save(data)
        attempts = {"100": {"count": 2, "at": "2026-01-01T00:00:00Z", "status": "failed"}}
        atomic_json(self.root / "raw/sync-state.json", {
            "schema_version": 1, "profile": "example", "pending": {"100": ["likes"]},
            "attempts": attempts, "scans": {"likes": {"known_ids": ["old"], "depth": 4}},
        })
        anchors = {}

        def discovery(client, profile, collection, output, known, **kwargs):
            anchors[collection] = known
            if collection == "likes":
                # The deleted item was imported from the incomplete scan's
                # prefix. Treating it as a new anchor would hide the old tail.
                if "100" in known:
                    return {"ids": ["100"], "status": "observed", "stop_reason": "known_overlap"}
                return {"ids": ["100", "999"], "status": "partial", "stop_reason": "scroll_budget"}
            return {"ids": ["100"], "status": "partial", "stop_reason": "scroll_budget"}

        self.discovery = discovery
        self.run_sync(max_entries=3)
        state = load_json(self.root / "raw/sync-state.json")
        self.assertEqual(anchors["likes"], {"old"})
        self.assertEqual(anchors["reposts"], {"100"})
        self.assertEqual(state["scans"]["likes"], {"known_ids": ["old"], "depth": 4})
        self.assertEqual(state["attempts"]["100"], attempts["100"])
        self.assertNotIn("100", state["pending"])
        self.assertEqual(state["pending"]["999"], ["likes"])
        self.assertNotIn("100", [tid for tid, _ in self.seen])

    def test_old_attempt_without_report_remains_retryable(self):
        atomic_json(self.root / "raw/sync-state.json", {
            "schema_version": 1, "profile": "example", "pending": {},
            "attempts": {"100": {"count": 4, "at": "2026-01-01T00:00:00Z", "status": "failed"}},
        })
        self.run_sync(max_entries=3)
        self.assertIn(("100", False), self.seen)

    def test_legacy_runtime_state_migrates_without_losing_partial_scan_anchor(self):
        legacy = self.root / ".archive/sync/state.json"
        atomic_json(
            legacy,
            {
                "schema_version": 1,
                "profile": "example",
                "pending": {},
                "attempts": {},
                "scans": {"likes": {"known_ids": ["old"], "depth": 4}},
            },
        )
        baselines = []

        def discovery(client, profile, collection, output, known, **kwargs):
            if collection == "likes":
                baselines.append((known, kwargs["max_scrolls"]))
            return {
                "ids": [],
                "status": "partial",
                "stop_reason": "scroll_budget",
                "observation_files": [],
            }

        with (
            patch("knowledge_archive.adapters.x.sync.discover", side_effect=discovery),
            patch(
                "knowledge_archive.adapters.x.sync.capture_thread",
                side_effect=self.capture,
            ),
            patch("knowledge_archive.adapters.x.sync.ingest", side_effect=self.ingest),
        ):
            sync(self.store, object(), max_entries=1, scan_scrolls=2)
        self.assertEqual(baselines, [({"old"}, 6)])
        self.assertTrue((self.root / "raw/sync-state.json").is_file())
        self.assertEqual(load_json(legacy)["scans"]["likes"]["depth"], 4)

    def test_invalid_config_and_resume_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "Resume requires"):
            self.run_sync(resume="../escape")
        (self.root / "archive.toml").write_text(
            '[sources.x]\nprofile="../escape"\ncollections=["likes"]'
        )
        with self.assertRaisesRegex(ValueError, "profile"):
            self.run_sync()


if __name__ == "__main__":
    unittest.main()

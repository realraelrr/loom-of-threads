from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_archive.core.store import Store, digest
from knowledge_archive.adapters.x.content import from_capture
from knowledge_archive.adapters.x.ingest import ingest, _capture


def source(text="complete source", article=None):
    return {
        "id": "100",
        "author": {"id": "1", "handle": "example", "name": "Example"},
        "created_at": "2026-01-01T00:00:00Z",
        "reply_to_id": None,
        "reply_to_author_id": None,
        "quoted_id": None,
        "text": text,
        "media": [],
        "article": article,
        "urls": [],
        "inline_media": [],
        "richtext_tags": [],
    }


def article_with_tweets(*ids):
    return {
        "title": "Synthetic references",
        "content_state": {
            "blocks": [
                {"type": "atomic", "entityRanges": [{"key": index}]}
                for index, _ in enumerate(ids)
            ],
            "entityMap": [
                {"key": str(index), "value": {"type": "TWEET", "data": {"tweetId": tid}}}
                for index, tid in enumerate(ids)
            ],
        },
    }


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "archive")
        self.store.save(
            {
                "schema_version": 1,
                "records": {},
                "entries": {},
                "assets": {},
                "taxonomy": [
                    {
                        "id": "01",
                        "name": "Topic",
                        "subtopics": [{"id": "1.1", "name": "Sub"}],
                    }
                ],
                "synthesis": {},
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def capture(
        self,
        name,
        record=None,
        status="observed",
        observed="2026-11-05T00:00:00Z",
        issues=None,
    ):
        path = self.root / name
        path.mkdir()
        observation = {
            "schema_version": 1,
            "observed_at": observed,
            "page_url": "https://x.com/example/status/100",
            "page_state": "content",
            "visible_ids": ["100"],
            "records": [record or source()],
            "issues": issues or [],
            "coverage": {"tweet_elements": 1, "extracted_elements": 1},
        }
        (path / "obs.json").write_text(json.dumps(observation))
        (path / "report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "selected_id": "100",
                    "status": status,
                    "observation_files": ["obs.json"],
                    "pending_page_ids": [],
                }
            )
        )
        return path

    def test_article_references_are_records_not_collected_entries(self):
        root = source(article=article_with_tweets("200", "200"))
        root["quoted_id"] = "300"
        capture = self.capture("article-references", root)
        observation = json.loads((capture / "obs.json").read_text())
        for tid in ("200", "300", "400", "999"):
            record = source(text="Observed reference body")
            record["id"] = tid
            if tid == "200":
                record["quoted_id"] = "400"
            if tid == "400":
                record["quoted_id"] = "100"  # Cycles do not expand forever.
            observation["records"].append(record)
        (capture / "obs.json").write_text(json.dumps(observation))
        result = ingest(self.store, capture)
        data = self.store.load()
        self.assertEqual(result["capture_state"], "observed")
        self.assertEqual(set(data["records"]), {"x:100", "x:200", "x:300"})
        self.assertEqual(list(data["entries"]), ["x:100"])
        self.assertEqual(data["entries"]["x:100"]["members"], ["x:100"])
        self.assertEqual(data["entries"]["x:100"]["context"], [])

    def test_nested_reference_is_auditable_boundary_not_missing_content(self):
        root = source(article=article_with_tweets("200"))
        capture = self.capture("scope-boundary", root)
        observation = json.loads((capture / "obs.json").read_text())
        direct = source()
        direct["id"] = "200"
        direct["quoted_id"] = "300"
        observation["records"].append(direct)
        (capture / "obs.json").write_text(json.dumps(observation))
        # Older evidence has no scope field; the default is one edge.
        self.assertEqual(ingest(self.store, capture)["capture_state"], "observed")
        data = self.store.load()
        self.assertEqual(data["entries"]["x:100"]["capture_scope"], {
            "reference_depth": 1,
            "excluded_reference_edges": [{"source_id": "200", "target_id": "300"}],
        })
        quote = data["records"]["x:200"]["blocks"][-1]
        self.assertEqual(quote["record_id"], "x:300")
        self.assertEqual(quote["url"], "https://x.com/i/status/300")
        self.assertNotIn("x:300", data["records"])
        self.assertEqual(list(data["entries"]), ["x:100"])
        previously_observed = source()
        previously_observed["id"] = "300"
        data["records"]["x:300"] = from_capture(previously_observed, {})
        self.store.save(data)
        ingest(self.store, capture)
        self.assertIn("x:300", self.store.load()["records"])

    def test_unsupported_reference_depth_is_rejected(self):
        capture = self.capture("unsupported-depth")
        report_path = capture / "report.json"
        report = json.loads(report_path.read_text())
        report["reference_depth"] = 2
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "reference_depth"):
            ingest(self.store, capture)

    def test_missing_article_and_quote_references_override_observed_report(self):
        record = source(article=article_with_tweets("200"))
        record["quoted_id"] = "300"
        result = ingest(self.store, self.capture("missing-references", record))
        self.assertEqual(result["capture_state"], "partial")
        data = self.store.load()
        self.assertEqual(set(data["records"]), {"x:100"})
        self.assertEqual(
            data["entries"]["x:100"]["capture_issues"]["content"],
            ["100:missing_reference:200", "100:missing_reference:300"],
        )

    def test_partial_reference_keeps_entry_partial(self):
        capture = self.capture("partial-reference", source(article=article_with_tweets("200")))
        observation = json.loads((capture / "obs.json").read_text())
        reference = source(text=None)
        reference["id"] = "200"
        observation["records"].append(reference)
        (capture / "obs.json").write_text(json.dumps(observation))
        self.assertEqual(ingest(self.store, capture)["capture_state"], "partial")

    def test_truncated_partial_cannot_replace_full_article(self):
        article = {
            "title": "Full article",
            "content_state": {
                "blocks": [
                    {
                        "type": "unstyled",
                        "text": "Important long body",
                        "inlineStyleRanges": [],
                        "entityRanges": [],
                    }
                ],
                "entityMap": [],
            },
        }
        ingest(self.store, self.capture("full", source(article=article)))
        before = deepcopy(self.store.load()["records"]["x:100"])
        partial = self.capture(
            "partial",
            source("preview..."),
            status="partial",
            observed="2026-11-06T00:00:00Z",
            issues=[{"code": "truncated_text", "record_id": "100"}],
        )
        result = ingest(self.store, partial)
        self.assertEqual(self.store.load()["records"]["x:100"], before)
        self.assertEqual(result["capture_state"], "partial")
        self.assertIn(
            "100:truncated_text",
            self.store.load()["entries"]["x:100"]["capture_issues"]["content"],
        )

    def test_old_replay_does_not_rollback_content_or_refresh_capture_time(self):
        ingest(self.store, self.capture("new", source("new contents")))
        before = self.store.load()["entries"]["x:100"]["last_captured_at"]
        result = ingest(
            self.store,
            self.capture(
                "old", source("old contents"), observed="2026-01-01T00:00:00Z"
            ),
        )
        self.assertTrue(result["ignored_stale"])
        self.assertEqual(
            self.store.load()["records"]["x:100"]["blocks"][0]["text"], "new contents"
        )
        self.assertEqual(
            self.store.load()["entries"]["x:100"]["last_captured_at"], before
        )

    def test_rejected_partial_assets_do_not_pollute_store(self):
        ingest(self.store, self.capture("full"))
        partial = source(
            article={
                "title": "preview",
                "cover_media": {
                    "media_info": {"original_img_url": "https://pbs.twimg.com/new.jpg"}
                },
            }
        )
        ingest(
            self.store,
            self.capture(
                "partial", partial, status="partial", observed="2026-11-06T00:00:00Z"
            ),
        )
        self.assertEqual(self.store.load()["assets"], {})

    def test_evidence_copy_failure_is_atomic_and_retry_completes(self):
        capture = self.capture("capture")
        before = digest(self.store.path)
        original = Path.write_bytes
        count = [0]

        def fail(path, content):
            if ".staging-" in str(path):
                count[0] += 1
                if count[0] == 2:
                    raise OSError("disk failure")
            return original(path, content)

        with patch.object(Path, "write_bytes", fail):
            with self.assertRaises(OSError):
                ingest(self.store, capture)
        self.assertEqual(digest(self.store.path), before)
        self.assertEqual(
            list((self.store.root / "raw/evidence/captures").iterdir()), []
        )
        ingest(self.store, capture)
        dest = self.store.root / self.store.load()["records"]["x:100"]["evidence"]
        self.assertEqual({p.name for p in dest.iterdir()}, {"report.json", "obs.json"})

    def test_existing_incomplete_evidence_cannot_be_committed(self):
        capture = self.capture("capture")
        fingerprint = _capture(capture)[-1]
        dest = self.store.root / "raw/evidence/captures" / fingerprint
        dest.mkdir(parents=True)
        (dest / "report.json").write_bytes((capture / "report.json").read_bytes())
        before = digest(self.store.path)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ingest(self.store, capture)
        self.assertEqual(digest(self.store.path), before)

    def test_input_changes_after_read_do_not_change_saved_evidence(self):
        capture = self.capture("capture")
        original = (capture / "obs.json").read_bytes()
        from knowledge_archive.adapters.x.ingest import _save_evidence

        def changed(store, fingerprint, payloads):
            changed = json.loads(original)
            changed["records"][0]["text"] = "changed concurrently"
            (capture / "obs.json").write_text(json.dumps(changed))
            _save_evidence(store, fingerprint, payloads)

        with patch("knowledge_archive.adapters.x.ingest._save_evidence", changed):
            ingest(self.store, capture)
        data = self.store.load()
        dest = self.store.root / data["records"]["x:100"]["evidence"]
        self.assertEqual((dest / "obs.json").read_bytes(), original)
        self.assertEqual(
            data["records"]["x:100"]["blocks"][0]["text"], "complete source"
        )

    def test_repeated_import_is_idempotent_and_preserves_collection_reasons(self):
        capture = self.capture("capture")
        ingest(self.store, capture)
        before = digest(self.store.path)
        ingest(self.store, capture)
        self.assertEqual(digest(self.store.path), before)
        ingest(self.store, capture, "reposts")
        self.assertEqual(
            self.store.load()["entries"]["x:100"]["collections"], ["likes", "reposts"]
        )

    def test_legacy_entry_id_reused_only_for_same_source(self):
        capture = self.capture("capture")
        ingest(self.store, capture)
        data = self.store.load()
        data["entries"]["100"] = data["entries"].pop("x:100")
        self.store.save(data)
        self.assertEqual(ingest(self.store, capture)["entry"], "100")
        data = self.store.load()
        data["records"]["demo:100"] = {
            **data["records"]["x:100"],
            "source": "demo",
            "url": "https://example.com/100",
        }
        data["entries"]["100"]["record_id"] = "demo:100"
        data["entries"]["100"]["members"] = ["demo:100"]
        self.store.save(data)
        self.assertEqual(ingest(self.store, capture)["entry"], "x:100")
        self.assertEqual(self.store.load()["entries"]["100"]["record_id"], "demo:100")

    def test_nonpost_route_and_embedded_only_selection_are_rejected(self):
        capture = self.capture("bad-route")
        obs = json.loads((capture / "obs.json").read_text())
        obs["page_url"] = "https://x.com/settings/security"
        (capture / "obs.json").write_text(json.dumps(obs))
        with self.assertRaisesRegex(ValueError, "post observation"):
            ingest(self.store, capture)
        obs["page_url"] = "https://x.com/example/status/100"
        obs["visible_ids"] = []
        (capture / "obs.json").write_text(json.dumps(obs))
        with self.assertRaisesRegex(ValueError, "never observed"):
            ingest(self.store, capture)

    def test_expanded_urls_and_unresolved_inline_layout_are_preserved(self):
        record = source("https://t.co/a")
        record["urls"] = [
            {
                "url": "https://t.co/a",
                "expanded_url": "https://example.com/full",
                "display_url": "example.com/full",
            }
        ]
        record["inline_media"] = [{"media_id": "20", "index": 5}]
        converted = from_capture(record, {})
        self.assertIn("https://example.com/full", json.dumps(converted["blocks"]))
        self.assertEqual(
            converted["source_layout"]["inline_media"], record["inline_media"]
        )
        self.assertEqual(converted["coverage"], "partial")

    def test_invalid_quote_id_rejected(self):
        record = source()
        record["quoted_id"] = "../evil"
        with self.assertRaisesRegex(ValueError, "quoted_id"):
            from_capture(record, {})

    def test_older_capture_can_collect_a_previously_context_only_record(self):
        ingest(self.store, self.capture("new", source("newer known body")))
        data = self.store.load()
        data["entries"] = {}
        self.store.save(data)
        result = ingest(
            self.store,
            self.capture("old", source("older text"), observed="2026-01-01T00:00:00Z"),
            "reposts",
        )
        data = self.store.load()
        self.assertEqual(
            data["records"]["x:100"]["blocks"][0]["text"], "newer known body"
        )
        self.assertEqual(data["entries"]["x:100"]["collections"], ["reposts"])
        self.assertEqual(data["entries"]["x:100"]["members"], ["x:100"])
        self.assertIsNone(data["entries"]["x:100"]["label"])
        self.assertEqual(result["capture_state"], "partial")
        self.assertIn(
            "older_selection_observation",
            data["entries"]["x:100"]["capture_issues"]["content"],
        )

    def test_duplicate_json_keys_in_source_are_rejected(self):
        capture = self.capture("duplicates")
        raw = (
            (capture / "obs.json")
            .read_text()
            .replace(
                '"text": "complete source"',
                '"text": "complete source", "text": "replacement"',
            )
        )
        (capture / "obs.json").write_text(raw)
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            ingest(self.store, capture)

    def test_contradictory_identity_in_one_capture_is_rejected(self):
        capture = self.capture("conflict")
        obs = json.loads((capture / "obs.json").read_text())
        obs["records"][0]["author"]["id"] = "2"
        (capture / "obs2.json").write_text(json.dumps(obs))
        report = json.loads((capture / "report.json").read_text())
        report["observation_files"].append("obs2.json")
        (capture / "report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            ingest(self.store, capture)


if __name__ == "__main__":
    unittest.main()

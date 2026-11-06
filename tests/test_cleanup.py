from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_archive.cli import main
from knowledge_archive.core.cleanup import apply, preview
from knowledge_archive.core.model import validate
from knowledge_archive.core.store import Store, atomic_json, digest, load_json
from test_engine import example


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "archive")
        self.data = example()
        self.store.save(self.data)
        self.request = {
            "entries": [{"entry_id": "100", "reason": "Reviewed duplicate"}],
            "synthesis": {"1.1": []},
        }

    def add_record(self, rid):
        record = deepcopy(self.data["records"]["demo:100"])
        record["source_id"] = rid.split(":")[1]
        self.data["records"][rid] = record
        return record

    def add_entry(self, eid, rid):
        entry = deepcopy(self.data["entries"]["100"])
        entry.update(record_id=rid, members=[rid])
        self.data["entries"][eid] = entry
        return entry

    def test_preview_does_not_mutate_and_apply_backs_up_before_removal(self):
        before = digest(self.store.path)
        plan = preview(self.store, self.request)
        self.assertEqual(digest(self.store.path), before)
        self.assertFalse(self.store.runtime.exists())
        self.assertEqual(plan["remove_records"], ["demo:100"])
        result = apply(self.store, self.request)
        current = self.store.load()
        self.assertEqual(current["entries"], {})
        self.assertEqual(current["records"], {})
        tombstone = current["deleted_entries"]["100"]
        self.assertEqual(tombstone["source_id"], "100")
        self.assertEqual(tombstone["reason"], "Reviewed duplicate")
        validate(current, self.store)
        self.assertEqual(load_json(Path(result["backup"]) / "archive.json"), self.data)
        self.assertEqual(load_json(Path(result["backup"]) / "request.json"), self.request)
        self.assertIn("not disk erasure", result["retention"]["scope"])

    def test_missing_synthesis_and_note_repairs_fail_before_any_write(self):
        self.add_record("demo:200")
        entry = self.add_entry("200", "demo:200")
        entry["label"]["note"] = "Retain the context of [#100]"
        self.store.save(self.data)
        before = digest(self.store.path)
        impact = preview(self.store, {"entries": self.request["entries"]})
        self.assertTrue(any("reference" in error for error in impact["blocking_errors"]))
        with self.assertRaisesRegex(ValueError, "note reference"):
            apply(self.store, self.request)
        self.request["notes"] = {"200": "Context reviewed; entry retained."}
        plan = preview(self.store, self.request)
        self.assertEqual(plan["reference_replacements"]["notes"]["200"]["before"], entry["label"]["note"])
        self.assertEqual(digest(self.store.path), before)
        apply(self.store, self.request)
        self.assertEqual(self.store.load()["entries"]["200"]["label"]["note"], self.request["notes"]["200"])

    def test_shared_members_context_quote_and_reply_dependencies_survive(self):
        for relation in ("members", "context", "quote", "reply_to"):
            with self.subTest(relation=relation):
                self.data = example()
                other = self.add_record("demo:200")
                entry = self.add_entry("200", "demo:200")
                if relation in ("members", "context"):
                    entry[relation].append("demo:100")
                elif relation == "quote":
                    other["blocks"].append({"kind": "quote", "record_id": "demo:100"})
                else:
                    other["reply_to"] = "demo:100"
                self.store.save(self.data)
                plan = preview(self.store, self.request)
                self.assertEqual(plan["remove_records"], [])
                self.assertEqual(plan["retain_shared_records"], ["demo:100"])

    def test_unshared_reference_cycles_removed_but_unrelated_orphans_preserved(self):
        child = self.add_record("demo:200")
        self.data["records"]["demo:100"]["blocks"].append({"kind": "quote", "record_id": "demo:200"})
        child["blocks"].append({"kind": "quote", "record_id": "demo:100"})
        self.add_record("demo:300")["blocks"] = []
        self.store.save(self.data)
        plan = preview(self.store, self.request)
        self.assertEqual(plan["remove_records"], ["demo:100", "demo:200"])
        apply(self.store, self.request)
        self.assertEqual(set(self.store.load()["records"]), {"demo:300"})

    def test_shared_assets_and_posters_retained_physical_files_never_removed(self):
        self.data["assets"] = {
            aid: {"file": aid + ".jpg", "url": "https://example.com/" + aid, "kind": kind}
            for aid, kind in (("private", "image"), ("shared", "image"), ("video", "video"))
        }
        self.data["records"]["demo:100"]["blocks"] += [
            {"kind": "image", "asset": "private"},
            {"kind": "image", "asset": "shared"},
        ]
        other = self.add_record("demo:200")
        other["blocks"] = [{"kind": "video", "asset": "video", "poster": "shared"}]
        self.add_entry("200", "demo:200")
        self.store.media.mkdir()
        for asset in self.data["assets"].values():
            self.store.asset_path(asset).write_bytes(b"synthetic media")
        evidence = self.store.root / "raw/evidence/legacy/source.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("immutable synthetic evidence")
        self.store.save(self.data)
        plan = preview(self.store, self.request)
        self.assertEqual(plan["remove_asset_metadata"], ["private"])
        self.assertEqual(plan["retain_media_files"], ["private.jpg"])
        apply(self.store, self.request)
        self.assertEqual(set(self.store.load()["assets"]), {"video", "shared"})
        self.assertTrue((self.store.media / "private.jpg").is_file())
        self.assertEqual(evidence.read_text(), "immutable synthetic evidence")

    def test_unrelated_edit_does_not_invalidate_request_and_current_dependencies_survive(self):
        preview(self.store, self.request)
        self.add_record("demo:200")["blocks"] = [{"kind": "quote", "record_id": "demo:100"}]
        self.add_entry("200", "demo:200")
        self.store.save(self.data)
        result = apply(self.store, self.request)
        self.assertEqual(result["removed_records"], [])
        self.assertIn("demo:100", self.store.load()["records"])
        self.assertIn("200", self.store.load()["entries"])
        with self.assertRaisesRegex(ValueError, "Unknown cleanup entry"):
            apply(self.store, self.request)

    def test_writer_lock_backup_failure_and_concurrent_change_prevent_commit(self):
        preview(self.store, self.request)
        before = digest(self.store.path)
        with self.store.writer():
            with self.assertRaisesRegex(RuntimeError, "Another process"):
                apply(self.store, self.request)
        with patch("knowledge_archive.core.cleanup.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                apply(self.store, self.request)
        self.assertEqual(digest(self.store.path), before)
        real_save = self.store.save

        def edit_before_save(data, expected):
            changed = deepcopy(self.data)
            changed["records"]["demo:100"]["title"] = "Concurrent edit"
            real_save(changed)
            real_save(data, expected)

        with patch.object(self.store, "save", side_effect=edit_before_save):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                apply(self.store, self.request)
        self.assertEqual(self.store.load()["records"]["demo:100"]["title"], "Concurrent edit")
        self.assertIn("100", self.store.load()["entries"])

    def test_all_aliases_can_be_removed_in_one_reviewed_plan(self):
        self.add_entry("alias", "demo:100")
        self.store.save(self.data)
        self.request["entries"].append({"entry_id": "alias", "reason": "Same source alias"})
        preview(self.store, self.request)
        apply(self.store, self.request)
        current = self.store.load()
        self.assertEqual(current["entries"], {})
        self.assertEqual(set(current["deleted_entries"]), {"100", "alias"})
        self.assertEqual(current["records"], {})
        validate(current, self.store)

    def test_alias_entry_requires_explicit_selection_and_reasons_required(self):
        self.add_entry("alias", "demo:100")
        self.store.save(self.data)
        with self.assertRaisesRegex(ValueError, "Retained entry"):
            preview(self.store, self.request)
        for choices in ([], [{"entry_id": "100", "reason": " "}], self.request["entries"] * 2):
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                preview(self.store, {"entries": choices})

    def test_cli_roundtrip_and_preview_cannot_overwrite_source(self):
        request_path = Path(self.tmp.name) / "request.json"
        plan_path = Path(self.tmp.name) / "plan.json"
        atomic_json(request_path, self.request)
        prefix = ["--root", str(self.store.root)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(prefix + ["cleanup-preview", str(request_path), "--output", str(self.store.path)]), 1)
            self.assertEqual(main(prefix + ["cleanup-preview", str(request_path), "--output", str(plan_path)]), 0)
            self.assertEqual(load_json(plan_path)["blocking_errors"], [])
            self.assertEqual(main(prefix + ["cleanup-apply", str(request_path)]), 0)
        self.assertEqual(self.store.load()["entries"], {})


if __name__ == "__main__":
    unittest.main()

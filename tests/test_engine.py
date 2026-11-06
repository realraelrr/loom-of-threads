from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from knowledge_archive.core.store import Store, digest
from knowledge_archive.core.model import validate, pending
from knowledge_archive.core.build import build
from knowledge_archive.adapters.x.content import inline, asset


def example():
    return {
        "schema_version": 1,
        "taxonomy": [
            {
                "id": "01",
                "name": "Examples",
                "subtopics": [{"id": "1.1", "name": "Basics"}],
            }
        ],
        "records": {
            "demo:100": {
                "source": "demo",
                "source_id": "100",
                "title": "Test",
                "created_at": "2026-01-01T00:00:00Z",
                "url": "https://example.com/100",
                "author": {"id": "1", "name": "Example", "handle": "example"},
                "blocks": [{"kind": "markdown", "text": "Source evidence"}],
                "coverage": "observed",
                "issues": [],
            }
        },
        "entries": {
            "100": {
                "record_id": "demo:100",
                "members": ["demo:100"],
                "context": [],
                "collections": ["manual"],
                "label": {"l2": "1.1", "ttl": "evergreen", "status": "active"},
                "capture_state": "observed",
            }
        },
        "assets": {},
        "synthesis": {"1.1": ["See [#100]"]},
    }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "archive.toml").write_text(
            'title="Example"\nmedia_hosts=["example.com"]\n'
        )
        self.store = Store(self.root)
        self.data = example()
        self.store.save(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_generic_source_build_and_database(self):
        build(self.store)
        with closing(sqlite3.connect(self.root / "view/archive.db")) as c:
            self.assertEqual(
                c.execute("SELECT body FROM entries").fetchone()[0], "Source evidence"
            )
        self.assertTrue((self.root / "view/README.md").is_file())

    def test_fixed_view_reuses_unchanged_files_and_removes_obsolete_outputs(self):
        build(self.store)
        view = self.root / "view"
        self.assertFalse(view.is_symlink())
        doc = next((view / "records").glob("*.md"))
        before = doc.stat().st_mtime_ns
        obsolete = view / "records/obsolete.md"
        obsolete.write_text("old output")
        build(self.store)
        self.assertEqual(doc.stat().st_mtime_ns, before)
        self.assertFalse(obsolete.exists())
        self.assertFalse((self.store.runtime / "builds").exists())
        self.assertFalse(any(p.is_symlink() for p in view.rglob("*")))

    def test_publish_failure_can_be_repaired_by_next_build(self):
        build(self.store)
        self.data["records"]["demo:100"]["title"] = "Updated"
        self.store.save(self.data)
        with patch("knowledge_archive.core.build.shutil.copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                build(self.store)
        build(self.store)
        doc = next((self.root / "view/records").glob("*.md")).read_text()
        self.assertIn("Updated", doc)
        self.assertFalse(list((self.root / "view").rglob("*.tmp")))

    def test_failed_build_keeps_previous_generation(self):
        build(self.store)
        old = (self.root / "view/archive.db").read_bytes()
        with patch(
            "knowledge_archive.core.build.make_db", side_effect=OSError("disk failure")
        ):
            with self.assertRaises(OSError):
                build(self.store)
        self.assertEqual((self.root / "view/archive.db").read_bytes(), old)
        self.assertTrue((self.root / "view/archive.db").is_file())

    def test_changed_source_during_build_is_not_published(self):
        build(self.store)
        old = (self.root / "view/archive.db").read_bytes()
        from knowledge_archive.core.build import make_db

        def change(*args):
            make_db(*args)
            self.data["records"]["demo:100"]["title"] = "edited"
            self.store.save(self.data)

        with patch("knowledge_archive.core.build.make_db", side_effect=change):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                build(self.store)
        self.assertEqual((self.root / "view/archive.db").read_bytes(), old)

    def test_compare_and_swap_preserves_concurrent_edit(self):
        original = digest(self.store.path)
        self.data["synthesis"]["1.1"] = []
        self.store.save(self.data)
        with self.assertRaises(RuntimeError):
            self.store.save(example(), original)
        self.assertEqual(self.store.load()["synthesis"]["1.1"], [])

    def test_duplicate_taxonomy_and_broken_citation_fail(self):
        self.data["taxonomy"][0]["subtopics"].append({"id": "1.1", "name": "Duplicate"})
        with self.assertRaisesRegex(ValueError, "Duplicate L2"):
            validate(self.data)
        self.data = example()
        self.data["synthesis"]["1.1"] = ["See [#missing]"]
        with self.assertRaisesRegex(ValueError, "reference"):
            validate(self.data)

    def test_unlabeled_entries_remain_pending_on_every_check(self):
        self.data["entries"]["100"]["label"] = None
        self.assertEqual(pending(self.data)["need_labels"], ["100"])
        self.assertEqual(pending(self.data)["need_labels"], ["100"])

    def test_shared_url_is_one_asset_and_paths_cannot_escape(self):
        assets = {}
        first = asset(assets, "https://example.com/a.jpg", "image", "first.jpg")
        second = asset(assets, "https://example.com/a.jpg", "image", "second.jpg")
        self.assertEqual(first, second)
        self.assertEqual(len(assets), 1)
        with self.assertRaises(ValueError):
            self.store.asset_path({"file": "../outside"})

    def test_strikethrough_and_unicode_link_content_survive(self):
        self.assertEqual(
            inline("withdrawn", [{"style": "Strikethrough", "offset": 0, "length": 9}]),
            "<del>withdrawn</del>",
        )
        self.assertEqual(
            inline(
                "👉 link",
                [],
                [{"key": 0, "offset": 2, "length": 4}],
                {"0": {"type": "LINK", "data": {"url": "https://example.com"}}},
            ),
            '👉 <a href="https://example.com">link</a>',
        )

    def test_unverified_media_blocks_publication(self):
        key = asset(self.data["assets"], "https://example.com/a.jpg", "image")
        self.data["records"]["demo:100"]["blocks"].append(
            {"kind": "image", "asset": key}
        )
        self.store.save(self.data)
        with self.assertRaisesRegex(ValueError, "Unverified"):
            build(self.store)
        self.assertFalse((self.root / "view").exists())


if __name__ == "__main__":
    unittest.main()

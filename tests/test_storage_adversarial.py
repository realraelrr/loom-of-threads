from argparse import Namespace
from copy import deepcopy
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_engine import example
from knowledge_archive.cli import execute
from knowledge_archive.core.assets import fetch_assets, verify_asset
from knowledge_archive.core.build import build
from knowledge_archive.core.model import validate
from knowledge_archive.core.store import Store
from knowledge_archive.adapters.x.migrate import migrate


class Response(io.BytesIO):
    def __init__(self, payload, status=200, content_range=None):
        super().__init__(payload)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "video/mp4"
        self.headers["Content-Length"] = str(len(payload))
        if content_range is not None:
            self.headers["Content-Range"] = content_range


class Opener:
    def __init__(self, payload, status=200, content_range=None):
        self.payload, self.status, self.content_range = payload, status, content_range

    def open(self, *args, **kwargs):
        return Response(self.payload, self.status, self.content_range)


class StorageAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.save(example())
        (self.store.root / "archive.toml").write_text('title="Example"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_between_snapshot_read_and_caller_use_is_not_published(self):
        build(self.store)
        original_view = (self.store.root / "view/archive.db").read_bytes()
        snapshot = self.store.snapshot

        def edit_after_read():
            data, sha = snapshot()
            new = deepcopy(data)
            new["records"]["demo:100"]["blocks"][0]["text"] = "USER EDIT"
            self.store.save(new)
            return data, sha

        with patch.object(self.store, "snapshot", side_effect=edit_after_read):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                build(self.store)
        self.assertEqual((self.store.root / "view/archive.db").read_bytes(), original_view)
        self.assertEqual(
            self.store.load()["records"]["demo:100"]["blocks"][0]["text"], "USER EDIT"
        )

    def test_label_does_not_overwrite_edit_after_snapshot_read(self):
        original = Store.snapshot

        def edit_after_read(store):
            data, sha = original(store)
            new = deepcopy(data)
            new["records"]["demo:100"]["title"] = "USER EDIT"
            store.save(new)
            return data, sha

        args = Namespace(
            root=self.store.root,
            command="label",
            id="100",
            l2="1.1",
            ttl="evergreen",
            status="active",
            note=None,
            reviewed_at=None,
        )
        with patch.object(Store, "snapshot", edit_after_read):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                execute(args)
        self.assertEqual(self.store.load()["records"]["demo:100"]["title"], "USER EDIT")

    def test_config_snapshot_change_prevents_publication(self):
        snapshot = self.store.config_snapshot

        def edit_after_read():
            data, sha = snapshot()
            (self.store.root / "archive.toml").write_text('title="User edit"\n')
            return data, sha

        with patch.object(self.store, "config_snapshot", side_effect=edit_after_read):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                build(self.store)
        self.assertFalse((self.store.root / "view").exists())

    def test_duplicate_json_keys_fail_instead_of_silently_replacing_data(self):
        self.store.path.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.store.load()

    def test_partial_http_response_is_not_registered_as_complete(self):
        data = {
            "assets": {
                "a": {
                    "kind": "video",
                    "url": "https://example.com/video.mp4",
                    "file": "video.mp4",
                }
            }
        }
        opener = Opener(b"\x00\x00\x00\x18ftypisomrest", 206, "bytes 0-15/100000")
        with patch(
            "knowledge_archive.core.assets.urllib.request.build_opener",
            return_value=opener,
        ):
            failures = fetch_assets(self.store, data, {"media_hosts": ["example.com"]})
        self.assertIn("complete HTTP 200", failures[0]["reason"])
        self.assertNotIn("sha256", data["assets"]["a"])
        self.assertFalse((self.store.media / "video.mp4").exists())

    def test_false_media_mime_cannot_verify_arbitrary_bytes(self):
        data = {
            "assets": {
                "a": {
                    "kind": "video",
                    "url": "https://example.com/video.mp4",
                    "file": "video.mp4",
                }
            }
        }
        with patch(
            "knowledge_archive.core.assets.urllib.request.build_opener",
            return_value=Opener(b"NOT_A_VIDEO"),
        ):
            failures = fetch_assets(self.store, data, {"media_hosts": ["example.com"]})
        self.assertIn("container header", failures[0]["reason"])
        self.assertNotIn("sha256", data["assets"]["a"])

    def test_missing_known_asset_can_be_restored_with_matching_bytes(self):
        payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12
        a = {
            "kind": "video",
            "url": "https://example.com/video.mp4",
            "file": "video.mp4",
        }
        data = {"assets": {"a": a}}
        with patch(
            "knowledge_archive.core.assets.urllib.request.build_opener",
            return_value=Opener(payload),
        ):
            self.assertEqual(
                fetch_assets(self.store, data, {"media_hosts": ["example.com"]}), []
            )
            (self.store.media / "video.mp4").unlink()
            self.assertEqual(
                fetch_assets(self.store, data, {"media_hosts": ["example.com"]}), []
            )
        self.assertTrue(verify_asset(self.store, a))

    def test_unrelated_view_symlink_is_not_replaced(self):
        unrelated = self.store.root / "other"
        unrelated.mkdir()
        (self.store.root / "view").symlink_to("other", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            build(self.store)
        self.assertEqual((self.store.root / "view").readlink(), Path("other"))

    def test_block_cannot_use_a_different_asset_kind(self):
        data = example()
        data["assets"]["a"] = {
            "kind": "image",
            "url": "https://example.com/a.jpg",
            "file": "a.jpg",
        }
        data["records"]["demo:100"]["blocks"].append({"kind": "video", "asset": "a"})
        with self.assertRaisesRegex(ValueError, "kind mismatch"):
            validate(data, self.store)

    def test_legacy_article_metadata_without_body_remains_a_gap(self):
        self.store.path.unlink()
        tweet = {
            "id_str": "100",
            "created_at": "2026-01-01T00:00:00Z",
            "full_text": "teaser",
            "user": {"screen_name": "example", "name": "Example"},
            "extra": {"article": {"title": "Missing article"}},
        }
        raw = {
            "likes_raw.json": [tweet],
            "articles_raw.json": {},
            "videos_raw.json": {},
            "labels.json": {
                "100": {"l2": "1.1", "ttl": "evergreen", "status": "active"}
            },
            "taxonomy.json": example()["taxonomy"],
            "synthesis_l2.json": {},
        }
        for name, value in raw.items():
            (self.store.path.parent / name).write_text(json.dumps(value))
        migrate(self.store)
        record = self.store.load()["records"]["x:100"]
        self.assertTrue(record["is_document"])
        self.assertEqual(record["coverage"], "partial")
        self.assertIn("missing_article_body", record["issues"])
        self.assertEqual(self.store.load()["entries"]["100"]["collections"], ["likes"])
        build(self.store)
        topic = next((self.store.root / "view/topics").glob("*.md")).read_text()
        self.assertIn("missing_article_body", topic)


if __name__ == "__main__":
    unittest.main()

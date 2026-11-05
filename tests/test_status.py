import tempfile
import unittest
from knowledge_archive.cli import archive_pending
from knowledge_archive.core.store import Store, digest


class StatusTests(unittest.TestCase):
    def test_known_digest_does_not_hide_a_missing_local_file(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            store.media.mkdir()
            path = store.media / "a.jpg"
            path.write_bytes(b"\xff\xd8\xffexample")
            data = {
                "entries": {},
                "assets": {
                    "a": {
                        "file": "a.jpg",
                        "kind": "image",
                        "sha256": digest(path),
                        "size": path.stat().st_size,
                    }
                },
            }
            self.assertEqual(archive_pending(store, data)["need_media"], [])
            path.unlink()
            self.assertEqual(archive_pending(store, data)["need_media"], ["a"])


if __name__ == "__main__":
    unittest.main()

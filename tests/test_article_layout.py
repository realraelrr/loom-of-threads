"""Synthetic Draft.js layouts; no account captures belong in public tests."""

import unittest

from knowledge_archive.adapters.x.content import article_blocks, from_capture
from test_ingest import source


def image(mid):
    return {
        "media_id": mid,
        "media_info": {
            "__typename": "ApiImage",
            "original_img_url": f"https://example.com/{mid}.jpg",
        },
    }


def media_entity(key, *ids):
    return {
        "key": str(key),
        "value": {
            "type": "MEDIA",
            "data": {"mediaItems": [{"mediaId": mid} for mid in ids]},
        },
    }


def atomic(key):
    return {
        "type": "atomic",
        "text": " ",
        "entityRanges": [{"offset": 0, "length": 1, "key": key}],
    }


def article(media, blocks, entities):
    return {
        "title": "Synthetic layout",
        "media_entities": media,
        "content_state": {"blocks": blocks, "entityMap": entities},
    }


class ArticleLayoutTests(unittest.TestCase):
    def test_explicit_media_order_beats_asset_list_order(self):
        a = article(
            [image("20"), image("10")],
            [
                {"type": "unstyled", "text": "before"},
                atomic(0),
                {"type": "unstyled", "text": "between"},
                atomic(1),
                {"type": "unstyled", "text": "after"},
            ],
            [media_entity(0, "10"), media_entity(1, "20", "10")],
        )
        assets = {}
        blocks, issues = article_blocks(a, assets, "100")
        self.assertEqual(issues, [])
        self.assertEqual(
            [assets[b["asset"]]["url"] if "asset" in b else b["text"] for b in blocks],
            [
                "before",
                "https://example.com/10.jpg",
                "between",
                "https://example.com/20.jpg",
                "https://example.com/10.jpg",
                "after",
            ],
        )

    def test_incomplete_video_preserves_body_as_partial(self):
        a = article(
            [
                {
                    "media_id": "10",
                    "media_info": {"__typename": "ApiVideo", "variants": []},
                }
            ],
            [{"text": "before"}, atomic(0), {"text": "after"}],
            [media_entity(0, "10")],
        )
        result = from_capture(source(article=a), {})
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("missing_article_video_variants:10", result["issues"])
        self.assertEqual(
            [b["text"] for b in result["blocks"]][-2:], ["before", "after"]
        )

    def test_video_without_poster_retains_playable_source(self):
        a = article(
            [
                {
                    "media_id": "10",
                    "media_info": {
                        "__typename": "ApiVideo",
                        "variants": [
                            {
                                "content_type": "video/mp4",
                                "url": "https://example.com/video.mp4",
                            }
                        ],
                    },
                }
            ],
            [atomic(0)],
            [media_entity(0, "10")],
        )
        blocks, issues = article_blocks(a, {}, "100")
        self.assertEqual(issues, [])
        self.assertEqual(blocks[0]["kind"], "video")
        self.assertNotIn("poster", blocks[0])

    def test_missing_layout_entities_are_explicit_gaps(self):
        a = article(
            [],
            [{"text": "kept"}, atomic(0), atomic(1), {"type": "atomic"}],
            [media_entity(1)],
        )
        blocks, issues = article_blocks(a, {}, "100")
        self.assertEqual(blocks, [{"kind": "markdown", "text": "kept"}])
        self.assertEqual(
            set(issues),
            {
                "missing_article_entity:0",
                "missing_article_media_items",
                "missing_article_atomic_entity",
            },
        )

    def test_unplaced_assets_preserved_with_explicit_layout_gap(self):
        a = article([image("20"), image("10")], [{"text": "body"}], [])
        assets = {}
        blocks, issues = article_blocks(a, assets, "100")
        self.assertEqual(
            issues, ["unplaced_article_media:20", "unplaced_article_media:10"]
        )
        self.assertEqual(
            [assets[b["asset"]]["url"] for b in blocks[1:]],
            ["https://example.com/20.jpg", "https://example.com/10.jpg"],
        )


if __name__ == "__main__":
    unittest.main()

"""One-way migration from the original six-file X archive; no source deletion."""

from .content import article_blocks, asset, image_filename, iso, mp4, inline
from ...core.store import json_snapshot, digest
from ...core.assets import media_header
from ...core.model import validate


def migrate(store):
    with store.writer():
        if store.path.exists():
            raise ValueError(
                "Archive already migrated; refusing to overwrite current data"
            )
        source = store.root / "raw"
        names = (
            "likes_raw.json",
            "articles_raw.json",
            "videos_raw.json",
            "labels.json",
            "taxonomy.json",
            "synthesis_l2.json",
        )
        snapshots = {name: json_snapshot(source / name) for name in names}
        likes = snapshots["likes_raw.json"][0]
        articles = snapshots["articles_raw.json"][0]
        videos = snapshots["videos_raw.json"][0]
        labels = snapshots["labels.json"][0]
        if len({t["id_str"] for t in likes}) != len(likes) or set(labels) != {
            t["id_str"] for t in likes
        }:
            raise ValueError(
                "Legacy duplicate IDs or labels differ from selected posts"
            )
        selected_ids = {t["id_str"] for t in likes}
        if set(articles) - selected_ids or set(videos) - selected_ids:
            raise ValueError("Legacy details contain unknown selected post IDs")
        data = {
            "schema_version": 1,
            "records": {},
            "entries": {},
            "assets": {},
            "taxonomy": snapshots["taxonomy.json"][0],
            "synthesis": snapshots["synthesis_l2.json"][0],
        }
        for t in likes:
            tid = t["id_str"]
            blocks, issues = [], []
            if t.get("full_text"):
                blocks.append({"kind": "markdown", "text": inline(t["full_text"])})
            video_rows = iter(videos.get(tid, []))
            vi = 0
            for i, m in enumerate(t.get("media") or [], 1):
                image = m["media_url_https"]
                aid = asset(
                    data["assets"],
                    image,
                    "image",
                    image_filename(image, f"tweet_{tid}_{i}"),
                )
                if m["type"] in ("video", "animated_gif"):
                    vi += 1
                    v = next(video_rows, None)
                    if v is None:
                        raise ValueError(f"Missing legacy video: {tid}")
                    url = mp4(v["variants"], v.get("duration_millis"))
                    blocks.append(
                        {
                            "kind": "video",
                            "asset": asset(
                                data["assets"], url, "video", f"video_{tid}_{vi}.mp4"
                            ),
                            "poster": aid,
                        }
                    )
                else:
                    blocks.append({"kind": "image", "asset": aid, "alt": "推文配图"})
            if next(video_rows, None) is not None:
                raise ValueError(f"Extra legacy video: {tid}")
            article = articles.get(tid)
            article_metadata = (t.get("extra") or {}).get("article")
            expects_article = article is not None or article_metadata is not None
            if expects_article and not article:
                issues.append("missing_article_body")
            if article:
                content, missing = article_blocks(
                    article, data["assets"], tid, legacy=True
                )
                blocks.extend(content)
                issues.extend(missing)
            quoted = t.get("quoted_status")
            if quoted and quoted.get("full_text"):
                qid = "x:" + quoted["id_str"]
                # An embedded quote is evidence, not another collected entry.
                data["records"].setdefault(
                    qid,
                    {
                        "source": "x",
                        "source_id": quoted["id_str"],
                        "url": f"https://x.com/i/status/{quoted['id_str']}",
                        "created_at": iso(quoted["created_at"])
                        if quoted.get("created_at")
                        else None,
                        "author": {
                            "id": None,
                            "handle": quoted["user"]["screen_name"],
                            "name": quoted["user"]["name"],
                        },
                        "title": "",
                        "blocks": [
                            {"kind": "markdown", "text": inline(quoted["full_text"])}
                        ],
                        "coverage": "legacy",
                        "issues": ["legacy_quote_metadata_unverified"],
                    },
                )
                blocks.append(
                    {
                        "kind": "quote",
                        "record_id": qid,
                        "url": f"https://x.com/i/status/{quoted['id_str']}",
                    }
                )
            for url in t.get("urls") or []:
                expanded = url.get("expanded_url") or url["url"]
                blocks.append({"kind": "markdown", "text": f"附带链接：<{expanded}>"})
            extra = t.get("extra") or {}
            rid = "x:" + tid
            data["records"][rid] = {
                "source": "x",
                "source_id": tid,
                "url": f"https://x.com/i/status/{tid}",
                "created_at": iso(t["created_at"]),
                "author": {
                    "id": t["user"].get("id_str"),
                    "handle": t["user"]["screen_name"],
                    "name": t["user"]["name"],
                },
                "title": (article or article_metadata or {}).get("title", ""),
                "is_document": expects_article,
                "blocks": blocks,
                "reply_to": "x:" + extra["in_reply_to_status_id_str"]
                if extra.get("in_reply_to_status_id_str")
                else None,
                "coverage": "partial" if issues else "legacy",
                "issues": issues,
            }
            data["entries"][tid] = {
                "record_id": rid,
                "members": [rid],
                "context": [],
                "collections": ["likes"],
                "label": labels[tid],
                "capture_state": "legacy",
                "last_captured_at": None,
            }
        validate(data, store)
        for a in data["assets"].values():
            p = store.asset_path(a)
            if p.is_file() and p.stat().st_size:
                if not media_header(p, a["kind"]):
                    raise ValueError(f"Invalid legacy media container: {a['file']}")
                a.update(sha256=digest(p), size=p.stat().st_size)
        hashes = {name: value[1] for name, value in snapshots.items()}
        if any(digest(source / name) != sha for name, sha in hashes.items()):
            raise RuntimeError(
                "Legacy inputs changed during migration; refusing to commit mixed evidence"
            )
        data["migration"] = {"format": "x-archive-v0", "evidence": hashes}
        store.save(data)
        return {
            "entries": len(data["entries"]),
            "records": len(data["records"]),
            "assets": len(data["assets"]),
        }

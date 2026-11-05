"""X payloads become provider-independent content blocks at this boundary."""

from datetime import datetime, timezone
import hashlib
import html
from urllib.parse import urlsplit
from copy import deepcopy
from ...core.model import web_url
from .threads import Node


def iso(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    if dt.tzinfo is None:
        raise ValueError("Source timestamp has no timezone")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def asset(assets, url, kind, legacy_file=None):
    if not url:
        raise ValueError("Missing media URL")
    key = "a" + hashlib.sha256(url.encode()).hexdigest()
    if key not in assets:
        ext = ".mp4" if kind == "video" else ".jpg"
        suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if kind == "image" and suffix in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "." + suffix
        elif kind == "image" and "format=png" in url:
            ext = ".png"
        assets[key] = {"url": url, "kind": kind, "file": legacy_file or key + ext}
    return key


def image_filename(url, prefix):
    suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
    ext = (
        suffix
        if suffix in ("jpg", "jpeg", "png", "webp", "gif")
        else "png"
        if "format=png" in url
        else "jpg"
    )
    return f"{prefix}.{ext}"


def mp4(variants, duration):
    choices = sorted(
        (v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")),
        key=lambda v: v.get("bitrate") or 0,
    )
    if not choices:
        raise ValueError("No playable MP4 variant")
    seconds = (duration or 0) / 1000
    cap = 10_500_000 if seconds <= 180 else 2_200_000 if seconds <= 600 else 300_000
    return ([v for v in choices if (v.get("bitrate") or 0) <= cap] or choices[:1])[-1][
        "url"
    ]


def inline(text, styles=(), entities=(), entity_map=None):
    """X article offsets in the verified payloads index Unicode code points.

    Segmenting at all boundaries preserves overlapping styles and links without
    dropping text. We emit escaped inline HTML, never source-provided HTML tags.
    """
    spans = []
    tags = {
        "Bold": "strong",
        "BOLD": "strong",
        "Italic": "em",
        "ITALIC": "em",
        "Strikethrough": "del",
        "STRIKETHROUGH": "del",
        "Code": "code",
        "CODE": "code",
        "Underline": "u",
    }
    for style in styles:
        if style["style"] not in tags:
            raise ValueError(f"Unsupported inline style: {style['style']}")
        spans.append(
            (
                style["offset"],
                style["offset"] + style["length"],
                tags[style["style"]],
                None,
            )
        )
    for entity in entities:
        value = (entity_map or {})[str(entity["key"])]
        if value["type"] == "LINK":
            url = value["data"]["url"]
            if urlsplit(url).scheme not in ("http", "https", "mailto"):
                raise ValueError("Unsupported link scheme")
            spans.append(
                (entity["offset"], entity["offset"] + entity["length"], "a", url)
            )
    for start, end, _, _ in spans:
        if not 0 <= start <= end <= len(text):
            raise ValueError("Inline range outside source text")
    bounds = sorted({0, len(text), *(x for span in spans for x in span[:2])})
    result = []
    for start, end in zip(bounds, bounds[1:]):
        segment = html.escape(text[start:end])
        for lo, hi, tag, url in reversed(spans):
            if lo <= start and hi >= end:
                opening = (
                    f'<a href="{html.escape(url, quote=True)}">'
                    if tag == "a"
                    else f"<{tag}>"
                )
                segment = opening + segment + f"</{tag}>"
        result.append(segment)
    return "".join(result)


def article_blocks(article, assets, tid, legacy=False):
    blocks, media, issues = [], {}, []
    cover = ((article.get("cover_media") or {}).get("media_info") or {}).get(
        "original_img_url"
    )
    if cover:
        blocks.append(
            {
                "kind": "image",
                "asset": asset(
                    assets,
                    cover,
                    "image",
                    image_filename(cover, f"article_{tid}_cover") if legacy else None,
                ),
                "alt": "封面",
            }
        )
    for entity in article.get("media_entities") or []:
        mid, info = str(entity["media_id"]), entity["media_info"]
        if info["__typename"] == "ApiImage":
            url = info["original_img_url"]
            media[mid] = {
                "kind": "image",
                "asset": asset(
                    assets,
                    url,
                    "image",
                    image_filename(url, f"article_{tid}_{mid}") if legacy else None,
                ),
                "alt": "文章配图",
            }
        elif info["__typename"] in ("ApiVideo", "ApiAnimatedGif") or "variants" in info:
            try:
                url = mp4(info.get("variants") or [], info.get("duration_millis"))
            except ValueError:
                issues.append(f"missing_article_video_variants:{mid}")
                continue
            media[mid] = {
                "kind": "video",
                "asset": asset(
                    assets, url, "video", f"article_{tid}_{mid}.mp4" if legacy else None
                ),
            }
            thumb = (info.get("preview_image") or {}).get("original_img_url")
            if thumb:
                media[mid]["poster"] = asset(
                    assets,
                    thumb,
                    "image",
                    image_filename(thumb, f"article_{tid}_{mid}_thumb")
                    if legacy
                    else None,
                )
        else:
            issues.append(f"unsupported_article_media:{info['__typename']}")
    state = article.get("content_state")
    if not state or not state.get("blocks"):
        return blocks, ["missing_article_body", *issues]
    entity_map = {str(e["key"]): e["value"] for e in state.get("entityMap") or []}
    referenced_media = set()
    for block in state["blocks"]:
        kind = block.get("type", "unstyled")
        if kind == "atomic":
            refs = block.get("entityRanges") or []
            if not refs:
                issues.append("missing_article_atomic_entity")
            for ref in refs:
                ent = entity_map.get(str(ref["key"]))
                if ent is None:
                    issues.append(f"missing_article_entity:{ref['key']}")
                    continue
                typ, data = ent["type"], ent.get("data", {})
                if typ == "MEDIA":
                    items = data.get("mediaItems") or []
                    if not items:
                        issues.append("missing_article_media_items")
                    for item in items:
                        mid = str(item["mediaId"])
                        referenced_media.add(mid)
                        if mid in media:
                            blocks.append(media[mid])
                        else:
                            issues.append(f"missing_article_media:{mid}")
                elif typ == "MARKDOWN":
                    blocks.append(
                        {"kind": "markdown", "text": data.get("markdown", "")}
                    )
                elif typ == "DIVIDER":
                    blocks.append({"kind": "divider"})
                elif typ == "TWEET":
                    blocks.append(
                        {
                            "kind": "quote",
                            "record_id": "x:" + str(data["tweetId"]),
                            "url": f"https://x.com/i/status/{data['tweetId']}",
                        }
                    )
                else:
                    issues.append(f"unsupported_article_entity:{typ}")
            continue
        text = inline(
            block.get("text", ""),
            block.get("inlineStyleRanges") or [],
            block.get("entityRanges") or [],
            entity_map,
        )
        if not text.strip():
            continue
        prefix = {
            "header-one": "## ",
            "header-two": "### ",
            "unordered-list-item": "- ",
            "ordered-list-item": "1. ",
            "blockquote": "> ",
            "unstyled": "",
        }.get(kind)
        if prefix is None:
            issues.append(f"unsupported_article_block:{kind}")
            prefix = ""
        blocks.append({"kind": "markdown", "text": prefix + text})
    # Keep available assets when the layout is incomplete, without inventing
    # their position in the body. The issue distinguishes this attachment tail
    # from media placed by an explicit Draft.js MEDIA entity.
    for mid in media:
        if mid not in referenced_media:
            issues.append(f"unplaced_article_media:{mid}")
            blocks.append(media[mid])
    return blocks, issues


def from_capture(source, assets, source_issues=()):
    Node.from_record(source)
    tid = source["id"]
    for key in ("quoted_id", "reposted_id"):
        value = source.get(key)
        if value is not None and (
            not isinstance(value, str) or not value.isascii() or not value.isdigit()
        ):
            raise ValueError(f"Invalid {key}")
    if source.get("text") is not None and not isinstance(source["text"], str):
        raise ValueError("Source text must be a string or null")
    blocks, issues = [], list(source_issues)
    if source["author"].get("id") is None:
        issues.append("missing_author_id")
    if source.get("inline_media"):
        issues.append("inline_media_layout_unresolved")
    # Rich note offsets are preserved by the capture; supported tags are rendered
    # with the same range conversion as article spans.
    styles = []
    for tag in source.get("richtext_tags") or []:
        for typ in tag.get("richtext_types") or []:
            styles.append(
                {
                    "offset": tag["from_index"],
                    "length": tag["to_index"] - tag["from_index"],
                    "style": typ,
                }
            )
    if source.get("text"):
        blocks.append({"kind": "markdown", "text": inline(source["text"], styles)})
    for m in source.get("media") or []:
        if m["type"] == "photo":
            blocks.append(
                {
                    "kind": "image",
                    "asset": asset(assets, m["image_url"], "image"),
                    "alt": "推文配图",
                }
            )
        elif m["type"] in ("video", "animated_gif"):
            try:
                url = mp4(m["variants"], m.get("duration_millis"))
            except ValueError:
                issues.append(f"missing_video_variants:{m['id']}")
                continue
            blocks.append(
                {
                    "kind": "video",
                    "asset": asset(assets, url, "video"),
                    "poster": asset(assets, m["image_url"], "image"),
                }
            )
        else:
            issues.append(f"unsupported_media_type:{m['type']}")
    article = source.get("article")
    if article:
        content, missing = article_blocks(article, assets, tid)
        blocks.extend(content)
        issues.extend(missing)
    for key in ("quoted_id", "reposted_id"):
        if source.get(key):
            blocks.append(
                {
                    "kind": "quote",
                    "record_id": "x:" + source[key],
                    "url": "https://x.com/i/status/" + source[key],
                    "relation": "repost" if key == "reposted_id" else "quote",
                }
            )
    for link in source.get("urls") or []:
        expanded = link.get("expanded_url") or link.get("url")
        web_url(expanded)
        label = html.escape(link.get("display_url") or expanded)
        blocks.append(
            {
                "kind": "markdown",
                "text": f'附带链接：<a href="{html.escape(expanded, quote=True)}">{label}</a>',
            }
        )
    if source.get("text") is None:
        issues.append("missing_text")
    return {
        "source": "x",
        "source_id": tid,
        "url": f"https://x.com/i/status/{tid}",
        "author": source["author"],
        "created_at": iso(source["created_at"]),
        "title": (article or {}).get("title", ""),
        "is_document": bool(article),
        "blocks": blocks,
        "reply_to": "x:" + source["reply_to_id"] if source.get("reply_to_id") else None,
        "coverage": "partial" if issues else "observed",
        "issues": sorted(set(issues)),
        "source_layout": {
            "inline_media": deepcopy(source.get("inline_media") or []),
            "display_text_range": deepcopy(source.get("display_text_range")),
        },
    }

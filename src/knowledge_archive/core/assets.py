"""Content-verified local assets with an explicit download host policy."""

import http.client
import os
import tempfile
import urllib.request
from urllib.parse import urlsplit
from .store import digest


class HostPolicy(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        if not isinstance(allowed, list) or any(
            not isinstance(host, str) for host in allowed
        ):
            raise ValueError("media_hosts must be a list of hostnames")
        self.allowed = set(allowed)

    def check(self, url):
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname not in self.allowed
            or p.username
            or p.password
            or p.port not in (None, 443)
        ):
            raise ValueError("Media URL is outside configured HTTPS hosts")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def media_header(path, kind):
    """Recognize supported containers; this is not a full codec integrity test."""
    with open(path, "rb") as stream:
        head = stream.read(32)
    if kind == "video":
        return (
            len(head) >= 12
            and head[4:8] == b"ftyp"
            and int.from_bytes(head[:4], "big") >= 12
        )
    return (
        head.startswith(b"\xff\xd8\xff")
        or head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def verify_asset(store, a):
    p = store.asset_path(a)
    return (
        p.is_file()
        and p.stat().st_size > 0
        and a.get("sha256") == digest(p)
        and a.get("size") == p.stat().st_size
        and media_header(p, a["kind"])
    )


def fetch_assets(store, data, config):
    store.media.mkdir(parents=True, exist_ok=True)
    policy = HostPolicy(config.get("media_hosts", []))
    limit = config.get("max_asset_bytes", 1024 * 1024 * 1024)
    if type(limit) is not int or limit <= 0:
        raise ValueError("max_asset_bytes must be a positive integer")
    opener = urllib.request.build_opener(policy)
    failures = []
    for aid, a in data["assets"].items():
        dest = store.asset_path(a)
        if a.get("sha256") and dest.exists():
            if not verify_asset(store, a):
                failures.append(
                    {"asset": aid, "reason": "local_hash_or_format_mismatch"}
                )
            continue
        tmp = None
        try:
            policy.check(a["url"])
            fd, tmp = tempfile.mkstemp(prefix=".download-", dir=store.media)
            with (
                os.fdopen(fd, "wb") as out,
                opener.open(
                    urllib.request.Request(
                        a["url"], headers={"User-Agent": "KnowledgeArchive/0.1"}
                    ),
                    timeout=30,
                ) as response,
            ):
                if (
                    response.status != 200
                    or response.headers.get("Content-Range") is not None
                ):
                    raise ValueError(
                        "Expected a complete HTTP 200 response, not a partial asset"
                    )
                mime = response.headers.get_content_type()
                if not mime.startswith("image/" if a["kind"] == "image" else "video/"):
                    raise ValueError(f"Unexpected media MIME: {mime}")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Asset exceeds configured download size limit")
                    out.write(chunk)
                expected = response.headers.get("Content-Length")
                if total == 0 or (expected is not None and total != int(expected)):
                    raise ValueError("Empty or truncated asset response")
            if not media_header(tmp, a["kind"]):
                raise ValueError("Unsupported or invalid media container header")
            sha = digest(tmp)
            if a.get("sha256") and a["sha256"] != sha:
                raise ValueError(
                    "Remote bytes differ from archived digest; preserving evidence"
                )
            os.replace(tmp, dest)
            a.update(sha256=sha, size=total)
        except (OSError, ValueError, http.client.HTTPException) as error:
            failures.append({"asset": aid, "reason": str(error)})
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
    return failures

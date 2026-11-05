"""Atomic JSON source snapshots and a single writer per local archive."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib


def utcnow():
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def json_snapshot(path):
    raw = Path(path).read_bytes()
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object
    ), hashlib.sha256(raw).hexdigest()


def load_json(path):
    return json_snapshot(path)[0]


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "raw/archive.json"
        self.runtime = self.root / ".archive"
        self.media = self.root / "media"

    def config_snapshot(self):
        raw = (self.root / "archive.toml").read_bytes()
        return tomllib.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()

    def config(self):
        return self.config_snapshot()[0]

    @contextmanager
    def writer(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another process is writing this archive") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def snapshot(self):
        data, sha = json_snapshot(self.path)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported archive schema_version")
        return data, sha

    def load(self):
        return self.snapshot()[0]

    def save(self, data, expected_hash=None):
        if expected_hash is not None and digest(self.path) != expected_hash:
            raise RuntimeError(
                "Archive changed during this operation; refusing to overwrite edits"
            )
        atomic_json(self.path, data)

    def asset_path(self, asset):
        name = asset["file"]
        if (
            not isinstance(name, str)
            or name in ("", ".", "..")
            or Path(name).name != name
            or "\\" in name
        ):
            raise ValueError("Asset file must be a single relative filename")
        path = self.media / name
        if not path.resolve().is_relative_to(self.media.resolve()):
            raise ValueError("Asset path escapes media directory")
        return path

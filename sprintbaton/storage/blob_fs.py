"""FilesystemBlobStore — the tool-mode BlobStore backend (zero-infra-storage
spec §3.2): one file per key under a local root, mirroring the tenant-rooted
key shape (storage/keys.py) as a directory path.

Atomicity: every write stages under {root}/.tmp/ and os.rename()s into place.
os.rename() is atomic on POSIX when source and destination share a filesystem,
which is guaranteed by construction here — the staging directory is a
subdirectory of the store's own root, never system /tmp (spec §9). A reader
sees fully-old or fully-new content, never a partial write; a crash before the
rename leaves only a harmless orphaned temp file, swept on the next startup.
"""

import logging
import os
import time
import uuid
from pathlib import Path

from sprintbaton.storage.keys import BlobKeyMixin

log = logging.getLogger(__name__)

_TMP_DIR = ".tmp"
# Orphaned staging files younger than this survive the startup sweep — a
# concurrent process may still be mid-write. Defensive only; nothing depends
# on the sweep for correctness (spec §3.2).
_TMP_SWEEP_GRACE_SECONDS = 3600


class FilesystemBlobStore(BlobKeyMixin):
    def __init__(self, root: str):
        self._root = Path(root).expanduser().resolve()
        self._tmp = self._root / _TMP_DIR
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._sweep_tmp()

    def _sweep_tmp(self) -> None:
        cutoff = time.time() - _TMP_SWEEP_GRACE_SECONDS
        for orphan in self._tmp.iterdir():
            try:
                if orphan.is_file() and orphan.stat().st_mtime < cutoff:
                    orphan.unlink()
            except OSError:
                pass  # best-effort; another sweep will retry

    # ------------------------------------------------------------------ write

    def put_text(self, key: str, text: str) -> str:
        return self._put(key, text.encode("utf-8"))

    def put_bytes(self, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        # content_type kept for interface parity; the filesystem has no
        # metadata channel for it and no reader ever consumes it.
        return self._put(key, data)

    def _put(self, key: str, data: bytes) -> str:
        final = self._root / key
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp / uuid.uuid4().hex
        tmp.write_bytes(data)
        os.rename(tmp, final)  # atomic: same filesystem by construction (§9)
        return self.url_for(key)

    def delete(self, key: str) -> None:
        """Unlink one key and prune the now-empty parent directories up to (never
        including) the root. A missing key is a no-op (project-initialization-
        task spec §9.3)."""
        path = self._root / key
        try:
            path.unlink()
        except FileNotFoundError:
            return
        parent = path.parent
        while parent != self._root and parent.is_relative_to(self._root):
            try:
                parent.rmdir()  # only succeeds when empty
            except OSError:
                break
            parent = parent.parent

    # ------------------------------------------------------------------- read

    def get_text(self, key: str) -> str | None:
        try:
            return (self._root / key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def get_bytes(self, key: str) -> bytes | None:
        try:
            return (self._root / key).read_bytes()
        except FileNotFoundError:
            return None

    def get_text_by_url(self, url: str) -> str | None:
        key = self.key_for(url)
        return self.get_text(key) if key is not None else None

    # ------------------------------------------------------------------- keys

    def url_for(self, key: str) -> str:
        return f"file://{self._root}/{key}"

    def key_for(self, url: str) -> str | None:
        """Inverse of url_for; None when the URL is not under this root —
        stored URLs stay self-describing about which backend wrote them."""
        prefix = f"file://{self._root}/"
        return url[len(prefix):] if url.startswith(prefix) else None

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._root / prefix if prefix else self._root
        if not base.exists():
            return []
        keys = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            if rel.startswith(f"{_TMP_DIR}/"):
                continue  # staging area is never a stored key
            keys.append(rel)
        return keys

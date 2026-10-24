from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class DiskCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Any:
        """Return the cached payload, or ``None`` if missing or corrupt.

        Corruption handling (Plan S2): a JSON parse failure is treated as a
        one-time disaster — we log a warning, move the broken file aside as
        ``<path>.corrupt-<unix-ts>`` so a human can inspect it, and return
        ``None`` so the caller rehydrates from a fresh source.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("disk cache %s unreadable: %s", self.path, exc)
            return None

        try:
            return json.loads(text)
        except ValueError as exc:
            self._quarantine_corrupt(exc)
            return None

    def save(self, payload: Any) -> None:
        self.save_serialized(self.serialize(payload))

    @staticmethod
    def serialize(payload: Any) -> bytes:
        """Encode a cache payload once for disk and other local consumers."""
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def save_serialized(self, payload: bytes) -> None:
        """Atomically persist an already encoded JSON payload."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        # Machine-owned cache: compact JSON reduces periodic SD-card writes.
        temp_path.write_bytes(payload)
        temp_path.replace(self.path)

    def _quarantine_corrupt(self, exc: Exception) -> None:
        target = self.path.with_name(
            f"{self.path.name}.corrupt-{int(time.time())}"
        )
        try:
            os.replace(self.path, target)
            logger.warning(
                "disk cache %s corrupt (%s) — moved aside as %s",
                self.path,
                exc,
                target,
            )
        except OSError as replace_exc:
            logger.warning(
                "disk cache %s corrupt (%s); quarantine rename failed: %s",
                self.path,
                exc,
                replace_exc,
            )

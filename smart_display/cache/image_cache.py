from __future__ import annotations

import hashlib
import io
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from smart_display.cache.disk_cache import DiskCache
from smart_display.http_client import HttpClient, HttpError
from smart_display.models import PhotoManifestEntry


logger = logging.getLogger(__name__)


try:
    from PIL import Image, ImageOps  # type: ignore

    # Plan B9: cap the decoded-pixel budget so a malicious or broken album
    # entry can't exhaust the 512 MB Pi. 24 Mpx ≈ 96 MB at 4 bytes/pixel —
    # comfortably below the working set we can afford.
    Image.MAX_IMAGE_PIXELS = 24_000_000
except ImportError:  # pragma: no cover
    Image = None
    ImageOps = None


DownloadResult = tuple[bytes, dict[str, str]]

# Plan B9: bounded read for screensaver image downloads. Any single image
# larger than this is treated as a payload error and skipped.
IMAGE_MAX_BYTES = 8_000_000

# Retry backoff for failed downloads. Doubles on each attempt, capped at 24 h.
_INITIAL_BACKOFF_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 24 * 60 * 60.0


def _compute_next_retry(attempts: int) -> float:
    backoff = _INITIAL_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0))
    return min(backoff, _MAX_BACKOFF_SECONDS)


class ImageCache:
    def __init__(
        self,
        cache_dir: str | Path,
        manifest_path: str | Path,
        *,
        display_size: tuple[int, int] = (1024, 600),
        downloader: Callable[[str, int], DownloadResult] | None = None,
        remote_enabled: bool = True,
        local_dir: str | Path | None = None,
        demo_dir: str | Path | None = None,
        http_client: HttpClient | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.remote_enabled = remote_enabled
        self.local_dir = Path(local_dir) if local_dir else None
        self.demo_dir = Path(demo_dir) if demo_dir else None
        self.manifest_cache = DiskCache(manifest_path)
        self.failures_cache = DiskCache(self.cache_dir / "failed.json")
        self.display_size = display_size
        self._http = http_client  # created lazily so tests with a custom
        # downloader never touch the network.
        self._downloader = downloader or self._default_download
        self._random = random.Random()
        self._clock = clock or time.time
        self._entries = self._load_manifest()
        self._failures: dict[str, dict[str, Any]] = self._load_failures()
        # Plan hardening: only persist ``failed.json`` when something
        # actually changed during ``sync_remote_images``. On a Pi Zero
        # writing the file every refresh tick is pointless churn against
        # the SD card when the typical case is "nothing failed".
        self._failures_dirty = False
        # User-owned photos live outside the Lightroom cache so a remote
        # manifest refresh can never delete them. Cache the directory listing
        # just like the bundled demo set; the slideshow asks for a new entry
        # every 15 seconds on the Pi.
        self._local_entries_cache: list[PhotoManifestEntry] | None = None
        self._local_entries_mtime: float | None = None
        self._selection_queue: list[PhotoManifestEntry] = []
        self._selection_signature: tuple[str, ...] = ()
        self._last_selection_path: str | None = None
        # Plan C2: caching the demo-entries list avoids re-walking the demo
        # directory every slideshow tick (≤15 s on the Pi). Invalidate via
        # manifest mtime so edits still land on the next call.
        self._demo_entries_cache: list[PhotoManifestEntry] | None = None
        self._demo_entries_mtime: float | None = None

    def entries(self) -> list[PhotoManifestEntry]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def available_entries(
        self, *, include_demo: bool = True
    ) -> list[PhotoManifestEntry]:
        entries = (self._entries if self.remote_enabled else []) + self.local_entries()
        if not entries and include_demo:
            entries = self.demo_entries()
        return list(entries)

    def available_count(self, *, include_demo: bool = True) -> int:
        return len(self.available_entries(include_demo=include_demo))

    def sync_remote_images(
        self,
        source_urls: list[str],
        *,
        timeout_seconds: int,
        max_new_downloads: int | None = None,
    ) -> list[PhotoManifestEntry]:
        """Reconcile the manifest against ``source_urls``.

        Plan B9:
        - At most ``max_new_downloads`` new entries are fetched per call
          (default: unbounded; the provider passes a small cap like 1 to
          spread CPU across refresh ticks on the Pi Zero).
        - URLs that previously failed with a decompression bomb, payload
          error, or decode error are skipped until their exponential
          backoff window elapses.
        - Existing cached entries are preserved across calls even when the
          per-tick budget is exhausted — only the manifest's URL set
          shrinks to ``source_urls``.
        """
        unique_urls = list(dict.fromkeys(source_urls))
        existing = {
            entry.source_url: entry
            for entry in self._entries
            if Path(entry.local_path).exists()
        }

        next_entries: list[PhotoManifestEntry] = []
        used_filenames: set[str] = set()
        pending_urls: list[str] = []

        for url in unique_urls:
            cached = existing.get(url)
            if cached:
                next_entries.append(cached)
                used_filenames.add(Path(cached.local_path).name)
            else:
                pending_urls.append(url)

        now = self._clock()
        budget = max_new_downloads if max_new_downloads is not None else len(pending_urls)
        attempted = 0
        for url in pending_urls:
            if attempted >= budget:
                break
            if self._is_in_backoff(url, now):
                continue
            attempted += 1
            try:
                downloaded = self._download_and_prepare(
                    url, timeout_seconds=timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 — Pillow exceptions vary
                self._record_failure(url, exc)
                continue
            if downloaded is None:
                continue
            self._clear_failure(url)
            next_entries.append(downloaded)
            used_filenames.add(Path(downloaded.local_path).name)

        # Drop failures for URLs that are no longer in the source list.
        self._prune_failures(set(unique_urls))

        self._entries = next_entries
        self._cleanup_unused_files(used_filenames)
        self.manifest_cache.save([entry.to_dict() for entry in self._entries])
        if self._failures_dirty:
            self.failures_cache.save(self._failures)
            self._failures_dirty = False
        return self.entries()

    def next_entry(self, *, include_demo: bool = True) -> PhotoManifestEntry | None:
        pool = self.available_entries(include_demo=include_demo)
        if not pool:
            self._selection_queue = []
            self._selection_signature = ()
            return None

        signature = tuple(entry.public_path for entry in pool)
        if signature != self._selection_signature or not self._selection_queue:
            self._selection_queue = list(pool)
            self._random.shuffle(self._selection_queue)
            self._selection_signature = signature

            # Every photo appears once per shuffled round. At the boundary
            # between rounds, avoid placing the previous final photo first
            # again; otherwise the screen can look frozen for 30 seconds.
            if len(self._selection_queue) > 1:
                next_index = len(self._selection_queue) - 1
                if (
                    self._selection_queue[next_index].public_path
                    == self._last_selection_path
                ):
                    self._selection_queue[0], self._selection_queue[next_index] = (
                        self._selection_queue[next_index],
                        self._selection_queue[0],
                    )

        selected = self._selection_queue.pop()
        self._last_selection_path = selected.public_path
        return selected

    def local_entries(self) -> list[PhotoManifestEntry]:
        entries, mtime = self._static_entries(
            directory=self.local_dir,
            public_prefix="/media/screensaver-local",
            source_prefix="local",
            cached_entries=self._local_entries_cache,
            cached_mtime=self._local_entries_mtime,
        )
        self._local_entries_cache = entries
        self._local_entries_mtime = mtime
        return list(entries)

    def demo_entries(self) -> list[PhotoManifestEntry]:
        # Plan C2: re-walking the demo directory on every slideshow tick is
        # wasted syscalls — cache the list and invalidate on demo-dir mtime.
        if not self.demo_dir or not self.demo_dir.exists():
            self._demo_entries_cache = []
            self._demo_entries_mtime = None
            return []
        try:
            current_mtime = self.demo_dir.stat().st_mtime
        except OSError:
            current_mtime = None
        if (
            self._demo_entries_cache is not None
            and current_mtime == self._demo_entries_mtime
        ):
            return list(self._demo_entries_cache)

        entries: list[PhotoManifestEntry] = []
        for path in sorted(self.demo_dir.iterdir()):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
                continue
            entries.append(
                PhotoManifestEntry(
                    id=path.stem,
                    source_url=path.name,
                    local_path=str(path.resolve()),
                    public_path=f"/static/images/demo-screensaver/{path.name}",
                    width=self.display_size[0],
                    height=self.display_size[1],
                    content_hash=path.stem,
                )
            )
        self._demo_entries_cache = list(entries)
        self._demo_entries_mtime = current_mtime
        return entries

    def local_entry_for_filename(self, filename: str) -> PhotoManifestEntry | None:
        for entry in self.local_entries():
            if Path(entry.local_path).name == filename:
                return entry
        return None

    def _static_entries(
        self,
        *,
        directory: Path | None,
        public_prefix: str,
        source_prefix: str,
        cached_entries: list[PhotoManifestEntry] | None,
        cached_mtime: float | None,
    ) -> tuple[list[PhotoManifestEntry], float | None]:
        if not directory or not directory.exists():
            return [], None
        try:
            current_mtime = directory.stat().st_mtime
        except OSError:
            current_mtime = None
        if cached_entries is not None and current_mtime == cached_mtime:
            return list(cached_entries), current_mtime

        entries: list[PhotoManifestEntry] = []
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            entries.append(
                PhotoManifestEntry(
                    id=f"{source_prefix}-{path.stem}",
                    source_url=f"{source_prefix}:{path.name}",
                    local_path=str(path.resolve()),
                    public_path=f"{public_prefix}/{path.name}",
                    width=self.display_size[0],
                    height=self.display_size[1],
                    content_hash=path.stem,
                )
            )
        return entries, current_mtime

    def entry_for_filename(self, filename: str) -> PhotoManifestEntry | None:
        for entry in self._entries:
            if Path(entry.local_path).name == filename:
                return entry
        return None

    def failed_urls(self) -> dict[str, dict[str, Any]]:
        """Expose failure state (used by tests and status reporting)."""
        return dict(self._failures)

    # ---- Failure bookkeeping -----------------------------------------------

    def _is_in_backoff(self, url: str, now: float) -> bool:
        failure = self._failures.get(url)
        if not failure:
            return False
        next_retry = float(failure.get("next_retry_at", 0.0))
        return now < next_retry

    def _record_failure(self, url: str, exc: BaseException) -> None:
        current = self._failures.get(url, {"attempts": 0})
        attempts = int(current.get("attempts", 0)) + 1
        backoff = _compute_next_retry(attempts)
        next_retry_at = self._clock() + backoff
        self._failures[url] = {
            "attempts": attempts,
            "next_retry_at": next_retry_at,
            "next_retry_iso": datetime.fromtimestamp(
                next_retry_at, tz=timezone.utc
            ).isoformat(),
            "last_error": f"{type(exc).__name__}: {exc}",
        }
        self._failures_dirty = True
        logger.warning(
            "screensaver image download failed for %s (attempt %d, retry in %.0fs): %s",
            url,
            attempts,
            backoff,
            exc,
        )

    def _clear_failure(self, url: str) -> None:
        if self._failures.pop(url, None) is not None:
            self._failures_dirty = True

    def _prune_failures(self, live_urls: set[str]) -> None:
        stale = [url for url in self._failures if url not in live_urls]
        for url in stale:
            self._failures.pop(url, None)
        if stale:
            self._failures_dirty = True

    def _load_failures(self) -> dict[str, dict[str, Any]]:
        raw = self.failures_cache.load()
        if isinstance(raw, dict):
            return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        return {}

    def _load_manifest(self) -> list[PhotoManifestEntry]:
        # DiskCache.load() already quarantines unparseable JSON (Plan S2).
        # Here we additionally defend against per-entry schema drift — a
        # single malformed record must not poison the whole manifest.
        raw = self.manifest_cache.load()
        if not isinstance(raw, list):
            if raw is not None:
                logger.warning(
                    "image manifest at %s has unexpected shape (%s) — resetting",
                    self.manifest_cache.path,
                    type(raw).__name__,
                )
                self.manifest_cache.save([])
            return []

        entries: list[PhotoManifestEntry] = []
        skipped = 0
        for item in raw:
            if not isinstance(item, dict):
                skipped += 1
                continue
            local_path_value = str(item.get("local_path", ""))
            if not local_path_value or not Path(local_path_value).exists():
                skipped += 1
                continue
            try:
                entries.append(PhotoManifestEntry.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "image manifest entry dropped (%s): %s", exc, item
                )
                skipped += 1

        if skipped:
            self.manifest_cache.save([entry.to_dict() for entry in entries])
        return entries

    def _cleanup_unused_files(self, used_filenames: set[str]) -> None:
        for path in self.cache_dir.iterdir():
            if (
                path.is_file()
                and path.name not in {"manifest.json", "failed.json"}
                and path.name not in used_filenames
            ):
                path.unlink(missing_ok=True)

    def _download_and_prepare(
        self, source_url: str, *, timeout_seconds: int
    ) -> PhotoManifestEntry | None:
        payload, headers = self._downloader(source_url, timeout_seconds)
        digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]
        filename = f"{digest}.jpg"
        local_path = self.cache_dir / filename
        width, height = self.display_size

        if Image is not None and ImageOps is not None:
            # Plan B9: any Pillow error — malformed bytes, dimensions over
            # the MAX_IMAGE_PIXELS cap, unsupported format — becomes a
            # skipped entry with an exponential-backoff retry, never a
            # crash of the refresh loop.
            try:
                with Image.open(io.BytesIO(payload)) as image:
                    prepared = ImageOps.exif_transpose(image).convert("RGB")
                    fitted = ImageOps.fit(
                        prepared, self.display_size, method=Image.Resampling.LANCZOS
                    )
                    fitted.save(local_path, format="JPEG", quality=86, optimize=True)
                    width, height = fitted.size
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise RuntimeError(
                    f"unable to decode image {source_url}: {exc}"
                ) from exc
        else:  # pragma: no cover
            local_path.write_bytes(payload)

        return PhotoManifestEntry(
            id=digest,
            source_url=source_url,
            local_path=str(local_path.resolve()),
            public_path=f"/media/screensaver/{filename}",
            width=width,
            height=height,
            content_hash=hashlib.sha1(payload).hexdigest(),
            etag=headers.get("etag"),
            last_modified=headers.get("last-modified"),
        )

    def _default_download(self, url: str, timeout_seconds: int) -> DownloadResult:
        if self._http is None:
            self._http = HttpClient()
        try:
            response = self._http.get(
                url,
                timeout=timeout_seconds,
                max_bytes=IMAGE_MAX_BYTES,
            )
        except HttpError as exc:
            raise RuntimeError(f"image download failed for {url}: {exc}") from exc
        if not response.ok:
            raise RuntimeError(
                f"image download failed for {url}: status {response.status}"
            )
        return response.body, dict(response.headers)

from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

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

# An edited photo (#15) whose rendition still comes back byte-identical is
# retried with the backoff above, and accepted as-is after this many
# identical answers. Enough to ride out Lightroom re-rendering, bounded so
# an edit that leaves the rendition alone stops costing downloads.
UNCHANGED_RENDITION_MAX_ATTEMPTS = 8


LOCAL_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
DEMO_IMAGE_SUFFIXES = LOCAL_IMAGE_SUFFIXES | {".svg"}

# A request thread decodes at most this many new local photos per call; the
# provider thread prepares the rest. One full-size decode is seconds on the
# Pi Zero — forty in a row would stall the slideshow request for minutes.
LOCAL_PREPARE_PER_REQUEST = 1
# Without a directory-mtime change, re-stat the local photos at most this
# often. An in-place overwrite (same name) leaves the directory mtime alone,
# so it is picked up within this window.
LOCAL_RESCAN_SECONDS = 60.0
# A replaced or removed photo's prepared copy stays servable this long: the
# kiosk may have just received its URL for the hidden crossfade slot.
LOCAL_RETIRE_GRACE_SECONDS = 300.0
# Decode budget per provider tick. A backlog of forty phone photos must not
# hold up the Lightroom sync and the status update for minutes; what is left
# continues next tick and, while the slideshow runs, one per request.
LOCAL_PREPARE_TICK_SECONDS = 20.0
# ``draft`` lets libjpeg decode at reduced scale; PNG and WebP have no such
# path and decode in full. 12 Mpx is ~36 MB RGB, well inside the backend's
# share of a 512 MB Pi — anything larger is skipped rather than risking the
# OOM killer taking the whole backend down.
NON_JPEG_MAX_PIXELS = 12_000_000

LocalKey = tuple[str, int, int]


def _compute_next_retry(attempts: int) -> float:
    backoff = _INITIAL_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0))
    return min(backoff, _MAX_BACKOFF_SECONDS)


def _image_files(directory: Path, suffixes: frozenset[str]) -> list[Path]:
    """Shared walk for the demo set and user-owned photos. Dotfiles are
    skipped: macOS AppleDouble companions (``._photo.jpg``) carry the image
    suffix but hold resource-fork bytes no browser can decode."""
    return [
        path
        for path in sorted(directory.iterdir())
        if not path.name.startswith(".")
        and path.suffix.lower() in suffixes
        and path.is_file()
    ]


def _public_path(prefix: str, filename: str) -> str:
    # ``#``, ``%`` or ``?`` in a user's filename would otherwise end the path
    # early or form a broken escape, so the browser requests a different URL.
    return f"{prefix}/{quote(filename)}"


@dataclass
class RemoteSyncPlan:
    """What ``sync_remote_images`` would do, before any download."""

    urls: list[str]
    # Cached photos whose file is still on disk, by source URL.
    existing: dict[str, PhotoManifestEntry] = field(default_factory=dict)
    new: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)
    # Cached before #15 (no fingerprint yet): take the current one as is.
    adopt: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)


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
        # manifest refresh can never delete them. Each one is normalised once
        # into ``local_cache_dir`` under a digest of (name, size, mtime): a
        # file replaced under the same name gets a new copy, and an unchanged
        # one reuses its copy across restarts without decoding again.
        self.local_cache_dir = self.cache_dir / "local"
        self._local_prepared: dict[LocalKey, PhotoManifestEntry] = {}
        self._local_failed: set[LocalKey] = set()
        self._local_order: list[LocalKey] = []
        self._local_pending: list[tuple[Path, LocalKey]] = []
        self._local_retired: dict[str, tuple[PhotoManifestEntry, float]] = {}
        self._local_scan_mtime: float | None = None
        self._local_scanned_at = float("-inf")
        self._local_next_expiry = float("inf")
        # Photos shown in the current round, so a pool that grows mid-round
        # (a newly prepared local photo) does not re-shuffle the round.
        self._shown_this_round: set[str] = set()
        # Waitress serves /api/screensaver/next on several threads; the
        # rotation queue and the local-photo bookkeeping are shared state.
        # ``_lock`` guards short critical sections only. Decoding runs under
        # ``_prepare_lock`` instead, so a slow decode never blocks rotation.
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
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
        versions: dict[str, str | None] | None = None,
        respect_backoff: bool = True,
    ) -> list[PhotoManifestEntry]:
        """Reconcile the manifest against ``source_urls``.

        ``versions`` maps a URL to the fingerprint of its edit state (#15).
        A cached photo whose fingerprint changed is downloaded again; it
        shares the per-tick budget with new photos. ``respect_backoff=False``
        is for the manual sync: someone asked for everything, now.

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
        versions = versions or {}
        plan = self.plan_remote_sync(source_urls, versions)
        # Album order, with an edited photo's old copy holding its slot until
        # the new one is in.
        slots: dict[str, PhotoManifestEntry | None] = {
            url: plan.existing.get(url) for url in plan.urls
        }
        for url in plan.adopt:
            slots[url] = replace(slots[url], source_version=versions[url])

        pending_urls = [url for url in plan.urls if url in plan.new or url in plan.edited]
        now = self._clock()
        budget = max_new_downloads if max_new_downloads is not None else len(pending_urls)
        attempted = 0
        for url in pending_urls:
            if attempted >= budget:
                break
            if respect_backoff and self._is_in_backoff(url, now):
                continue
            attempted += 1
            version = versions.get(url)
            try:
                downloaded = self._download_and_prepare(
                    url, timeout_seconds=timeout_seconds, version=version
                )
            except Exception as exc:  # noqa: BLE001 — Pillow exceptions vary
                self._record_failure(url, exc)
                continue
            if downloaded is None:
                continue
            previous = slots[url]
            if (
                previous is not None
                and previous.content_hash == downloaded.content_hash
            ):
                # Edited in Lightroom, but the rendition still has the old
                # bytes: Lightroom has not re-rendered yet. Keep the old copy
                # and retry with backoff — but accept after a few identical
                # answers, so an edit that does not change the rendition
                # cannot cost a download per tick forever. The streak is
                # counted per target version, apart from download errors,
                # and accepting keeps the new filename like any other edit.
                prior = self._failures.get(url, {})
                streak = 1
                if prior.get("unchanged_for") == version:
                    streak += int(prior.get("unchanged_attempts", 0))
                if streak < UNCHANGED_RENDITION_MAX_ATTEMPTS:
                    if downloaded.local_path != previous.local_path:
                        Path(downloaded.local_path).unlink(missing_ok=True)
                    self._record_failure(
                        url, RuntimeError("Lightroom-Rendition noch unverändert")
                    )
                    self._failures[url].update(
                        unchanged_for=version, unchanged_attempts=streak
                    )
                    continue
            self._clear_failure(url)
            slots[url] = downloaded

        # Drop failures for URLs that are no longer in the source list.
        self._prune_failures(set(plan.urls))

        next_entries = [entry for entry in slots.values() if entry is not None]
        used_filenames = {Path(entry.local_path).name for entry in next_entries}
        self._entries = next_entries
        self._cleanup_unused_files(used_filenames)
        self.manifest_cache.save([entry.to_dict() for entry in self._entries])
        if self._failures_dirty:
            self.failures_cache.save(self._failures)
            self._failures_dirty = False
        return self.entries()

    def plan_remote_sync(
        self,
        source_urls: list[str],
        versions: dict[str, str | None] | None = None,
    ) -> RemoteSyncPlan:
        versions = versions or {}
        existing = self._existing_by_url()
        plan = RemoteSyncPlan(urls=list(dict.fromkeys(source_urls)), existing=existing)
        for url in plan.urls:
            cached = existing.get(url)
            wanted = versions.get(url)
            if cached is None:
                plan.new.add(url)
            elif wanted is None or cached.source_version == wanted:
                continue
            elif cached.source_version is None:
                plan.adopt.add(url)
            else:
                plan.edited.add(url)
        plan.gone = set(existing) - set(plan.urls)
        return plan

    def _existing_by_url(self) -> dict[str, PhotoManifestEntry]:
        return {
            entry.source_url: entry
            for entry in self._entries
            if Path(entry.local_path).exists()
        }

    def next_entry(self, *, include_demo: bool = True) -> PhotoManifestEntry | None:
        pool = self.available_entries(include_demo=include_demo)
        with self._lock:
            return self._select_locked(pool)

    def _select_locked(self, pool: list[PhotoManifestEntry]) -> PhotoManifestEntry | None:
        if not pool:
            self._selection_queue = []
            self._selection_signature = ()
            self._shown_this_round = set()
            return None

        by_path = {entry.public_path: entry for entry in pool}
        signature = tuple(by_path)
        if signature != self._selection_signature:
            # The pool changed mid-round. Keep what is still queued, drop what
            # left, and slot photos that are new to this round in at random —
            # re-shuffling everything would bring back photos already shown.
            self._shown_this_round &= by_path.keys()
            queued = [
                by_path[entry.public_path]
                for entry in self._selection_queue
                if entry.public_path in by_path
            ]
            queued_paths = {entry.public_path for entry in queued}
            for path, entry in by_path.items():
                if path not in queued_paths and path not in self._shown_this_round:
                    queued.insert(self._random.randint(0, len(queued)), entry)
            self._selection_queue = queued
            self._selection_signature = signature

        if not self._selection_queue:
            self._selection_queue = list(pool)
            self._random.shuffle(self._selection_queue)
            self._shown_this_round = set()

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
        self._shown_this_round.add(selected.public_path)
        self._last_selection_path = selected.public_path
        return selected

    def local_entries(
        self,
        *,
        max_new: int | None = LOCAL_PREPARE_PER_REQUEST,
        wait: bool = False,
        time_budget: float | None = None,
    ) -> list[PhotoManifestEntry]:
        """User-owned photos that are ready to show, each normalised to the
        display size like the Lightroom images. A full-resolution phone photo
        would otherwise be decoded at ~96 MB per crossfade slot inside WPE on
        a 512 MB Pi.

        Request threads use the defaults: at most ``max_new`` decodes, and
        none at all while another thread is preparing. The provider thread
        calls ``prepare_local_photos`` to work through the whole backlog."""
        if not self.local_dir or not self.local_dir.is_dir():
            return []
        with self._lock:
            self._rescan_local_locked()
            pending = list(self._local_pending)
            # Retired copies only need a sweep once the earliest grace period
            # has run out, not on every request during those five minutes.
            work = bool(pending) or self._clock() >= self._local_next_expiry
        if work:
            self._prepare_pending(
                pending, max_new=max_new, wait=wait, time_budget=time_budget
            )
        with self._lock:
            return [
                self._local_prepared[key]
                for key in self._local_order
                if key in self._local_prepared
            ]

    def prepare_local_photos(self) -> list[PhotoManifestEntry]:
        # Retry photos that failed before, once per provider tick: a
        # MemoryError while WPE held a lot of RAM is transient. A file that is
        # simply broken fails fast and costs one attempt per tick.
        with self._lock:
            if self._local_failed:
                self._local_failed.clear()
                self._local_scanned_at = float("-inf")
        return self.local_entries(
            max_new=None, wait=True, time_budget=LOCAL_PREPARE_TICK_SECONDS
        )

    def demo_entries(self) -> list[PhotoManifestEntry]:
        # Plan C2: re-walking the demo directory on every slideshow tick is
        # wasted syscalls — cache the list and invalidate on demo-dir mtime.
        # The demo set ships with the repo, so a directory mtime is enough.
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

        entries = [
            PhotoManifestEntry(
                id=path.stem,
                source_url=path.name,
                local_path=str(path.resolve()),
                public_path=_public_path("/static/images/demo-screensaver", path.name),
                width=self.display_size[0],
                height=self.display_size[1],
                content_hash=path.stem,
            )
            for path in _image_files(self.demo_dir, DEMO_IMAGE_SUFFIXES)
        ]
        self._demo_entries_cache = list(entries)
        self._demo_entries_mtime = current_mtime
        return entries

    def local_entry_for_filename(self, filename: str) -> PhotoManifestEntry | None:
        entry = self._lookup_local(filename)
        if entry is None:
            # Right after a restart nothing is registered yet, although the
            # copies are on disk. Adopt those (``max_new=0``: never decode on
            # the media route) and look again.
            self.local_entries(max_new=0)
            entry = self._lookup_local(filename)
        return entry

    def _lookup_local(self, filename: str) -> PhotoManifestEntry | None:
        with self._lock:
            candidates = list(self._local_prepared.values()) + [
                entry for entry, _ in self._local_retired.values()
            ]
        for entry in candidates:
            if Path(entry.local_path).name == filename:
                return entry
        return None

    def _rescan_local_locked(self) -> None:
        now = self._clock()
        try:
            mtime = self.local_dir.stat().st_mtime
        except OSError:
            mtime = None
        if (
            mtime == self._local_scan_mtime
            and now - self._local_scanned_at < LOCAL_RESCAN_SECONDS
        ):
            return
        order: list[LocalKey] = []
        pending: list[tuple[Path, LocalKey]] = []
        try:
            paths = _image_files(self.local_dir, LOCAL_IMAGE_SUFFIXES)
        except OSError as exc:
            logger.warning("local screensaver photos unreadable: %s", exc)
            return
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (path.name, stat.st_size, stat.st_mtime_ns)
            order.append(key)
            if key not in self._local_prepared and key not in self._local_failed:
                pending.append((path, key))
        live = set(order)
        for key in [key for key in self._local_prepared if key not in live]:
            entry = self._local_prepared.pop(key)
            self._local_retired[entry.local_path] = (entry, now)
            self._local_next_expiry = min(
                self._local_next_expiry, now + LOCAL_RETIRE_GRACE_SECONDS
            )
        self._local_failed &= live
        self._local_order = order
        self._local_pending = pending
        self._local_scan_mtime = mtime
        self._local_scanned_at = now

    def _prepare_pending(
        self,
        pending: list[tuple[Path, LocalKey]],
        *,
        max_new: int | None,
        wait: bool,
        time_budget: float | None = None,
    ) -> None:
        if not self._prepare_lock.acquire(blocking=wait):
            return
        try:
            started = time.monotonic()
            decoded = 0
            for path, key in pending:
                target = self.local_cache_dir / f"{self._local_digest(key)[:16]}.jpg"
                if not target.exists():
                    if max_new is not None and decoded >= max_new:
                        continue
                    # At least one decode per call, so a tick always progresses.
                    if (
                        time_budget is not None
                        and decoded
                        and time.monotonic() - started >= time_budget
                    ):
                        continue
                    decoded += 1
                entry = self._prepare_local(path, key, target)
                with self._lock:
                    if entry is None:
                        self._local_failed.add(key)
                    else:
                        self._local_prepared[key] = entry
                    if (path, key) in self._local_pending:
                        self._local_pending.remove((path, key))
            self._sweep_local_cache()
        finally:
            self._prepare_lock.release()

    @staticmethod
    def _local_digest(key: LocalKey) -> str:
        name, size, mtime_ns = key
        return hashlib.sha1(f"{name}\0{size}\0{mtime_ns}".encode("utf-8")).hexdigest()

    def _prepare_local(
        self, path: Path, key: LocalKey, target: Path
    ) -> PhotoManifestEntry | None:
        name = key[0]
        width, height = self.display_size
        try:
            if Image is None or ImageOps is None:  # pragma: no cover
                target = path
            elif not target.exists():
                self.local_cache_dir.mkdir(parents=True, exist_ok=True)
                width, height = self._write_fitted(path, target)
        except Exception as exc:  # noqa: BLE001 — Pillow exceptions vary
            logger.warning("local screensaver photo %s skipped: %s", name, exc)
            return None
        return PhotoManifestEntry(
            id=f"local-{name}",
            source_url=f"local:{name}",
            local_path=str(target.resolve()),
            public_path=_public_path("/media/screensaver-local", target.name),
            width=width,
            height=height,
            # Same identity the copy is keyed by: name, size and mtime change
            # whenever the photo does, without reading it a second time.
            content_hash=self._local_digest(key),
        )

    def _sweep_local_cache(self) -> None:
        """Delete prepared copies nothing points to any more. Runs under
        ``_prepare_lock`` so it never races a copy that is being written."""
        now = self._clock()
        with self._lock:
            for path, (_, retired_at) in list(self._local_retired.items()):
                if now - retired_at >= LOCAL_RETIRE_GRACE_SECONDS:
                    del self._local_retired[path]
            self._local_next_expiry = min(
                (at + LOCAL_RETIRE_GRACE_SECONDS for _, at in self._local_retired.values()),
                default=float("inf"),
            )
            keep = {Path(entry.local_path).name for entry in self._local_prepared.values()}
            keep |= {Path(path).name for path in self._local_retired}
        try:
            stale = [
                path
                for path in self.local_cache_dir.iterdir()
                if path.is_file() and path.name not in keep
            ]
        except OSError:
            return
        for path in stale:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("could not remove stale local copy %s: %s", path, exc)

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
        self, source_url: str, *, timeout_seconds: int, version: str | None = None
    ) -> PhotoManifestEntry | None:
        payload, headers = self._downloader(source_url, timeout_seconds)
        # An edit gets its own filename, so the kiosk never shows a copy it
        # cached under the old one. Unedited photos keep the pre-#15 name.
        cache_key = f"{source_url}#{version}" if version else source_url
        digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:16]
        filename = f"{digest}.jpg"
        local_path = self.cache_dir / filename
        width, height = self.display_size

        if Image is not None and ImageOps is not None:
            try:
                width, height = self._write_fitted(payload, local_path)
            except RuntimeError as exc:
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
            source_version=version,
        )

    def _write_fitted(self, source: bytes | Path, target: Path) -> tuple[int, int]:
        # Plan B9: any Pillow error — malformed bytes, dimensions over the
        # MAX_IMAGE_PIXELS cap, unsupported format — becomes a RuntimeError the
        # caller turns into a skipped entry, never a crash of the refresh loop.
        try:
            opened = io.BytesIO(source) if isinstance(source, bytes) else source
            with Image.open(opened) as image:
                # JPEG only (a no-op otherwise): let libjpeg decode at 1/2–1/8
                # scale instead of building the full-resolution bitmap. A
                # square request keeps both edges at least the display's long
                # edge whatever the EXIF rotation, so the cover-fit below
                # never has to upscale a draft.
                edge = max(self.display_size)
                image.draft("RGB", (edge, edge))
                if image.format != "JPEG" and image.width * image.height > NON_JPEG_MAX_PIXELS:
                    raise ValueError(
                        f"{image.format} {image.width}x{image.height} exceeds "
                        f"{NON_JPEG_MAX_PIXELS} px without a reduced decode path"
                    )
                prepared = ImageOps.exif_transpose(image).convert("RGB")
                fitted = ImageOps.fit(
                    prepared, self.display_size, method=Image.Resampling.LANCZOS
                )
                # Write aside, then rename: a power cut mid-save must not leave
                # a truncated JPEG under the final name, where the local-photo
                # path would reuse it as "already prepared" forever.
                partial = target.with_name(f"{target.name}.partial")
                fitted.save(partial, format="JPEG", quality=86, optimize=True)
                os.replace(partial, target)
                return fitted.size
        except (OSError, ValueError, MemoryError, Image.DecompressionBombError) as exc:
            raise RuntimeError(str(exc)) from exc

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

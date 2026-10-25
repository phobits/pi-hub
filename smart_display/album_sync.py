"""One-shot Lightroom album sync, run on the Pi by ``scripts/sync-album.sh`` (#15).

The scheduler pulls one new photo per tick (``images_per_tick``), so a freshly
filled album takes hours. This reconciles everything at once: new photos,
edited ones, and photos that left the album.

The backend must be stopped first. It holds the manifest in memory and writes
it back on its next tick, deleting every file it does not know about — this
run's downloads included. ``main`` refuses to run while ``/health`` answers.

    .venv/bin/python -m smart_display.album_sync [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

from smart_display.cache.image_cache import ImageCache
from smart_display.config import AppConfig, load_config
from smart_display.http_client import HttpError
from smart_display.providers.lightroom_source import LightroomSourceProvider
from smart_display.sd_notify import probe_health


def _backend_is_running(config: AppConfig) -> bool:
    # The host serve_app actually binds (127.0.0.1, ::1 or localhost), as the
    # watchdog probe does — a fixed 127.0.0.1 misses a backend on ::1.
    return probe_health(config.app.host or "127.0.0.1", config.app.port, timeout=2)


def _image_cache(config: AppConfig) -> ImageCache:
    # Same arguments as create_app, minus the demo set: this run only ever
    # touches the album cache.
    return ImageCache(
        cache_dir=config.screensaver_cache_dir,
        manifest_path=config.screensaver_manifest_path,
        remote_enabled=True,
        local_dir=config.screensaver_local_dir,
    )


def main(argv: list[str] | None = None, *, config: AppConfig | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="nur anzeigen, nichts laden oder löschen"
    )
    args = parser.parse_args(argv)
    config = config or load_config()

    if not config.screensaver.source_url:
        print("Keine Bildquelle konfiguriert (SCREENSAVER_SOURCE_URL).", file=sys.stderr)
        return 1
    if not args.dry_run and _backend_is_running(config):
        print(
            "Das Backend läuft noch und würde diesen Abgleich beim nächsten Tick"
            " überschreiben. Erst beide Units stoppen — scripts/sync-album.sh"
            " macht das.",
            file=sys.stderr,
        )
        return 2

    cache = _image_cache(config)
    # The provider is only used for fetching; it never touches the store.
    provider = LightroomSourceProvider(config, None, cache)  # type: ignore[arg-type]
    try:
        images = provider.fetch_album_images()
    except (HttpError, RuntimeError) as exc:
        print(f"Album nicht abrufbar: {exc}", file=sys.stderr)
        return 1

    plan = cache.plan_remote_sync(list(images), images)
    print(
        f"Album: {len(plan.urls)} · gecacht: {len(plan.existing)} · neu: {len(plan.new)}"
        f" · bearbeitet: {len(plan.edited)} · weg: {len(plan.gone)}"
    )
    if plan.adopt:
        print(f"Altbestand ohne Bearbeitungsstand, wird übernommen: {len(plan.adopt)}")
    if args.dry_run:
        print("Dry-Run — nichts verändert.")
        return 0

    entries = cache.sync_remote_images(
        list(images),
        timeout_seconds=config.screensaver.timeout_seconds,
        versions=images,
        respect_backoff=False,
    )
    failures = {url: f for url, f in cache.failed_urls().items() if url in images}
    print(f"Fertig: {len(entries)} von {len(plan.urls)} Fotos gecacht.")
    for url, failure in failures.items():
        if "unchanged_for" in failure:
            # Not an error: the edit is in, the re-rendered image is not.
            print(f"  wartet auf Lightroom (Rendition noch unverändert): {url}")
        else:
            print(f"  offen: {failure.get('last_error')} — {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

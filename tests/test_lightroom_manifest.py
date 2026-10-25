from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from smart_display import album_sync
from smart_display.cache.image_cache import (
    _INITIAL_BACKOFF_SECONDS,
    _MAX_BACKOFF_SECONDS,
    _compute_next_retry,
    ImageCache,
    UNCHANGED_RENDITION_MAX_ATTEMPTS,
)
from smart_display.providers.lightroom_source import (
    LightroomSourceProvider,
    _extract_lightroom_api_images,
    extract_image_urls,
)
from tests._support import FakeHttpClient, make_app_config, make_state_store


SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z4xQAAAAASUVORK5CYII="
)


def _synthetic_jpeg(width: int, height: int) -> bytes:
    """Render a real JPEG of the requested size so the Pillow pipeline gets
    exercised with non-trivial data (LANCZOS fit, exif_transpose, re-encode).

    Uses a simple gradient + two solid blocks so the output is clearly
    distinguishable from a blank or a 1x1 placeholder.
    """
    from PIL import Image as _Image  # type: ignore

    image = _Image.new("RGB", (width, height), color=(48, 96, 192))
    # Draw a high-frequency pattern so LANCZOS resampling has something real
    # to filter — otherwise the codec might degenerate to a trivial entropy.
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()


class LightroomManifestTest(unittest.TestCase):
    def test_extract_image_urls_deduplicates_candidates(self) -> None:
        html = """
        <html>
          <head><meta property="og:image" content="/img/cover.jpg"></head>
          <body><img src="/img/cover.jpg"><img data-src="https://example.com/img/second.png"></body>
        </html>
        """
        urls = extract_image_urls(html, "https://example.com/gallery")
        self.assertEqual(
            urls,
            [
                "https://example.com/img/cover.jpg",
                "https://example.com/img/second.png",
            ],
        )

    def test_extract_image_urls_accepts_lightroom_renditions(self) -> None:
        html = """
        <meta property="og:image"
          content="https://photos.adobe.io/v2/spaces/s/assets/a/revisions/r/renditions/x?api_key=LightroomMobileWeb1">
        """
        urls = extract_image_urls(html, "https://lightroom.adobe.com/shares/abc")

        self.assertEqual(
            urls,
            [
                "https://photos.adobe.io/v2/spaces/s/assets/a/revisions/r/renditions/x?api_key=LightroomMobileWeb1",
            ],
        )

    def test_sync_remote_images_reuses_existing_manifest_entries(self) -> None:
        calls: list[str] = []

        def fake_downloader(url: str, timeout_seconds: int):
            calls.append(url)
            return SAMPLE_PNG, {}

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = ImageCache(
                cache_dir=Path(temp_dir) / "screensaver",
                manifest_path=Path(temp_dir) / "screensaver" / "manifest.json",
                downloader=fake_downloader,
            )
            urls = [
                "https://example.com/a.jpg",
                "https://example.com/b.jpg",
            ]
            cache.sync_remote_images(urls, timeout_seconds=5)
            cache.sync_remote_images(urls, timeout_seconds=5)

            self.assertEqual(calls, urls)
            self.assertEqual(cache.count(), 2)

    def test_provider_follows_shared_album_redirect(self) -> None:
        calls: list[str] = []

        def fake_downloader(url: str, timeout_seconds: int):
            calls.append(url)
            return SAMPLE_PNG, {}

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            base = make_app_config(tmp)
            config = replace(
                base,
                screensaver=replace(
                    base.screensaver,
                    source_url="https://adobe.ly/short",
                    images_per_tick=1,
                ),
            )
            store = make_state_store(tmp)
            cache = ImageCache(
                cache_dir=tmp / "screensaver",
                manifest_path=tmp / "screensaver" / "manifest.json",
                downloader=fake_downloader,
            )
            http = FakeHttpClient()
            http.add_response(
                "GET",
                "https://adobe.ly/short",
                status=301,
                headers={"location": "https://lightroom.adobe.com/shares/album"},
            )
            http.add_response(
                "GET",
                "https://lightroom.adobe.com/shares/album",
                body="""
                <script>
                  window.SharesConfig = {
                    spaceAttributes: {"base":"https://photos.adobe.io/v2/"},
                    albumAttributes: {
                      "base":"https://photos.adobe.io/v2/spaces/space/",
                      "links":{
                        "/rels/space_album_images_videos":{
                          "href":"albums/album/assets?embed=asset\\u0026subtype=image%3Bvideo"
                        }
                      }
                    }
                  };
                </script>
                """,
            )
            http.add_response(
                "GET",
                "https://photos.adobe.io/v2/spaces/space/albums/album/assets?embed=asset&subtype=image%3Bvideo",
                body="""
                while (1) {}
                {
                  "base": "https://photos.adobe.io/v2/spaces/space/",
                  "resources": [
                    {
                      "asset": {
                        "links": {
                          "/rels/rendition_type/1280": {
                            "href": "assets/asset/revisions/rev/renditions/render"
                          }
                        }
                      }
                    }
                  ]
                }
                """,
            )

            provider = LightroomSourceProvider(config, store, cache, http_client=http)
            provider.refresh()

            health = store.health_payload()["providers"]["screensaver"]
            self.assertEqual(health["status"], "ok")
            self.assertEqual(
                calls,
                [
                    "https://photos.adobe.io/v2/spaces/space/assets/asset/revisions/rev/renditions/render?api_key=LightroomMobileWeb1",
                ],
            )

    def test_album_cover_is_not_cached_as_a_second_photo(self) -> None:
        # The share page's og:image is the album cover: one of the album's
        # assets again, as a different rendition. With the album API
        # answering, only its list counts — otherwise the cover photo is
        # cached twice and shown twice per round.
        calls: list[str] = []

        def fake_downloader(url: str, timeout_seconds: int):
            calls.append(url)
            return SAMPLE_PNG, {}

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            base = make_app_config(tmp)
            config = replace(
                base,
                screensaver=replace(
                    base.screensaver,
                    source_url="https://lightroom.adobe.com/shares/album",
                    images_per_tick=10,
                ),
            )
            store = make_state_store(tmp)
            cache = ImageCache(
                cache_dir=tmp / "screensaver",
                manifest_path=tmp / "screensaver" / "manifest.json",
                downloader=fake_downloader,
            )
            http = FakeHttpClient()
            http.add_response(
                "GET",
                "https://lightroom.adobe.com/shares/album",
                body="""
                <meta property="og:image"
                  content="https://photos.adobe.io/v2/spaces/space/assets/a1/revisions/rev/renditions/cover?api_key=LightroomMobileWeb1">
                <script>
                  window.SharesConfig = {
                    albumAttributes: {
                      "base":"https://photos.adobe.io/v2/spaces/space/",
                      "links":{
                        "/rels/space_album_images_videos":{"href":"albums/album/assets"}
                      }
                    }
                  };
                </script>
                """,
            )
            http.add_response(
                "GET",
                "https://photos.adobe.io/v2/spaces/space/albums/album/assets",
                body="""
                {
                  "base": "https://photos.adobe.io/v2/spaces/space/",
                  "resources": [
                    {"asset": {"links": {"/rels/rendition_type/1280": {
                      "href": "assets/a1/revisions/rev/renditions/r1280"}}}},
                    {"asset": {"links": {"/rels/rendition_type/1280": {
                      "href": "assets/a2/revisions/rev/renditions/r1280"}}}}
                  ]
                }
                """,
            )

            provider = LightroomSourceProvider(config, store, cache, http_client=http)
            provider.refresh()

            self.assertEqual(
                calls,
                [
                    "https://photos.adobe.io/v2/spaces/space/assets/a1/revisions/rev/renditions/r1280?api_key=LightroomMobileWeb1",
                    "https://photos.adobe.io/v2/spaces/space/assets/a2/revisions/rev/renditions/r1280?api_key=LightroomMobileWeb1",
                ],
            )
            self.assertEqual(cache.count(), 2)


class AlbumApiFailureKeepsCacheTest(unittest.TestCase):
    """The share page's HTML only carries the album cover. If a failed or
    empty album-API answer fell back to it, the cache would be reconciled
    down to that one photo and every other cached photo deleted — then
    re-downloaded at images_per_tick per refresh, i.e. for hours."""

    PAGE_URL = "https://lightroom.adobe.com/shares/album"
    API_URL = "https://photos.adobe.io/v2/spaces/space/albums/album/assets"
    PAGE = """
    <meta property="og:image"
      content="https://photos.adobe.io/v2/spaces/space/assets/a1/revisions/rev/renditions/cover?api_key=LightroomMobileWeb1">
    <script>
      window.SharesConfig = {
        albumAttributes: {
          "base":"https://photos.adobe.io/v2/spaces/space/",
          "links":{"/rels/space_album_images_videos":{"href":"albums/album/assets"}}
        }
      };
    </script>
    """
    API_OK = """
    {
      "base": "https://photos.adobe.io/v2/spaces/space/",
      "resources": [
        {"asset": {"links": {"/rels/rendition_type/1280": {
          "href": "assets/a1/revisions/rev/renditions/r1280"}}}},
        {"asset": {"links": {"/rels/rendition_type/1280": {
          "href": "assets/a2/revisions/rev/renditions/r1280"}}}}
      ]
    }
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        base = make_app_config(self.tmp)
        self.config = replace(
            base,
            screensaver=replace(
                base.screensaver, source_url=self.PAGE_URL, images_per_tick=10
            ),
        )
        self.store = make_state_store(self.tmp)
        self.cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )

    def _refresh(self, **api_response) -> None:
        http = FakeHttpClient()
        http.add_response("GET", self.PAGE_URL, body=self.PAGE)
        http.add_response("GET", self.API_URL, **api_response)
        LightroomSourceProvider(
            self.config, self.store, self.cache, http_client=http
        ).refresh()

    def _cached_files(self) -> set[str]:
        return {Path(entry.local_path).name for entry in self.cache.entries()}

    def _assert_cache_kept(self) -> set[str]:
        before = self._cached_files()
        self.assertEqual(len(before), 2)
        return before

    def test_api_error_keeps_cached_album(self) -> None:
        self._refresh(body=self.API_OK)
        before = self._assert_cache_kept()

        self._refresh(status=500, body="")

        self.assertEqual(self._cached_files(), before)
        for name in before:
            self.assertTrue((self.tmp / "screensaver" / name).exists())
        health = self.store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "stale")

    def test_empty_api_answer_keeps_cached_album(self) -> None:
        # A changed Adobe payload shape parses to no URLs — that must look
        # like an outage, not like a healthy one-photo album.
        self._refresh(body=self.API_OK)
        before = self._assert_cache_kept()

        self._refresh(body='{"resources": []}')

        self.assertEqual(self._cached_files(), before)
        health = self.store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "stale")


class PerTickDownloadLimitTest(unittest.TestCase):
    """Plan B9: the provider may only fetch a small number of new images
    per refresh tick so the Pi Zero isn't monopolised by a fresh album
    import. Already-cached entries stay visible across ticks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_cache(self, downloader):
        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=downloader,
        )

    def test_per_tick_budget_caps_new_downloads(self) -> None:
        calls: list[str] = []

        def fake_downloader(url: str, timeout_seconds: int):
            calls.append(url)
            return SAMPLE_PNG, {}

        cache = self._make_cache(fake_downloader)
        urls = [
            "https://example.com/a.jpg",
            "https://example.com/b.jpg",
            "https://example.com/c.jpg",
        ]
        cache.sync_remote_images(urls, timeout_seconds=5, max_new_downloads=1)
        self.assertEqual(cache.count(), 1)
        self.assertEqual(len(calls), 1)

        cache.sync_remote_images(urls, timeout_seconds=5, max_new_downloads=1)
        self.assertEqual(cache.count(), 2)
        self.assertEqual(len(calls), 2)

        cache.sync_remote_images(urls, timeout_seconds=5, max_new_downloads=1)
        self.assertEqual(cache.count(), 3)
        self.assertEqual(len(calls), 3)

    def test_unbounded_budget_downloads_everything(self) -> None:
        calls: list[str] = []

        def fake_downloader(url: str, timeout_seconds: int):
            calls.append(url)
            return SAMPLE_PNG, {}

        cache = self._make_cache(fake_downloader)
        cache.sync_remote_images(
            ["https://example.com/a.jpg", "https://example.com/b.jpg"],
            timeout_seconds=5,
        )
        self.assertEqual(len(calls), 2)


class DownloadFailureBackoffTest(unittest.TestCase):
    """Plan B9: payload/decode failures must not crash the refresh loop,
    must be recorded, and must respect exponential backoff so the Pi
    doesn't thrash on a broken album."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._now = 1_000_000.0

    def _now_fn(self) -> float:
        return self._now

    def _make_cache(self, downloader):
        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=downloader,
            clock=self._now_fn,
        )

    def test_bad_payload_is_recorded_and_skipped(self) -> None:
        def fake_downloader(url: str, timeout_seconds: int):
            return b"not an image", {}

        cache = self._make_cache(fake_downloader)
        cache.sync_remote_images(["https://example.com/bad.jpg"], timeout_seconds=5)
        self.assertEqual(cache.count(), 0)
        failures = cache.failed_urls()
        self.assertIn("https://example.com/bad.jpg", failures)
        self.assertEqual(failures["https://example.com/bad.jpg"]["attempts"], 1)

    def test_failure_blocks_retries_until_backoff_elapses(self) -> None:
        call_count = {"count": 0}

        def fake_downloader(url: str, timeout_seconds: int):
            call_count["count"] += 1
            return b"not an image", {}

        cache = self._make_cache(fake_downloader)
        cache.sync_remote_images(["https://example.com/bad.jpg"], timeout_seconds=5)
        self.assertEqual(call_count["count"], 1)

        # Within the backoff window: do not retry.
        self._now += 10.0
        cache.sync_remote_images(["https://example.com/bad.jpg"], timeout_seconds=5)
        self.assertEqual(call_count["count"], 1)

        # Past the backoff window: retry, attempts increments.
        self._now += _INITIAL_BACKOFF_SECONDS + 5.0
        cache.sync_remote_images(["https://example.com/bad.jpg"], timeout_seconds=5)
        self.assertEqual(call_count["count"], 2)
        self.assertEqual(
            cache.failed_urls()["https://example.com/bad.jpg"]["attempts"], 2
        )

    def test_successful_retry_clears_failure(self) -> None:
        # First call fails, second call (well past backoff) succeeds.
        attempts = {"count": 0}

        def fake_downloader(url: str, timeout_seconds: int):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return b"not an image", {}
            return SAMPLE_PNG, {}

        cache = self._make_cache(fake_downloader)
        cache.sync_remote_images(["https://example.com/one.jpg"], timeout_seconds=5)
        self.assertEqual(cache.count(), 0)
        self.assertIn("https://example.com/one.jpg", cache.failed_urls())

        self._now += _INITIAL_BACKOFF_SECONDS + 5.0
        cache.sync_remote_images(["https://example.com/one.jpg"], timeout_seconds=5)
        self.assertEqual(cache.count(), 1)
        self.assertNotIn("https://example.com/one.jpg", cache.failed_urls())

    def test_failures_prune_when_url_leaves_source_list(self) -> None:
        def fake_downloader(url: str, timeout_seconds: int):
            return b"not an image", {}

        cache = self._make_cache(fake_downloader)
        cache.sync_remote_images(["https://example.com/gone.jpg"], timeout_seconds=5)
        self.assertIn("https://example.com/gone.jpg", cache.failed_urls())

        cache.sync_remote_images([], timeout_seconds=5)
        self.assertNotIn("https://example.com/gone.jpg", cache.failed_urls())


class RetryBackoffMathTest(unittest.TestCase):
    def test_backoff_doubles_each_attempt(self) -> None:
        self.assertEqual(_compute_next_retry(1), _INITIAL_BACKOFF_SECONDS)
        self.assertEqual(_compute_next_retry(2), _INITIAL_BACKOFF_SECONDS * 2)
        self.assertEqual(_compute_next_retry(3), _INITIAL_BACKOFF_SECONDS * 4)

    def test_backoff_caps_at_24h(self) -> None:
        # 60 * 2^20 is way past 24 h — must clamp.
        self.assertEqual(_compute_next_retry(20), _MAX_BACKOFF_SECONDS)


class PillowPipelineRealBytesTest(unittest.TestCase):
    """Exercise the actual Pillow pipeline — exif_transpose, LANCZOS fit,
    JPEG re-encode — with a non-trivial bitmap. Previous coverage only used
    a 1x1 placeholder which skipped the resize codepath entirely.

    These are the steps the Pi Zero actually runs on every new album photo,
    so if the code path is broken we want to know before the hardware does.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_cache(self, payload: bytes) -> ImageCache:
        def fake_downloader(url: str, timeout_seconds: int):
            return payload, {}

        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=fake_downloader,
        )

    def test_oversized_jpeg_is_fit_to_display_size(self) -> None:
        # Typical Lightroom export: 2400x1600 landscape.
        payload = _synthetic_jpeg(2400, 1600)
        cache = self._make_cache(payload)
        entries = cache.sync_remote_images(
            ["https://example.com/big.jpg"], timeout_seconds=5
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.width, 1024)
        self.assertEqual(entry.height, 600)
        # The cached file exists and is a valid JPEG that Pillow can re-open.
        from PIL import Image as _Image  # type: ignore

        with _Image.open(entry.local_path) as reopened:
            self.assertEqual(reopened.size, (1024, 600))
            self.assertEqual(reopened.format, "JPEG")

    def test_portrait_jpeg_is_cover_fit(self) -> None:
        # Portrait photo: Pillow's ImageOps.fit crops to cover, so the final
        # aspect must still be 1024x600.
        payload = _synthetic_jpeg(1200, 1800)
        cache = self._make_cache(payload)
        entries = cache.sync_remote_images(
            ["https://example.com/portrait.jpg"], timeout_seconds=5
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].width, 1024)
        self.assertEqual(entries[0].height, 600)

    def test_small_jpeg_is_upscaled_to_display(self) -> None:
        # Tiny thumbnail: fit() still normalises dimensions, even if the
        # resulting quality is ugly. Important property: no crash.
        payload = _synthetic_jpeg(320, 240)
        cache = self._make_cache(payload)
        entries = cache.sync_remote_images(
            ["https://example.com/thumb.jpg"], timeout_seconds=5
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].width, 1024)
        self.assertEqual(entries[0].height, 600)

    def test_encoded_output_is_bounded(self) -> None:
        # Sanity: after quality=86 re-encode the on-disk file must be
        # smaller than the IMAGE_MAX_BYTES cap (8 MB). A 2400x1600 random
        # pattern is a pessimistic case for JPEG compression.
        payload = _synthetic_jpeg(2400, 1600)
        cache = self._make_cache(payload)
        entries = cache.sync_remote_images(
            ["https://example.com/pattern.jpg"], timeout_seconds=5
        )
        size = Path(entries[0].local_path).stat().st_size
        self.assertLess(size, 1_500_000, f"expected <1.5 MB, got {size}")


class DemoEntriesCacheTest(unittest.TestCase):
    """Plan C2: ``demo_entries`` used to re-walk the demo dir on every
    ``next_entry`` tick. Cache it, but invalidate when the directory mtime
    changes so adding/removing demo files still takes effect."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.demo_dir = self.tmp / "demo"
        self.demo_dir.mkdir()
        (self.demo_dir / "first.jpg").write_bytes(SAMPLE_PNG)

    def _make_cache(self) -> ImageCache:
        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            demo_dir=self.demo_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )

    def test_demo_entries_cached_between_calls(self) -> None:
        cache = self._make_cache()
        first = cache.demo_entries()
        self.assertEqual(len(first), 1)

        # Monkey-patch iterdir to detect any re-scan attempts.
        calls = {"count": 0}
        original_iterdir = Path.iterdir

        def counting_iterdir(self_path):
            if self_path == self.demo_dir:
                calls["count"] += 1
            return original_iterdir(self_path)

        Path.iterdir = counting_iterdir  # type: ignore[assignment]
        try:
            second = cache.demo_entries()
            third = cache.demo_entries()
        finally:
            Path.iterdir = original_iterdir  # type: ignore[assignment]

        self.assertEqual(len(second), 1)
        self.assertEqual(len(third), 1)
        self.assertEqual(calls["count"], 0, "cache must not re-walk demo dir")

    def test_demo_cache_invalidates_on_mtime_change(self) -> None:
        cache = self._make_cache()
        self.assertEqual(len(cache.demo_entries()), 1)

        # Add a second file and bump the directory mtime.
        (self.demo_dir / "second.jpg").write_bytes(SAMPLE_PNG)
        import os as _os

        st = self.demo_dir.stat()
        _os.utime(self.demo_dir, (st.st_atime, st.st_mtime + 10))

        refreshed = cache.demo_entries()
        self.assertEqual(len(refreshed), 2)

    def test_demo_cache_returns_empty_if_dir_vanishes(self) -> None:
        cache = self._make_cache()
        self.assertEqual(len(cache.demo_entries()), 1)

        # Delete the demo directory entirely.
        for child in self.demo_dir.iterdir():
            child.unlink()
        self.demo_dir.rmdir()

        self.assertEqual(cache.demo_entries(), [])


class LocalEntriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.local_dir = self.tmp / "screensaver-local"
        self.local_dir.mkdir()
        (self.local_dir / "family.jpg").write_bytes(SAMPLE_PNG)

    def _make_cache(self, clock=None) -> ImageCache:
        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            local_dir=self.local_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
            clock=clock,
        )

    def test_local_photos_join_remote_rotation(self) -> None:
        cache = self._make_cache()
        cache.sync_remote_images(["https://example.com/remote.jpg"], timeout_seconds=5)

        self.assertEqual(cache.count(), 1)
        self.assertEqual(cache.available_count(), 2)
        self.assertEqual(
            {entry.source_url for entry in cache.available_entries()},
            {"https://example.com/remote.jpg", "local:family.jpg"},
        )

    def test_local_photos_replace_demo_fallback(self) -> None:
        demo_dir = self.tmp / "demo"
        demo_dir.mkdir()
        (demo_dir / "demo.jpg").write_bytes(SAMPLE_PNG)
        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            local_dir=self.local_dir,
            demo_dir=demo_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )

        self.assertEqual(cache.available_count(), 1)
        self.assertEqual(cache.next_entry().source_url, "local:family.jpg")

    def test_local_lookup_rejects_unknown_and_nested_paths(self) -> None:
        cache = self._make_cache()
        (entry,) = cache.local_entries()
        served_name = Path(entry.local_path).name

        self.assertEqual(entry.public_path, f"/media/screensaver-local/{served_name}")
        self.assertEqual(cache.local_entry_for_filename(served_name), entry)
        self.assertIsNone(cache.local_entry_for_filename("family.jpg"))
        self.assertIsNone(cache.local_entry_for_filename(f"../{served_name}"))
        self.assertIsNone(cache.local_entry_for_filename("missing.jpg"))

    def test_local_photo_is_fit_to_display_size(self) -> None:
        # #5.1: a phone photo used to reach WPE at full resolution.
        (self.local_dir / "family.jpg").write_bytes(_synthetic_jpeg(1600, 1200))
        cache = self._make_cache()

        (entry,) = cache.local_entries()

        from PIL import Image as _Image  # type: ignore

        with _Image.open(entry.local_path) as served:
            self.assertEqual(served.size, (1024, 600))
        self.assertEqual((entry.width, entry.height), (1024, 600))
        self.assertTrue(Path(entry.local_path).is_relative_to(cache.local_cache_dir.resolve()))
        # The user's original is never touched.
        with _Image.open(self.local_dir / "family.jpg") as original:
            self.assertEqual(original.size, (1600, 1200))

    def test_awkward_filenames_get_safe_urls_and_distinct_ids(self) -> None:
        # #5.2 + #5.7: "#"/"%" broke the URL, and a shared stem shared the id.
        (self.local_dir / "Urlaub #2 100%.jpg").write_bytes(SAMPLE_PNG)
        (self.local_dir / "family.png").write_bytes(SAMPLE_PNG)
        cache = self._make_cache()

        entries = cache.prepare_local_photos()

        self.assertEqual(len(entries), 3)
        self.assertEqual(len({entry.id for entry in entries}), 3)
        for entry in entries:
            self.assertRegex(
                entry.public_path, r"^/media/screensaver-local/[0-9a-f]{16}\.jpg$"
            )

    def test_dotfiles_are_not_photos(self) -> None:
        # #5.6: macOS AppleDouble companions carry the suffix, not an image.
        (self.local_dir / "._family.jpg").write_bytes(b"\x00\x05\x16\x07 resource fork")
        (self.local_dir / ".hidden.jpg").write_bytes(SAMPLE_PNG)
        demo_dir = self.tmp / "demo"
        demo_dir.mkdir()
        (demo_dir / "demo.jpg").write_bytes(SAMPLE_PNG)
        (demo_dir / "._demo.jpg").write_bytes(b"resource fork")
        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            local_dir=self.local_dir,
            demo_dir=demo_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )

        self.assertEqual(
            [entry.source_url for entry in cache.local_entries()], ["local:family.jpg"]
        )
        self.assertEqual([entry.id for entry in cache.demo_entries()], ["demo"])

    def test_replaced_photo_gets_new_hash_and_old_copy_retires(self) -> None:
        # #5.7: same name, new content — the directory mtime need not change,
        # so the swap is found by the periodic re-stat.
        import os as _os

        from smart_display.cache.image_cache import (
            LOCAL_RESCAN_SECONDS,
            LOCAL_RETIRE_GRACE_SECONDS,
        )

        now = [1000.0]
        cache = self._make_cache(clock=lambda: now[0])
        (before,) = cache.local_entries()
        photo = self.local_dir / "family.jpg"
        photo.write_bytes(_synthetic_jpeg(64, 48))
        stat = photo.stat()
        _os.utime(photo, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        dir_stat = self.local_dir.stat()
        _os.utime(self.local_dir, ns=(dir_stat.st_atime_ns, dir_stat.st_mtime_ns))

        self.assertEqual(cache.local_entries(), [before], "no re-stat inside the window")

        now[0] += LOCAL_RESCAN_SECONDS + 1
        (after,) = cache.local_entries()

        self.assertNotEqual(after.content_hash, before.content_hash)
        self.assertNotEqual(after.local_path, before.local_path)
        self.assertEqual(after.id, before.id)
        # #5 review: the kiosk may have just been handed the old URL.
        self.assertTrue(Path(before.local_path).exists())
        served_before = Path(before.local_path).name
        self.assertEqual(cache.local_entry_for_filename(served_before), before)

        now[0] += LOCAL_RETIRE_GRACE_SECONDS + 1
        cache.local_entries()

        self.assertFalse(Path(before.local_path).exists())
        self.assertIsNone(cache.local_entry_for_filename(served_before))

    def test_removed_photo_leaves_rotation_at_once_and_disk_after_grace(self) -> None:
        from smart_display.cache.image_cache import LOCAL_RETIRE_GRACE_SECONDS

        now = [1000.0]
        cache = self._make_cache(clock=lambda: now[0])
        (entry,) = cache.local_entries()
        (self.local_dir / "family.jpg").unlink()

        self.assertEqual(cache.local_entries(), [])
        self.assertTrue(Path(entry.local_path).exists())

        now[0] += LOCAL_RETIRE_GRACE_SECONDS + 1
        cache.local_entries()
        self.assertFalse(Path(entry.local_path).exists())

    def test_request_path_decodes_at_most_one_new_photo(self) -> None:
        # #5 review: forty new photos must not stall one slideshow request.
        for index in range(4):
            (self.local_dir / f"photo-{index}.jpg").write_bytes(SAMPLE_PNG)
        cache = self._make_cache()
        decodes = {"count": 0}
        original = cache._write_fitted

        def counting(source, target):
            decodes["count"] += 1
            return original(source, target)

        cache._write_fitted = counting

        self.assertEqual(len(cache.local_entries()), 1)
        self.assertEqual(decodes["count"], 1)
        self.assertEqual(len(cache.prepare_local_photos()), 5)
        self.assertEqual(decodes["count"], 5)

    def test_restart_reuses_prepared_copies_without_decoding(self) -> None:
        # #5 review: every deploy restarts the backend.
        first = self._make_cache()
        (prepared,) = first.prepare_local_photos()
        restarted = self._make_cache()
        restarted._write_fitted = lambda source, target: self.fail("decoded again")

        (reused,) = restarted.local_entries()

        self.assertEqual(reused, prepared)
        self.assertTrue(Path(reused.local_path).exists())

    def test_media_lookup_after_restart_finds_copy_on_disk(self) -> None:
        # #5 review: the kiosk may hold a URL from before the restart.
        (prepared,) = self._make_cache().prepare_local_photos()
        restarted = self._make_cache()
        restarted._write_fitted = lambda source, target: self.fail("decoded on media route")

        found = restarted.local_entry_for_filename(Path(prepared.local_path).name)

        self.assertEqual(found, prepared)

    def test_interrupted_write_is_never_reused(self) -> None:
        # #5 review: power cut while the copy was being written.
        cache = self._make_cache()

        def interrupted(source, target):
            target.with_name(f"{target.name}.partial").write_bytes(b"truncated")
            raise OSError("power cut")

        cache._write_fitted = interrupted
        self.assertEqual(cache.local_entries(), [])
        restarted = self._make_cache()

        (entry,) = restarted.prepare_local_photos()

        from PIL import Image as _Image  # type: ignore

        with _Image.open(entry.local_path) as served:
            self.assertEqual(served.size, (1024, 600))
        self.assertEqual(list(restarted.local_cache_dir.glob("*.partial")), [])

    def test_provider_tick_retries_a_failed_photo(self) -> None:
        cache = self._make_cache()
        original = cache._write_fitted

        def exhausted(source, target):
            raise MemoryError()

        cache._write_fitted = exhausted
        self.assertEqual(cache.local_entries(), [])
        cache._write_fitted = original

        self.assertEqual(len(cache.local_entries()), 0, "requests do not retry")
        self.assertEqual(len(cache.prepare_local_photos()), 1)

    def test_photo_added_mid_round_does_not_repeat_shown_ones(self) -> None:
        # #5 review: a growing pool used to re-shuffle the whole round.
        for name in ("a.jpg", "b.jpg"):
            (self.local_dir / name).write_bytes(SAMPLE_PNG)
        cache = self._make_cache()
        cache.prepare_local_photos()  # family, a, b
        shown = [cache.next_entry().source_url for _ in range(2)]
        (self.local_dir / "late.jpg").write_bytes(SAMPLE_PNG)
        cache.prepare_local_photos()

        rest = [cache.next_entry().source_url for _ in range(2)]

        self.assertEqual(set(shown) & set(rest), set())
        self.assertEqual(len(set(shown + rest)), 4)

    def test_provider_tick_decode_budget_still_progresses(self) -> None:
        # #5 review: a large backlog must not hold up one provider tick.
        import smart_display.cache.image_cache as _module

        for index in range(3):
            (self.local_dir / f"photo-{index}.jpg").write_bytes(SAMPLE_PNG)
        cache = self._make_cache()
        original = _module.LOCAL_PREPARE_TICK_SECONDS
        _module.LOCAL_PREPARE_TICK_SECONDS = 0.0
        try:
            self.assertEqual(len(cache.prepare_local_photos()), 1)
            self.assertEqual(len(cache.prepare_local_photos()), 2)
        finally:
            _module.LOCAL_PREPARE_TICK_SECONDS = original

    def test_oversized_non_jpeg_is_skipped(self) -> None:
        # #5 review: PNG/WebP have no reduced-scale decode.
        from PIL import Image as _Image  # type: ignore

        from smart_display.cache.image_cache import NON_JPEG_MAX_PIXELS

        _Image.new("RGB", (4000, NON_JPEG_MAX_PIXELS // 4000 + 1)).save(
            self.local_dir / "screenshot.png"
        )
        cache = self._make_cache()

        self.assertEqual(
            [entry.source_url for entry in cache.prepare_local_photos()],
            ["local:family.jpg"],
        )

    def test_retired_copies_are_swept_once_not_per_request(self) -> None:
        # #5 review: no directory sweep on every request during the grace.
        from smart_display.cache.image_cache import LOCAL_RETIRE_GRACE_SECONDS

        now = [1000.0]
        cache = self._make_cache(clock=lambda: now[0])
        cache.local_entries()
        (self.local_dir / "family.jpg").unlink()
        cache.local_entries()
        sweeps = {"count": 0}
        original = cache._sweep_local_cache

        def counting() -> None:
            sweeps["count"] += 1
            original()

        cache._sweep_local_cache = counting
        for _ in range(5):
            cache.local_entries()
        self.assertEqual(sweeps["count"], 0)

        now[0] += LOCAL_RETIRE_GRACE_SECONDS + 1
        cache.local_entries()
        cache.local_entries()
        self.assertEqual(sweeps["count"], 1)

    def test_undecodable_local_file_is_skipped(self) -> None:
        (self.local_dir / "broken.jpg").write_bytes(b"not an image")
        cache = self._make_cache()

        self.assertEqual(
            [entry.source_url for entry in cache.prepare_local_photos()],
            ["local:family.jpg"],
        )
        # Remembered, so the next slideshow tick does not decode it again.
        self.assertEqual(len(cache._local_failed), 1)

    def test_out_of_memory_decode_skips_the_photo(self) -> None:
        # #5 review: MemoryError used to escape as a 500 on every tick.
        cache = self._make_cache()

        def exhausted(source, target):
            raise MemoryError()

        cache._write_fitted = exhausted

        self.assertEqual(cache.local_entries(), [])
        self.assertEqual(len(cache._local_failed), 1)
        self.assertIsNone(cache.next_entry())

    def test_unremovable_stale_copy_does_not_break_rotation(self) -> None:
        # #5 review: a root-owned leftover in the copy dir raised out of the scan.
        cache = self._make_cache()
        cache.local_cache_dir.mkdir(parents=True, exist_ok=True)
        (cache.local_cache_dir / "leftover.jpg").write_bytes(SAMPLE_PNG)
        original_unlink = Path.unlink

        def refusing_unlink(self_path, missing_ok=False):
            raise PermissionError("owned by root")

        Path.unlink = refusing_unlink  # type: ignore[assignment]
        try:
            entry = cache.next_entry()
        finally:
            Path.unlink = original_unlink  # type: ignore[assignment]

        self.assertEqual(entry.source_url, "local:family.jpg")

    def test_concurrent_next_entry_never_fails(self) -> None:
        # #5.5: Waitress serves /api/screensaver/next on several threads.
        import threading as _threading

        for index in range(5):
            (self.local_dir / f"photo-{index}.jpg").write_bytes(SAMPLE_PNG)
        cache = self._make_cache()
        cache.prepare_local_photos()
        errors: list[BaseException] = []
        picks: list[str] = []

        def worker() -> None:
            try:
                for _ in range(60):
                    picks.append(cache.next_entry().source_url)
            except BaseException as exc:  # noqa: BLE001 — the test reports it
                errors.append(exc)

        threads = [_threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(picks), 480)

    def test_remote_cache_is_excluded_when_remote_source_is_disabled(self) -> None:
        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            local_dir=self.local_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )
        cache.sync_remote_images(["https://example.com/remote.jpg"], timeout_seconds=5)

        local_only = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            remote_enabled=False,
            local_dir=self.local_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )

        self.assertEqual(local_only.count(), 1)
        self.assertEqual(local_only.available_count(), 1)
        self.assertEqual(local_only.next_entry().source_url, "local:family.jpg")

    def test_shuffle_round_shows_every_photo_without_immediate_repeats(self) -> None:
        (self.local_dir / "second.jpg").write_bytes(SAMPLE_PNG)
        (self.local_dir / "third.jpg").write_bytes(SAMPLE_PNG)
        cache = self._make_cache()
        cache.prepare_local_photos()

        selections = [cache.next_entry().source_url for _ in range(6)]

        self.assertEqual(len(set(selections[:3])), 3)
        self.assertEqual(len(set(selections[3:])), 3)
        self.assertTrue(all(a != b for a, b in zip(selections, selections[1:])))

    def test_local_only_provider_reports_healthy_photo_count(self) -> None:
        config = make_app_config(self.tmp)
        store = make_state_store(self.tmp)
        cache = self._make_cache()

        LightroomSourceProvider(config, store, cache).refresh()

        health = store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["source"], "local")
        self.assertEqual(store.get_state().system.screensaver_photo_count, 1)

    def _album_provider(self, cache: ImageCache, *, album_status: int = 200):
        base = make_app_config(self.tmp)
        config = replace(
            base,
            screensaver=replace(base.screensaver, source_url="https://album.example"),
        )
        store = make_state_store(self.tmp)
        http = FakeHttpClient()
        http.add_response("GET", "https://album.example", status=album_status, body="")
        provider = LightroomSourceProvider(config, store, cache, http_client=http)
        provider._image_urls_from_album_page = lambda html, url: {
            "https://example.com/a.jpg": None
        }
        return provider, store

    def test_empty_album_with_local_photos_is_ok(self) -> None:
        # #5.3: same situation as the local-only branch, same verdict.
        def failing_download(url: str, timeout: int):
            raise RuntimeError("download failed")

        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            local_dir=self.local_dir,
            downloader=failing_download,
        )
        provider, store = self._album_provider(cache)

        provider.refresh()

        health = store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "ok")

    def test_demo_images_do_not_hide_a_fetch_error(self) -> None:
        # #5.4: fresh install, wrong URL, only the bundled demo set.
        demo_dir = self.tmp / "demo"
        demo_dir.mkdir()
        (demo_dir / "demo.jpg").write_bytes(SAMPLE_PNG)
        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            demo_dir=demo_dir,
            downloader=lambda url, timeout: (SAMPLE_PNG, {}),
        )
        provider, store = self._album_provider(cache, album_status=404)

        provider.refresh()

        health = store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "error")
        # The demo set still shows, so it still counts for the slideshow.
        self.assertEqual(store.get_state().system.screensaver_photo_count, 1)

    def test_fetch_error_with_local_photos_is_stale(self) -> None:
        cache = self._make_cache()
        provider, store = self._album_provider(cache, album_status=404)

        provider.refresh()

        health = store.health_payload()["providers"]["screensaver"]
        self.assertEqual(health["status"], "stale")


class FailuresWriteOnChangeTest(unittest.TestCase):
    """``failed.json`` is persisted on every ``sync_remote_images`` call,
    but in the happy path (all URLs already cached, nothing failed) there
    is nothing to persist. Writing the file anyway burns SD-card cycles on
    the Pi Zero for no benefit. The cache must only write when the
    failures dict actually changed during the call."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_cache(self, downloader):
        cache = ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=downloader,
        )
        # Count every failures_cache.save() call so tests can assert
        # nothing touched the file between two no-op syncs.
        self._save_calls = 0
        original_save = cache.failures_cache.save

        def counting_save(data):
            self._save_calls += 1
            return original_save(data)

        cache.failures_cache.save = counting_save  # type: ignore[assignment]
        return cache

    def test_no_write_when_nothing_changed(self) -> None:
        payload = _synthetic_jpeg(100, 100)

        def ok_downloader(url: str, timeout_seconds: int):
            return payload, {}

        cache = self._make_cache(ok_downloader)

        # First sync downloads and produces 1 entry, no failures.
        cache.sync_remote_images(
            ["https://example.com/ok.jpg"], timeout_seconds=5
        )
        saves_after_first = self._save_calls

        # Second sync: same URL, already cached, no new work. Failures
        # dict is unchanged (still empty) — must not touch disk.
        cache.sync_remote_images(
            ["https://example.com/ok.jpg"], timeout_seconds=5
        )
        self.assertEqual(
            self._save_calls,
            saves_after_first,
            "failures file must not be rewritten when nothing changed",
        )

    def test_write_when_new_failure_recorded(self) -> None:
        def bad_downloader(url: str, timeout_seconds: int):
            return b"not an image", {}

        cache = self._make_cache(bad_downloader)
        cache.sync_remote_images(
            ["https://example.com/bad.jpg"], timeout_seconds=5
        )
        self.assertGreaterEqual(self._save_calls, 1)

    def test_write_when_failure_pruned(self) -> None:
        def bad_downloader(url: str, timeout_seconds: int):
            return b"not an image", {}

        cache = self._make_cache(bad_downloader)
        cache.sync_remote_images(
            ["https://example.com/gone.jpg"], timeout_seconds=5
        )
        saves_after_fail = self._save_calls

        # Remove URL from source list → pruning must dirty the cache.
        cache.sync_remote_images([], timeout_seconds=5)
        self.assertGreater(self._save_calls, saves_after_fail)


def _solid_png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image as _Image  # type: ignore

    buffer = io.BytesIO()
    _Image.new("RGB", (4, 4), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _album_api_payload(*assets: dict) -> str:
    return json.dumps(
        {
            "base": "https://photos.adobe.io/v2/spaces/space/",
            "resources": [{"asset": asset} for asset in assets],
        }
    )


def _api_asset(asset_id: str, **payload) -> dict:
    return {
        "id": asset_id,
        "links": {
            "/rels/rendition_type/1280": {
                "href": f"assets/{asset_id}/revisions/rev/renditions/r1280"
            }
        },
        "payload": payload,
    }


class DevelopFingerprintTest(unittest.TestCase):
    """#15: the rendition URL survives an edit, the develop block does not."""

    def _fingerprint(self, **payload) -> str | None:
        images = _extract_lightroom_api_images(
            _album_api_payload(_api_asset("a1", **payload)), "https://api.example/"
        )
        self.assertEqual(len(images), 1)
        return next(iter(images.values()))

    def test_edit_changes_fingerprint_url_stays(self) -> None:
        before = _extract_lightroom_api_images(
            _album_api_payload(_api_asset("a1", develop={"xmpCameraRaw": {"sha256": "1"}})),
            "https://api.example/",
        )
        after = _extract_lightroom_api_images(
            _album_api_payload(_api_asset("a1", develop={"xmpCameraRaw": {"sha256": "2"}})),
            "https://api.example/",
        )
        self.assertEqual(list(before), list(after))
        self.assertNotEqual(list(before.values()), list(after.values()))

    def test_metadata_change_keeps_fingerprint(self) -> None:
        develop = {"croppedWidth": 3456, "xmpCameraRaw": {"sha256": "1"}}
        self.assertEqual(
            self._fingerprint(develop=develop, aesthetics={"score": 86}),
            self._fingerprint(develop=dict(develop), aesthetics={"score": 12}),
        )

    def test_asset_without_develop_gets_empty_fingerprint(self) -> None:
        self.assertEqual(self._fingerprint(), "")

    def test_fingerprint_ignores_key_order(self) -> None:
        self.assertEqual(
            self._fingerprint(develop={"a": 1, "b": 2}),
            self._fingerprint(develop={"b": 2, "a": 1}),
        )


class EditedPhotoSyncTest(unittest.TestCase):
    """#15: an edited photo is downloaded again; until then the old copy
    keeps its slot in the rotation."""

    URL = "https://example.com/a.jpg"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._now = 1_000_000.0
        self.payload = _solid_png((10, 20, 30))
        self.calls: list[str] = []

    def _downloader(self, url: str, timeout_seconds: int):
        self.calls.append(url)
        return self.payload, {}

    def _make_cache(self) -> ImageCache:
        return ImageCache(
            cache_dir=self.tmp / "screensaver",
            manifest_path=self.tmp / "screensaver" / "manifest.json",
            downloader=self._downloader,
            clock=lambda: self._now,
        )

    def _sync(self, cache: ImageCache, version: str | None, **kwargs):
        return cache.sync_remote_images(
            [self.URL], timeout_seconds=5, versions={self.URL: version}, **kwargs
        )

    def test_edited_photo_is_downloaded_under_a_new_name(self) -> None:
        cache = self._make_cache()
        (before,) = self._sync(cache, "v1")
        self.payload = _solid_png((200, 20, 30))
        (after,) = self._sync(cache, "v2")

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(after.source_version, "v2")
        self.assertNotEqual(after.public_path, before.public_path)
        self.assertTrue(Path(after.local_path).exists())
        self.assertFalse(Path(before.local_path).exists())

    def test_unchanged_fingerprint_is_not_downloaded_again(self) -> None:
        cache = self._make_cache()
        self._sync(cache, "v1")
        self._sync(cache, "v1")
        self.assertEqual(len(self.calls), 1)

    def test_old_copy_stays_while_budget_is_spent_on_new_photos(self) -> None:
        cache = self._make_cache()
        (before,) = self._sync(cache, "v1")
        entries = cache.sync_remote_images(
            ["https://example.com/new.jpg", self.URL],
            timeout_seconds=5,
            max_new_downloads=1,
            versions={"https://example.com/new.jpg": "", self.URL: "v2"},
        )
        self.assertEqual(self.calls[-1], "https://example.com/new.jpg")
        self.assertIn(before, entries)
        self.assertTrue(Path(before.local_path).exists())

    def test_identical_rendition_keeps_old_copy_then_gives_up(self) -> None:
        # Lightroom has the edit but has not re-rendered: same bytes back.
        cache = self._make_cache()
        (before,) = self._sync(cache, "v1")
        for attempt in range(1, UNCHANGED_RENDITION_MAX_ATTEMPTS):
            (entry,) = self._sync(cache, "v2")
            self.assertEqual(entry, before)
            self.assertEqual(cache.failed_urls()[self.URL]["attempts"], attempt)
            self._now += _MAX_BACKOFF_SECONDS + 1
        self.assertEqual(sorted(p.name for p in cache.cache_dir.glob("*.jpg")),
                         [Path(before.local_path).name])

        (entry,) = self._sync(cache, "v2")
        self.assertEqual(entry.source_version, "v2")
        # Accepted under its own filename, like any edit — reverting to v1
        # later must not overwrite a path the kiosk may have cached.
        self.assertNotEqual(entry.public_path, before.public_path)
        self.assertFalse(Path(before.local_path).exists())
        self.assertNotIn(self.URL, cache.failed_urls())
        downloads = len(self.calls)
        self._now += _MAX_BACKOFF_SECONDS + 1
        self._sync(cache, "v2")
        self.assertEqual(len(self.calls), downloads)

    def test_download_errors_do_not_count_toward_unchanged_streak(self) -> None:
        cache = self._make_cache()
        (before,) = self._sync(cache, "v1")
        good_payload = self.payload
        self.payload = b"not an image"
        for _ in range(UNCHANGED_RENDITION_MAX_ATTEMPTS):
            self._sync(cache, "v2", respect_backoff=False)
        self.payload = good_payload  # Lightroom answers, still old bytes
        (entry,) = self._sync(cache, "v2", respect_backoff=False)
        self.assertEqual(entry, before)
        self.assertEqual(cache.failed_urls()[self.URL]["unchanged_attempts"], 1)

    def test_unchanged_streak_restarts_for_a_newer_edit(self) -> None:
        cache = self._make_cache()
        (before,) = self._sync(cache, "v1")
        for _ in range(UNCHANGED_RENDITION_MAX_ATTEMPTS - 1):
            self._sync(cache, "v2", respect_backoff=False)
        (entry,) = self._sync(cache, "v3", respect_backoff=False)
        self.assertEqual(entry, before)
        self.assertEqual(cache.failed_urls()[self.URL]["unchanged_attempts"], 1)

    def test_rerendered_photo_replaces_old_copy_after_identical_answer(self) -> None:
        cache = self._make_cache()
        self._sync(cache, "v1")
        self._sync(cache, "v2")  # still the old bytes
        self.payload = _solid_png((200, 20, 30))
        self._now += _INITIAL_BACKOFF_SECONDS + 1
        (entry,) = self._sync(cache, "v2")
        self.assertEqual(entry.source_version, "v2")
        self.assertNotIn(self.URL, cache.failed_urls())

    def test_manual_sync_ignores_backoff(self) -> None:
        cache = self._make_cache()
        self._sync(cache, "v1")
        self._sync(cache, "v2")  # identical → in backoff
        self._sync(cache, "v2")
        self.assertEqual(len(self.calls), 2)
        self._sync(cache, "v2", respect_backoff=False)
        self.assertEqual(len(self.calls), 3)

    def test_entry_from_before_15_adopts_fingerprint_without_download(self) -> None:
        cache = self._make_cache()
        (legacy,) = cache.sync_remote_images([self.URL], timeout_seconds=5)
        self.assertIsNone(legacy.source_version)

        restarted = self._make_cache()
        (entry,) = self._sync(restarted, "v1")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(entry.local_path, legacy.local_path)
        self.assertEqual(entry.source_version, "v1")
        # Persisted: the next edit after a restart is still noticed.
        plan = self._make_cache().plan_remote_sync([self.URL], {self.URL: "v2"})
        self.assertEqual(plan.edited, {self.URL})

    def test_plan_classifies_new_edited_gone(self) -> None:
        cache = self._make_cache()
        cache.sync_remote_images(
            [self.URL, "https://example.com/gone.jpg"],
            timeout_seconds=5,
            versions={self.URL: "v1", "https://example.com/gone.jpg": ""},
        )
        plan = cache.plan_remote_sync(
            [self.URL, "https://example.com/new.jpg"],
            {self.URL: "v2", "https://example.com/new.jpg": ""},
        )
        self.assertEqual(plan.new, {"https://example.com/new.jpg"})
        self.assertEqual(plan.edited, {self.URL})
        self.assertEqual(plan.gone, {"https://example.com/gone.jpg"})
        self.assertEqual(plan.adopt, set())


class AlbumSyncCommandTest(unittest.TestCase):
    """#15: scripts/sync-album.sh runs ``python -m smart_display.album_sync``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        base = make_app_config(self.tmp)
        self.config = replace(
            base,
            screensaver=replace(base.screensaver, source_url="https://album.example"),
        )
        self.calls: list[str] = []
        self.cache = ImageCache(
            cache_dir=self.config.screensaver_cache_dir,
            manifest_path=self.config.screensaver_manifest_path,
            downloader=self._downloader,
        )
        album = {"https://example.com/a.jpg": "", "https://example.com/b.jpg": "v1"}
        for patcher in (
            mock.patch.object(album_sync, "_image_cache", return_value=self.cache),
            mock.patch.object(
                LightroomSourceProvider, "fetch_album_images", return_value=album
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _downloader(self, url: str, timeout_seconds: int):
        self.calls.append(url)
        return SAMPLE_PNG, {}

    def _run(self, *argv: str, backend_running: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(
            album_sync, "_backend_is_running", return_value=backend_running
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = album_sync.main(list(argv), config=self.config)
        return code, out.getvalue()

    def test_refuses_while_backend_runs(self) -> None:
        code, output = self._run(backend_running=True)
        self.assertEqual(code, 2)
        self.assertIn("Backend läuft noch", output)
        self.assertEqual(self.calls, [])

    def test_dry_run_downloads_nothing_even_with_backend_up(self) -> None:
        code, output = self._run("--dry-run", backend_running=True)
        self.assertEqual(code, 0)
        self.assertIn("neu: 2", output)
        self.assertEqual(self.calls, [])

    def test_sync_fetches_whole_album_at_once(self) -> None:
        code, output = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("Fertig: 2 von 2", output)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from urllib import parse as urllib_parse

from smart_display.cache.image_cache import ImageCache
from smart_display.config import AppConfig
from smart_display.http_client import HttpClient, HttpError
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_ALBUM_REDIRECTS = 5
LIGHTROOM_API_KEY = "LightroomMobileWeb1"
ALBUM_API_MAX_BYTES = 2_000_000

# Plan B9: HTML pages from Lightroom public albums are small JSON islands
# inside a static shell — 512 KiB is plenty and protects us from accidental
# multi-MB error pages.
HTML_MAX_BYTES = 512_000


class _ImageCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "img":
            for name in ("src", "data-src", "data-image", "data-fullres"):
                if attributes.get(name):
                    self.urls.append(attributes[name] or "")
        if tag == "source" and attributes.get("srcset"):
            self.urls.extend(item.split(" ")[0] for item in attributes["srcset"].split(","))
        if tag == "meta" and attributes.get("property") in {"og:image", "twitter:image"}:
            content = attributes.get("content")
            if content:
                self.urls.append(content)


def extract_image_urls(html_text: str, base_url: str) -> list[str]:
    parser = _ImageCollector()
    parser.feed(html_text)
    candidates = list(parser.urls)

    escaped_urls = re.findall(
        r"https?:\\\\?/\\\\?/[^\"'\\s]+?\.(?:jpg|jpeg|png|webp)",
        html_text,
        re.IGNORECASE,
    )
    candidates.extend(url.replace("\\/", "/") for url in escaped_urls)

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = html.unescape(candidate).strip()
        if not candidate:
            continue
        absolute_url = urllib_parse.urljoin(base_url, candidate)
        lowered = absolute_url.lower()
        if not lowered.startswith(("http://", "https://")):
            continue
        if not _looks_like_image_url(lowered):
            continue
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        normalized.append(absolute_url)
    return normalized


def _looks_like_image_url(lowered_url: str) -> bool:
    parsed = urllib_parse.urlsplit(lowered_url)
    if parsed.path.endswith(IMAGE_EXTENSIONS):
        return True
    return parsed.netloc == "photos.adobe.io" and "/renditions/" in parsed.path


class LightroomSourceProvider(BaseProvider):
    section_name = "screensaver"
    source_name = "lightroom"

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        image_cache: ImageCache,
        http_client: HttpClient | None = None,
    ):
        super().__init__(config.refresh_intervals.lightroom_seconds)
        self.config = config
        self.state_store = state_store
        self.image_cache = image_cache
        self._http = http_client or HttpClient()

    def refresh(self) -> None:
        def available_photo_count() -> int:
            return self.image_cache.available_count(
                include_demo=self.config.screensaver.demo_images_enabled
            )

        if not self.config.screensaver.enabled:
            self.state_store.set_provider_snapshot(
                self.section_name,
                self.snapshot(status="empty", error_message="Screensaver deaktiviert."),
            )
            self.state_store.set_screensaver_photo_count(available_photo_count())
            return

        # Decode new local photos here, on the scheduler thread, so a request
        # thread never has to work through a backlog (#5).
        local_photo_count = len(self.image_cache.prepare_local_photos())

        if not self.config.screensaver.source_url:
            self.state_store.set_provider_snapshot(
                self.section_name,
                self.snapshot(status="ok", source="local")
                if local_photo_count
                else self.snapshot(
                    status="empty", error_message="Keine Bildquelle konfiguriert."
                ),
            )
            self.state_store.set_screensaver_photo_count(available_photo_count())
            return

        try:
            images = self.fetch_album_images()
            entries = self.image_cache.sync_remote_images(
                list(images),
                timeout_seconds=self.config.screensaver.timeout_seconds,
                max_new_downloads=max(self.config.screensaver.images_per_tick, 1),
                versions=images,
            )
            # Local photos rotate alongside the album, so an empty album with
            # local photos is as healthy as the local-only branch above.
            status = "ok" if entries or local_photo_count else "empty"
            self.state_store.set_provider_snapshot(self.section_name, self.snapshot(status=status))
            self.state_store.set_screensaver_photo_count(available_photo_count())
        except (HttpError, RuntimeError) as exc:
            self.logger.warning("screensaver refresh failed: %s", exc)
            # Bundled demo images are a placeholder, not data this provider
            # ever fetched, so they must not turn a fresh install's error into
            # "stale".
            real_photo_count = self.image_cache.available_count(include_demo=False)
            fallback_status = "stale" if real_photo_count > 0 else "error"
            self.state_store.set_provider_snapshot(
                self.section_name,
                self.snapshot(status=fallback_status, error_message=str(exc)),
            )
            self.state_store.set_screensaver_photo_count(available_photo_count())

    def fetch_album_images(self) -> dict[str, str | None]:
        """The album as ``{rendition URL: edit fingerprint}``, in album order.

        Raises ``HttpError``/``RuntimeError`` rather than returning an empty
        album, so a failed fetch never reconciles the cache down to nothing.
        """
        response = self._get_album_page()
        if not response.ok:
            raise HttpError(
                f"Lightroom antwortete mit {response.status}"
            )
        images = self._image_urls_from_album_page(response.text(), response.url)
        if not images:
            raise RuntimeError("Keine Bild-URLs im Lightroom-Album gefunden.")
        return images

    def _get_album_page(self):
        url = self.config.screensaver.source_url
        for _ in range(MAX_ALBUM_REDIRECTS + 1):
            response = self._http.get(
                url,
                timeout=self.config.screensaver.timeout_seconds,
                max_bytes=HTML_MAX_BYTES,
            )
            if response.status not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = urllib_parse.urljoin(url, location)
            if not next_url.lower().startswith(("http://", "https://")):
                raise HttpError("Lightroom-Weiterleitung ist keine HTTP-URL.")
            url = next_url
        raise HttpError("Lightroom-Weiterleitungslimit erreicht.")

    def _image_urls_from_album_page(
        self, html_text: str, page_url: str
    ) -> dict[str, str | None]:
        album_api_url = _extract_lightroom_album_api_url(html_text, page_url)
        if not album_api_url:
            # A plain page knows nothing about edits.
            return dict.fromkeys(extract_image_urls(html_text, page_url))

        # Once the page names an album API, only its answer is the album. The
        # HTML adds just the cover (og:image) — one of the album's assets
        # again, as a different rendition — so it is no fallback: a failed or
        # empty answer would reconcile the cache down to that one photo and
        # delete the rest. Raising leaves the cache alone and the tile stale.
        response = self._http.get(
            album_api_url,
            timeout=self.config.screensaver.timeout_seconds,
            max_bytes=ALBUM_API_MAX_BYTES,
        )
        if not response.ok:
            raise HttpError(f"Lightroom album API antwortete mit {response.status}")

        api_images = _extract_lightroom_api_images(response.text(), response.url)
        if not api_images:
            raise RuntimeError("Lightroom album API lieferte keine Bilder.")
        return api_images


def _extract_lightroom_album_api_url(html_text: str, page_url: str) -> str | None:
    album = _extract_json_object_after_marker(html_text, "albumAttributes:")
    space = _extract_json_object_after_marker(html_text, "spaceAttributes:")
    if not isinstance(album, dict):
        return None
    links = album.get("links")
    if not isinstance(links, dict):
        return None
    relation = links.get("/rels/space_album_images_videos")
    if not isinstance(relation, dict):
        return None
    href = relation.get("href")
    if not isinstance(href, str) or not href:
        return None
    base = album.get("base")
    if not isinstance(base, str) and isinstance(space, dict):
        base = space.get("base")
    if not isinstance(base, str) or not base:
        base = page_url
    return urllib_parse.urljoin(base, href)


def _extract_json_object_after_marker(html_text: str, marker: str) -> dict | None:
    marker_index = html_text.find(marker)
    if marker_index < 0:
        return None
    start = html_text.find("{", marker_index)
    if start < 0:
        return None

    in_string = False
    escaped = False
    depth = 0
    for index in range(start, len(html_text)):
        char = html_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(html_text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _extract_lightroom_api_images(
    payload_text: str, api_url: str
) -> dict[str, str | None]:
    payload = _parse_guarded_json(payload_text)
    if not isinstance(payload, dict):
        return {}
    base = payload.get("base")
    if not isinstance(base, str) or not base:
        base = api_url

    images: dict[str, str | None] = {}
    resources = payload.get("resources")
    if not isinstance(resources, list):
        return {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        asset = resource.get("asset")
        if not isinstance(asset, dict):
            continue
        links = asset.get("links")
        if not isinstance(links, dict):
            continue
        href = _best_rendition_href(links)
        if href:
            url = _with_api_key(urllib_parse.urljoin(base, href))
            images.setdefault(url, _develop_fingerprint(asset))
    return images


def _develop_fingerprint(asset: dict) -> str:
    """Fingerprint of an asset's edit state (#15).

    The rendition URL does not change when a photo is edited — its
    ``revisions/<id>`` is the original's revision. ``payload.develop``
    (crop, orientation, ``xmpCameraRaw.sha256``, masks) does, while
    metadata and the aesthetics score sit outside it. An asset without a
    develop block gets ``""``, so its first edit still counts as a change.
    """
    payload = asset.get("payload")
    develop = payload.get("develop") if isinstance(payload, dict) else None
    if not isinstance(develop, dict):
        return ""
    canonical = json.dumps(develop, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _parse_guarded_json(payload_text: str):
    trimmed = payload_text.lstrip()
    if trimmed.startswith("while (1) {}"):
        trimmed = trimmed[len("while (1) {}") :].lstrip()
    start = trimmed.find("{")
    if start < 0:
        return None
    try:
        return json.loads(trimmed[start:])
    except json.JSONDecodeError:
        return None


def _best_rendition_href(links: dict) -> str | None:
    for relation in (
        "/rels/rendition_type/1280",
        "/rels/rendition_type/2048",
        "/rels/rendition_type/640",
        "/rels/rendition_type/thumbnail2x",
    ):
        value = links.get(relation)
        if isinstance(value, dict) and isinstance(value.get("href"), str):
            return value["href"]
    return None


def _with_api_key(url: str) -> str:
    parsed = urllib_parse.urlsplit(url)
    query = urllib_parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "api_key" for key, _value in query):
        return url
    query.append(("api_key", LIGHTROOM_API_KEY))
    return urllib_parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib_parse.urlencode(query),
            parsed.fragment,
        )
    )

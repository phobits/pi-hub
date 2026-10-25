from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from smart_display.config import AppConfig
from smart_display.http_client import HttpClient, HttpError, HttpResponse
from smart_display.models import ProviderSnapshot, SpotifyState
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore

# Sentinel so ``_api_request`` can tell "caller passed no device_id, fall back
# to the configured default" apart from "caller explicitly wants no device_id
# in the query" (transfer step carries the device in the body instead).
_UNSET: Any = object()

# Device/playlist lists are fetched on-demand when the picker overlay opens, not
# on the poll loop. A short TTL dedupes rapid open/close taps without hiding a
# speaker the user just woke up; playlists change rarely, so they cache longer.
_DEVICES_CACHE_TTL_SECONDS = 15
_PLAYLISTS_CACHE_TTL_SECONDS = 600
_QUEUE_CACHE_TTL_SECONDS = 15
# Upper bound for serving a TTL-expired queue as last-known-good while Spotify
# errors. Past this the honest "not reachable" message beats a plausible lie.
_QUEUE_STALE_MAX_AGE_SECONDS = 300
_QUEUE_ITEM_LIMIT = 4

# Canned data so the picker overlay can be exercised at 1024x600 on the demo
# server (APP_DEMO_MODE / local-demo.yaml) without a real Spotify account.
_DEMO_DEVICES: list[dict[str, Any]] = [
    {"id": "demo-living", "name": "Wohnzimmer", "type": "Speaker", "is_active": True, "is_restricted": False, "volume_percent": 42},
    {"id": "demo-kitchen", "name": "Küche", "type": "Speaker", "is_active": False, "is_restricted": False, "volume_percent": 30},
    {"id": "demo-office", "name": "Arbeitszimmer", "type": "Speaker", "is_active": False, "is_restricted": False, "volume_percent": 18},
]
_DEMO_PLAYLISTS: list[dict[str, Any]] = [
    {"uri": "spotify:playlist:demo1", "name": "Sonntagskaffee", "image_url": None, "track_count": 48},
    {"uri": "spotify:playlist:demo2", "name": "Fokus", "image_url": None, "track_count": 120},
    {"uri": "spotify:playlist:demo3", "name": "Abendessen", "image_url": None, "track_count": 64},
    {"uri": "spotify:playlist:demo4", "name": "Laufrunde", "image_url": None, "track_count": 33},
    {"uri": "spotify:playlist:demo5", "name": "Kinderlieder", "image_url": None, "track_count": 25},
    {"uri": "spotify:playlist:demo6", "name": "Jazz Klassiker", "image_url": None, "track_count": 90},
]
_DEMO_QUEUE: list[dict[str, Any]] = [
    {"title": "Idioteque", "subtitle": "Radiohead", "image_url": None, "kind": "track", "duration_ms": 309_000},
    {"title": "Morning Bell", "subtitle": "Radiohead", "image_url": None, "kind": "track", "duration_ms": 275_000},
    {"title": "Motion Picture Soundtrack", "subtitle": "Radiohead", "image_url": None, "kind": "track", "duration_ms": 421_000},
    {"title": "Untitled", "subtitle": "Radiohead", "image_url": None, "kind": "track", "duration_ms": 58_000},
]


def _smallest_image_url(images: Any) -> str | None:
    """Pick the lightest cover URL — saves bandwidth/decode on the Pi.

    Spotify image arrays are usually largest-first; playlist mosaics often carry
    null widths, so fall back to the last (typically smallest) entry.
    """
    if not isinstance(images, list):
        return None
    usable = [im for im in images if isinstance(im, dict) and im.get("url")]
    if not usable:
        return None
    sized = [im for im in usable if isinstance(im.get("width"), int)]
    if sized:
        return str(min(sized, key=lambda im: im["width"]).get("url"))
    return str(usable[-1].get("url"))


def map_spotify_devices(raw: Any) -> list[dict[str, Any]]:
    """Flatten ``/me/player/devices`` into the small shape the picker consumes."""
    devices: list[dict[str, Any]] = []
    for device in raw or []:
        if not isinstance(device, dict):
            continue
        devices.append(
            {
                "id": device.get("id"),
                "name": str(device.get("name") or "Unbenanntes Gerät"),
                "type": str(device.get("type") or ""),
                "is_active": bool(device.get("is_active", False)),
                "is_restricted": bool(device.get("is_restricted", False)),
                "volume_percent": device.get("volume_percent"),
            }
        )
    devices.sort(key=lambda item: item["name"].lower())
    return devices


def map_spotify_playlists(raw: Any) -> list[dict[str, Any]]:
    """Flatten ``/me/playlists`` items into ``{uri, name, image_url, track_count}``."""
    playlists: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not uri:
            continue
        tracks = item.get("tracks") or {}
        playlists.append(
            {
                "uri": str(uri),
                "name": str(item.get("name") or "Playlist"),
                "image_url": _smallest_image_url(item.get("images")),
                "track_count": tracks.get("total") if isinstance(tracks, dict) else None,
            }
        )
    return playlists


def map_spotify_queue(raw: Any) -> list[dict[str, Any]]:
    """Flatten tracks and episodes into the four rows used by the kiosk UI."""
    items: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "other").lower()
        title = str(item.get("name") or "Unbekannter Inhalt")
        image_url: str | None = None

        if kind == "track":
            artists = item.get("artists") or []
            subtitle = ", ".join(
                str(artist.get("name"))
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            )
            album = item.get("album") or {}
            if isinstance(album, dict):
                image_url = _smallest_image_url(album.get("images"))
                if not subtitle:
                    subtitle = str(album.get("name") or "Titel")
        elif kind == "episode":
            show = item.get("show") or {}
            subtitle = (
                str(show.get("name") or show.get("publisher") or "Podcast")
                if isinstance(show, dict)
                else "Podcast"
            )
            image_url = _smallest_image_url(item.get("images"))
            if image_url is None and isinstance(show, dict):
                image_url = _smallest_image_url(show.get("images"))
        else:
            subtitle = "Spotify"
            image_url = _smallest_image_url(item.get("images"))

        duration = item.get("duration_ms")
        items.append(
            {
                "title": title,
                "subtitle": subtitle,
                "image_url": image_url,
                "kind": kind,
                # Shown as m:ss at the end of the queue row; None hides it.
                "duration_ms": int(duration) if isinstance(duration, (int, float)) else None,
            }
        )
        if len(items) >= _QUEUE_ITEM_LIMIT:
            break
    return items


def build_spotify_state_from_payload(
    payload: dict,
    snapshot: ProviderSnapshot,
) -> SpotifyState:
    device = payload.get("device") or {}
    item = payload.get("item") or {}
    kind = str(item.get("type") or "track").lower()
    artists = item.get("artists") or []
    album = item.get("album") or {}
    show = item.get("show") or {}
    if kind == "episode":
        images = item.get("images") or (show.get("images") if isinstance(show, dict) else []) or []
        artist_name = (
            str(show.get("name") or show.get("publisher") or "Podcast")
            if isinstance(show, dict)
            else "Podcast"
        )
        album_name = str(show.get("name") or "Podcast") if isinstance(show, dict) else "Podcast"
    else:
        images = album.get("images") if isinstance(album, dict) else []
        images = images or []
        artist_name = ", ".join(
            str(artist.get("name", ""))
            for artist in artists
            if isinstance(artist, dict) and artist.get("name")
        )
        album_name = str(album.get("name", "")) if isinstance(album, dict) else ""
    album_art_url = (
        images[1].get("url")
        if len(images) > 1 and isinstance(images[1], dict)
        else images[0].get("url")
        if images and isinstance(images[0], dict)
        else None
    )
    device_type = device.get("type")
    can_control = bool(device) and not bool(device.get("is_restricted", False))
    supports_volume = bool(device) and not bool(device.get("is_restricted", False))
    if device.get("supports_volume") is not None:
        supports_volume = bool(device.get("supports_volume"))
    # Controlling the phone's own output volume from the display is awkward and
    # rarely wanted — when playback runs on a smartphone, keep transport active
    # but mark the volume as uncontrollable so the slider goes inactive.
    if isinstance(device_type, str) and device_type.strip().lower() == "smartphone":
        supports_volume = False

    return SpotifyState(
        snapshot=snapshot,
        connected=True,
        is_playing=bool(payload.get("is_playing", False)),
        track_title=str(item.get("name", "")),
        artist_name=artist_name,
        album_name=album_name,
        album_art_url=album_art_url,
        device_name=device.get("name"),
        device_type=device_type,
        volume_percent=device.get("volume_percent"),
        supports_volume=supports_volume,
        can_control=can_control,
        progress_ms=payload.get("progress_ms"),
        duration_ms=item.get("duration_ms"),
        empty_message="Derzeit keine Wiedergabe.",
    )


# Plan C3: Spotify API failures get a short German message so the frontend
# toast surface (Plan A4) can show something honest instead of "Spotify
# antwortete mit 429". Anything we don't recognise falls back to a generic
# "not reachable" string.
_SPOTIFY_STATUS_MESSAGES: dict[int, str] = {
    401: "Spotify-Anmeldung abgelaufen.",
    403: "Spotify-Aktion nicht erlaubt.",
    404: "Kein aktives Spotify-Gerät.",
    429: "Spotify-Limit erreicht. Kurz warten.",
    500: "Spotify nicht erreichbar.",
    502: "Spotify nicht erreichbar.",
    503: "Spotify nicht erreichbar.",
    504: "Spotify nicht erreichbar.",
}
_SPOTIFY_DEFAULT_ERROR = "Spotify nicht erreichbar."


class SpotifyAuthRejected(RuntimeError):
    """The token endpoint refused the refresh token or the client — revoked,
    expired or for another app. Retrying does not help, so the boot grace is
    skipped (#19). Decided on the OAuth error code, not the status: a 400
    from a proxy or captive portal stays an ordinary, transient failure."""


# RFC 6749 §5.2 codes no retry heals; anything else from the token endpoint
# (5xx, a 400 without an OAuth body) is treated as transient.
_OAUTH_PERMANENT_ERRORS = frozenset({"invalid_grant", "invalid_client"})


def _oauth_error_code(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    return str(payload.get("error", "")) if isinstance(payload, dict) else ""


def spotify_status_message(status: int) -> str:
    """Return the German-localised error message for a Spotify HTTP status."""
    return _SPOTIFY_STATUS_MESSAGES.get(status, _SPOTIFY_DEFAULT_ERROR)


class SpotifyProvider(BaseProvider):
    section_name = "spotify"
    source_name = "spotify"

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        http_client: HttpClient | None = None,
    ):
        super().__init__(config.refresh_intervals.spotify_seconds)
        self.config = config
        self.state_store = state_store
        self._http = http_client or HttpClient()
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expiry = datetime.now(timezone.utc)
        self._list_cache_lock = threading.Lock()
        self._list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ---- scheduler entrypoint --------------------------------------------

    def refresh(self) -> None:
        if self.config.app.demo_mode and not self.config.spotify.enabled:
            return

        if not self._is_configured():
            self.state_store.update_section(
                self.section_name,
                SpotifyState(
                    snapshot=self.snapshot(status="disabled"),
                    empty_message="Spotify nicht verbunden.",
                ),
            )
            return

        try:
            # Without `additional_types` Spotify keeps legacy-client behaviour and
            # returns `item: null` for anything that isn't a track — a playing
            # podcast would render as "Keine aktive Wiedergabe". Opt in explicitly
            # so the episode branch in build_spotify_state_from_payload is reachable.
            response = self._api_request(
                "GET", "/me/player?additional_types=episode"
            )
        except Exception as exc:
            self.logger.exception("spotify refresh failed")
            rejected = isinstance(exc, SpotifyAuthRejected)
            self.state_store.mark_error(
                self.section_name,
                error_message=spotify_status_message(401) if rejected else str(exc),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
                transient=not rejected,
            )
            return

        # Nothing playing: 204 (or 202/404 from a player without a device), or
        # a 2xx without a body. Any other status without a body — 3xx included,
        # the HTTP client follows no redirects — is an error (#19); it used to
        # read as "Keine aktive Wiedergabe", i.e. silent.
        if response.status in {202, 204, 404} or (
            response.ok and not response.body
        ):
            self.state_store.update_section(
                self.section_name,
                SpotifyState(
                    snapshot=self.snapshot(status="ok"),
                    connected=True,
                    empty_message="Keine aktive Wiedergabe.",
                ),
            )
            return

        if response.status == 401:
            with self._token_lock:
                self._access_token = None
            self.state_store.mark_error(
                self.section_name,
                error_message=spotify_status_message(401),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return

        if not response.ok:
            self.state_store.mark_error(
                self.section_name,
                error_message=spotify_status_message(response.status),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"player payload is {type(payload).__name__}")
        except Exception as exc:
            self.logger.exception("spotify payload parse failed")
            self.state_store.mark_error(
                self.section_name,
                error_message=str(exc),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return

        self.state_store.update_section(
            self.section_name,
            build_spotify_state_from_payload(payload, self.snapshot(status="ok")),
        )

    # ---- control commands -------------------------------------------------

    def toggle_playback(self) -> dict[str, Any]:
        state = self.state_store.get_state().spotify
        endpoint = "/me/player/pause" if state.is_playing else "/me/player/play"
        return self._send_command("PUT", endpoint)

    def next_track(self) -> dict[str, Any]:
        result = self._send_command("POST", "/me/player/next")
        if result.get("ok"):
            self._invalidate_list_cache("queue")
        return result

    def previous_track(self) -> dict[str, Any]:
        result = self._send_command("POST", "/me/player/previous")
        if result.get("ok"):
            self._invalidate_list_cache("queue")
        return result

    def set_volume(self, volume_percent: int) -> dict[str, Any]:
        value = max(0, min(int(volume_percent), 100))
        return self._send_command(
            "PUT",
            f"/me/player/volume?{urllib.parse.urlencode({'volume_percent': value})}",
        )

    def seek_to(self, position_ms: int) -> dict[str, Any]:
        """Seek within the current item without allowing accidental track skips."""
        if type(position_ms) is not int:
            return {
                "ok": False,
                "message": "Wiedergabeposition ist ungültig.",
                "state": None,
            }
        value = position_ms

        current = self.state_store.get_state().spotify
        duration = current.duration_ms
        if value < 0 or duration is None or duration <= 0 or value > duration:
            return {
                "ok": False,
                "message": "Wiedergabeposition ist ungültig.",
                "state": None,
            }

        if self.config.app.demo_mode and not self.config.spotify.enabled:
            updated = replace(current, progress_ms=value)
            self.state_store.update_section(self.section_name, updated)
            return {"ok": True, "message": "ok", "state": updated.to_dict()}

        query = urllib.parse.urlencode({"position_ms": value})
        return self._send_command("PUT", f"/me/player/seek?{query}")

    def start_playback(
        self, device_id: str, context_uri: str | None = None
    ) -> dict[str, Any]:
        """Start (or transfer) playback on a chosen Spotify Connect device.

        Two-step, mirroring spotipi: transfer first to wake/select the target
        device, then issue ``play``. The transfer is best-effort — a sleeping
        speaker can reject it, but the subsequent ``play?device_id=`` still
        activates it. Without a ``context_uri`` this simply moves the current
        session to ``device_id`` (the "Hierher übertragen" affordance).
        """
        if self.config.app.demo_mode and not self.config.spotify.enabled:
            return {
                "ok": True,
                "message": "Demo: Wiedergabe würde starten.",
                "state": self.state_store.get_state().spotify.to_dict(),
            }
        if not self._is_configured():
            return {
                "ok": False,
                "message": "Spotify ist nicht konfiguriert.",
                "state": None,
            }
        device_id = (device_id or "").strip()
        if not device_id:
            return {"ok": False, "message": "Kein Gerät ausgewählt.", "state": None}

        # Step 1 — transfer/wake. Device lives in the body here, so suppress the
        # query device_id. Failures are logged but never abort the play call.
        try:
            self._api_request(
                "PUT",
                "/me/player",
                body={"device_ids": [device_id], "play": False},
                device_id=None,
            )
        except Exception as exc:  # noqa: BLE001 — transfer is best-effort
            self.logger.info("spotify transfer step failed (continuing): %s", exc)

        body: dict[str, Any] | None = None
        context_uri = (context_uri or "").strip()
        if context_uri:
            if context_uri.startswith("spotify:track:"):
                body = {"uris": [context_uri]}
            else:
                body = {"context_uri": context_uri}

        try:
            response = self._api_request(
                "PUT", "/me/player/play", body=body, device_id=device_id
            )
        except HttpError:
            return {"ok": False, "message": _SPOTIFY_DEFAULT_ERROR, "state": None}
        except Exception:  # noqa: BLE001 — token refresh errors bubble here
            return {"ok": False, "message": _SPOTIFY_DEFAULT_ERROR, "state": None}

        # The device list is now stale (active device changed) — drop it so the
        # next picker open reflects reality.
        self._invalidate_list_cache("devices")
        result = self._handle_command_response(response)
        if result.get("ok"):
            self._invalidate_list_cache("queue")
        return result

    def list_devices(self) -> dict[str, Any]:
        """Available Connect devices for the picker. On-demand, lightly cached."""
        if self.config.app.demo_mode and not self.config.spotify.enabled:
            return {"ok": True, "message": "ok", "devices": list(_DEMO_DEVICES)}
        if not self._is_configured():
            return {
                "ok": False,
                "message": "Spotify ist nicht konfiguriert.",
                "devices": [],
            }
        cached = self._list_cache_get("devices", _DEVICES_CACHE_TTL_SECONDS)
        if cached is not None:
            return {"ok": True, "message": "ok", "devices": cached}
        payload, error = self._list_get("/me/player/devices")
        if error is not None:
            return {"ok": False, "message": error, "devices": []}
        devices = map_spotify_devices(payload.get("devices"))
        self._list_cache_set("devices", devices)
        return {"ok": True, "message": "ok", "devices": devices}

    def list_playlists(self) -> dict[str, Any]:
        """User's own playlists for the picker. On-demand, cached for minutes."""
        if self.config.app.demo_mode and not self.config.spotify.enabled:
            return {"ok": True, "message": "ok", "playlists": list(_DEMO_PLAYLISTS)}
        if not self._is_configured():
            return {
                "ok": False,
                "message": "Spotify ist nicht konfiguriert.",
                "playlists": [],
            }
        cached = self._list_cache_get("playlists", _PLAYLISTS_CACHE_TTL_SECONDS)
        if cached is not None:
            return {"ok": True, "message": "ok", "playlists": cached}
        limit = max(1, min(int(self.config.spotify.playlists_limit), 50))
        payload, error = self._list_get(f"/me/playlists?limit={limit}")
        if error is not None:
            return {"ok": False, "message": error, "playlists": []}
        playlists = map_spotify_playlists(payload.get("items"))
        self._list_cache_set("playlists", playlists)
        return {"ok": True, "message": "ok", "playlists": playlists}

    def list_queue(self) -> dict[str, Any]:
        """Return the short upcoming queue, keeping the last good result on errors."""
        if self.config.app.demo_mode and not self.config.spotify.enabled:
            return {
                "ok": True,
                "status": "ok",
                "message": "ok",
                "items": list(_DEMO_QUEUE),
            }
        if not self._is_configured():
            return {
                "ok": False,
                "status": "error",
                "message": "Spotify ist nicht konfiguriert.",
                "items": [],
            }

        cached = self._list_cache_get("queue", _QUEUE_CACHE_TTL_SECONDS)
        if cached is not None:
            return self._queue_result(cached)

        payload, error = self._list_get("/me/player/queue")
        if error is not None:
            stale = self._list_cache_peek("queue", _QUEUE_STALE_MAX_AGE_SECONDS)
            if stale is None:
                return {"ok": False, "status": "error", "message": error, "items": []}
            if not stale:
                # A cached empty queue is a real answer ("Keine weiteren Titel."),
                # not degraded data — don't dress it up as a cache hit.
                return self._queue_result(stale)
            return {
                "ok": True,
                "status": "stale",
                "message": "Warteschlange zuletzt erfolgreich geladen.",
                "items": stale,
            }

        items = map_spotify_queue(payload.get("queue"))
        self._list_cache_set("queue", items)
        return self._queue_result(items)

    @staticmethod
    def _queue_result(items: list[dict[str, Any]]) -> dict[str, Any]:
        if items:
            return {"ok": True, "status": "ok", "message": "ok", "items": items}
        return {
            "ok": True,
            "status": "empty",
            "message": "Keine weiteren Titel.",
            "items": [],
        }

    def _send_command(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        device_id: Any = _UNSET,
    ) -> dict[str, Any]:
        if not self._is_configured():
            return {
                "ok": False,
                "message": "Spotify ist nicht konfiguriert.",
                "state": None,
            }

        try:
            response = self._api_request(method, path, body=body, device_id=device_id)
        except HttpError:
            return {"ok": False, "message": _SPOTIFY_DEFAULT_ERROR, "state": None}
        except Exception:  # noqa: BLE001 — token refresh errors bubble here
            return {"ok": False, "message": _SPOTIFY_DEFAULT_ERROR, "state": None}

        return self._handle_command_response(response)

    def _handle_command_response(self, response: HttpResponse) -> dict[str, Any]:
        """Map a control-command response to the ``{ok, message, state}`` shape.

        On success it pulls fresh truth inline so the UI never waits on the next
        poll tick to reflect play/pause/volume/device changes.
        """
        if response.status == 401:
            with self._token_lock:
                self._access_token = None
            return {
                "ok": False,
                "message": spotify_status_message(401),
                "state": None,
            }

        if response.status not in {200, 202, 204}:
            return {
                "ok": False,
                "message": spotify_status_message(response.status),
                "state": None,
            }

        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 — never let inline refresh break the command
            self.logger.warning("spotify inline refresh failed: %s", exc)

        return {
            "ok": True,
            "message": "ok",
            "state": self.state_store.get_state().spotify.to_dict(),
        }

    # ---- list fetch / cache helpers --------------------------------------

    def _list_get(self, path: str) -> tuple[dict[str, Any], None] | tuple[None, str]:
        """GET a JSON list endpoint. Returns ``(payload, None)`` or ``(None, message)``."""
        try:
            response = self._api_request("GET", path)
        except Exception:  # noqa: BLE001 — network/token errors → localized message
            return None, _SPOTIFY_DEFAULT_ERROR
        if response.status == 401:
            with self._token_lock:
                self._access_token = None
            return None, spotify_status_message(401)
        if not response.ok:
            return None, spotify_status_message(response.status)
        try:
            payload = response.json() or {}
        except Exception:  # noqa: BLE001
            return None, _SPOTIFY_DEFAULT_ERROR
        if not isinstance(payload, dict):
            return None, _SPOTIFY_DEFAULT_ERROR
        return payload, None

    def _list_cache_get(self, key: str, ttl_seconds: int) -> list[dict[str, Any]] | None:
        with self._list_cache_lock:
            entry = self._list_cache.get(key)
            if entry is None:
                return None
            stored_at, data = entry
            if time.monotonic() - stored_at > ttl_seconds:
                return None
            return data

    def _list_cache_set(self, key: str, data: list[dict[str, Any]]) -> None:
        with self._list_cache_lock:
            self._list_cache[key] = (time.monotonic(), data)

    def _list_cache_peek(
        self, key: str, max_age_seconds: float
    ) -> list[dict[str, Any]] | None:
        """Return TTL-expired cached data for last-known-good fallbacks.

        Bounded on purpose: serving an unbounded-age queue as ``ok`` would let a
        long Spotify outage show tracks that finished playing hours ago.
        """
        with self._list_cache_lock:
            entry = self._list_cache.get(key)
            if entry is None:
                return None
            stored_at, data = entry
            if time.monotonic() - stored_at > max_age_seconds:
                return None
            return data

    def _invalidate_list_cache(self, key: str) -> None:
        with self._list_cache_lock:
            self._list_cache.pop(key, None)

    # ---- HTTP / auth internals -------------------------------------------

    def _is_configured(self) -> bool:
        return self.config.spotify.enabled and all(
            [
                self.config.spotify.client_id,
                self.config.spotify.client_secret,
                self.config.spotify.refresh_token,
            ]
        )

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        device_id: Any = _UNSET,
    ) -> HttpResponse:
        token = self._access_token_value()
        url = f"https://api.spotify.com/v1{path}"
        if method in {"PUT", "POST"}:
            # ``_UNSET`` → fall back to the configured default device; an explicit
            # value (including None/"") lets callers target a device or suppress
            # the query entirely (e.g. the transfer step carries it in the body).
            effective_device = (
                self.config.spotify.device_id if device_id is _UNSET else device_id
            )
            if effective_device:
                query = urllib.parse.urlencode({"device_id": effective_device})
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{query}"

        if body is not None:
            request_body: bytes | None = json.dumps(body).encode("utf-8")
        elif method in {"POST", "PUT"}:
            request_body = b""
        else:
            request_body = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        return self._http.request(
            method,
            url,
            body=request_body,
            headers=headers,
            timeout=self.config.spotify.timeout_seconds,
        )

    def _access_token_value(self) -> str:
        with self._token_lock:
            if self._access_token and datetime.now(timezone.utc) < self._token_expiry:
                return self._access_token

            credentials = (
                f"{self.config.spotify.client_id}:{self.config.spotify.client_secret}"
            )
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            response = self._http.post_form(
                "https://accounts.spotify.com/api/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.config.spotify.refresh_token,
                },
                headers={"Authorization": f"Basic {encoded}"},
                timeout=self.config.spotify.timeout_seconds,
            )
            if not response.ok:
                error = (
                    SpotifyAuthRejected
                    if _oauth_error_code(response) in _OAUTH_PERMANENT_ERRORS
                    else RuntimeError
                )
                raise error(
                    response.text() or "Spotify token refresh fehlgeschlagen."
                )
            payload = response.json()
            self._access_token = str(payload["access_token"])
            expires_in = int(payload.get("expires_in", 3600))
            self._token_expiry = datetime.now(timezone.utc) + timedelta(
                seconds=max(expires_in - 60, 60)
            )
            return self._access_token

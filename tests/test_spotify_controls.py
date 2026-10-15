from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smart_display.http_client import HttpError
from smart_display.models import ProviderSnapshot, SpotifyState
from smart_display.providers.spotify_provider import (
    SpotifyProvider,
    build_spotify_state_from_payload,
    map_spotify_devices,
    map_spotify_playlists,
    map_spotify_queue,
    spotify_status_message,
)
from smart_display.providers.spotify_provider import (
    _QUEUE_CACHE_TTL_SECONDS,
    _QUEUE_STALE_MAX_AGE_SECONDS,
)
from tests._support import FakeHttpClient, make_app_config, make_state_store


def _json_body(call) -> dict:
    import json

    return json.loads((call.body or b"").decode("utf-8"))


# refresh() opts into episodes so podcasts are not reported as `item: null`.
PLAYER_STATE_URL = "https://api.spotify.com/v1/me/player?additional_types=episode"

PLAYING_PAYLOAD = {
    "is_playing": True,
    "device": {
        "name": "Wohnzimmer",
        "type": "Speaker",
        "is_restricted": False,
        "volume_percent": 42,
        "supports_volume": True,
    },
    "item": {
        "name": "Everything In Its Right Place",
        "duration_ms": 251_000,
        "artists": [{"name": "Radiohead"}],
        "album": {"name": "Kid A", "images": [{"url": "https://example.com/kida.jpg"}]},
    },
}

PAUSED_PAYLOAD = {
    **PLAYING_PAYLOAD,
    "is_playing": False,
}


def _configured_provider(tmp_dir: Path) -> tuple[SpotifyProvider, FakeHttpClient]:
    config = make_app_config(
        tmp_dir,
        spotify_enabled=True,
        spotify_client_id="id",
        spotify_client_secret="secret",
        spotify_refresh_token="refresh",
    )
    store = make_state_store(
        tmp_dir,
        spotify_enabled=True,
        spotify_client_id="id",
        spotify_client_secret="secret",
        spotify_refresh_token="refresh",
    )
    fake = FakeHttpClient()
    provider = SpotifyProvider(config, store, http_client=fake)
    # Pre-seed the cached access token so tests don't need to mock the
    # OAuth refresh endpoint on every call.
    provider._access_token = "seeded-token"
    provider._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    return provider, fake


class SpotifyStateTest(unittest.TestCase):
    def test_build_spotify_state_sets_control_flag(self) -> None:
        payload = {
            "is_playing": True,
            "device": {
                "name": "Wohnzimmer",
                "type": "Speaker",
                "is_restricted": False,
                "volume_percent": 37,
                "supports_volume": True,
            },
            "item": {
                "name": "Track",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album", "images": [{"url": "https://example.com/cover.jpg"}]},
            },
        }

        state = build_spotify_state_from_payload(
            payload,
            ProviderSnapshot(status="ok", updated_at="2026-04-08T09:00:00+00:00"),
        )

        self.assertTrue(state.can_control)
        self.assertTrue(state.is_playing)
        self.assertEqual(state.artist_name, "Artist")
        self.assertEqual(state.device_type, "Speaker")
        self.assertEqual(state.volume_percent, 37)
        self.assertTrue(state.supports_volume)

    def test_restricted_device_disables_controls(self) -> None:
        payload = {
            "is_playing": False,
            "device": {"name": "TV", "type": "TV", "is_restricted": True, "volume_percent": 14},
            "item": {"name": "Track", "artists": [], "album": {"images": []}},
        }
        state = build_spotify_state_from_payload(payload, ProviderSnapshot(status="ok"))
        self.assertFalse(state.can_control)
        self.assertFalse(state.supports_volume)

    def test_progress_and_duration_taken_from_payload(self) -> None:
        payload = {
            "is_playing": True,
            "progress_ms": 61_000,
            "device": {"name": "Wohnzimmer", "type": "Speaker", "is_restricted": False},
            "item": {
                "name": "Track",
                "duration_ms": 225_000,
                "artists": [{"name": "Artist"}],
                "album": {"images": []},
            },
        }
        state = build_spotify_state_from_payload(payload, ProviderSnapshot(status="ok"))
        self.assertEqual(state.progress_ms, 61_000)
        self.assertEqual(state.duration_ms, 225_000)
        # Round-trips cleanly through the cache serialisation.
        self.assertEqual(SpotifyState.from_dict(state.to_dict()).progress_ms, 61_000)
        self.assertEqual(SpotifyState.from_dict(state.to_dict()).duration_ms, 225_000)

    def test_smartphone_output_disables_volume_but_keeps_transport(self) -> None:
        payload = {
            "is_playing": True,
            "device": {
                "name": "iPhone",
                "type": "Smartphone",
                "is_restricted": False,
                "volume_percent": 55,
                "supports_volume": True,
            },
            "item": {"name": "Track", "artists": [{"name": "Artist"}], "album": {"images": []}},
        }
        state = build_spotify_state_from_payload(payload, ProviderSnapshot(status="ok"))
        self.assertFalse(state.supports_volume)
        self.assertTrue(state.can_control)

    def test_progress_and_duration_default_to_none_when_absent(self) -> None:
        payload = {
            "is_playing": False,
            "device": {"name": "Box", "type": "Speaker", "is_restricted": False},
            "item": {"name": "Track", "artists": [], "album": {"images": []}},
        }
        state = build_spotify_state_from_payload(payload, ProviderSnapshot(status="ok"))
        self.assertIsNone(state.progress_ms)
        self.assertIsNone(state.duration_ms)

    def test_episode_uses_show_metadata_and_artwork(self) -> None:
        payload = {
            "is_playing": True,
            "device": {"name": "Wohnzimmer", "type": "Speaker", "is_restricted": False},
            "item": {
                "type": "episode",
                "name": "Folge 7",
                "duration_ms": 1_800_000,
                "show": {"name": "Mein Podcast"},
                "images": [
                    {"url": "large.jpg"},
                    {"url": "medium.jpg"},
                ],
            },
        }
        state = build_spotify_state_from_payload(payload, ProviderSnapshot(status="ok"))
        self.assertEqual(state.track_title, "Folge 7")
        self.assertEqual(state.artist_name, "Mein Podcast")
        self.assertEqual(state.album_name, "Mein Podcast")
        self.assertEqual(state.album_art_url, "medium.jpg")


class SpotifyControlFlowTest(unittest.TestCase):
    """Covers the A1 release-blocker: control commands must reflect fresh
    truth synchronously so the UI doesn't fight a stale poll."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider, self.fake = _configured_provider(Path(self._tmp.name))
        # Seed the store with a "playing" state so toggle goes to pause.
        self.provider.state_store.update_section(
            "spotify",
            build_spotify_state_from_payload(
                PLAYING_PAYLOAD, ProviderSnapshot(status="ok")
            ),
        )

    def test_toggle_returns_fresh_state_on_success(self) -> None:
        # Command endpoint → 204, inline refresh → paused payload.
        self.fake.add_response("PUT", "https://api.spotify.com/v1/me/player/pause", status=204)
        self.fake.add_response(
            "GET", PLAYER_STATE_URL, status=200, body=PAUSED_PAYLOAD
        )

        result = self.provider.toggle_playback()

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["state"])
        self.assertFalse(result["state"]["is_playing"])
        self.assertEqual(result["state"]["snapshot"]["status"], "ok")
        # Exactly one command + one refresh call — no extra round-trips.
        self.assertEqual(len(self.fake.calls_matching("PUT", "https://api.spotify.com/v1/me/player/pause")), 1)
        self.assertEqual(len(self.fake.calls_matching("GET", PLAYER_STATE_URL)), 1)

    def test_toggle_propagates_error_message_on_401(self) -> None:
        self.fake.add_response(
            "PUT", "https://api.spotify.com/v1/me/player/pause", status=401, body={"error": "expired"}
        )

        result = self.provider.toggle_playback()

        self.assertFalse(result["ok"])
        self.assertIn("Spotify", result["message"])
        self.assertIsNone(result["state"])
        # 401 path must invalidate the cached token so the next call re-auths.
        self.assertIsNone(self.provider._access_token)

    def test_toggle_propagates_error_message_on_network_failure(self) -> None:
        self.fake.add_error(
            "PUT",
            "https://api.spotify.com/v1/me/player/pause",
            HttpError("connection reset"),
        )

        result = self.provider.toggle_playback()

        self.assertFalse(result["ok"])
        # Plan C3: user-facing messages are localised, not raw exception text.
        self.assertEqual(result["message"], "Spotify nicht erreichbar.")
        self.assertIsNone(result["state"])

    def test_toggle_propagates_error_message_on_generic_5xx(self) -> None:
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/pause",
            status=502,
            body=b"bad gateway",
        )

        result = self.provider.toggle_playback()

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Spotify nicht erreichbar.")
        self.assertIsNone(result["state"])

    def test_volume_command_refreshes_inline(self) -> None:
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/volume?volume_percent=55",
            status=204,
        )
        updated_payload = {
            **PLAYING_PAYLOAD,
            "device": {**PLAYING_PAYLOAD["device"], "volume_percent": 55},
        }
        self.fake.add_response(
            "GET",
            PLAYER_STATE_URL,
            status=200,
            body=updated_payload,
        )

        result = self.provider.set_volume(55)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["volume_percent"], 55)
        # Refresh fired exactly once — the UI does not need to poll again.
        self.assertEqual(
            len(self.fake.calls_matching("GET", PLAYER_STATE_URL)), 1
        )

    def test_seek_command_refreshes_inline(self) -> None:
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/seek?position_ms=42000",
            status=204,
        )
        updated_payload = {**PLAYING_PAYLOAD, "progress_ms": 42_000}
        self.fake.add_response(
            "GET",
            PLAYER_STATE_URL,
            status=200,
            body=updated_payload,
        )

        result = self.provider.seek_to(42_000)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["progress_ms"], 42_000)

    def test_seek_rejects_position_outside_current_item(self) -> None:
        for position in (-1, 251_001, None, 3.2, True):
            with self.subTest(position=position):
                result = self.provider.seek_to(position)
                self.assertFalse(result["ok"])
                self.assertEqual(result["message"], "Wiedergabeposition ist ungültig.")
        self.assertEqual(self.fake.calls, [])

    def test_seek_propagates_spotify_error(self) -> None:
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/seek?position_ms=42000",
            status=403,
        )
        result = self.provider.seek_to(42_000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Spotify-Aktion nicht erlaubt.")

    def test_seek_maps_rate_limit_and_network_failure(self) -> None:
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/seek?position_ms=42000",
            status=429,
        )
        limited = self.provider.seek_to(42_000)
        self.assertFalse(limited["ok"])
        self.assertEqual(limited["message"], "Spotify-Limit erreicht. Kurz warten.")

        self.fake.add_error(
            "PUT",
            "https://api.spotify.com/v1/me/player/seek?position_ms=43000",
            HttpError("offline"),
        )
        offline = self.provider.seek_to(43_000)
        self.assertFalse(offline["ok"])
        self.assertEqual(offline["message"], "Spotify nicht erreichbar.")

    def test_next_track_returns_fresh_state(self) -> None:
        self.fake.add_response(
            "POST", "https://api.spotify.com/v1/me/player/next", status=204
        )
        self.fake.add_response(
            "GET",
            PLAYER_STATE_URL,
            status=200,
            body=PLAYING_PAYLOAD,
        )

        result = self.provider.next_track()

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["track_title"], "Everything In Its Right Place")

    def test_not_configured_returns_ok_false_without_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_app_config(Path(tmp))  # spotify disabled
            store = make_state_store(Path(tmp))
            fake = FakeHttpClient()
            provider = SpotifyProvider(config, store, http_client=fake)

            result = provider.toggle_playback()

            self.assertFalse(result["ok"])
            self.assertEqual(result["message"], "Spotify ist nicht konfiguriert.")
            self.assertEqual(fake.calls, [])


class SpotifyStatusMessageTest(unittest.TestCase):
    """Plan C3: raw Spotify HTTP status codes get mapped to short German
    toast messages so the user sees actionable German UI copy instead of
    ``Spotify antwortete mit 429``."""

    def test_401_maps_to_login_expired(self) -> None:
        self.assertEqual(
            spotify_status_message(401), "Spotify-Anmeldung abgelaufen."
        )

    def test_403_maps_to_action_not_allowed(self) -> None:
        self.assertEqual(
            spotify_status_message(403), "Spotify-Aktion nicht erlaubt."
        )

    def test_404_maps_to_no_device(self) -> None:
        self.assertEqual(
            spotify_status_message(404), "Kein aktives Spotify-Gerät."
        )

    def test_429_maps_to_rate_limit(self) -> None:
        self.assertEqual(
            spotify_status_message(429), "Spotify-Limit erreicht. Kurz warten."
        )

    def test_generic_5xx_maps_to_not_reachable(self) -> None:
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(
                    spotify_status_message(status), "Spotify nicht erreichbar."
                )

    def test_unknown_status_falls_back_to_generic(self) -> None:
        self.assertEqual(
            spotify_status_message(418), "Spotify nicht erreichbar."
        )

    def test_messages_contain_real_umlauts(self) -> None:
        # AGENTS.md rule: echte Umlaute, kein Mojibake.
        for status in (401, 403, 404, 429, 500):
            message = spotify_status_message(status)
            self.assertNotIn("ae", message.lower().replace("warten", ""))
            self.assertNotIn("?", message)


class SpotifyMapperTest(unittest.TestCase):
    """The picker consumes a flattened, Pi-friendly shape — verify the maps."""

    def test_map_devices_flattens_and_sorts(self) -> None:
        raw = [
            {"id": "b", "name": "Zimmer", "type": "Speaker", "is_active": True, "volume_percent": 30},
            {"id": "a", "name": "Bad", "type": "Speaker", "is_restricted": True},
            "garbage",
        ]
        devices = map_spotify_devices(raw)
        # Non-dict entries dropped; remaining sorted case-insensitively by name.
        self.assertEqual([d["name"] for d in devices], ["Bad", "Zimmer"])
        self.assertTrue(devices[1]["is_active"])
        self.assertTrue(devices[0]["is_restricted"])
        self.assertIsNone(devices[0]["volume_percent"])

    def test_map_playlists_picks_smallest_image_and_skips_uriless(self) -> None:
        raw = [
            {
                "uri": "spotify:playlist:1",
                "name": "Morgens",
                "images": [
                    {"url": "big.jpg", "width": 640},
                    {"url": "small.jpg", "width": 60},
                ],
                "tracks": {"total": 12},
            },
            {"name": "kein uri"},  # dropped
            {"uri": "spotify:playlist:2", "name": "Ohne Bild", "images": []},
        ]
        playlists = map_spotify_playlists(raw)
        self.assertEqual(len(playlists), 2)
        self.assertEqual(playlists[0]["image_url"], "small.jpg")
        self.assertEqual(playlists[0]["track_count"], 12)
        self.assertIsNone(playlists[1]["image_url"])

    def test_map_playlists_falls_back_to_last_image_when_unsized(self) -> None:
        raw = [
            {
                "uri": "spotify:playlist:1",
                "name": "Mosaik",
                "images": [{"url": "a.jpg"}, {"url": "b.jpg"}],
            }
        ]
        # Playlist mosaics carry null widths — take the last (smallest) entry.
        self.assertEqual(map_spotify_playlists(raw)[0]["image_url"], "b.jpg")

    def test_map_queue_supports_tracks_episodes_and_unknown_items(self) -> None:
        raw = [
            {
                "type": "track",
                "name": "Track",
                "artists": [{"name": "Artist"}],
                "album": {
                    "images": [
                        {"url": "large.jpg", "width": 640},
                        {"url": "small.jpg", "width": 64},
                    ]
                },
            },
            {
                "type": "episode",
                "name": "Folge 7",
                "show": {"name": "Mein Podcast"},
                "images": [{"url": "episode.jpg", "width": 300}],
            },
            {"type": "ad", "name": "Spotify Hinweis"},
            "garbage",
        ]

        items = map_spotify_queue(raw)

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {
            "title": "Track",
            "subtitle": "Artist",
            "image_url": "small.jpg",
            "kind": "track",
            "duration_ms": None,
        })
        self.assertEqual(items[1]["subtitle"], "Mein Podcast")
        self.assertEqual(items[1]["kind"], "episode")
        self.assertEqual(items[2]["subtitle"], "Spotify")

    def test_map_queue_limits_result_to_four_rows(self) -> None:
        raw = [
            {"type": "track", "name": f"Track {index}", "artists": []}
            for index in range(8)
        ]
        self.assertEqual(len(map_spotify_queue(raw)), 4)


class SpotifyDeviceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider, self.fake = _configured_provider(Path(self._tmp.name))

    def test_list_devices_maps_and_caches(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/devices",
            status=200,
            body={"devices": [{"id": "x", "name": "Box", "type": "Speaker", "is_active": True}]},
        )

        first = self.provider.list_devices()
        second = self.provider.list_devices()

        self.assertTrue(first["ok"])
        self.assertEqual(first["devices"][0]["name"], "Box")
        self.assertEqual(second["devices"][0]["name"], "Box")
        # Second call served from the short TTL cache — only one upstream hit.
        self.assertEqual(
            len(self.fake.calls_matching("GET", "https://api.spotify.com/v1/me/player/devices")),
            1,
        )

    def test_list_devices_403_maps_to_german_message(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/devices",
            status=403,
            body={"error": "forbidden"},
        )
        result = self.provider.list_devices()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Spotify-Aktion nicht erlaubt.")
        self.assertEqual(result["devices"], [])

    def test_list_playlists_requests_configured_limit(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/playlists?limit=50",
            status=200,
            body={"items": [{"uri": "spotify:playlist:1", "name": "P", "images": []}]},
        )
        result = self.provider.list_playlists()
        self.assertTrue(result["ok"])
        self.assertEqual(result["playlists"][0]["uri"], "spotify:playlist:1")

    def test_list_devices_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = SpotifyProvider(
                make_app_config(Path(tmp)), make_state_store(Path(tmp)), http_client=FakeHttpClient()
            )
            result = provider.list_devices()
            self.assertFalse(result["ok"])
            self.assertEqual(result["devices"], [])


class SpotifyQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider, self.fake = _configured_provider(Path(self._tmp.name))

    def _age_queue_cache(self, seconds: float) -> None:
        """Backdate the cached queue entry relative to the monotonic clock."""
        stored_at, data = self.provider._list_cache["queue"]
        self.provider._list_cache["queue"] = (stored_at - seconds, data)

    def _seed_queue(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=200,
            body={
                "queue": [
                    {
                        "type": "track",
                        "name": "Nächster Titel",
                        "artists": [{"name": "Künstler"}],
                        "album": {"images": []},
                    }
                ]
            },
        )

    def test_list_queue_maps_and_caches(self) -> None:
        self._seed_queue()

        first = self.provider.list_queue()
        second = self.provider.list_queue()

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["items"][0]["title"], "Nächster Titel")
        self.assertEqual(second["items"], first["items"])
        self.assertEqual(
            len(self.fake.calls_matching("GET", "https://api.spotify.com/v1/me/player/queue")),
            1,
        )

    def test_expired_queue_falls_back_to_last_good_on_error(self) -> None:
        self._seed_queue()
        self.provider.list_queue()
        # Age the entry past the TTL but inside the stale window. Relative to
        # time.monotonic(), not a literal 0.0 — that is uptime-dependent and
        # would still look fresh on a Pi in its first seconds after boot.
        self._age_queue_cache(_QUEUE_CACHE_TTL_SECONDS + 1)
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=429,
        )

        stale = self.provider.list_queue()

        self.assertTrue(stale["ok"])
        self.assertEqual(stale["status"], "stale")
        # Compare against an independently built expectation: asserting against
        # the first call's list would compare the cached object with itself.
        self.assertEqual(
            stale["items"],
            [
                {
                    "title": "Nächster Titel",
                    "subtitle": "Künstler",
                    "image_url": None,
                    "kind": "track",
                    "duration_ms": None,
                }
            ],
        )

    def test_queue_stale_fallback_expires(self) -> None:
        self._seed_queue()
        self.provider.list_queue()
        self._age_queue_cache(_QUEUE_STALE_MAX_AGE_SECONDS + 1)
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=429,
        )

        result = self.provider.list_queue()

        # Past the stale window an honest error beats an hours-old queue.
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["items"], [])

    def test_empty_queue_is_not_resurrected_as_stale(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=200,
            body={"queue": []},
        )
        self.provider.list_queue()
        self._age_queue_cache(_QUEUE_CACHE_TTL_SECONDS + 1)
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=429,
        )

        result = self.provider.list_queue()

        # A cached empty list is a real answer, not last-known-good data — the
        # calm empty-state message must win over the "loaded from cache" status.
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["message"], "Keine weiteren Titel.")
        self.assertEqual(result["items"], [])

    def test_queue_error_without_cache_is_isolated(self) -> None:
        self.fake.add_error(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            HttpError("offline"),
        )
        result = self.provider.list_queue()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["items"], [])

    def test_empty_queue_has_deliberate_state(self) -> None:
        self.fake.add_response(
            "GET",
            "https://api.spotify.com/v1/me/player/queue",
            status=200,
            body={"queue": []},
        )
        result = self.provider.list_queue()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["message"], "Keine weiteren Titel.")

    def test_successful_skip_invalidates_queue_cache(self) -> None:
        self.provider._list_cache_set(
            "queue",
            [{"title": "Alt", "subtitle": "Artist", "image_url": None, "kind": "track"}],
        )
        self.fake.add_response(
            "POST", "https://api.spotify.com/v1/me/player/next", status=204
        )
        self.fake.add_response(
            "GET", PLAYER_STATE_URL, status=200, body=PLAYING_PAYLOAD
        )

        result = self.provider.next_track()

        self.assertTrue(result["ok"])
        self.assertNotIn("queue", self.provider._list_cache)


class SpotifyStartPlaybackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider, self.fake = _configured_provider(Path(self._tmp.name))

    def _seed_play_routes(self) -> None:
        self.fake.add_response("PUT", "https://api.spotify.com/v1/me/player", status=204)
        self.fake.add_response(
            "PUT",
            "https://api.spotify.com/v1/me/player/play?device_id=box-1",
            status=204,
        )
        self.fake.add_response(
            "GET", PLAYER_STATE_URL, status=200, body=PLAYING_PAYLOAD
        )

    def test_start_playback_transfers_then_plays_context(self) -> None:
        self._seed_play_routes()

        result = self.provider.start_playback("box-1", "spotify:playlist:42")

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["state"])
        # Transfer step carries the device in the body, not the query.
        transfer = self.fake.calls_matching("PUT", "https://api.spotify.com/v1/me/player")[0]
        self.assertEqual(_json_body(transfer), {"device_ids": ["box-1"], "play": False})
        # Play step targets the chosen device via query + context_uri body.
        play = self.fake.calls_matching(
            "PUT", "https://api.spotify.com/v1/me/player/play?device_id=box-1"
        )[0]
        self.assertEqual(_json_body(play), {"context_uri": "spotify:playlist:42"})

    def test_start_playback_track_uri_uses_uris(self) -> None:
        self._seed_play_routes()
        self.provider.start_playback("box-1", "spotify:track:abc")
        play = self.fake.calls_matching(
            "PUT", "https://api.spotify.com/v1/me/player/play?device_id=box-1"
        )[0]
        self.assertEqual(_json_body(play), {"uris": ["spotify:track:abc"]})

    def test_start_playback_continues_when_transfer_fails(self) -> None:
        self.fake.add_error(
            "PUT", "https://api.spotify.com/v1/me/player", HttpError("transfer boom")
        )
        self.fake.add_response(
            "PUT", "https://api.spotify.com/v1/me/player/play?device_id=box-1", status=204
        )
        self.fake.add_response(
            "GET", PLAYER_STATE_URL, status=200, body=PLAYING_PAYLOAD
        )

        result = self.provider.start_playback("box-1", "spotify:playlist:42")

        self.assertTrue(result["ok"])

    def test_start_playback_play_404_maps_to_no_device(self) -> None:
        self.fake.add_response("PUT", "https://api.spotify.com/v1/me/player", status=204)
        self.fake.add_response(
            "PUT", "https://api.spotify.com/v1/me/player/play?device_id=box-1", status=404
        )

        result = self.provider.start_playback("box-1", "spotify:playlist:42")

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Kein aktives Spotify-Gerät.")
        self.assertIsNone(result["state"])

    def test_start_playback_without_device_rejected(self) -> None:
        result = self.provider.start_playback("   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Kein Gerät ausgewählt.")
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from datetime import datetime
from zoneinfo import ZoneInfo

from smart_display.i18n import format_initial_clock
from smart_display.models import PhotoManifestEntry
from smart_display.night import is_night, window_for_ui
from smart_display.scheduler import DEFAULT_PAUSE_TTL_SECONDS
from smart_display.system_probe import time_is_synchronized
from smart_display.watch_faces import (
    QLOCKTWO_GRID,
    QLOCKTWO_OOE_GRID,
    analog_hand_angles,
    lcd_segments_for,
    qlocktwo_active_cells,
    qlocktwo_ooe_active_cells,
)
from smart_display.web.origin_guard import local_only


# Das lokale /api/state ist in-process und ohne Upstream-Call praktisch gratis,
# daher entkoppeln wir den Browser-Poll von der Spotify-Backend-Frequenz: ein
# schneller lokaler Poll zeigt Backend-Updates binnen weniger Sekunden, ohne
# zusätzliche Spotify-API-Last. Gedeckelt niedrig, gefloort gegen Busy-Spin.
FRONTEND_POLL_CEILING_SECONDS = 4
FRONTEND_POLL_FLOOR_SECONDS = 3


def _poll_interval_seconds(config) -> int:
    if not config.spotify.enabled:
        return 30
    return max(
        min(config.refresh_intervals.spotify_seconds, FRONTEND_POLL_CEILING_SECONDS),
        FRONTEND_POLL_FLOOR_SECONDS,
    )


def _with_runtime_flags(state: dict, services: dict) -> dict:
    """Facts that are true *now* and not worth persisting in last_good.json:
    NTP sync (Ticket #1) and the live timer view (Ticket #3, status derived
    from the clock)."""
    probe = services.get("time_sync_probe", time_is_synchronized)
    state["system"]["time_synced"] = bool(probe())
    timer_service = services.get("timer_service")
    if timer_service is not None:
        state["timers"] = timer_service.list()
    return state


def create_blueprint() -> Blueprint:
    blueprint = Blueprint("web", __name__)

    @blueprint.get("/")
    def index():
        services = current_app.extensions["smart_display"]
        config = services["config"]
        state = _with_runtime_flags(services["state_store"].to_dict(), services)
        # Plan B14: render the hero clock on the server so the cold-reload
        # first frame is correct. JS overwrites these values on its first tick.
        clock_factory = services.get("clock_factory", format_initial_clock)
        initial_clock = clock_factory(config.app.timezone)
        now_local = datetime.now(ZoneInfo(config.app.timezone))
        night_window = window_for_ui(config.app.night_from, config.app.night_to)
        initial_night = is_night(now_local, config.app.night_from, config.app.night_to)
        initial_qlocktwo = qlocktwo_active_cells(now_local.hour, now_local.minute)
        initial_qlocktwo_ooe = qlocktwo_ooe_active_cells(
            now_local.hour, now_local.minute
        )
        initial_analog = analog_hand_angles(now_local.hour, now_local.minute)
        # Pre-compute digit payloads so flip/lcd faces can SSR cleanly instead
        # of flashing all-off segments before JS takes over.
        time_digits = [
            initial_clock["time"][0],
            initial_clock["time"][1],
            initial_clock["time"][3],
            initial_clock["time"][4],
        ]
        lcd_digits = [
            {"value": d, "segments": sorted(lcd_segments_for(d))}
            for d in time_digits
        ]
        return render_template(
            "index.html",
            app_title="Smart Display",
            ui_config={
                "locale": config.app.locale,
                "timezone": config.app.timezone,
                "idle_timeout_seconds": config.screensaver.idle_timeout_seconds,
                "image_duration_seconds": config.screensaver.image_duration_seconds,
                "poll_interval_seconds": _poll_interval_seconds(config),
                "watch_face": config.app.watch_face,
                "night": night_window,
                "allow_shutdown": bool(config.app.allow_shutdown),
            },
            initial_state=state,
            initial_night=initial_night,
            initial_clock=initial_clock,
            qlocktwo_grid=QLOCKTWO_GRID,
            qlocktwo_active=initial_qlocktwo,
            qlocktwo_ooe_grid=QLOCKTWO_OOE_GRID,
            qlocktwo_ooe_active=initial_qlocktwo_ooe,
            initial_analog=initial_analog,
            initial_watch_face=config.app.watch_face,
            time_digits=time_digits,
            lcd_digits=lcd_digits,
        )

    @blueprint.get("/api/state")
    def api_state():
        services = current_app.extensions["smart_display"]
        return jsonify(_with_runtime_flags(services["state_store"].to_dict(), services))

    # ---- Timers (Ticket #3) ---------------------------------------------------

    def _timer_status(result: dict) -> int:
        if result.get("ok"):
            return 200
        return 404 if result.get("not_found") else 400

    @blueprint.post("/api/timers")
    @local_only
    def timers_create():
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        result = services["timer_service"].create(
            payload.get("duration_seconds"), payload.get("label", "")
        )
        return jsonify(result), _timer_status(result)

    @blueprint.post("/api/timers/<timer_id>/cancel")
    @local_only
    def timers_cancel(timer_id: str):
        services = current_app.extensions["smart_display"]
        result = services["timer_service"].cancel(timer_id)
        return jsonify(result), _timer_status(result)

    @blueprint.post("/api/timers/<timer_id>/dismiss")
    @local_only
    def timers_dismiss(timer_id: str):
        services = current_app.extensions["smart_display"]
        result = services["timer_service"].dismiss(timer_id)
        return jsonify(result), _timer_status(result)

    @blueprint.post("/api/timers/<timer_id>/extend")
    @local_only
    def timers_extend(timer_id: str):
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        result = services["timer_service"].extend(timer_id, payload.get("seconds"))
        return jsonify(result), _timer_status(result)

    # ---- Power (Ticket #2) ----------------------------------------------------

    @blueprint.post("/api/system/shutdown")
    @local_only
    def system_shutdown():
        services = current_app.extensions["smart_display"]
        services["state_store"].flush()
        result = services["power_controller"].shutdown()
        return jsonify(result), (200 if result.get("ok") else 403)

    @blueprint.post("/api/system/reboot")
    @local_only
    def system_reboot():
        services = current_app.extensions["smart_display"]
        services["state_store"].flush()
        result = services["power_controller"].reboot()
        return jsonify(result), (200 if result.get("ok") else 403)

    @blueprint.get("/api/screensaver/next")
    def next_screensaver_image():
        services = current_app.extensions["smart_display"]
        image_cache = services["image_cache"]
        config = services["config"]
        entry: PhotoManifestEntry | None = image_cache.next_entry(
            include_demo=config.screensaver.demo_images_enabled
        )
        if entry is None:
            return jsonify({"image": None})
        return jsonify({"image": entry.to_dict()})

    @blueprint.post("/api/screensaver/state")
    @local_only
    def screensaver_state():
        """Plan B1: the frontend tells us when the screensaver is visible so
        we can pause the Spotify polling group. Saves one request every 30 s
        whenever the user is away from the panel.

        The pause carries a TTL watchdog (``DEFAULT_PAUSE_TTL_SECONDS``) so
        a crashed frontend cannot strand the Spotify polling forever — the
        pause self-clears well before anyone notices, and a live frontend
        refreshes the timer implicitly via its periodic state POSTs.
        """
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        active = bool(payload.get("active", False))
        scheduler = services.get("scheduler")
        if scheduler is not None:
            if active:
                scheduler.set_paused(
                    "spotify", True, ttl_seconds=DEFAULT_PAUSE_TTL_SECONDS
                )
            else:
                scheduler.set_paused("spotify", False)
        return jsonify({"ok": True, "active": active})

    @blueprint.post("/api/spotify/toggle")
    @local_only
    def spotify_toggle():
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].toggle_playback())

    @blueprint.post("/api/spotify/next")
    @local_only
    def spotify_next():
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].next_track())

    @blueprint.post("/api/spotify/previous")
    @local_only
    def spotify_previous():
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].previous_track())

    @blueprint.post("/api/spotify/volume")
    @local_only
    def spotify_volume():
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        try:
            volume_percent = int(payload["volume_percent"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "message": "volume_percent fehlt oder ist ungültig."}), 400
        return jsonify(services["spotify_provider"].set_volume(volume_percent))

    @blueprint.post("/api/spotify/seek")
    @local_only
    def spotify_seek():
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        position_ms = payload.get("position_ms")
        if type(position_ms) is not int or position_ms < 0:
            return jsonify(
                {"ok": False, "message": "position_ms fehlt oder ist ungültig."}
            ), 400
        return jsonify(services["spotify_provider"].seek_to(position_ms))

    @blueprint.get("/api/spotify/queue")
    @local_only
    def spotify_queue():
        """Short upcoming queue for the detail screen, fetched only on demand."""
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].list_queue())

    @blueprint.get("/api/spotify/devices")
    @local_only
    def spotify_devices():
        """Available Spotify Connect devices for the picker overlay. Fetched
        on demand (not on the poll loop) and lightly cached in the provider."""
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].list_devices())

    @blueprint.get("/api/spotify/sources")
    @local_only
    def spotify_sources():
        """The user's own Spotify playlists — the picker's "what to play" list."""
        services = current_app.extensions["smart_display"]
        return jsonify(services["spotify_provider"].list_playlists())

    @blueprint.post("/api/spotify/play")
    @local_only
    def spotify_play():
        services = current_app.extensions["smart_display"]
        payload = request.get_json(silent=True) or {}
        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or not device_id.strip():
            return jsonify({"ok": False, "message": "device_id fehlt oder ist ungültig."}), 400
        context_uri = payload.get("context_uri")
        if context_uri is not None and not isinstance(context_uri, str):
            return jsonify({"ok": False, "message": "context_uri ist ungültig."}), 400
        return jsonify(
            services["spotify_provider"].start_playback(device_id, context_uri)
        )

    @blueprint.get("/health")
    def health():
        services = current_app.extensions["smart_display"]
        return jsonify(services["state_store"].health_payload())

    @blueprint.get("/media/screensaver/<path:filename>")
    def screensaver_media(filename: str):
        services = current_app.extensions["smart_display"]
        entry = services["image_cache"].entry_for_filename(filename)
        if entry is None:
            return jsonify({"error": "not found"}), 404
        return send_file(Path(entry.local_path))

    return blueprint

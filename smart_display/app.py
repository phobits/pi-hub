from __future__ import annotations

import atexit
import logging
from collections.abc import Callable

from flask import Flask

from smart_display import sd_notify
from smart_display.cache.image_cache import ImageCache
from smart_display.config import AppConfig, load_config
from smart_display.power import PowerController
from smart_display.providers.caldav_provider import CalDAVProvider
from smart_display.providers.lightroom_source import LightroomSourceProvider
from smart_display.providers.mock_provider import MockProvider
from smart_display.providers.spotify_provider import SpotifyProvider
from smart_display.providers.weather_openmeteo import OpenMeteoProvider
from smart_display.scheduler import ScheduledJob, Scheduler
from smart_display.state_store import StateStore
from smart_display.timers import TimerService
from smart_display.web.routes import create_blueprint


logger = logging.getLogger(__name__)


def create_app(
    config: AppConfig | None = None, *, start_scheduler: bool = True
) -> Flask:
    app_config = config or load_config()
    _configure_logging(app_config)
    app_config.app.data_dir.mkdir(parents=True, exist_ok=True)
    app_config.screensaver_cache_dir.mkdir(parents=True, exist_ok=True)
    app_config.screensaver_local_dir.mkdir(parents=True, exist_ok=True)

    state_store = StateStore(app_config)
    timer_service = TimerService(state_store)
    power_controller = PowerController(allowed=app_config.app.allow_shutdown)
    image_cache = ImageCache(
        cache_dir=app_config.screensaver_cache_dir,
        manifest_path=app_config.screensaver_manifest_path,
        remote_enabled=bool(app_config.screensaver.source_url.strip()),
        local_dir=app_config.screensaver_local_dir,
        demo_dir=app_config.root_dir / "smart_display" / "web" / "static" / "images" / "demo-screensaver",
    )

    weather_provider = OpenMeteoProvider(app_config, state_store)
    calendar_provider = CalDAVProvider(app_config, state_store)
    spotify_provider = SpotifyProvider(app_config, state_store)
    screensaver_provider = LightroomSourceProvider(app_config, state_store, image_cache)
    mock_provider = MockProvider(app_config, state_store)

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )
    app.json.sort_keys = False
    app.extensions["smart_display"] = {
        "config": app_config,
        "state_store": state_store,
        "image_cache": image_cache,
        "spotify_provider": spotify_provider,
        "timer_service": timer_service,
        "power_controller": power_controller,
    }
    app.register_blueprint(create_blueprint())

    if start_scheduler:
        scheduler = Scheduler()
        jobs = _build_scheduled_jobs(
            app_config,
            weather_refresh=weather_provider.refresh,
            calendar_refresh=calendar_provider.refresh,
            spotify_refresh=spotify_provider.refresh,
            screensaver_refresh=screensaver_provider.refresh,
            mock_refresh=mock_provider.refresh,
            retry_wait=state_store.retry_wait,
        )
        _seed_unscheduled_provider_state(
            jobs,
            weather_refresh=weather_provider.refresh,
            calendar_refresh=calendar_provider.refresh,
            spotify_refresh=spotify_provider.refresh,
            screensaver_refresh=screensaver_provider.refresh,
        )
        scheduler.start(jobs)
        app.extensions["smart_display"]["scheduler"] = scheduler
        # atexit runs LIFO: stop the providers first, then flush whatever the
        # persistence debounce still holds (Ticket #1).
        atexit.register(state_store.flush)
        atexit.register(scheduler.stop)

    return app


def _build_scheduled_jobs(
    config: AppConfig,
    *,
    weather_refresh: Callable[[], None],
    calendar_refresh: Callable[[], None],
    spotify_refresh: Callable[[], None],
    screensaver_refresh: Callable[[], None],
    mock_refresh: Callable[[], None],
    retry_wait: Callable[[str], float | None] | None = None,
) -> list[ScheduledJob]:
    """Return only jobs that have useful periodic work on the Pi.

    Disabled or incomplete providers still get seeded once at boot by
    ``_seed_unscheduled_provider_state`` so the UI shows a deliberate empty
    state, but they do not keep a sleeping thread alive forever.
    """
    jobs: list[ScheduledJob] = []

    # Plan B2: stagger the boot fan-out so the Pi Zero doesn't hammer all
    # upstream APIs in the first second after `systemctl start`. Jitter ±1 s
    # on every subsequent interval spreads refreshes over long uptimes.
    if config.weather.enabled:
        jobs.append(
            ScheduledJob(
                name="weather",
                interval_seconds=config.refresh_intervals.weather_seconds,
                task=weather_refresh,
                retry_after=_retry_for(retry_wait, "weather"),
                startup_delay_seconds=0.0,
                jitter_seconds=1.0,
            )
        )

    if _calendar_has_credentials(config):
        jobs.append(
            ScheduledJob(
                name="calendar",
                interval_seconds=config.refresh_intervals.calendar_seconds,
                task=calendar_refresh,
                retry_after=_retry_for(retry_wait, "calendar"),
                startup_delay_seconds=2.0,
                jitter_seconds=1.0,
            )
        )

    if _spotify_has_credentials(config):
        jobs.append(
            ScheduledJob(
                name="spotify",
                interval_seconds=config.refresh_intervals.spotify_seconds,
                task=spotify_refresh,
                retry_after=_retry_for(retry_wait, "spotify"),
                startup_delay_seconds=4.0,
                jitter_seconds=1.0,
                pause_group="spotify",
            )
        )

    if config.screensaver.enabled and bool(config.screensaver.source_url):
        jobs.append(
            ScheduledJob(
                name="screensaver",
                interval_seconds=config.refresh_intervals.lightroom_seconds,
                task=screensaver_refresh,
                startup_delay_seconds=6.0,
                jitter_seconds=1.0,
            )
        )

    if config.app.demo_mode:
        jobs.append(
            ScheduledJob(
                name="mock",
                interval_seconds=60,
                task=mock_refresh,
                startup_delay_seconds=1.0,
                jitter_seconds=1.0,
            )
        )

    return jobs


def _retry_for(
    retry_wait: Callable[[str], float | None] | None, section: str
) -> Callable[[], float | None] | None:
    if retry_wait is None:
        return None
    return lambda: retry_wait(section)


def _calendar_has_credentials(config: AppConfig) -> bool:
    return config.calendar.enabled and all(
        [
            config.calendar.url,
            config.calendar.username,
            config.calendar.password,
        ]
    )


def _spotify_has_credentials(config: AppConfig) -> bool:
    return config.spotify.enabled and all(
        [
            config.spotify.client_id,
            config.spotify.client_secret,
            config.spotify.refresh_token,
        ]
    )


def _seed_unscheduled_provider_state(
    jobs: list[ScheduledJob],
    *,
    weather_refresh: Callable[[], None],
    calendar_refresh: Callable[[], None],
    spotify_refresh: Callable[[], None],
    screensaver_refresh: Callable[[], None],
) -> None:
    scheduled = {job.name for job in jobs}
    for name, refresh in (
        ("weather", weather_refresh),
        ("calendar", calendar_refresh),
        ("spotify", spotify_refresh),
        ("screensaver", screensaver_refresh),
    ):
        if name in scheduled:
            continue
        try:
            refresh()
        except Exception:  # pragma: no cover
            logger.exception("startup provider seed failed: %s", name)


def main() -> None:
    app = create_app()
    serve_app(app)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def serve_app(app: Flask) -> None:
    config: AppConfig = app.extensions["smart_display"]["config"]
    try:
        from waitress import create_server
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("waitress is required to run the production server.") from exc

    # Plan B10: refuse to serve on anything other than loopback. Previously
    # a misconfigured ``APP_HOST=0.0.0.0`` was silently coerced back to
    # ``127.0.0.1`` with a WARNING log line — which is exactly the kind of
    # footgun that makes ops think the setting is respected when it isn't.
    # Failing loudly at start-up surfaces the mistake before the kiosk ever
    # comes up. The POST endpoints remain guarded by ``local_only`` as a
    # second line of defence.
    host = config.app.host or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"refusing to serve Smart Display on non-loopback host {host!r}; "
            "set app.host to 127.0.0.1, ::1 or localhost"
        )
    # Keep Waitress' four-worker default. Two is not enough: opening the picker
    # fires /api/spotify/devices and /api/spotify/sources concurrently, and both
    # block on the shared api.spotify.com connection lock for up to
    # spotify.timeout_seconds (x2 with the retry) — that would pin every worker
    # and stall the /api/state poll and the screensaver media route with it.
    # The saving was illusory anyway: extra worker threads reserve virtual stack,
    # not resident memory, so this costs the 512 MB Pi effectively nothing.
    server = create_server(app, host=host, port=config.app.port, threads=4)

    # Ticket #1: SIGTERM (systemctl stop/restart) must take the normal exit
    # path so atexit runs — scheduler stop, then the state-store flush. Python's
    # default handler terminates without either.
    _install_sigterm_handler()

    # Ticket #1: the socket is bound now, so READY=1 is truthful. The watchdog
    # thread only pings after a real loopback /health succeeds.
    sd_notify.notify("READY=1")
    interval = sd_notify.watchdog_interval_seconds()
    if interval is not None:
        # Probe the host Waitress actually bound (#4): with APP_HOST=::1 a
        # hard-coded 127.0.0.1 never answers and systemd would restart the
        # service on every WatchdogSec.
        pinger = sd_notify.WatchdogPinger(
            interval=interval, host=host, port=config.app.port
        )
        pinger.start()
        app.extensions["smart_display"]["watchdog"] = pinger
        logger.info("systemd watchdog active, pinging every %.0f s", interval)

    server.run()


def _install_sigterm_handler() -> None:
    import signal
    import sys

    def _terminate(signum, _frame):  # pragma: no cover — signal path
        logger.info("received signal %s, shutting down", signum)
        sd_notify.notify("STOPPING=1")
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:  # pragma: no cover — not the main thread
        pass


def _configure_logging(config: AppConfig) -> None:
    logging.basicConfig(
        level=getattr(logging, config.app.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover
    main()

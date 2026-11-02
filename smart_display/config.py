from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smart_display.night import parse_hhmm
from smart_display.watch_faces import normalize_watch_face


try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppSection:
    host: str
    port: int
    locale: str
    timezone: str
    data_dir: Path
    log_level: str
    demo_mode: bool
    watch_face: str
    # Ticket #2: night window as "HH:MM" wall-clock bounds; "" disables.
    night_from: str = "22:30"
    night_to: str = "06:00"
    # Ticket #2: gate for POST /api/system/{shutdown,reboot}. Only the Pi's
    # .env sets this — the local demo must never halt a dev machine.
    allow_shutdown: bool = False


@dataclass(slots=True)
class WeatherConfig:
    enabled: bool
    provider: str
    label: str
    latitude: float
    longitude: float
    api_key: str | None
    timeout_seconds: int
    # Weather detail screen (#22). Both GeoSphere sources need nothing but the
    # coordinates above; switching them off leaves the screen on Open-Meteo.
    geosphere_enabled: bool = True
    warnings_enabled: bool = True


@dataclass(slots=True)
class CalendarConfig:
    enabled: bool
    url: str
    username: str
    password: str
    calendar_names: list[str]
    timeout_seconds: int


@dataclass(slots=True)
class SpotifyConfig:
    enabled: bool
    client_id: str
    client_secret: str
    refresh_token: str
    device_id: str
    market: str
    timeout_seconds: int
    playlists_limit: int = 50


@dataclass(slots=True)
class ScreensaverConfig:
    enabled: bool
    idle_timeout_seconds: int
    image_duration_seconds: int
    refresh_interval_seconds: int
    source_url: str
    cache_dir: str
    demo_images_enabled: bool
    timeout_seconds: int
    images_per_tick: int = 1


@dataclass(slots=True)
class RefreshIntervalsConfig:
    weather_seconds: int
    calendar_seconds: int
    spotify_seconds: int
    lightroom_seconds: int
    # #22: C-LAEF computes a run every 3 h and publishes it hours later, so an
    # hourly look finds a new run within the hour. INCA runs every 15 min.
    weather_hourly_seconds: int = 3600
    weather_nowcast_seconds: int = 900
    weather_warnings_seconds: int = 300


@dataclass(slots=True)
class Pi5StatusConfig:
    # #24: the pi5's pistatus endpoint (socket-activated, LAN only). Empty
    # means no pi5 screen at all. Read only while that screen is visible.
    # pistatus answers in ~0.15 s; HttpClient retries once on a timeout, so a
    # powered-off pi5 holds a request for 2 × this.
    url: str = ""
    timeout_seconds: int = 2


@dataclass(slots=True)
class TransitStop:
    # #27: one line at one stop of the Linz AG EFA. ``stop_id`` is the EFA
    # stop id (stopfinder ``stateless``), ``line`` the public line number as
    # the departure monitor spells it, ``label`` what the panel shows.
    stop_id: str
    line: str
    label: str
    # Departures sooner than this are left out — the bus one cannot reach.
    walk_minutes: int = 0


@dataclass(slots=True)
class TransitConfig:
    # #27: departures under the agenda, only on ``days`` (ISO, 1 = Monday)
    # between ``window_from`` (inclusive) and ``window_to`` (exclusive). No
    # stops means no transit rows and no request, ever.
    stops: list[TransitStop] = field(default_factory=list)
    base_url: str = "https://www.linzag.at/static"
    days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    window_from: str = "06:00"
    window_to: str = "10:00"
    departures: int = 3
    # HttpClient retries once on a timeout and the stops are read one after
    # the other: an EFA outage holds a request (and the provider's lock) for
    # 2 × stops × this. Capped at 5 s in ``_transit_config`` for that reason.
    timeout_seconds: int = 4


@dataclass(slots=True)
class AppConfig:
    root_dir: Path
    app: AppSection
    weather: WeatherConfig
    calendar: CalendarConfig
    spotify: SpotifyConfig
    screensaver: ScreensaverConfig
    refresh_intervals: RefreshIntervalsConfig
    pi5_status: Pi5StatusConfig = field(default_factory=Pi5StatusConfig)
    transit: TransitConfig = field(default_factory=TransitConfig)

    @property
    def last_good_path(self) -> Path:
        return self.app.data_dir / "last_good.json"

    @property
    def screensaver_cache_dir(self) -> Path:
        cache_dir = Path(self.screensaver.cache_dir)
        if cache_dir.is_absolute():
            return cache_dir
        return self.app.data_dir / cache_dir

    @property
    def screensaver_manifest_path(self) -> Path:
        return self.screensaver_cache_dir / "manifest.json"

    @property
    def screensaver_local_dir(self) -> Path:
        return self.app.data_dir / "screensaver-local"

    @property
    def default_config_path(self) -> Path:
        return self.root_dir / "config" / "default.yaml"


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    config_path: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    dotenv_path: str | Path | None = None,
    root_dir: str | Path | None = None,
) -> AppConfig:
    root = Path(root_dir or Path(__file__).resolve().parent.parent).resolve()
    base = _load_mapping(root / "config" / "default.yaml")

    selected_config = (
        Path(config_path)
        if config_path
        else Path(
            (env or os.environ).get(
                "SMART_DISPLAY_CONFIG", root / "config" / "default.yaml"
            )
        )
    )
    if selected_config.resolve() != (root / "config" / "default.yaml").resolve():
        _deep_merge(base, _load_mapping(selected_config))

    merged_env = dict(load_dotenv_values(dotenv_path or root / ".env"))
    merged_env.update(env or os.environ)
    _apply_env_overrides(base, merged_env)
    return load_config_from_mapping(base, root_dir=root)


def load_config_from_mapping(
    mapping: dict[str, Any], *, root_dir: str | Path | None = None
) -> AppConfig:
    root = Path(root_dir or Path(__file__).resolve().parent.parent).resolve()
    app_data = mapping.get("app", {})
    data_dir = Path(str(app_data.get("data_dir", "./data")))
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()

    return AppConfig(
        root_dir=root,
        app=AppSection(
            host=str(app_data.get("host", "127.0.0.1")),
            port=int(app_data.get("port", 8080)),
            locale=str(app_data.get("locale", "de-AT")),
            timezone=str(app_data.get("timezone", "Europe/Vienna")),
            data_dir=data_dir,
            log_level=str(app_data.get("log_level", "INFO")).upper(),
            demo_mode=parse_bool(app_data.get("demo_mode", False)),
            watch_face=normalize_watch_face(app_data.get("watch_face")),
            night_from=str(app_data.get("night_from", "22:30") or ""),
            night_to=str(app_data.get("night_to", "06:00") or ""),
            allow_shutdown=parse_bool(app_data.get("allow_shutdown", False)),
        ),
        weather=WeatherConfig(
            enabled=parse_bool(mapping.get("weather", {}).get("enabled", True)),
            provider=str(mapping.get("weather", {}).get("provider", "openmeteo")),
            label=str(mapping.get("weather", {}).get("label", "Zuhause")),
            latitude=float(mapping.get("weather", {}).get("latitude", 48.2082)),
            longitude=float(mapping.get("weather", {}).get("longitude", 16.3738)),
            api_key=_normalize_optional_str(mapping.get("weather", {}).get("api_key")),
            timeout_seconds=int(mapping.get("weather", {}).get("timeout_seconds", 10)),
            geosphere_enabled=parse_bool(
                mapping.get("weather", {}).get("geosphere_enabled", True)
            ),
            warnings_enabled=parse_bool(
                mapping.get("weather", {}).get("warnings_enabled", True)
            ),
        ),
        calendar=CalendarConfig(
            enabled=parse_bool(mapping.get("calendar", {}).get("enabled", False)),
            url=str(mapping.get("calendar", {}).get("url", "")),
            username=str(mapping.get("calendar", {}).get("username", "")),
            password=str(mapping.get("calendar", {}).get("password", "")),
            calendar_names=[
                str(item)
                for item in mapping.get("calendar", {}).get("calendar_names", []) or []
            ],
            timeout_seconds=int(
                mapping.get("calendar", {}).get("timeout_seconds", 10)
            ),
        ),
        spotify=SpotifyConfig(
            enabled=parse_bool(mapping.get("spotify", {}).get("enabled", False)),
            client_id=str(mapping.get("spotify", {}).get("client_id", "")),
            client_secret=str(mapping.get("spotify", {}).get("client_secret", "")),
            refresh_token=str(mapping.get("spotify", {}).get("refresh_token", "")),
            device_id=str(mapping.get("spotify", {}).get("device_id", "")),
            market=str(mapping.get("spotify", {}).get("market", "AT")),
            timeout_seconds=int(mapping.get("spotify", {}).get("timeout_seconds", 10)),
            playlists_limit=int(mapping.get("spotify", {}).get("playlists_limit", 50)),
        ),
        screensaver=ScreensaverConfig(
            enabled=parse_bool(mapping.get("screensaver", {}).get("enabled", True)),
            idle_timeout_seconds=int(
                mapping.get("screensaver", {}).get("idle_timeout_seconds", 120)
            ),
            image_duration_seconds=int(
                mapping.get("screensaver", {}).get("image_duration_seconds", 15)
            ),
            refresh_interval_seconds=int(
                mapping.get("screensaver", {}).get("refresh_interval_seconds", 1800)
            ),
            source_url=str(mapping.get("screensaver", {}).get("source_url", "")),
            cache_dir=str(mapping.get("screensaver", {}).get("cache_dir", "screensaver")),
            demo_images_enabled=parse_bool(
                mapping.get("screensaver", {}).get("demo_images_enabled", True)
            ),
            timeout_seconds=int(
                mapping.get("screensaver", {}).get("timeout_seconds", 15)
            ),
            images_per_tick=int(
                mapping.get("screensaver", {}).get("images_per_tick", 1)
            ),
        ),
        refresh_intervals=RefreshIntervalsConfig(
            weather_seconds=int(
                mapping.get("refresh_intervals", {}).get("weather_seconds", 900)
            ),
            calendar_seconds=int(
                mapping.get("refresh_intervals", {}).get("calendar_seconds", 300)
            ),
            spotify_seconds=int(
                mapping.get("refresh_intervals", {}).get("spotify_seconds", 10)
            ),
            lightroom_seconds=int(
                mapping.get("refresh_intervals", {}).get("lightroom_seconds", 1800)
            ),
            weather_hourly_seconds=int(
                mapping.get("refresh_intervals", {}).get("weather_hourly_seconds", 3600)
            ),
            weather_nowcast_seconds=int(
                mapping.get("refresh_intervals", {}).get("weather_nowcast_seconds", 900)
            ),
            weather_warnings_seconds=int(
                mapping.get("refresh_intervals", {}).get("weather_warnings_seconds", 300)
            ),
        ),
        pi5_status=_pi5_status_config(mapping.get("pi5_status") or {}),
        transit=_transit_config(mapping.get("transit") or {}),
    )


def _pi5_status_config(section: dict[str, Any]) -> Pi5StatusConfig:
    # An optional screen must never keep the app from starting: an empty or
    # garbled timeout ("PI5_STATUS_TIMEOUT_SECONDS=") falls back to the default.
    default = Pi5StatusConfig()
    try:
        timeout = int(section.get("timeout_seconds", default.timeout_seconds))
    except (TypeError, ValueError):
        timeout = default.timeout_seconds
    return Pi5StatusConfig(
        url=str(section.get("url", "") or "").strip(),
        timeout_seconds=timeout if timeout > 0 else default.timeout_seconds,
    )


def _transit_config(section: dict[str, Any]) -> TransitConfig:
    """#27. Like the pi5 screen, an optional row must never keep the app from
    starting: a garbled entry is dropped (and logged), a garbled window or
    day list switches the rows off instead of guessing."""
    default = TransitConfig()
    stops = _transit_stops(section.get("stops"))
    # An empty value ("TRANSIT_WINDOW=") means the default, as for every other
    # key here; only a garbled one switches the rows off.
    window = _transit_window(
        section.get("window") or f"{default.window_from}-{default.window_to}"
    )
    days_value = section.get("days")
    days = _transit_days(default.days if days_value in (None, "") else days_value)
    if stops and (window is None or not days):
        logger.warning("transit: window or days unparsable, departures stay off")
        stops = []
    window_from, window_to = window or (default.window_from, default.window_to)
    return TransitConfig(
        stops=stops,
        base_url=str(section.get("base_url") or default.base_url).strip().rstrip("/"),
        days=days or default.days,
        window_from=window_from,
        window_to=window_to,
        departures=_bounded_int(section.get("departures"), default.departures, 1, 6),
        timeout_seconds=_bounded_int(
            section.get("timeout_seconds"), default.timeout_seconds, 1, 5
        ),
    )


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _transit_stops(value: Any) -> list[TransitStop]:
    """``"60501350:70:Schiffswerft,60500097:46:Hafenportal:3"`` (env) or a
    list of ``{stop_id, line, label, walk_minutes}`` (YAML)."""
    if isinstance(value, str):
        entries: list[Any] = [part for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        entries = value
    else:
        return []
    stops: list[TransitStop] = []
    for entry in entries:
        if isinstance(entry, str):
            parts = [part.strip() for part in entry.split(":")]
            if len(parts) not in (3, 4):
                logger.warning("transit: ignoring stop %r (want id:line:label[:walk])", entry)
                continue
            raw = {
                "stop_id": parts[0],
                "line": parts[1],
                "label": parts[2],
                "walk_minutes": parts[3] if len(parts) == 4 else 0,
            }
        elif isinstance(entry, dict):
            raw = entry
        else:
            logger.warning("transit: ignoring stop %r", entry)
            continue
        stop_id = str(raw.get("stop_id") or "").strip()
        line = str(raw.get("line") or "").strip()
        label = str(raw.get("label") or "").strip()
        walk = _bounded_int(raw.get("walk_minutes", 0), -1, 0, 60)
        if not (stop_id.isdigit() and line and label) or walk < 0:
            logger.warning("transit: ignoring stop %r", entry)
            continue
        stops.append(TransitStop(stop_id=stop_id, line=line, label=label, walk_minutes=walk))
    return stops


def _transit_window(value: Any) -> tuple[str, str] | None:
    """``"06:00-10:00"`` → ``("06:00", "10:00")``. Must not wrap midnight."""
    text = str(value or "").strip()
    if text.count("-") != 1:
        return None
    start_text, end_text = (part.strip() for part in text.split("-"))
    start, end = parse_hhmm(start_text), parse_hhmm(end_text)
    if start is None or end is None or start >= end:
        return None
    return f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"


def _transit_days(value: Any) -> list[int]:
    """ISO weekdays: ``"1-5"``, ``"1,3,5"`` or ``[1, 2, 3]``. ``[]`` if any
    part is garbled — half a day list would be a guess."""
    if isinstance(value, list):
        parts: list[str] = [str(item) for item in value]
    else:
        parts = [part for part in str(value or "").split(",") if part.strip()]
    days: set[int] = set()
    for part in parts:
        part = part.strip()
        if "-" in part:
            low_text, _, high_text = part.partition("-")
            if not (low_text.strip().isdigit() and high_text.strip().isdigit()):
                return []
            low, high = int(low_text), int(high_text)
            if not 1 <= low <= high <= 7:
                return []
            days.update(range(low, high + 1))
        elif part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
        else:
            return []
    return sorted(days)


def _normalize_optional_str(value: Any) -> str | None:
    if value in (None, "", "null", "None"):
        return None
    return str(value)


def _load_mapping(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {}

    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if yaml is None:
            raise RuntimeError(
                f"Cannot parse {file_path}: install PyYAML for non-JSON YAML files."
            )
        data = yaml.safe_load(text)
        return data or {}


def load_dotenv_values(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}

    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _deep_merge(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _apply_env_overrides(config: dict[str, Any], env: dict[str, str]) -> None:
    mapping = {
        "APP_HOST": ("app", "host"),
        "APP_PORT": ("app", "port"),
        "APP_LOCALE": ("app", "locale"),
        "APP_TIMEZONE": ("app", "timezone"),
        "APP_DATA_DIR": ("app", "data_dir"),
        "APP_LOG_LEVEL": ("app", "log_level"),
        "APP_DEMO_MODE": ("app", "demo_mode"),
        "APP_WATCH_FACE": ("app", "watch_face"),
        "APP_NIGHT_FROM": ("app", "night_from"),
        "APP_NIGHT_TO": ("app", "night_to"),
        "APP_ALLOW_SHUTDOWN": ("app", "allow_shutdown"),
        "WEATHER_ENABLED": ("weather", "enabled"),
        "WEATHER_PROVIDER": ("weather", "provider"),
        "WEATHER_LABEL": ("weather", "label"),
        "WEATHER_LATITUDE": ("weather", "latitude"),
        "WEATHER_LONGITUDE": ("weather", "longitude"),
        "WEATHER_API_KEY": ("weather", "api_key"),
        "WEATHER_TIMEOUT_SECONDS": ("weather", "timeout_seconds"),
        "WEATHER_GEOSPHERE_ENABLED": ("weather", "geosphere_enabled"),
        "WEATHER_WARNINGS_ENABLED": ("weather", "warnings_enabled"),
        "CALENDAR_ENABLED": ("calendar", "enabled"),
        "CALENDAR_URL": ("calendar", "url"),
        "CALENDAR_USERNAME": ("calendar", "username"),
        "CALENDAR_PASSWORD": ("calendar", "password"),
        "CALENDAR_NAME": ("calendar", "calendar_names"),
        "CALENDAR_TIMEOUT_SECONDS": ("calendar", "timeout_seconds"),
        "SPOTIFY_ENABLED": ("spotify", "enabled"),
        "SPOTIFY_CLIENT_ID": ("spotify", "client_id"),
        "SPOTIFY_CLIENT_SECRET": ("spotify", "client_secret"),
        "SPOTIFY_REFRESH_TOKEN": ("spotify", "refresh_token"),
        "SPOTIFY_DEVICE_ID": ("spotify", "device_id"),
        "SPOTIFY_MARKET": ("spotify", "market"),
        "SPOTIFY_TIMEOUT_SECONDS": ("spotify", "timeout_seconds"),
        "SPOTIFY_PLAYLISTS_LIMIT": ("spotify", "playlists_limit"),
        "SCREENSAVER_ENABLED": ("screensaver", "enabled"),
        "SCREENSAVER_IDLE_TIMEOUT_SECONDS": ("screensaver", "idle_timeout_seconds"),
        "SCREENSAVER_IMAGE_DURATION_SECONDS": (
            "screensaver",
            "image_duration_seconds",
        ),
        "SCREENSAVER_REFRESH_INTERVAL_SECONDS": (
            "screensaver",
            "refresh_interval_seconds",
        ),
        "SCREENSAVER_SOURCE_URL": ("screensaver", "source_url"),
        "SCREENSAVER_CACHE_DIR": ("screensaver", "cache_dir"),
        "SCREENSAVER_DEMO_IMAGES_ENABLED": (
            "screensaver",
            "demo_images_enabled",
        ),
        "SCREENSAVER_TIMEOUT_SECONDS": ("screensaver", "timeout_seconds"),
        "SCREENSAVER_IMAGES_PER_TICK": ("screensaver", "images_per_tick"),
        "REFRESH_WEATHER_SECONDS": ("refresh_intervals", "weather_seconds"),
        "REFRESH_CALENDAR_SECONDS": ("refresh_intervals", "calendar_seconds"),
        "REFRESH_SPOTIFY_SECONDS": ("refresh_intervals", "spotify_seconds"),
        "REFRESH_LIGHTROOM_SECONDS": ("refresh_intervals", "lightroom_seconds"),
        "REFRESH_WEATHER_HOURLY_SECONDS": ("refresh_intervals", "weather_hourly_seconds"),
        "REFRESH_WEATHER_NOWCAST_SECONDS": ("refresh_intervals", "weather_nowcast_seconds"),
        "REFRESH_WEATHER_WARNINGS_SECONDS": ("refresh_intervals", "weather_warnings_seconds"),
        "PI5_STATUS_URL": ("pi5_status", "url"),
        "PI5_STATUS_TIMEOUT_SECONDS": ("pi5_status", "timeout_seconds"),
        "TRANSIT_STOPS": ("transit", "stops"),
        "TRANSIT_WINDOW": ("transit", "window"),
        "TRANSIT_DAYS": ("transit", "days"),
        "TRANSIT_DEPARTURES": ("transit", "departures"),
        "TRANSIT_BASE_URL": ("transit", "base_url"),
        "TRANSIT_TIMEOUT_SECONDS": ("transit", "timeout_seconds"),
    }

    for env_key, path in mapping.items():
        if env_key not in env:
            continue
        section, key = path
        config.setdefault(section, {})
        value: Any = env[env_key]
        if env_key == "CALENDAR_NAME":
            value = [part.strip() for part in str(value).split(",") if part.strip()]
        config[section][key] = value

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_display.watch_faces import normalize_watch_face


try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None


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


@dataclass(slots=True)
class AppConfig:
    root_dir: Path
    app: AppSection
    weather: WeatherConfig
    calendar: CalendarConfig
    spotify: SpotifyConfig
    screensaver: ScreensaverConfig
    refresh_intervals: RefreshIntervalsConfig

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
        ),
    )


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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Snapshot status (#19). Configuration and fetch health share one field:
#   disabled  source not set up / switched off — calm, never an error
#   empty     set up, no fetch has finished yet ("wird geladen")
#   ok        last fetch succeeded — with or without items
#   stale     first failure after good data (data kept, age shown)
#   error     repeated failure, or a failure without any good data yet
# ``updated_at`` is the time of the last good fetch; None means there never was
# one. Adding ``disabled`` was additive — no schema bump.
@dataclass(slots=True)
class ProviderSnapshot:
    status: str = "empty"
    updated_at: str | None = None
    stale_after_seconds: int = 0
    error_message: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "stale_after_seconds": self.stale_after_seconds,
            "error_message": self.error_message,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProviderSnapshot":
        data = data or {}
        return cls(
            status=str(data.get("status", "empty")),
            updated_at=data.get("updated_at"),
            stale_after_seconds=int(data.get("stale_after_seconds", 0) or 0),
            error_message=data.get("error_message"),
            source=data.get("source"),
        )


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


# One day of the Open-Meteo overview. The first three feed the home screen as
# before (day_label, condition_code, max/min); the rest is the detail screen's
# week bar (#22). ``icon`` is the shared display key (weather_codes.py), the
# day variant by design — a day symbol is not a night-time snapshot.
@dataclass(slots=True)
class WeatherForecastItem:
    day_label: str
    condition: str
    condition_code: int | None = None
    temperature_max_c: float | None = None
    temperature_min_c: float | None = None
    date: str = ""
    icon: str = ""
    precipitation_probability_max: int | None = None
    precipitation_sum_mm: float | None = None
    sunrise: str | None = None
    sunset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_label": self.day_label,
            "condition": self.condition,
            "condition_code": self.condition_code,
            "temperature_max_c": self.temperature_max_c,
            "temperature_min_c": self.temperature_min_c,
            "date": self.date,
            "icon": self.icon,
            "precipitation_probability_max": self.precipitation_probability_max,
            "precipitation_sum_mm": self.precipitation_sum_mm,
            "sunrise": self.sunrise,
            "sunset": self.sunset,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeatherForecastItem":
        return cls(
            day_label=str(data.get("day_label", "")),
            condition=str(data.get("condition", "")),
            condition_code=(
                int(data.get("condition_code"))
                if data.get("condition_code") is not None
                else None
            ),
            temperature_max_c=data.get("temperature_max_c"),
            temperature_min_c=data.get("temperature_min_c"),
            date=str(data.get("date", "") or ""),
            icon=str(data.get("icon", "") or ""),
            precipitation_probability_max=_opt_int(data.get("precipitation_probability_max")),
            precipitation_sum_mm=_opt_float(data.get("precipitation_sum_mm")),
            sunrise=_opt_str(data.get("sunrise")),
            sunset=_opt_str(data.get("sunset")),
        )


@dataclass(slots=True)
class WeatherState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    location_label: str = ""
    temperature_c: float | None = None
    apparent_temperature_c: float | None = None
    condition: str = ""
    condition_code: int | None = None
    forecast: list[WeatherForecastItem] = field(default_factory=list)
    # #22, additive: the detail screen's "now" block. Open-Meteo's current
    # values are 15-minute model data, not a station reading — ``observed_at``
    # is the model time they are valid for (UTC ISO).
    observed_at: str | None = None
    icon: str = ""
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    wind_gusts_kmh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "location_label": self.location_label,
            "temperature_c": self.temperature_c,
            "apparent_temperature_c": self.apparent_temperature_c,
            "condition": self.condition,
            "condition_code": self.condition_code,
            "forecast": [item.to_dict() for item in self.forecast],
            "observed_at": self.observed_at,
            "icon": self.icon,
            "wind_speed_kmh": self.wind_speed_kmh,
            "wind_direction_deg": self.wind_direction_deg,
            "wind_gusts_kmh": self.wind_gusts_kmh,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WeatherState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            location_label=str(data.get("location_label", "")),
            temperature_c=data.get("temperature_c"),
            apparent_temperature_c=data.get("apparent_temperature_c"),
            condition=str(data.get("condition", "")),
            condition_code=(
                int(data.get("condition_code"))
                if data.get("condition_code") is not None
                else None
            ),
            forecast=[
                WeatherForecastItem.from_dict(item)
                for item in data.get("forecast", []) or []
            ],
            observed_at=_opt_str(data.get("observed_at")),
            icon=str(data.get("icon", "") or ""),
            wind_speed_kmh=_opt_float(data.get("wind_speed_kmh")),
            wind_direction_deg=_opt_float(data.get("wind_direction_deg")),
            wind_gusts_kmh=_opt_float(data.get("wind_gusts_kmh")),
        )


# ---- Weather detail (#22) ----------------------------------------------------
# Every series below is its own section with its own snapshot, so one failing
# source degrades only its own part of the screen. Missing values stay None —
# never 0 mm, 0 %, 0 °C or calm. Times are UTC ISO strings.


# One hour of a forecast series. ``time`` is the end of the hour: temperature
# and wind speed are the value at that instant, precipitation and gusts belong
# to the hour before it (both sources document it that way).
@dataclass(slots=True)
class HourlyPoint:
    time: str
    temperature_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: int | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    wind_gusts_kmh: float | None = None
    icon: str = "unknown"
    condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "temperature_c": self.temperature_c,
            "precipitation_mm": self.precipitation_mm,
            "precipitation_probability": self.precipitation_probability,
            "wind_speed_kmh": self.wind_speed_kmh,
            "wind_direction_deg": self.wind_direction_deg,
            "wind_gusts_kmh": self.wind_gusts_kmh,
            "icon": self.icon,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HourlyPoint":
        return cls(
            time=str(data.get("time", "")),
            temperature_c=_opt_float(data.get("temperature_c")),
            precipitation_mm=_opt_float(data.get("precipitation_mm")),
            precipitation_probability=_opt_int(data.get("precipitation_probability")),
            wind_speed_kmh=_opt_float(data.get("wind_speed_kmh")),
            wind_direction_deg=_opt_float(data.get("wind_direction_deg")),
            wind_gusts_kmh=_opt_float(data.get("wind_gusts_kmh")),
            icon=str(data.get("icon", "unknown") or "unknown"),
            condition=str(data.get("condition", "") or ""),
        )


# A whole hourly series from one model. ``model_run`` is the model start
# (C-LAEF reference time; None for Open-Meteo, which does not expose one),
# ``valid_to`` the last hour the source delivered.
@dataclass(slots=True)
class HourlyForecastState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    model_run: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    points: list[HourlyPoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "model_run": self.model_run,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "points": [point.to_dict() for point in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "HourlyForecastState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            model_run=_opt_str(data.get("model_run")),
            valid_from=_opt_str(data.get("valid_from")),
            valid_to=_opt_str(data.get("valid_to")),
            points=[
                HourlyPoint.from_dict(item)
                for item in data.get("points", []) or []
                if isinstance(item, dict)
            ],
        )


# One INCA interval: the precipitation that falls in the 15 minutes ending at
# ``ends_at``.
@dataclass(slots=True)
class NowcastPoint:
    ends_at: str
    precipitation_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ends_at": self.ends_at, "precipitation_mm": self.precipitation_mm}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NowcastPoint":
        return cls(
            ends_at=str(data.get("ends_at", "")),
            precipitation_mm=_opt_float(data.get("precipitation_mm")),
        )


@dataclass(slots=True)
class NowcastState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    model_run: str | None = None
    interval_minutes: int = 15
    points: list[NowcastPoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "model_run": self.model_run,
            "interval_minutes": self.interval_minutes,
            "points": [point.to_dict() for point in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NowcastState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            model_run=_opt_str(data.get("model_run")),
            interval_minutes=_opt_int(data.get("interval_minutes")) or 15,
            points=[
                NowcastPoint.from_dict(item)
                for item in data.get("points", []) or []
                if isinstance(item, dict)
            ],
        )


# An official GeoSphere warning for the configured municipality. ``ends_at``
# is exclusive; None means the source gave no usable end (see
# weather_detail.warning_effective_end for the bounded rule that applies then).
@dataclass(slots=True)
class WeatherWarning:
    id: str
    version: int | None = None
    type: int | None = None
    type_label: str = ""
    level: int | None = None
    level_label: str = ""
    begins_at: str | None = None
    ends_at: str | None = None
    text: str = ""
    impacts: str = ""
    recommendations: str = ""
    meteo_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "type": self.type,
            "type_label": self.type_label,
            "level": self.level,
            "level_label": self.level_label,
            "begins_at": self.begins_at,
            "ends_at": self.ends_at,
            "text": self.text,
            "impacts": self.impacts,
            "recommendations": self.recommendations,
            "meteo_text": self.meteo_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeatherWarning":
        return cls(
            id=str(data.get("id", "")),
            version=_opt_int(data.get("version")),
            type=_opt_int(data.get("type")),
            type_label=str(data.get("type_label", "") or ""),
            level=_opt_int(data.get("level")),
            level_label=str(data.get("level_label", "") or ""),
            begins_at=_opt_str(data.get("begins_at")),
            ends_at=_opt_str(data.get("ends_at")),
            text=str(data.get("text", "") or ""),
            impacts=str(data.get("impacts", "") or ""),
            recommendations=str(data.get("recommendations", "") or ""),
            meteo_text=str(data.get("meteo_text", "") or ""),
        )


# ``ok`` with no items is a confirmed all-clear — the only state that may say
# "Keine Warnungen". The municipality is what the point query resolved; it is
# kept so a changed answer is noticed and the screen can name the place.
@dataclass(slots=True)
class WarningsState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    municipality_id: str | None = None
    municipality_name: str = ""
    items: list[WeatherWarning] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "municipality_id": self.municipality_id,
            "municipality_name": self.municipality_name,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WarningsState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            municipality_id=_opt_str(data.get("municipality_id")),
            municipality_name=str(data.get("municipality_name", "") or ""),
            items=[
                WeatherWarning.from_dict(item)
                for item in data.get("items", []) or []
                if isinstance(item, dict)
            ],
        )


@dataclass(slots=True)
class CalendarEventItem:
    title: str
    starts_at: str
    ends_at: str | None
    time_label: str
    all_day: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "time_label": self.time_label,
            "all_day": self.all_day,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalendarEventItem":
        return cls(
            title=str(data.get("title", "")),
            starts_at=str(data.get("starts_at", "")),
            ends_at=data.get("ends_at"),
            time_label=str(data.get("time_label", "")),
            all_day=bool(data.get("all_day", False)),
        )


@dataclass(slots=True)
class CalendarDaySection:
    day_key: str
    section_date: str  # ISO date, e.g. "2026-04-08" — client computes the label
    items: list[CalendarEventItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_key": self.day_key,
            "section_date": self.section_date,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalendarDaySection":
        return cls(
            day_key=str(data.get("day_key", "")),
            section_date=str(data.get("section_date", "")),
            items=[
                CalendarEventItem.from_dict(item)
                for item in data.get("items", []) or []
            ],
        )


@dataclass(slots=True)
class CalendarState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    items: list[CalendarEventItem] = field(default_factory=list)
    sections: list[CalendarDaySection] = field(default_factory=list)
    empty_message: str = "Keine Termine heute."

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "sections": [section.to_dict() for section in self.sections],
            "empty_message": self.empty_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CalendarState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            items=[
                CalendarEventItem.from_dict(item) for item in data.get("items", []) or []
            ],
            sections=[
                CalendarDaySection.from_dict(item)
                for item in data.get("sections", []) or []
            ],
            empty_message=str(data.get("empty_message", "Keine Termine heute.")),
        )


@dataclass(slots=True)
class SpotifyState:
    snapshot: ProviderSnapshot = field(default_factory=ProviderSnapshot)
    connected: bool = False
    is_playing: bool = False
    track_title: str = ""
    artist_name: str = ""
    album_name: str = ""
    album_art_url: str | None = None
    device_name: str | None = None
    device_type: str | None = None
    volume_percent: int | None = None
    supports_volume: bool = False
    can_control: bool = False
    progress_ms: int | None = None
    duration_ms: int | None = None
    empty_message: str = "Derzeit keine Wiedergabe."

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "connected": self.connected,
            "is_playing": self.is_playing,
            "track_title": self.track_title,
            "artist_name": self.artist_name,
            "album_name": self.album_name,
            "album_art_url": self.album_art_url,
            "device_name": self.device_name,
            "device_type": self.device_type,
            "volume_percent": self.volume_percent,
            "supports_volume": self.supports_volume,
            "can_control": self.can_control,
            "progress_ms": self.progress_ms,
            "duration_ms": self.duration_ms,
            "empty_message": self.empty_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SpotifyState":
        data = data or {}
        return cls(
            snapshot=ProviderSnapshot.from_dict(data.get("snapshot")),
            connected=bool(data.get("connected", False)),
            is_playing=bool(data.get("is_playing", False)),
            track_title=str(data.get("track_title", "")),
            artist_name=str(data.get("artist_name", "")),
            album_name=str(data.get("album_name", "")),
            album_art_url=data.get("album_art_url"),
            device_name=data.get("device_name"),
            device_type=data.get("device_type"),
            volume_percent=(
                int(data.get("volume_percent"))
                if data.get("volume_percent") is not None
                else None
            ),
            supports_volume=bool(data.get("supports_volume", False)),
            can_control=bool(data.get("can_control", False)),
            progress_ms=(
                int(data.get("progress_ms"))
                if data.get("progress_ms") is not None
                else None
            ),
            duration_ms=(
                int(data.get("duration_ms"))
                if data.get("duration_ms") is not None
                else None
            ),
            empty_message=str(data.get("empty_message", "Derzeit keine Wiedergabe.")),
        )


@dataclass(slots=True)
class SystemState:
    locale: str
    timezone: str
    idle_timeout_seconds: int
    screensaver_interval_seconds: int
    generated_at: str = field(default_factory=utcnow_iso)
    screensaver_photo_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "timezone": self.timezone,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "screensaver_interval_seconds": self.screensaver_interval_seconds,
            "generated_at": self.generated_at,
            "screensaver_photo_count": self.screensaver_photo_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SystemState":
        data = data or {}
        return cls(
            locale=str(data.get("locale", "de-AT")),
            timezone=str(data.get("timezone", "Europe/Vienna")),
            idle_timeout_seconds=int(data.get("idle_timeout_seconds", 120)),
            screensaver_interval_seconds=int(
                data.get("screensaver_interval_seconds", 15)
            ),
            generated_at=str(data.get("generated_at", utcnow_iso())),
            screensaver_photo_count=int(data.get("screensaver_photo_count", 0)),
        )


@dataclass(slots=True)
class TimerState:
    """A countdown timer (Ticket #3). ``ends_at`` is wall-clock epoch seconds —
    browser and backend share the Pi's clock, so the UI counts down from it
    without any round trip. Status is derived, never stored:
    running (now < ends_at) → ringing (until dismissed or RING_TIMEOUT) → done."""

    id: str
    label: str
    duration_seconds: int
    ends_at: float
    created_at: float
    dismissed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "duration_seconds": self.duration_seconds,
            "ends_at": self.ends_at,
            "created_at": self.created_at,
            "dismissed": self.dismissed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimerState":
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            duration_seconds=int(data.get("duration_seconds", 0) or 0),
            ends_at=float(data.get("ends_at", 0.0) or 0.0),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            dismissed=bool(data.get("dismissed", False)),
        )


@dataclass(slots=True)
class PhotoManifestEntry:
    id: str
    source_url: str
    local_path: str
    public_path: str
    width: int = 1024
    height: int = 600
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    # Fingerprint of the Lightroom develop settings (#15). None = written
    # before #15, or a source that has no notion of edits.
    source_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_url": self.source_url,
            "local_path": self.local_path,
            "public_path": self.public_path,
            "width": self.width,
            "height": self.height,
            "content_hash": self.content_hash,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "source_version": self.source_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PhotoManifestEntry":
        return cls(
            id=str(data.get("id", "")),
            source_url=str(data.get("source_url", "")),
            local_path=str(data.get("local_path", "")),
            public_path=str(data.get("public_path", "")),
            width=int(data.get("width", 1024)),
            height=int(data.get("height", 600)),
            content_hash=data.get("content_hash"),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            source_version=data.get("source_version"),
        )


# Bump on any breaking change to the DashboardState shape (field renamed,
# nested dataclass removed, semantics changed). On load, a mismatch is
# surfaced via IncompatibleSchemaError so StateStore can quarantine the
# stale cache file and boot from a fresh empty state instead of limping
# along with a half-populated dashboard. Additive changes (new optional
# field with a default) do NOT need a bump.
DASHBOARD_SCHEMA_VERSION = 1


class IncompatibleSchemaError(ValueError):
    """Raised when ``last_good.json`` was written by a different schema.

    The caller — ``StateStore._load_initial_state`` — quarantines the file
    and boots from empty so the UI still comes up.
    """


@dataclass(slots=True)
class DashboardState:
    weather: WeatherState
    calendar: CalendarState
    spotify: SpotifyState
    system: SystemState
    # Additive (default []) so a last_good.json written before timers existed
    # still loads — no schema bump needed, see the note above the constant.
    timers: list[TimerState] = field(default_factory=list)
    # #22, additive in the same way: the weather detail sources. Both hourly
    # series are kept as last known good; /api/state sends only the one
    # weather_detail.select_hourly picks.
    weather_hourly_geosphere: HourlyForecastState = field(
        default_factory=lambda: HourlyForecastState(
            snapshot=ProviderSnapshot(status="empty", source="geosphere-claef")
        )
    )
    weather_hourly_openmeteo: HourlyForecastState = field(
        default_factory=lambda: HourlyForecastState(
            snapshot=ProviderSnapshot(status="empty", source="open-meteo")
        )
    )
    weather_nowcast: NowcastState = field(
        default_factory=lambda: NowcastState(
            snapshot=ProviderSnapshot(status="empty", source="geosphere-inca")
        )
    )
    weather_warnings: WarningsState = field(
        default_factory=lambda: WarningsState(
            snapshot=ProviderSnapshot(status="empty", source="geosphere-warnings")
        )
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "weather": self.weather.to_dict(),
            "calendar": self.calendar.to_dict(),
            "spotify": self.spotify.to_dict(),
            "system": self.system.to_dict(),
            "timers": [timer.to_dict() for timer in self.timers],
            "weather_hourly_geosphere": self.weather_hourly_geosphere.to_dict(),
            "weather_hourly_openmeteo": self.weather_hourly_openmeteo.to_dict(),
            "weather_nowcast": self.weather_nowcast.to_dict(),
            "weather_warnings": self.weather_warnings.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DashboardState":
        data = data or {}
        # Absence of ``schema_version`` is treated as "pre-versioning" — i.e.
        # caches written by the V1-release code are ALWAYS tagged, anything
        # without a tag is a payload from before this field existed. Since
        # V1 is our first shipping version we treat untagged == compatible
        # (the fields are the same). Once we ship V1, bumping the constant
        # will start rejecting untagged caches on upgrade.
        version = data.get("schema_version")
        if version is not None and version != DASHBOARD_SCHEMA_VERSION:
            raise IncompatibleSchemaError(
                f"dashboard schema_version {version!r} does not match "
                f"runtime {DASHBOARD_SCHEMA_VERSION}"
            )
        return cls(
            weather=WeatherState.from_dict(data.get("weather")),
            calendar=CalendarState.from_dict(data.get("calendar")),
            spotify=SpotifyState.from_dict(data.get("spotify")),
            system=SystemState.from_dict(data.get("system")),
            timers=[
                TimerState.from_dict(item)
                for item in data.get("timers", []) or []
                if isinstance(item, dict)
            ],
            **_detail_sections(data),
        )


def _detail_sections(data: dict[str, Any]) -> dict[str, Any]:
    """#22: a cache from before the detail screen has none of these keys —
    leave them to the dataclass defaults (``empty``) rather than build a
    snapshot without a source."""
    sections: dict[str, Any] = {}
    for key, loader in (
        ("weather_hourly_geosphere", HourlyForecastState.from_dict),
        ("weather_hourly_openmeteo", HourlyForecastState.from_dict),
        ("weather_nowcast", NowcastState.from_dict),
        ("weather_warnings", WarningsState.from_dict),
    ):
        if isinstance(data.get(key), dict):
            sections[key] = loader(data[key])
    return sections

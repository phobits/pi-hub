"""GeoSphere Austria sources for the weather detail screen (#22).

Three providers, three sections, one point query each — never a raster:

* ``ClaefProvider`` → ``weather_hourly_geosphere``: C-LAEF ``nwp-v2-1h-1km``
  hourly forecast (2t, 10u, 10v, 10fg, tp, sy), today and tomorrow.
* ``IncaProvider`` → ``weather_nowcast``: INCA ``nowcast-v1-15min-1km``
  precipitation ``rr`` per 15 minutes, three hours ahead.
* ``WarningsProvider`` → ``weather_warnings``: official warnings for the
  municipality the warn API resolves for the configured coordinates.

Endpoints, parameters and units were checked against the live APIs and their
metadata on 2026-10-26 (fixtures in tests/fixtures/weather). Licence of all
three: CC BY 4.0, "Datenquelle: GeoSphere Austria".
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib import parse as urllib_parse
from zoneinfo import ZoneInfo

from smart_display.config import AppConfig
from smart_display.http_client import HttpClient
from smart_display.models import (
    HourlyForecastState,
    HourlyPoint,
    NowcastPoint,
    NowcastState,
    WarningsState,
    WeatherWarning,
)
from smart_display.providers._fetch import Backoff, FetchError, fetch_json
from smart_display.providers._parsing import safe_float, safe_get, safe_index, safe_int
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore
from smart_display.sun import is_daylight
from smart_display.weather_codes import geosphere_condition

DATASET_URL = "https://dataset.api.hub.geosphere.at/v1/timeseries/forecast"
WARNINGS_URL = "https://warnungen.zamg.at/wsapp/api/getWarningsForCoords"
CLAEF_RESOURCE = "nwp-v2-1h-1km"
INCA_RESOURCE = "nowcast-v1-15min-1km"
CLAEF_PARAMETERS = "2t,10u,10v,10fg,tp,sy"
# The warn API's local times ("27.03.2023 08:00") are Austrian wall clock.
WARN_ZONE = ZoneInfo("Europe/Vienna")
MS_TO_KMH = 3.6


def parse_utc(value: object) -> datetime | None:
    """GeoSphere timestamps ("2026-10-26T09:00+00:00") as aware UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None  # an unzoned stamp is ambiguous — do not guess
    return moment.astimezone(timezone.utc)


def wind_from_components(u: float | None, v: float | None) -> tuple[float | None, float | None]:
    """Speed (km/h) and meteorological direction (degrees the wind comes
    *from*, 0 = north) from the eastward/northward components (m/s)."""
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v) * MS_TO_KMH
    if speed < 0.05:
        return 0.0, None  # calm has no direction
    direction = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return speed, direction


class _DatasetProvider(BaseProvider):
    """Shared plumbing of the two Dataset-API point queries."""

    resource: str
    label: str

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        refresh_interval_seconds: int,
        http_client: HttpClient | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(refresh_interval_seconds)
        self.config = config
        self.state_store = state_store
        self._http = http_client or HttpClient()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._backoff = Backoff(refresh_interval_seconds, monotonic=monotonic)

    @property
    def enabled(self) -> bool:
        return self.config.weather.enabled and self.config.weather.geosphere_enabled

    def parameters(self) -> str:
        raise NotImplementedError

    def request_url(self) -> str:
        # lat_lon is latitude first; without ``start`` the API answers from
        # the current hour (INCA: the next interval) of the latest run, so
        # the URL stays the same between calls.
        query = urllib_parse.urlencode(
            {
                "lat_lon": f"{self.config.weather.latitude},{self.config.weather.longitude}",
                "parameters": self.parameters(),
                "output_format": "geojson",
            }
        )
        return f"{DATASET_URL}/{self.resource}?{query}"

    def _empty_state(self, status: str, message: str | None = None):
        raise NotImplementedError

    def refresh(self) -> None:
        if self.config.app.demo_mode and not self.config.weather.enabled:
            return
        if not self.enabled:
            self.state_store.update_section(self.section_name, self._empty_state("disabled"))
            return
        if not self._backoff.ready():
            self.logger.info("%s skipped, backing off %.0f s", self.label, self._backoff.blocked_for)
            return
        try:
            payload = fetch_json(
                self._http,
                self.request_url(),
                timeout=self.config.weather.timeout_seconds,
                label=self.label,
            )
            state = self._parse(payload, self._now())
        except FetchError as exc:
            self._backoff.failure(exc)
            if exc.status == 400 and "outside" in exc.detail:
                # Not an outage: the coordinates lie outside the model area.
                # Calm "not set up", the screen falls back to Open-Meteo.
                self.logger.info("%s: location outside the dataset", self.label)
                self.state_store.update_section(
                    self.section_name,
                    self._empty_state("disabled", "Standort außerhalb des GeoSphere-Gebiets"),
                )
                return
            self.logger.warning("%s fetch failed: %s", self.label, exc)
            self.state_store.mark_error(
                self.section_name,
                error_message=str(exc),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
                transient=exc.transient,
            )
            return
        except Exception as exc:  # noqa: BLE001 — surface any parse drift
            self.logger.warning("%s payload parse failed: %s", self.label, exc)
            self.state_store.mark_error(
                self.section_name,
                error_message=f"{self.label}: ungültige Antwort ({exc})",
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return
        self._backoff.success()
        self.state_store.update_section(self.section_name, state)

    def _parse(self, payload: object, now: datetime):
        raise NotImplementedError

    def _columns(self, payload: object) -> tuple[list[datetime | None], dict, str | None]:
        """Timestamps, parameter columns and model run, or a FetchError for a
        payload that is not a usable point series."""
        if not isinstance(payload, dict):
            raise FetchError(f"{self.label}: Antwort ist kein Objekt")
        stamps = payload.get("timestamps")
        parameters = safe_get(payload, "features", default=None)
        parameters = safe_get(safe_index(parameters, 0), "properties", "parameters")
        if not isinstance(stamps, list) or not stamps or not isinstance(parameters, dict):
            raise FetchError(f"{self.label}: Antwort ohne Zeitreihe")
        run = parse_utc(payload.get("reference_time"))
        return [parse_utc(stamp) for stamp in stamps], parameters, run.isoformat() if run else None


def _column(parameters: dict, name: str, unit: str | None = None) -> list:
    entry = parameters.get(name)
    if not isinstance(entry, dict):
        return []
    if unit is not None and entry.get("unit") not in (None, unit):
        # A changed unit is a contract change, not something to convert
        # silently — the column counts as missing.
        return []
    data = entry.get("data")
    return data if isinstance(data, list) else []


class ClaefProvider(_DatasetProvider):
    section_name = "weather_hourly_geosphere"
    source_name = "geosphere-claef"
    resource = CLAEF_RESOURCE
    label = "GeoSphere C-LAEF"

    def __init__(self, config: AppConfig, state_store: StateStore, http_client: HttpClient | None = None, **kwargs) -> None:
        super().__init__(
            config,
            state_store,
            config.refresh_intervals.weather_hourly_seconds,
            http_client,
            **kwargs,
        )

    def parameters(self) -> str:
        return CLAEF_PARAMETERS

    def _empty_state(self, status: str, message: str | None = None) -> HourlyForecastState:
        return HourlyForecastState(snapshot=self.snapshot(status=status, error_message=message))

    def _parse(self, payload: object, now: datetime) -> HourlyForecastState:
        times, parameters, run = self._columns(payload)
        temps = _column(parameters, "2t", "degree Celsius")
        east = _column(parameters, "10u", "m s-1")
        north = _column(parameters, "10v", "m s-1")
        gusts = _column(parameters, "10fg", "m s-1")
        precip = _column(parameters, "tp", "kg m-2")
        symbols = _column(parameters, "sy")

        zone = ZoneInfo(self.config.app.timezone)
        last_day = now.astimezone(zone).date() + timedelta(days=1)
        hour_start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        lat, lon = self.config.weather.latitude, self.config.weather.longitude
        points: list[HourlyPoint] = []
        for index, moment in enumerate(times):
            if moment is None or moment < hour_start:
                continue
            # The hour ending at local midnight still belongs to its day.
            if (moment - timedelta(minutes=1)).astimezone(zone).date() > last_day:
                break
            speed, direction = wind_from_components(
                safe_float(safe_index(east, index)), safe_float(safe_index(north, index))
            )
            gust = safe_float(safe_index(gusts, index))
            icon, label = geosphere_condition(
                safe_index(symbols, index), is_day=is_daylight(moment, lat, lon)
            )
            points.append(
                HourlyPoint(
                    time=moment.isoformat(),
                    temperature_c=safe_float(safe_index(temps, index)),
                    precipitation_mm=_non_negative(safe_float(safe_index(precip, index))),
                    # C-LAEF has no probability in this dataset; never invent one.
                    precipitation_probability=None,
                    wind_speed_kmh=speed,
                    wind_direction_deg=direction,
                    wind_gusts_kmh=gust * MS_TO_KMH if gust is not None else None,
                    icon=icon,
                    condition=label,
                )
            )
        if not any(point.temperature_c is not None for point in points):
            raise FetchError(f"{self.label}: keine gültigen Stundenwerte")
        return HourlyForecastState(
            snapshot=self.snapshot(status="ok"),
            model_run=run,
            valid_from=points[0].time,
            valid_to=points[-1].time,
            points=points,
        )


class IncaProvider(_DatasetProvider):
    section_name = "weather_nowcast"
    source_name = "geosphere-inca"
    resource = INCA_RESOURCE
    label = "GeoSphere INCA"

    def __init__(self, config: AppConfig, state_store: StateStore, http_client: HttpClient | None = None, **kwargs) -> None:
        super().__init__(
            config,
            state_store,
            config.refresh_intervals.weather_nowcast_seconds,
            http_client,
            **kwargs,
        )

    def parameters(self) -> str:
        return "rr"

    def _empty_state(self, status: str, message: str | None = None) -> NowcastState:
        return NowcastState(snapshot=self.snapshot(status=status, error_message=message))

    def _parse(self, payload: object, now: datetime) -> NowcastState:
        times, parameters, run = self._columns(payload)
        amounts = _column(parameters, "rr", "kg m-2")
        # ``rr`` is the sum per 15-minute step. The metadata does not say
        # which end of the step a timestamp marks; like C-LAEF's "in the
        # last forecast interval" it is read as the end (README, Quellen).
        points = [
            NowcastPoint(
                ends_at=moment.isoformat(),
                precipitation_mm=_non_negative(safe_float(safe_index(amounts, index))),
            )
            for index, moment in enumerate(times)
            if moment is not None
        ]
        if not any(point.precipitation_mm is not None for point in points):
            raise FetchError(f"{self.label}: keine gültigen Niederschlagswerte")
        return NowcastState(
            snapshot=self.snapshot(status="ok"),
            model_run=run,
            interval_minutes=15,
            points=points,
        )


def _non_negative(value: float | None) -> float | None:
    """A negative amount is a broken value, not "less than nothing"."""
    if value is None or value < 0:
        return None
    return value


WARN_TYPES = {
    1: "Wind",
    2: "Regen",
    3: "Schnee",
    4: "Glatteis",
    5: "Gewitter",
    6: "Hitze",
    7: "Kälte",
}
WARN_LEVELS = {1: "Gelb", 2: "Orange", 3: "Rot"}


def _epoch(value: object) -> datetime | None:
    seconds = safe_int(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _warn_local(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    return moment.replace(tzinfo=WARN_ZONE).astimezone(timezone.utc)


def parse_warning(raw: object) -> WeatherWarning | None:
    """One warn-API feature. The Unix stamps in ``rawinfo`` are the precise
    validity; the local ``begin``/``end`` strings are the fallback."""
    props = safe_get(raw, "properties")
    if not isinstance(props, dict):
        return None
    warn_id = props.get("warnid")
    if warn_id is None:
        return None
    raw_info = props.get("rawinfo") if isinstance(props.get("rawinfo"), dict) else {}
    begins = _epoch(raw_info.get("start")) or _warn_local(props.get("begin"))
    ends = _epoch(raw_info.get("end")) or _warn_local(props.get("end"))
    if begins is not None and ends is not None and ends <= begins:
        ends = None  # an inverted window is no window; the bounded rule applies
    warn_type = safe_int(props.get("warntypid"))
    if warn_type is None:
        warn_type = safe_int(raw_info.get("wtype"))
    level = safe_int(props.get("warnstufeid"))
    if level is None:
        level = safe_int(raw_info.get("wlevel"))
    if level not in WARN_LEVELS:
        level = None
    return WeatherWarning(
        id=str(warn_id),
        version=safe_int(props.get("chgid")),
        type=warn_type,
        type_label=WARN_TYPES.get(warn_type, "Wetter"),
        level=level,
        level_label=WARN_LEVELS.get(level, ""),
        begins_at=begins.isoformat() if begins else None,
        ends_at=ends.isoformat() if ends else None,
        text=str(props.get("text") or ""),
        impacts=str(props.get("auswirkungen") or ""),
        recommendations=str(props.get("empfehlungen") or ""),
        meteo_text=str(props.get("meteotext") or ""),
    )


class WarningsProvider(BaseProvider):
    section_name = "weather_warnings"
    source_name = "geosphere-warnings"
    label = "GeoSphere Warnungen"

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        http_client: HttpClient | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(config.refresh_intervals.weather_warnings_seconds)
        self.config = config
        self.state_store = state_store
        self._http = http_client or HttpClient()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._backoff = Backoff(self.refresh_interval_seconds, monotonic=monotonic)

    @property
    def enabled(self) -> bool:
        return self.config.weather.enabled and self.config.weather.warnings_enabled

    def request_url(self) -> str:
        # The municipality is resolved by the API from the point (its
        # documented getWarningsForCoords), never by matching a name.
        query = urllib_parse.urlencode(
            {
                "lon": self.config.weather.longitude,
                "lat": self.config.weather.latitude,
                "lang": "de",
            }
        )
        return f"{WARNINGS_URL}?{query}"

    def refresh(self) -> None:
        if self.config.app.demo_mode and not self.config.weather.enabled:
            return
        if not self.enabled:
            self.state_store.update_section(
                self.section_name, WarningsState(snapshot=self.snapshot(status="disabled"))
            )
            return
        if not self._backoff.ready():
            self.logger.info("warnings skipped, backing off %.0f s", self._backoff.blocked_for)
            return
        try:
            payload = fetch_json(
                self._http,
                self.request_url(),
                timeout=self.config.weather.timeout_seconds,
                label=self.label,
            )
            state = self._parse(payload, self._now())
        except FetchError as exc:
            self._backoff.failure(exc)
            if exc.status == 404 and "municipal" in exc.detail:
                # "Could not find municipal for coords": the point is not in
                # an Austrian municipality, so there is nothing to warn about
                # — calm, not an outage. Any other 404 (a moved endpoint, a
                # proxy) is an outage and must say so.
                self.logger.info("warnings: no municipality for the configured point")
                self.state_store.update_section(
                    self.section_name,
                    WarningsState(
                        snapshot=self.snapshot(
                            status="disabled",
                            error_message="Keine österreichische Gemeinde für diesen Standort",
                        )
                    ),
                )
                return
            self.logger.warning("warnings fetch failed: %s", exc)
            self._mark_error(str(exc), transient=exc.transient)
            return
        except Exception as exc:  # noqa: BLE001 — surface any parse drift
            self.logger.warning("warnings payload parse failed: %s", exc)
            self._mark_error(f"{self.label}: ungültige Antwort ({exc})", transient=True)
            return
        self._backoff.success()
        previous = self.state_store.get_state().weather_warnings.municipality_id
        if previous and previous != state.municipality_id:
            self.logger.warning(
                "warn municipality changed from %s to %s (%s)",
                previous,
                state.municipality_id,
                state.municipality_name,
            )
        self.state_store.update_section(self.section_name, state)

    def _mark_error(self, message: str, *, transient: bool) -> None:
        # The section keeps its last confirmed items; the client shows them
        # with their age until each one ends (weather_detail).
        self.state_store.mark_error(
            self.section_name,
            error_message=message,
            stale_after_seconds=self.refresh_interval_seconds * 2,
            source=self.source_name,
            transient=transient,
        )

    def _parse(self, payload: object, now: datetime) -> WarningsState:
        props = safe_get(payload, "properties")
        warnings = safe_get(props, "warnings")
        municipality = safe_get(props, "location", "properties")
        municipality_id = safe_get(municipality, "gemeindenr")
        # Only a list counts as an answer. A missing or malformed list is
        # not "no warnings" — that would be an all-clear nobody gave.
        if not isinstance(warnings, list) or municipality_id is None:
            raise FetchError(f"{self.label}: Antwort ohne Warnliste")
        newest: dict[str, WeatherWarning] = {}
        for raw in warnings:
            warning = parse_warning(raw)
            if warning is None:
                raise FetchError(f"{self.label}: unlesbare Warnung")
            known = newest.get(warning.id)
            if known is None or (warning.version or 0) >= (known.version or 0):
                newest[warning.id] = warning
        # Ended ones go only after the newest revision is known: a revision
        # that shortened a warning into the past must not let an older one
        # with the original end come back.
        current = []
        for warning in newest.values():
            ends = parse_utc(warning.ends_at)
            if ends is None or ends > now:  # end is exclusive
                current.append(warning)
        return WarningsState(
            snapshot=self.snapshot(status="ok"),
            municipality_id=str(municipality_id),
            municipality_name=str(safe_get(municipality, "name") or ""),
            items=current,
        )

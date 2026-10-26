from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from urllib import parse as urllib_parse
from zoneinfo import ZoneInfo

from smart_display.config import AppConfig
from smart_display.http_client import HttpClient
from smart_display.models import (
    HourlyForecastState,
    HourlyPoint,
    WeatherForecastItem,
    WeatherState,
)
from smart_display.providers._fetch import Backoff, FetchError, fetch_json
from smart_display.providers._parsing import (
    safe_float,
    safe_get,
    safe_index,
    safe_int,
)
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore
from smart_display.sun import is_daylight
from smart_display.weather_codes import WEEKDAY_SHORT, WMO_CODES, wmo_condition


# One table for every WMO label (#22): the home line and the week bar must
# not name the same code differently. The labels are the home screen's.
WEATHER_CODES = {code: label for code, (_icon, label) in WMO_CODES.items()}


# #22: seven local calendar days including today — the detail screen's week
# bar. The home screen still shows the first three.
FORECAST_DAYS = 7
# The hourly fallback series covers today and tomorrow, like C-LAEF.
HOURLY_DAYS = 2
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoProvider(BaseProvider):
    section_name = "weather"
    hourly_section = "weather_hourly_openmeteo"
    source_name = "open-meteo"

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        http_client: HttpClient | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        super().__init__(config.refresh_intervals.weather_seconds)
        self.config = config
        self.state_store = state_store
        self._http = http_client or HttpClient()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._backoff = Backoff(self.refresh_interval_seconds, monotonic=monotonic)

    def request_url(self) -> str:
        # Unix timestamps, not ISO: with ``timezone`` set, Open-Meteo's ISO
        # times are local wall-clock strings at one fixed offset, which
        # gives the 25-hour day of the autumn change 24 hours (verified
        # 2026-10-26 against the 25.10. window in tests/fixtures/weather).
        params = urllib_parse.urlencode(
            {
                "latitude": self.config.weather.latitude,
                "longitude": self.config.weather.longitude,
                "current": "temperature_2m,apparent_temperature,weather_code,"
                "wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum,sunrise,sunset",
                "hourly": "temperature_2m,precipitation,precipitation_probability,"
                "weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "timezone": self.config.app.timezone,
                "forecast_days": FORECAST_DAYS,
                "wind_speed_unit": "kmh",
                "timeformat": "unixtime",
            }
        )
        return f"{OPEN_METEO_URL}?{params}"

    def refresh(self) -> None:
        if self.config.app.demo_mode and not self.config.weather.enabled:
            return

        if not self.config.weather.enabled:
            self.state_store.update_section(
                self.section_name,
                WeatherState(
                    snapshot=self.snapshot(status="disabled"),
                    location_label=self.config.weather.label,
                ),
            )
            self.state_store.update_section(
                self.hourly_section,
                HourlyForecastState(snapshot=self.snapshot(status="disabled")),
            )
            return

        if not self._backoff.ready():
            self.logger.info("weather fetch skipped, backing off %.0f s", self._backoff.blocked_for)
            return

        try:
            payload = fetch_json(
                self._http,
                self.request_url(),
                timeout=self.config.weather.timeout_seconds,
                label="Open-Meteo",
            )
            if not isinstance(payload, dict) or payload.get("error"):
                raise FetchError(
                    f"Open-Meteo: {safe_get(payload, 'reason') or 'ungültige Antwort'}",
                    transient=False,
                )
            if not _has_values(payload):
                raise FetchError("Open-Meteo: Antwort ohne Wetterwerte")
        except FetchError as exc:
            self._backoff.failure(exc)
            self.logger.warning("weather fetch failed: %s", exc)
            self._mark_error(str(exc), transient=exc.transient)
            return

        try:
            state = self._build_state_from_payload(payload)
            hourly = self._build_hourly(payload, self._now())
        except Exception as exc:  # noqa: BLE001 — surface any parse drift
            self.logger.warning("weather payload parse failed: %s", exc)
            self._mark_error(f"Ungültige Wetter-Antwort: {exc}", transient=True)
            return

        self._backoff.success()
        self.state_store.update_section(self.section_name, state)
        if hourly.points:
            self.state_store.update_section(self.hourly_section, hourly)
        else:
            # A response without usable hours is a failed hourly fetch, not
            # a confirmed empty forecast.
            self.state_store.mark_error(
                self.hourly_section,
                error_message="Open-Meteo: keine Stundenwerte",
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )

    def _mark_error(self, message: str, *, transient: bool) -> None:
        for section in (self.section_name, self.hourly_section):
            self.state_store.mark_error(
                section,
                error_message=message,
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
                transient=transient,
            )

    def _build_state_from_payload(self, payload: dict) -> WeatherState:
        """Plan B13: build a WeatherState tolerantly. Missing or malformed
        sub-fields degrade gracefully instead of crashing the refresh."""
        current_code = safe_int(safe_get(payload, "current", "weather_code"))
        temperature = safe_float(safe_get(payload, "current", "temperature_2m"))
        apparent = safe_float(safe_get(payload, "current", "apparent_temperature"))
        is_day_raw = safe_int(safe_get(payload, "current", "is_day"))
        is_day = None if is_day_raw is None else bool(is_day_raw)
        offset = safe_int(safe_get(payload, "utc_offset_seconds")) or 0

        dates = safe_get(payload, "daily", "time", default=[]) or []
        codes = safe_get(payload, "daily", "weather_code", default=[]) or []
        max_values = safe_get(payload, "daily", "temperature_2m_max", default=[]) or []
        min_values = safe_get(payload, "daily", "temperature_2m_min", default=[]) or []
        probabilities = safe_get(payload, "daily", "precipitation_probability_max", default=[]) or []
        sums = safe_get(payload, "daily", "precipitation_sum", default=[]) or []
        sunrises = safe_get(payload, "daily", "sunrise", default=[]) or []
        sunsets = safe_get(payload, "daily", "sunset", default=[]) or []

        forecast: list[WeatherForecastItem] = []
        if isinstance(dates, list):
            for index, raw_day in enumerate(dates):
                day = _daily_date(raw_day, offset)
                code = safe_int(safe_index(codes, index))
                # A daily code is the day's summary: always the day icon.
                icon, label = wmo_condition(code, is_day=None)
                forecast.append(
                    WeatherForecastItem(
                        day_label=_short_day_label(index, day or str(raw_day)),
                        condition=label or "Unbekannt",
                        condition_code=code,
                        temperature_max_c=safe_float(safe_index(max_values, index)),
                        temperature_min_c=safe_float(safe_index(min_values, index)),
                        date=day or "",
                        icon=icon,
                        precipitation_probability_max=safe_int(safe_index(probabilities, index)),
                        precipitation_sum_mm=safe_float(safe_index(sums, index)),
                        sunrise=_unix_iso(safe_index(sunrises, index)),
                        sunset=_unix_iso(safe_index(sunsets, index)),
                    )
                )

        current_icon, _label = wmo_condition(current_code, is_day=is_day)
        return WeatherState(
            snapshot=self.snapshot(status="ok"),
            location_label=self.config.weather.label,
            temperature_c=temperature,
            apparent_temperature_c=apparent,
            condition=WEATHER_CODES.get(current_code, "Unbekannt") if current_code is not None else "Unbekannt",
            condition_code=current_code,
            forecast=forecast,
            observed_at=_unix_iso(safe_get(payload, "current", "time")),
            icon=current_icon,
            wind_speed_kmh=safe_float(safe_get(payload, "current", "wind_speed_10m")),
            wind_direction_deg=safe_float(safe_get(payload, "current", "wind_direction_10m")),
            wind_gusts_kmh=safe_float(safe_get(payload, "current", "wind_gusts_10m")),
        )

    def _build_hourly(self, payload: dict, now: datetime) -> HourlyForecastState:
        """Hours from the current one to the end of tomorrow (local), as the
        same contract the C-LAEF series uses. Open-Meteo documents
        precipitation, its probability and gusts for the preceding hour."""
        zone = ZoneInfo(self.config.app.timezone)
        times = safe_get(payload, "hourly", "time", default=[]) or []
        if not isinstance(times, list):
            return HourlyForecastState(snapshot=self.snapshot(status="ok"))
        column = lambda name: safe_get(payload, "hourly", name, default=[]) or []  # noqa: E731
        temps = column("temperature_2m")
        precip = column("precipitation")
        probability = column("precipitation_probability")
        codes = column("weather_code")
        speeds = column("wind_speed_10m")
        directions = column("wind_direction_10m")
        gusts = column("wind_gusts_10m")

        first_day = now.astimezone(zone).date()
        last_day = first_day + timedelta(days=HOURLY_DAYS - 1)
        hour_start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        points: list[HourlyPoint] = []
        for index, raw in enumerate(times):
            moment = _unix_datetime(raw)
            if moment is None or moment < hour_start:
                continue
            # The hour ending at local midnight still belongs to the day it ends.
            if (moment - timedelta(minutes=1)).astimezone(zone).date() > last_day:
                break
            is_day = is_daylight(moment, self.config.weather.latitude, self.config.weather.longitude)
            icon, label = wmo_condition(safe_int(safe_index(codes, index)), is_day=is_day)
            points.append(
                HourlyPoint(
                    time=moment.isoformat(),
                    temperature_c=safe_float(safe_index(temps, index)),
                    precipitation_mm=safe_float(safe_index(precip, index)),
                    precipitation_probability=safe_int(safe_index(probability, index)),
                    wind_speed_kmh=safe_float(safe_index(speeds, index)),
                    wind_direction_deg=safe_float(safe_index(directions, index)),
                    wind_gusts_kmh=safe_float(safe_index(gusts, index)),
                    icon=icon,
                    condition=label,
                )
            )
        points = [p for p in points if p.temperature_c is not None or p.precipitation_mm is not None]
        return HourlyForecastState(
            snapshot=self.snapshot(status="ok"),
            model_run=None,
            valid_from=points[0].time if points else None,
            valid_to=points[-1].time if points else None,
            points=points,
        )


def _has_values(payload: dict) -> bool:
    current = safe_float(safe_get(payload, "current", "temperature_2m"))
    days = safe_get(payload, "daily", "time", default=[]) or []
    return current is not None or (isinstance(days, list) and len(days) > 0)


def _unix_datetime(value: object) -> datetime | None:
    seconds = safe_float(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _unix_iso(value: object) -> str | None:
    moment = _unix_datetime(value)
    return moment.isoformat() if moment else None


def _daily_date(value: object, offset_seconds: int) -> str | None:
    """The local date of a daily entry. Unix days are Open-Meteo's local
    midnight at the response's fixed offset, so that offset gives the date
    back; an ISO date string is taken as it is."""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    moment = _unix_datetime(value)
    if moment is None:
        return None
    return (moment + timedelta(seconds=offset_seconds)).date().isoformat()


def _short_day_label(index: int, raw_label: str) -> str:
    if index == 0:
        return "Heute"
    if index == 1:
        return "Morgen"
    try:
        parsed = date.fromisoformat(raw_label)
    except ValueError:
        return raw_label
    return WEEKDAY_SHORT[parsed.weekday()]


def weather_icon_key(condition_code: int | None) -> str:
    if condition_code in {0, 1}:
        return "clear"
    if condition_code == 2:
        return "partly-cloudy"
    if condition_code == 3:
        return "cloudy"
    if condition_code in {45, 48}:
        return "fog"
    if condition_code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if condition_code in {95, 96, 99}:
        return "storm"
    if condition_code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain"
    return "cloudy"

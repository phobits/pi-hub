from __future__ import annotations

import json
from datetime import date
from urllib import parse as urllib_parse

from smart_display.config import AppConfig
from smart_display.http_client import HttpClient, HttpError
from smart_display.models import WeatherForecastItem, WeatherState
from smart_display.providers._parsing import (
    safe_float,
    safe_get,
    safe_index,
    safe_int,
)
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore


WEATHER_CODES = {
    0: "Klar",
    1: "Überwiegend klar",
    2: "Leicht bewölkt",
    3: "Bedeckt",
    45: "Nebel",
    48: "Reifnebel",
    51: "Leichter Nieselregen",
    53: "Nieselregen",
    55: "Starker Nieselregen",
    61: "Leichter Regen",
    63: "Regen",
    65: "Starker Regen",
    71: "Leichter Schnee",
    73: "Schnee",
    75: "Starker Schneefall",
    80: "Leichte Schauer",
    81: "Regenschauer",
    82: "Starke Schauer",
    95: "Gewitter",
    96: "Gewitter mit Hagel",
    99: "Starkes Gewitter",
}


class OpenMeteoProvider(BaseProvider):
    section_name = "weather"
    source_name = "open-meteo"

    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        http_client: HttpClient | None = None,
    ):
        super().__init__(config.refresh_intervals.weather_seconds)
        self.config = config
        self.state_store = state_store
        self._http = http_client or HttpClient()

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
            return

        try:
            params = urllib_parse.urlencode(
                {
                    "latitude": self.config.weather.latitude,
                    "longitude": self.config.weather.longitude,
                    "current": "temperature_2m,apparent_temperature,weather_code",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": self.config.app.timezone,
                    "forecast_days": 3,
                }
            )
            response = self._http.get(
                f"https://api.open-meteo.com/v1/forecast?{params}",
                timeout=self.config.weather.timeout_seconds,
            )
            if not response.ok:
                raise HttpError(
                    f"Open-Meteo antwortete mit {response.status}"
                )
            payload = response.json()
        except (HttpError, ValueError, json.JSONDecodeError, OSError) as exc:
            self.logger.warning("weather fetch failed: %s", exc)
            self.state_store.mark_error(
                self.section_name,
                error_message=str(exc),
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return

        try:
            state = self._build_state_from_payload(payload)
        except Exception as exc:  # noqa: BLE001 — surface any parse drift
            self.logger.warning("weather payload parse failed: %s", exc)
            self.state_store.mark_error(
                self.section_name,
                error_message=f"Ungültige Wetter-Antwort: {exc}",
                stale_after_seconds=self.refresh_interval_seconds * 2,
                source=self.source_name,
            )
            return

        self.state_store.update_section(self.section_name, state)

    def _build_state_from_payload(self, payload: dict) -> WeatherState:
        """Plan B13: build a WeatherState tolerantly. Missing or malformed
        sub-fields degrade gracefully instead of crashing the refresh."""
        current_code = safe_int(safe_get(payload, "current", "weather_code"))
        temperature = safe_float(safe_get(payload, "current", "temperature_2m"))
        apparent = safe_float(safe_get(payload, "current", "apparent_temperature"))

        dates = safe_get(payload, "daily", "time", default=[]) or []
        codes = safe_get(payload, "daily", "weather_code", default=[]) or []
        max_values = safe_get(payload, "daily", "temperature_2m_max", default=[]) or []
        min_values = safe_get(payload, "daily", "temperature_2m_min", default=[]) or []

        forecast: list[WeatherForecastItem] = []
        if isinstance(dates, list):
            for index, day_label in enumerate(dates):
                code = safe_int(safe_index(codes, index))
                forecast.append(
                    WeatherForecastItem(
                        day_label=_short_day_label(index, str(day_label)),
                        condition=WEATHER_CODES.get(code, "Unbekannt") if code is not None else "Unbekannt",
                        condition_code=code,
                        temperature_max_c=safe_float(safe_index(max_values, index)),
                        temperature_min_c=safe_float(safe_index(min_values, index)),
                    )
                )

        return WeatherState(
            snapshot=self.snapshot(status="ok"),
            location_label=self.config.weather.label,
            temperature_c=temperature,
            apparent_temperature_c=apparent,
            condition=WEATHER_CODES.get(current_code, "Unbekannt") if current_code is not None else "Unbekannt",
            condition_code=current_code,
            forecast=forecast,
        )


def _short_day_label(index: int, raw_label: str) -> str:
    if index == 0:
        return "Heute"
    if index == 1:
        return "Morgen"
    weekday_names = ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."]
    try:
        parsed = date.fromisoformat(raw_label)
    except ValueError:
        return raw_label
    return weekday_names[parsed.weekday()]


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

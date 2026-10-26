from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from smart_display.config import AppConfig
from smart_display.models import (
    CalendarEventItem,
    CalendarState,
    HourlyForecastState,
    HourlyPoint,
    NowcastPoint,
    NowcastState,
    SpotifyState,
    WarningsState,
    WeatherForecastItem,
    WeatherState,
)
from smart_display.providers.caldav_provider import build_calendar_sections
from smart_display.providers.base import BaseProvider
from smart_display.state_store import StateStore
from smart_display.sun import is_daylight, sun_times
from smart_display.weather_codes import geosphere_condition, wmo_condition


class MockProvider(BaseProvider):
    section_name = "mock"
    source_name = "demo"

    def __init__(self, config: AppConfig, state_store: StateStore):
        super().__init__(60)
        self.config = config
        self.state_store = state_store

    def refresh(self) -> None:
        if not self.config.app.demo_mode:
            return

        if not self.config.weather.enabled:
            now = datetime.now(timezone.utc)
            self.state_store.update_section("weather", self._demo_weather(now))
            self.state_store.update_section(
                "weather_hourly_geosphere", self._demo_hourly(now)
            )
            self.state_store.update_section("weather_nowcast", self._demo_nowcast(now))
            # A confirmed all-clear; scripts/screenshot-states.py injects
            # warnings where a state needs them.
            self.state_store.update_section(
                "weather_warnings",
                WarningsState(
                    snapshot=self.snapshot(status="ok", source="geosphere-warnings"),
                    municipality_id="90101",
                    municipality_name="Wien-Innere Stadt",
                ),
            )

        if not self.config.calendar.enabled:
            now = datetime.now(ZoneInfo(self.config.app.timezone)).replace(
                minute=0, second=0, microsecond=0
            )
            tomorrow = now + timedelta(days=1)
            day_after_tomorrow = now + timedelta(days=2)
            items = [
                CalendarEventItem(
                    title="Stand-up",
                    starts_at=now.isoformat(),
                    ends_at=(now + timedelta(minutes=30)).isoformat(),
                    time_label=f"{now:%H:%M}–{(now + timedelta(minutes=30)):%H:%M}",
                ),
                CalendarEventItem(
                    title="Abendessen",
                    starts_at=(now + timedelta(hours=3)).isoformat(),
                    ends_at=(now + timedelta(hours=5)).isoformat(),
                    time_label=f"{(now + timedelta(hours=3)):%H:%M}–{(now + timedelta(hours=5)):%H:%M}",
                ),
                CalendarEventItem(
                    title="Fokusblock",
                    starts_at=tomorrow.replace(hour=9).isoformat(),
                    ends_at=tomorrow.replace(hour=10, minute=30).isoformat(),
                    time_label=f"{tomorrow.replace(hour=9):%H:%M}–{tomorrow.replace(hour=10, minute=30):%H:%M}",
                ),
                CalendarEventItem(
                    title="Lieferung",
                    starts_at=day_after_tomorrow.replace(hour=0, minute=0).isoformat(),
                    ends_at=day_after_tomorrow.replace(hour=23, minute=59).isoformat(),
                    time_label="Ganztägig",
                    all_day=True,
                ),
            ]
            self.state_store.update_section(
                "calendar",
                CalendarState(
                    snapshot=self.snapshot(status="ok"),
                    items=items,
                    sections=build_calendar_sections(
                        items,
                        timezone_name=self.config.app.timezone,
                        base_date=now.date(),
                        days=3,
                    ),
                ),
            )

        if not self.config.spotify.enabled:
            self.state_store.update_section(
                "spotify",
                SpotifyState(
                    snapshot=self.snapshot(status="ok"),
                    connected=True,
                    is_playing=True,
                    track_title="Everything In Its Right Place",
                    artist_name="Radiohead",
                    album_name="Kid A",
                    can_control=True,
                    device_name="Wohnzimmer",
                    device_type="Speaker",
                    volume_percent=42,
                    supports_volume=True,
                    progress_ms=72_000,
                    duration_ms=251_000,
                ),
            )

    # ---- Weather detail demo (#22) -------------------------------------------
    # Deterministic shapes around "now" so the demo server shows every part of
    # the detail screen: a day curve, rain tomorrow afternoon, a short shower
    # in the INCA window. Same contracts as the real providers.

    def _demo_weather(self, now: datetime) -> WeatherState:
        zone = ZoneInfo(self.config.app.timezone)
        today = now.astimezone(zone).date()
        lat, lon = self.config.weather.latitude, self.config.weather.longitude
        days = [
            ("Leicht bewölkt", 2, 16, 7, 10, 0.0),
            ("Regen", 63, 13, 8, 80, 6.4),
            ("Klar", 0, 15, 5, 5, 0.0),
            ("Bedeckt", 3, 12, 6, 20, 0.2),
            ("Regenschauer", 81, 11, 6, 60, 3.1),
            ("Nebel", 45, 9, 3, 10, 0.0),
            ("Leichter Schnee", 71, 4, -1, 70, 2.5),
        ]
        forecast = []
        for index, (label, code, high, low, chance, amount) in enumerate(days):
            day = today + timedelta(days=index)
            rise, set_ = sun_times(day, lat, lon)
            icon, _ = wmo_condition(code, is_day=None)
            forecast.append(
                WeatherForecastItem(
                    day_label=("Heute", "Morgen")[index] if index < 2 else _weekday(day),
                    condition=label,
                    condition_code=code,
                    temperature_max_c=high,
                    temperature_min_c=low,
                    date=day.isoformat(),
                    icon=icon,
                    precipitation_probability_max=chance,
                    precipitation_sum_mm=amount,
                    sunrise=rise.replace(microsecond=0).isoformat() if rise else None,
                    sunset=set_.replace(microsecond=0).isoformat() if set_ else None,
                )
            )
        icon, _ = wmo_condition(2, is_day=is_daylight(now, lat, lon))
        return WeatherState(
            snapshot=self.snapshot(status="ok"),
            location_label="Demo Zuhause",
            temperature_c=21.4,
            apparent_temperature_c=20.8,
            condition="Leicht bewölkt",
            condition_code=2,
            forecast=forecast,
            observed_at=now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).isoformat(),
            icon=icon,
            wind_speed_kmh=14.0,
            wind_direction_deg=290.0,
            wind_gusts_kmh=31.0,
        )

    def _demo_hourly(self, now: datetime) -> HourlyForecastState:
        zone = ZoneInfo(self.config.app.timezone)
        lat, lon = self.config.weather.latitude, self.config.weather.longitude
        start = now.replace(minute=0, second=0, microsecond=0)
        end_local = datetime.combine(
            now.astimezone(zone).date() + timedelta(days=2), datetime.min.time(), zone
        )
        end = end_local.astimezone(timezone.utc)
        points = []
        moment = start
        while moment <= end:
            local = moment.astimezone(zone)
            day_index = (local.date() - now.astimezone(zone).date()).days
            hour = local.hour + local.minute / 60
            temp = 11.5 + 4.5 * math.sin((hour - 9) / 24 * 2 * math.pi) - 1.5 * day_index
            rain = 0.0
            symbol = 3 if 10 <= hour <= 17 else 2
            if day_index == 1 and 13 <= hour <= 19:
                rain = round(0.4 + 1.6 * math.sin((hour - 13) / 6 * math.pi), 1)
                symbol = 9 if rain >= 1 else 8
            icon, label = geosphere_condition(symbol, is_day=is_daylight(moment, lat, lon))
            points.append(
                HourlyPoint(
                    time=moment.isoformat(),
                    temperature_c=round(temp, 1),
                    precipitation_mm=rain,
                    precipitation_probability=None,
                    wind_speed_kmh=round(12 + 8 * math.sin(hour / 24 * math.pi), 1),
                    wind_direction_deg=(250 + 4 * hour) % 360,
                    wind_gusts_kmh=round(24 + 14 * math.sin(hour / 24 * math.pi), 1),
                    icon=icon,
                    condition=label,
                )
            )
            moment += timedelta(hours=1)
        run = (now - timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
        run = run.replace(hour=run.hour - run.hour % 3)
        return HourlyForecastState(
            snapshot=self.snapshot(status="ok", source="geosphere-claef"),
            model_run=run.isoformat(),
            valid_from=points[0].time,
            valid_to=points[-1].time,
            points=points,
        )

    def _demo_nowcast(self, now: datetime) -> NowcastState:
        quarter = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        amounts = [0.0, 0.0, 0.05, 0.2, 0.45, 0.6, 0.35, 0.15, 0.05, 0.0, 0.0, 0.0]
        points = [
            NowcastPoint(
                ends_at=(quarter + timedelta(minutes=15 * (index + 1))).isoformat(),
                precipitation_mm=amount,
            )
            for index, amount in enumerate(amounts)
        ]
        return NowcastState(
            snapshot=self.snapshot(status="ok", source="geosphere-inca"),
            model_run=quarter.isoformat(),
            interval_minutes=15,
            points=points,
        )


def _weekday(day) -> str:
    return ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")[day.weekday()]

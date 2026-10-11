"""
Pluggable Data Source Engine for Modular Dashboards.
Provides data access abstraction for transit feeds, weather telemetry,
system status, static data, and external HTTP/JSON endpoints (e.g. Home Assistant).
"""

import json
import logging
import os
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Optional

from bus_tracker import NJTransitBusTracker, normalize_arrival
from canvas import STOPS
from citibike import CitiBikeTracker
from weather import WeatherSnapshot, WeatherTracker, get_mock_weather_data

logger = logging.getLogger("data_sources")


def resolve_field_path(data: Any, path: str) -> Any:
    """
    Extracts a nested field from a dict/list using dot or bracket notation.
    e.g. 'attributes.temperature' or 'states[0].state'.
    Returns None if the path does not exist.
    """
    if not path or data is None:
        return data

    parts = path.replace("[", ".").replace("]", "").split(".")
    curr = data
    for part in parts:
        if not part:
            continue
        if isinstance(curr, dict):
            curr = curr.get(part)
        elif isinstance(curr, (list, tuple)):
            try:
                idx = int(part)
                curr = curr[idx] if 0 <= idx < len(curr) else None
            except ValueError:
                return None
        else:
            return None
        if curr is None:
            return None
    return curr


class DataSource(ABC):
    """Abstract base class for all dashboard data sources."""

    @abstractmethod
    def get_data(self, is_mock: bool = False) -> Any:
        """Returns the current data payload."""
        pass


class SystemDataSource(DataSource):
    """Provides system clock, battery, charging state, and schedule phase."""

    def __init__(
        self,
        batt_level: Optional[int] = None,
        is_charging: bool = False,
        phase: str = "peak",
    ) -> None:
        self.batt_level = batt_level
        self.is_charging = is_charging
        self.phase = phase

    def update_state(
        self,
        batt_level: Optional[int] = None,
        is_charging: bool = False,
        phase: str = "peak",
    ) -> None:
        self.batt_level = batt_level
        self.is_charging = is_charging
        self.phase = phase

    def get_data(self, is_mock: bool = False) -> dict[str, Any]:
        from datetime import datetime

        now = datetime.now()
        return {
            "time_str": now.strftime("%-I:%M %p"),
            "date_str": now.strftime("%A, %b %-d"),
            "batt_level": 85
            if (is_mock and self.batt_level is None)
            else self.batt_level,
            "is_charging": False if is_mock else self.is_charging,
            "phase": self.phase,
            "is_mock": is_mock,
        }


class NJTransitDataSource(DataSource):
    """Fetches real-time bus arrivals for designated stops."""

    def __init__(self, tracker: Optional[NJTransitBusTracker] = None) -> None:
        self.tracker = tracker or NJTransitBusTracker()

    def get_data(self, is_mock: bool = False) -> dict[str, Any]:
        if is_mock:
            from render_dashboard import get_mock_data

            return {
                "stops": get_mock_data(),
                "status": {s["id"]: "ok" for s in STOPS},
            }

        stops_data: dict[str, list[Any]] = {}
        stop_status: dict[str, str] = {}
        for stop in STOPS:
            sid = stop["id"]
            try:
                status, trips = self.tracker.get_arrivals_with_status(
                    stop_id=sid, route="126"
                )
                stops_data[sid] = [normalize_arrival(t) for t in trips]
                stop_status[sid] = status
            except Exception as e:
                logger.warning("Error fetching arrivals for stop %s: %s", sid, e)
                stops_data[sid] = []
                stop_status[sid] = "error"

        return {"stops": stops_data, "status": stop_status}


class CitiBikeDataSource(DataSource):
    """Fetches Citi Bike dock and e-bike availability."""

    def __init__(self, tracker: Optional[CitiBikeTracker] = None) -> None:
        self.tracker = tracker or CitiBikeTracker(cache_ttl=30)

    def get_data(self, is_mock: bool = False) -> list[Any]:
        try:
            if is_mock:
                return self.tracker.get_mock_data()
            return self.tracker.get_station_status()
        except Exception as e:
            logger.warning("Error fetching Citi Bike status: %s", e)
            return self.tracker.get_mock_data() if is_mock else []


class WeatherDataSource(DataSource):
    """Fetches weather conditions and forecast."""

    def __init__(self, tracker: Optional[WeatherTracker] = None) -> None:
        self.tracker = tracker or WeatherTracker()

    def get_data(self, is_mock: bool = False) -> WeatherSnapshot:
        if is_mock:
            return get_mock_weather_data()
        return self.tracker.get_weather()


class StaticDataSource(DataSource):
    """Provides static key-value data directly configured in the dashboard spec."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    def get_data(self, is_mock: bool = False) -> dict[str, Any]:
        return self.data


class HttpJsonDataSource(DataSource):
    """
    Queries an external JSON endpoint (e.g. Home Assistant, webhooks, IoT sensors)
    with optional headers, caching, and field path resolution.
    """

    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        fields: Optional[dict[str, str]] = None,
        cache_ttl: int = 60,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.fields = fields or {}
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache: Optional[dict[str, Any]] = None
        self._cache_time: float = 0.0
        self._lock = threading.Lock()

    def _resolve_headers(self) -> dict[str, str]:
        """Resolves ${ENV_VAR} references in headers."""
        resolved: dict[str, str] = {}
        for k, v in self.headers.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                env_var = v[2:-1]
                resolved[k] = os.environ.get(env_var, "")
            else:
                resolved[k] = str(v)
        return resolved

    def get_data(self, is_mock: bool = False) -> dict[str, Any]:
        if is_mock:
            # Generate mock data from field keys
            return {k: f"mock_{k}" for k in self.fields}

        now = time.time()
        with self._lock:
            if self._cache is not None and (now - self._cache_time) < self.cache_ttl:
                return self._cache

        try:
            req = urllib.request.Request(
                self.url,
                headers=self._resolve_headers(),
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_json = json.loads(resp.read().decode("utf-8"))

            if self.fields:
                extracted = {
                    k: resolve_field_path(raw_json, path)
                    for k, path in self.fields.items()
                }
            else:
                extracted = (
                    raw_json if isinstance(raw_json, dict) else {"data": raw_json}
                )

            with self._lock:
                self._cache = extracted
                self._cache_time = now
            return extracted
        except Exception as e:
            logger.warning("HttpJsonDataSource error fetching %s: %s", self.url, e)
            with self._lock:
                if self._cache is not None:
                    return self._cache
            return {}


class DataSourceRegistry:
    """Manages data source providers for dashboards."""

    def __init__(self) -> None:
        self._sources: dict[str, DataSource] = {}
        self._lock = threading.Lock()

    def register(self, name: str, source: DataSource) -> None:
        with self._lock:
            self._sources[name] = source

    def get(self, name: str) -> Optional[DataSource]:
        with self._lock:
            return self._sources.get(name)

    def create_source(self, config: dict[str, Any]) -> Optional[DataSource]:
        """Instantiates a data source from a configuration dict."""
        source_type = config.get("type", "").lower()
        if source_type == "static":
            return StaticDataSource(config.get("data", {}))
        if source_type == "http_json":
            return HttpJsonDataSource(
                url=config.get("url", ""),
                headers=config.get("headers"),
                fields=config.get("fields"),
                cache_ttl=int(config.get("cache_ttl", 60)),
                timeout=float(config.get("timeout", 5.0)),
            )
        if source_type == "weather":
            return WeatherDataSource()
        if source_type == "njtransit":
            return NJTransitDataSource()
        if source_type == "citibike":
            return CitiBikeDataSource()
        if source_type == "system":
            return SystemDataSource()
        return None


# Global process-wide data source registry
GLOBAL_DATA_SOURCES = DataSourceRegistry()
GLOBAL_DATA_SOURCES.register("system", SystemDataSource())
GLOBAL_DATA_SOURCES.register("weather", WeatherDataSource())
GLOBAL_DATA_SOURCES.register("njtransit", NJTransitDataSource())
GLOBAL_DATA_SOURCES.register("citibike", CitiBikeDataSource())

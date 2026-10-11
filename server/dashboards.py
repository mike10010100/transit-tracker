"""
Dashboard Registry and Specification Parser.
Manages built-in and user-defined dashboards, JSON schema validation,
and layout tree instantiation.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from dashboard_dsl import (
    Box,
    BusBarWidget,
    BusHeroWidget,
    ButtonBarWidget,
    CitiBikeBarWidget,
    CitiBikeHeroWidget,
    EntityListWidget,
    HeaderWidget,
    HStack,
    LayoutNode,
    MetricCardWidget,
    Rect,
    RenderContext,
    TextNoticeWidget,
    VStack,
    WeatherForecastWidget,
    WeatherHeroWidget,
    Widget,
    WidgetNode,
)
from data_sources import GLOBAL_DATA_SOURCES
from paths import resolve_dashboards_dir

logger = logging.getLogger("dashboards")

DASHBOARD_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
DEFAULT_DASHBOARDS_DIR = resolve_dashboards_dir()


class DashboardError(ValueError):
    """Raised when a dashboard specification fails validation."""


@dataclass
class DashboardSpec:
    """A validated, executable dashboard specification."""

    id: str
    title: str
    description: str = ""
    version: int = 1
    layout_data: dict[str, Any] = field(default_factory=dict)
    data_sources_config: dict[str, Any] = field(default_factory=dict)
    is_builtin: bool = False
    file_path: Optional[str] = None
    file_mtime: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "layout": self.layout_data,
            "data_sources": self.data_sources_config,
            "is_builtin": self.is_builtin,
        }


def parse_widget(data: dict[str, Any]) -> Widget:
    """Instantiates a Widget from a JSON component dictionary."""
    wtype = data.get("type", "").lower()
    if wtype == "header":
        return HeaderWidget(
            badge=data.get("badge", "TRANSIT"),
            title=data.get("title", "HOBOKEN DASHBOARD"),
            subtitle=data.get("subtitle", "LIVE TELEMETRY"),
            show_time=data.get("show_time", True),
            show_battery=data.get("show_battery", True),
            show_date=data.get("show_date", True),
        )
    if wtype in ("weather_hero", "weather"):
        return WeatherHeroWidget(
            show_details=data.get("show_details", True),
            border=data.get("border", True),
        )
    if wtype in ("weather_forecast", "forecast"):
        return WeatherForecastWidget(
            days=int(data.get("days", 4)),
            border=data.get("border", True),
        )
    if wtype == "metric_card":
        return MetricCardWidget(
            title=data.get("title", "METRIC"),
            value=data.get("value"),
            source=data.get("source"),
            unit=data.get("unit", ""),
            subtitle=data.get("subtitle", ""),
            badge=data.get("badge"),
            border=data.get("border", True),
        )
    if wtype == "entity_list":
        return EntityListWidget(
            entities=data.get("entities", []),
            border=data.get("border", True),
        )
    if wtype == "text_notice":
        return TextNoticeWidget(
            title=data.get("title", "NOTICE"),
            body=data.get("body", ""),
            border=data.get("border", True),
            badge=data.get("badge", "NOTICE"),
        )
    if wtype in ("button_bar", "status_strip"):
        buttons = data.get("buttons")
        return ButtonBarWidget(buttons=buttons)
    if wtype == "bus_hero":
        return BusHeroWidget(
            stops=data.get("stops"),
            route=data.get("route", "126"),
        )
    if wtype == "bus_bar":
        return BusBarWidget(
            stops=data.get("stops"),
            route=data.get("route", "126"),
        )
    if wtype in ("citibike_hero", "citibike_grid"):
        return CitiBikeHeroWidget(stations=data.get("stations"))
    if wtype == "citibike_bar":
        return CitiBikeBarWidget(stations=data.get("stations"))

    raise DashboardError(f"Unknown widget type: {wtype!r}")


def parse_layout_node(data: dict[str, Any]) -> LayoutNode:
    """Recursively instantiates a layout node tree from a dictionary."""
    if not isinstance(data, dict):
        raise DashboardError("Layout node must be a JSON object")

    node_type = data.get("type", "").lower()
    flex = float(data.get("flex", 1.0))
    fixed_size = data.get("height") or data.get("width")
    if fixed_size is not None:
        fixed_size = int(fixed_size)
    padding = int(data.get("padding", 0))
    gap = int(data.get("gap", 0))

    if node_type == "vstack":
        children = [parse_layout_node(c) for c in data.get("children", [])]
        return VStack(
            children=children,
            flex=flex,
            fixed_size=fixed_size,
            padding=padding,
            gap=gap,
        )

    if node_type == "hstack":
        children = [parse_layout_node(c) for c in data.get("children", [])]
        return HStack(
            children=children,
            flex=flex,
            fixed_size=fixed_size,
            padding=padding,
            gap=gap,
        )

    if node_type in ("box", "card"):
        child_raw = data.get("child")
        if not child_raw and "children" in data:
            child_raw = {"type": "vstack", "children": data["children"]}
        if not child_raw:
            raise DashboardError("Box container must have a 'child' or 'children'")
        child_node = parse_layout_node(child_raw)
        return Box(
            child=child_node,
            border=data.get("border", True),
            fill=data.get("fill"),
            radius=int(data.get("radius", 6)),
            padding=int(data.get("padding", 8)),
            flex=flex,
            fixed_size=fixed_size,
        )

    # Otherwise it is a leaf widget
    widget = parse_widget(data)
    return WidgetNode(widget=widget, flex=flex, fixed_size=fixed_size, padding=padding)


def validate_dashboard_spec(data: dict[str, Any]) -> DashboardSpec:
    """Validates raw JSON data and produces a DashboardSpec."""
    if not isinstance(data, dict):
        raise DashboardError("Dashboard specification must be a JSON object")

    did = data.get("id", "").strip().lower()
    if not did or not DASHBOARD_ID_RE.match(did):
        raise DashboardError(
            f"Invalid dashboard id {did!r}: must match [a-z0-9_-]{{1,32}}"
        )

    title = data.get("title", "").strip()
    if not title:
        raise DashboardError("Dashboard specification missing required 'title'")

    layout_data = data.get("layout")
    if not layout_data or not isinstance(layout_data, dict):
        raise DashboardError("Dashboard specification missing required 'layout' object")

    # Validate that layout can be parsed
    _ = parse_layout_node(layout_data)

    return DashboardSpec(
        id=did,
        title=title,
        description=data.get("description", ""),
        version=int(data.get("version", 1)),
        layout_data=layout_data,
        data_sources_config=data.get("data_sources", {}),
    )


# ---------------------------------------------------------------------------
# Built-in Dashboard Presets
# ---------------------------------------------------------------------------


def _builtin_weather_spec() -> DashboardSpec:
    layout = {
        "type": "vstack",
        "children": [
            {
                "type": "header",
                "badge": "WEATHER",
                "title": "HOBOKEN LOCAL FORECAST",
                "subtitle": "MILE SQUARE METEOROLOGY & CONDITIONS",
                "show_time": True,
                "show_battery": True,
                "show_date": True,
            },
            {
                "type": "hstack",
                "flex": 2.2,
                "gap": 10,
                "children": [
                    {
                        "type": "weather_hero",
                        "flex": 2.2,
                        "show_details": True,
                    },
                    {
                        "type": "metric_card",
                        "flex": 1.0,
                        "title": "PRECIPITATION",
                        "source": "precip_prob",
                        "unit": "%",
                        "subtitle": "Rain probability",
                        "badge": "RADAR",
                    },
                ],
            },
            {
                "type": "weather_forecast",
                "height": 115,
                "days": 4,
            },
            {
                "type": "button_bar",
            },
        ],
    }
    return DashboardSpec(
        id="weather",
        title="Hoboken Weather & Forecast",
        description="Current conditions, temperature hero, and upcoming 4-day forecast",
        version=1,
        layout_data=layout,
        is_builtin=True,
    )


def _builtin_bus_focus_spec() -> DashboardSpec:
    layout = {
        "type": "vstack",
        "children": [
            {
                "type": "header",
                "badge": "126",
                "title": "HOBOKEN → NYC PORT AUTHORITY",
                "subtitle": "NJ TRANSIT ROUTE 126 EXPRESS",
                "show_time": True,
                "show_battery": True,
                "show_date": True,
            },
            {
                "type": "bus_hero",
                "flex": 3.0,
                "stops": ["20512", "20494"],
                "route": "126",
            },
            {
                "type": "button_bar",
            },
        ],
    }
    return DashboardSpec(
        id="bus_focus",
        title="NJ Transit 126 Bus Focus",
        description="Dedicated Route 126 departure countdown board for Washington & Clinton St",
        version=1,
        layout_data=layout,
        is_builtin=True,
    )


def _builtin_citibike_focus_spec() -> DashboardSpec:
    layout = {
        "type": "vstack",
        "children": [
            {
                "type": "header",
                "badge": "CITI BIKE",
                "title": "HOBOKEN DOCKS & E-BIKES",
                "subtitle": "LIVE STATION INVENTORY",
                "show_time": True,
                "show_battery": True,
                "show_date": True,
            },
            {
                "type": "citibike_hero",
                "flex": 3.0,
            },
            {
                "type": "button_bar",
            },
        ],
    }
    return DashboardSpec(
        id="citibike_focus",
        title="Citi Bike Hub Focus",
        description="Dedicated Citi Bike station inventory and e-bike availability",
        version=1,
        layout_data=layout,
        is_builtin=True,
    )


class DashboardRegistry:
    """Registry managing built-in and user-configured dashboard specifications."""

    def __init__(self, dashboards_dir: Optional[str] = None) -> None:
        self.dashboards_dir = dashboards_dir or os.environ.get(
            "DASHBOARDS_DIR", DEFAULT_DASHBOARDS_DIR
        )
        self._dashboards: dict[str, DashboardSpec] = {}
        self._lock = threading.Lock()
        self._init_builtins()
        self.reload_custom_dashboards()

    def _init_builtins(self) -> None:
        # Register classic views as built-in IDs
        self._dashboards["morning"] = DashboardSpec(
            id="morning",
            title="Morning (Citi Bike Hero)",
            description="Citi Bike dock hero with compact bus departure bar",
            is_builtin=True,
        )
        self._dashboards["evening"] = DashboardSpec(
            id="evening",
            title="Evening (Bus Hero)",
            description="Route 126 bus departure countdown hero with Citi Bike status bar",
            is_builtin=True,
        )
        self._dashboards["weather"] = _builtin_weather_spec()
        self._dashboards["bus_focus"] = _builtin_bus_focus_spec()
        self._dashboards["citibike_focus"] = _builtin_citibike_focus_spec()

    def get(self, dashboard_id: str) -> Optional[DashboardSpec]:
        """Looks up a dashboard specification by ID, hot-reloading if changed on disk."""
        did = (dashboard_id or "").strip().lower()
        with self._lock:
            spec = self._dashboards.get(did)
            if spec and spec.file_path and os.path.exists(spec.file_path):
                try:
                    mtime = os.path.getmtime(spec.file_path)
                    if mtime > spec.file_mtime:
                        with open(spec.file_path, encoding="utf-8") as f:
                            data = json.load(f)
                        updated = validate_dashboard_spec(data)
                        updated.file_path = spec.file_path
                        updated.file_mtime = mtime
                        self._dashboards[did] = updated
                        return updated
                except Exception as e:
                    logger.warning("Hot-reload error for dashboard %s: %s", did, e)
            return spec

    def list_all(self) -> list[DashboardSpec]:
        """Returns all registered dashboards."""
        self.reload_custom_dashboards()
        with self._lock:
            return list(self._dashboards.values())

    def register(self, spec: DashboardSpec) -> None:
        with self._lock:
            self._dashboards[spec.id] = spec

    def save_custom_dashboard(self, data: dict[str, Any]) -> DashboardSpec:
        """Validates, persists to disk, and registers a custom dashboard."""
        spec = validate_dashboard_spec(data)
        os.makedirs(self.dashboards_dir, exist_ok=True)
        file_path = os.path.join(self.dashboards_dir, f"{spec.id}.json")
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(spec.to_dict(), f, indent=2)
        os.replace(tmp_path, file_path)

        spec.file_path = file_path
        spec.file_mtime = os.path.getmtime(file_path)
        self.register(spec)
        return spec

    def reload_custom_dashboards(self) -> None:
        """Loads or reloads any dashboard JSON files found in dashboards_dir."""
        if not self.dashboards_dir or not os.path.isdir(self.dashboards_dir):
            return

        for fname in os.listdir(self.dashboards_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.dashboards_dir, fname)
            try:
                mtime = os.path.getmtime(path)
                with self._lock:
                    existing = self._dashboards.get(fname[:-5])
                    if existing and existing.file_mtime >= mtime:
                        continue

                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                spec = validate_dashboard_spec(data)
                spec.file_path = path
                spec.file_mtime = mtime
                self.register(spec)
            except Exception as e:
                logger.warning("Failed to load dashboard from %s: %s", path, e)

    def render(
        self,
        dashboard_id: str,
        draw: Any,
        ctx: RenderContext,
    ) -> bool:
        """
        Renders the requested dashboard onto the canvas.
        Returns True if handled, or False if view should fall back to legacy renderers.
        """
        spec = self.get(dashboard_id)
        if not spec or not spec.layout_data:
            return False

        try:
            # Resolve any custom data sources declared on the dashboard
            if spec.data_sources_config:
                for s_name, s_cfg in spec.data_sources_config.items():
                    src = GLOBAL_DATA_SOURCES.create_source(s_cfg)
                    if src:
                        ctx.custom_data[s_name] = src.get_data(is_mock=ctx.is_mock)

            root_node = parse_layout_node(spec.layout_data)
            canvas_rect = Rect(0, 0, ctx.width, ctx.height)
            root_node.layout_and_render(draw, canvas_rect, ctx)
            return True
        except Exception as e:
            logger.error("Error rendering dashboard %s: %s", dashboard_id, e)
            return False


# Global registry instance
REGISTRY = DashboardRegistry()

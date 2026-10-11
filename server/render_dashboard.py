import os
import sys
from datetime import datetime
from typing import Any, Optional

from bus_tracker import NJTransitBusTracker, normalize_arrival
from canvas import (
    HEIGHT,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STOPS,
    WIDTH,
    ScaledDraw,
    draw_battery_indicator,
    draw_bottom_button_bar,
    draw_header_badge,
    draw_mock_badge,
    draw_status_strip,
    ellipsize_to_width,
    empty_state_message,
    get_font,
    mock_badge_width,
    parse_minutes,
)
from citibike import CitiBikeTracker
from dashboard_dsl import RenderContext
from dashboards import REGISTRY
from evening_view import render_evening_view
from morning_view import render_morning_view
from PIL import Image, ImageDraw, ImageFont
from weather import WeatherTracker, get_mock_weather_data

__all__ = [
    "HEIGHT",
    "ImageFont",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_OK",
    "STOPS",
    "WIDTH",
    "ScaledDraw",
    "os",
    "draw_battery_indicator",
    "draw_bottom_button_bar",
    "draw_header_badge",
    "draw_mock_badge",
    "draw_status_strip",
    "ellipsize_to_width",
    "empty_state_message",
    "get_font",
    "get_mock_data",
    "mock_badge_width",
    "parse_minutes",
    "render_dashboard",
    "resolve_view",
]


def resolve_view(view: str = "auto", hour: Optional[int] = None) -> str:
    """
    Resolves view mode across built-in presets and registered modular dashboards.
    - 'morning': Citi Bike Hero view (AM commute, 5:00 AM - 12:00 PM).
    - 'evening': Bus Hero view (PM commute / evening / night, 12:00 PM - 5:00 AM).
    - Any registered dashboard ID (e.g. 'weather', 'bus_focus', 'citibike_focus').
    - 'auto': Automatically switches based on current local hour.
    """
    v = (view or "auto").lower().strip()
    if v in ("morning", "citi", "citibike", "am"):
        return "morning"
    if v in ("evening", "bus", "pm", "afternoon", "night"):
        return "evening"

    # Check registered modular dashboards
    try:
        if REGISTRY.get(v) is not None:
            return v
    except Exception:
        pass

    if hour is None:
        hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    return "evening"


def render_dashboard(
    stops_data: dict[str, list[dict[str, Any]]],
    citibike_data: Any = None,
    output_path: Any = "dashboard.png",
    view: str = "auto",
    is_mock: bool = False,
    batt_level: Optional[int] = None,
    is_charging: bool = False,
    stop_status: Optional[dict[str, str]] = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    scale: float = 1.0,
    presentation: str = "interactive",
    status_note: str = "",
    available_views: Optional[list[str]] = None,
    weather_data: Any = None,
    custom_data: Optional[dict[str, Any]] = None,
) -> str:
    """
    Renders a high-contrast black-and-white image optimized for e-ink
    or low-power dashboard screens (default 800x480, or 800x600 for 4:3 displays).
    Supports built-in views and arbitrary modular dashboards.
    """
    if citibike_data is None:
        try:
            tracker = CitiBikeTracker()
            citibike_data = (
                tracker.get_mock_data() if is_mock else tracker.get_station_status()
            )
        except Exception:
            citibike_data = []

    active_view = resolve_view(view)

    # Create native-resolution canvas; view code draws in logical coordinates.
    native_w = max(1, int(round(width * scale)))
    native_h = max(1, int(round(height * scale)))
    img = Image.new("RGB", (native_w, native_h), color="white")
    draw = ScaledDraw(ImageDraw.Draw(img), scale)
    now = datetime.now()

    if active_view == "morning":
        render_morning_view(
            draw,
            stops_data,
            citibike_data,
            now,
            batt_level=batt_level,
            is_charging=is_charging,
            is_mock=is_mock,
            stop_status=stop_status,
            width=width,
            height=height,
        )
    elif active_view == "evening":
        render_evening_view(
            draw,
            stops_data,
            citibike_data,
            now,
            batt_level=batt_level,
            is_charging=is_charging,
            is_mock=is_mock,
            stop_status=stop_status,
            width=width,
            height=height,
        )
    else:
        # Modular dashboard rendering via REGISTRY
        if weather_data is None:
            try:
                weather_data = (
                    get_mock_weather_data()
                    if is_mock
                    else WeatherTracker().get_weather()
                )
            except Exception:
                weather_data = get_mock_weather_data() if is_mock else None

        ctx = RenderContext(
            now=now,
            stops_data=stops_data,
            stop_status=stop_status or {},
            citibike_data=citibike_data or [],
            weather_data=weather_data,
            custom_data=custom_data or {},
            batt_level=batt_level,
            is_charging=is_charging,
            presentation=presentation,
            status_note=status_note,
            active_view=active_view,
            available_views=available_views or [],
            is_mock=is_mock,
            width=width,
            height=height,
        )
        handled = REGISTRY.render(active_view, draw, ctx)
        if not handled:
            render_evening_view(
                draw,
                stops_data,
                citibike_data,
                now,
                batt_level=batt_level,
                is_charging=is_charging,
                is_mock=is_mock,
                stop_status=stop_status,
                width=width,
                height=height,
            )

    # Not interactive (idle-while-suspended or overnight): erase the button bar
    # and show a status strip so the panel does not masquerade as tappable.
    if presentation != "interactive":
        draw_status_strip(draw, width, height, note=status_note)

    img.save(output_path, "PNG")
    print(
        f"✓ Dashboard image successfully rendered [{active_view.upper()} VIEW]: {output_path} ({native_w}x{native_h}, scale={scale:g})"
    )
    return output_path


def get_mock_data():
    """Generates realistic peak-hour commute data for layout previewing."""
    return {
        "20512": [
            {
                "route": "126",
                "destination": "126 NEW YORK",
                "eta": "in 5 mins (8:35 AM)",
                "occupancy": "SEATS_AVAILABLE",
                "vehicle_id": "25248",
            },
            {
                "route": "126",
                "destination": "126 NEW YORK",
                "eta": "in 17 mins (8:47 AM)",
                "occupancy": "HALF_EMPTY",
                "vehicle_id": "21045",
            },
        ],
        "20494": [
            {
                "route": "126",
                "destination": "126 NEW YORK VIA CLINTON",
                "eta": "in 12 mins (8:42 AM)",
                "occupancy": "EMPTY",
                "vehicle_id": "22119",
            },
            {
                "route": "126",
                "destination": "126 NEW YORK VIA CLINTON",
                "eta": "in 24 mins (8:54 AM)",
                "occupancy": "SEATS_AVAILABLE",
                "vehicle_id": "25301",
            },
        ],
    }


if __name__ == "__main__":
    use_mock = "--mock" in sys.argv
    view_arg = "auto"
    for arg in sys.argv:
        if arg.startswith("--view="):
            view_arg = arg.split("=", 1)[1]
        elif arg in ("--morning", "-morning"):
            view_arg = "morning"
        elif arg in ("--evening", "-evening"):
            view_arg = "evening"

    stops_data = {}

    stop_status = {}

    if use_mock:
        print("Rendering with mock peak-commute data...")
        stops_data = get_mock_data()
        stop_status = {stop["id"]: STATUS_OK for stop in STOPS}
    else:
        print("Fetching live data from NJ Transit API...")
        tracker = NJTransitBusTracker()
        for stop in STOPS:
            sid = stop["id"]
            status, trips = tracker.get_arrivals_with_status(stop_id=sid, route="126")
            stops_data[sid] = [normalize_arrival(t) for t in trips]
            stop_status[sid] = status

    render_dashboard(
        stops_data,
        output_path="dashboard.png",
        view=view_arg,
        is_mock=use_mock,
        stop_status=stop_status,
    )

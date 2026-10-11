"""
Declarative E-Ink Design Language & Widget Layout Engine.
Provides layout containers (VStack, HStack, Box) and reusable high-contrast
widgets for transit, weather, metrics, and Home Assistant / IoT telemetry.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from canvas import (
    HEIGHT,
    STOPS,
    WIDTH,
    draw_battery_indicator,
    draw_bottom_button_bar,
    draw_header_badge,
    draw_mock_badge,
    draw_status_strip,
    ellipsize_to_width,
    get_font,
    mock_badge_width,
)
from data_sources import resolve_field_path
from weather import WeatherSnapshot


@dataclass(frozen=True)
class Rect:
    """Bounding rectangle in logical design coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return max(0, self.x1 - self.x0)

    @property
    def h(self) -> int:
        return max(0, self.y1 - self.y0)

    def inset(self, pad_x: int, pad_y: Optional[int] = None) -> "Rect":
        if pad_y is None:
            pad_y = pad_x
        return Rect(
            x0=self.x0 + pad_x,
            y0=self.y0 + pad_y,
            x1=max(self.x0 + pad_x, self.x1 - pad_x),
            y1=max(self.y0 + pad_y, self.y1 - pad_y),
        )


@dataclass
class RenderContext:
    """Telemetry data and rendering state provided to widgets during canvas drawing."""

    now: datetime
    stops_data: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stop_status: dict[str, str] = field(default_factory=dict)
    citibike_data: list[dict[str, Any]] = field(default_factory=list)
    weather_data: Optional[WeatherSnapshot] = None
    custom_data: dict[str, Any] = field(default_factory=dict)
    batt_level: Optional[int] = None
    is_charging: bool = False
    presentation: str = "interactive"
    status_note: str = ""
    active_view: str = "morning"
    available_views: list[str] = field(default_factory=list)
    is_mock: bool = False
    width: int = WIDTH
    height: int = HEIGHT


class Widget:
    """Base class for all dashboard UI widgets."""

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Universal Layout Containers
# ---------------------------------------------------------------------------


class LayoutNode:
    """A node in the declarative layout tree."""

    def __init__(
        self,
        flex: float = 1.0,
        fixed_size: Optional[int] = None,
        padding: int = 0,
        gap: int = 0,
    ) -> None:
        self.flex = flex
        self.fixed_size = fixed_size
        self.padding = padding
        self.gap = gap

    def layout_and_render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        raise NotImplementedError


class WidgetNode(LayoutNode):
    """A leaf layout node that wraps a Widget."""

    def __init__(
        self,
        widget: Widget,
        flex: float = 1.0,
        fixed_size: Optional[int] = None,
        padding: int = 0,
    ) -> None:
        super().__init__(flex=flex, fixed_size=fixed_size, padding=padding)
        self.widget = widget

    def layout_and_render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        r = rect.inset(self.padding) if self.padding > 0 else rect
        self.widget.render(draw, r, ctx)


class VStack(LayoutNode):
    """Vertical stack container that divides height among child elements."""

    def __init__(
        self,
        children: Optional[list[LayoutNode]] = None,
        flex: float = 1.0,
        fixed_size: Optional[int] = None,
        padding: int = 0,
        gap: int = 0,
    ) -> None:
        super().__init__(flex=flex, fixed_size=fixed_size, padding=padding, gap=gap)
        self.children = children or []

    def layout_and_render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if not self.children:
            return

        r = rect.inset(self.padding) if self.padding > 0 else rect
        total_h = r.h
        total_gaps = (len(self.children) - 1) * self.gap
        avail_h = max(0, total_h - total_gaps)

        # Allocate fixed heights first
        fixed_sum = sum(c.fixed_size for c in self.children if c.fixed_size is not None)
        flex_avail_h = max(0, avail_h - fixed_sum)
        flex_sum = sum(
            c.flex for c in self.children if c.fixed_size is None and c.flex > 0
        )
        flex_sum = flex_sum if flex_sum > 0 else 1.0

        curr_y = r.y0
        for i, child in enumerate(self.children):
            if child.fixed_size is not None:
                child_h = child.fixed_size
            else:
                child_h = int(round((child.flex / flex_sum) * flex_avail_h))

            # Ensure last child fills available remainder
            if i == len(self.children) - 1:
                child_h = max(0, r.y1 - curr_y)

            child_rect = Rect(r.x0, curr_y, r.x1, min(r.y1, curr_y + child_h))
            child.layout_and_render(draw, child_rect, ctx)
            curr_y += child_h + self.gap


class HStack(LayoutNode):
    """Horizontal stack container that divides width among child elements."""

    def __init__(
        self,
        children: Optional[list[LayoutNode]] = None,
        flex: float = 1.0,
        fixed_size: Optional[int] = None,
        padding: int = 0,
        gap: int = 0,
    ) -> None:
        super().__init__(flex=flex, fixed_size=fixed_size, padding=padding, gap=gap)
        self.children = children or []

    def layout_and_render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if not self.children:
            return

        r = rect.inset(self.padding) if self.padding > 0 else rect
        total_w = r.w
        total_gaps = (len(self.children) - 1) * self.gap
        avail_w = max(0, total_w - total_gaps)

        # Allocate fixed widths first
        fixed_sum = sum(c.fixed_size for c in self.children if c.fixed_size is not None)
        flex_avail_w = max(0, avail_w - fixed_sum)
        flex_sum = sum(
            c.flex for c in self.children if c.fixed_size is None and c.flex > 0
        )
        flex_sum = flex_sum if flex_sum > 0 else 1.0

        curr_x = r.x0
        for i, child in enumerate(self.children):
            if child.fixed_size is not None:
                child_w = child.fixed_size
            else:
                child_w = int(round((child.flex / flex_sum) * flex_avail_w))

            if i == len(self.children) - 1:
                child_w = max(0, r.x1 - curr_x)

            child_rect = Rect(curr_x, r.y0, min(r.x1, curr_x + child_w), r.y1)
            child.layout_and_render(draw, child_rect, ctx)
            curr_x += child_w + self.gap


class Box(LayoutNode):
    """Container with an e-ink card border and background fill."""

    def __init__(
        self,
        child: LayoutNode,
        border: bool = True,
        fill: Optional[str] = None,
        radius: int = 6,
        border_width: int = 1,
        padding: int = 8,
        flex: float = 1.0,
        fixed_size: Optional[int] = None,
    ) -> None:
        super().__init__(flex=flex, fixed_size=fixed_size, padding=padding)
        self.child = child
        self.border = border
        self.fill = fill
        self.radius = radius
        self.border_width = border_width

    def layout_and_render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.fill or self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=self.radius,
                fill=self.fill or "white",
                outline="black" if self.border else None,
                width=self.border_width if self.border else 0,
            )
        inner = rect.inset(self.padding)
        self.child.layout_and_render(draw, inner, ctx)


# ---------------------------------------------------------------------------
# Universal Widgets
# ---------------------------------------------------------------------------


class HeaderWidget(Widget):
    """Standard dashboard header with badge, title, time, date, battery, and mock tag."""

    def __init__(
        self,
        badge: str = "TRANSIT",
        title: str = "HOBOKEN DASHBOARD",
        subtitle: str = "LIVE TELEMETRY",
        show_time: bool = True,
        show_battery: bool = True,
        show_date: bool = True,
    ) -> None:
        self.badge = badge
        self.title = title
        self.subtitle = subtitle
        self.show_time = show_time
        self.show_battery = show_battery
        self.show_date = show_date

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        font_title = get_font(21, bold=True)
        font_sub = get_font(12, bold=False)
        font_time = get_font(18, bold=True)

        time_str = ctx.now.strftime("%-I:%M %p")
        date_str = ctx.now.strftime("%A, %b %-d")

        # Top-right clock
        time_x = rect.x1
        if self.show_time:
            tb = draw.textbbox((0, 0), time_str, font=font_time)
            time_w = tb[2] - tb[0]
            time_x = rect.x1 - time_w
            draw.text((time_x, rect.y0 + 2), time_str, fill="black", font=font_time)

        # Battery indicator to left of time
        batt_x = time_x
        if self.show_battery and ctx.batt_level is not None:
            font_batt = get_font(13, bold=True)
            label = f"{ctx.batt_level}%"
            bbox = draw.textbbox((0, 0), label, font=font_batt)
            label_w = bbox[2] - bbox[0]
            bolt_w = 12 if ctx.is_charging else 0
            total_batt_w = bolt_w + label_w + 6 + 28 + 3
            batt_x = int(time_x - total_batt_w - 18)
            draw_battery_indicator(
                draw,
                batt_x,
                rect.y0 + 3,
                ctx.batt_level,
                is_charging=ctx.is_charging,
                font=font_batt,
            )

        # Date string below time
        date_x = rect.x1
        if self.show_date:
            db = draw.textbbox((0, 0), date_str, font=font_sub)
            date_w = db[2] - db[0]
            date_x = rect.x1 - date_w
            draw.text((date_x, rect.y0 + 26), date_str, fill="#555555", font=font_sub)

        # Mock badge if applicable
        if ctx.is_mock:
            font_mock = get_font(11, bold=True)
            mw = mock_badge_width(draw, font_mock)
            mock_x = int(date_x - mw - 10)
            draw_mock_badge(draw, mock_x, rect.y0 + 23, font=font_mock)

        # Left badge + title
        bx1 = draw_header_badge(
            draw, rect.x0, rect.y0 + 1, self.badge, font_title, pad_x=12
        )
        title_text = ellipsize_to_width(
            draw, self.title, font_title, max(50, batt_x - bx1 - 20)
        )
        draw.text((bx1 + 12, rect.y0 + 2), title_text, fill="black", font=font_title)
        draw.text(
            (bx1 + 12, rect.y0 + 26), self.subtitle, fill="#555555", font=font_sub
        )


class MetricCardWidget(Widget):
    """
    Universal KPI / Metric Card:
    Displays a title, large value, unit, subtitle, and status pill.
    Can bind to custom_data, weather, battery, or transit fields.
    """

    def __init__(
        self,
        title: str,
        value: Any = None,
        source: Optional[str] = None,
        unit: str = "",
        subtitle: str = "",
        badge: Optional[str] = None,
        border: bool = True,
    ) -> None:
        self.title = title
        self.value = value
        self.source = source
        self.unit = unit
        self.subtitle = subtitle
        self.badge = badge
        self.border = border

    def _resolve_value(self, ctx: RenderContext) -> str:
        if self.source:
            # Check custom_data first
            val = resolve_field_path(ctx.custom_data, self.source)
            if val is not None:
                return str(val)
            # Check weather data
            if ctx.weather_data and self.source.startswith("weather."):
                w_field = self.source.split(".", 1)[1]
                val = getattr(ctx.weather_data, w_field, None)
                if val is not None:
                    return str(val)
            # Check special variables
            if self.source in ("battery", "battery_level"):
                return f"{ctx.batt_level or 0}"
            if self.source == "temp" and ctx.weather_data:
                return f"{ctx.weather_data.temp}"
            if self.source in ("precip_prob", "rain") and ctx.weather_data:
                return f"{ctx.weather_data.precip_prob}"
        if self.value is not None:
            return str(self.value)
        return "--"

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=6,
                fill="white",
                outline="black",
                width=1,
            )

        pad = 12
        inner_w = rect.w - (pad * 2)

        font_label = get_font(12, bold=True)
        font_val = get_font(44 if rect.h >= 110 else 36, bold=True)
        font_unit = get_font(18, bold=True)
        font_sub = get_font(11, bold=False)

        # Title
        draw.text(
            (rect.x0 + pad, rect.y0 + pad),
            self.title.upper(),
            fill="#555555",
            font=font_label,
        )

        # Status badge in top right of card
        if self.badge:
            font_badge = get_font(10, bold=True)
            bb = draw.textbbox((0, 0), self.badge, font=font_badge)
            bw = bb[2] - bb[0] + 12
            bx0 = rect.x1 - pad - bw
            draw.rounded_rectangle(
                [bx0, rect.y0 + pad - 2, rect.x1 - pad, rect.y0 + pad + 14],
                radius=4,
                fill="black",
            )
            draw.text(
                (bx0 + 6, rect.y0 + pad), self.badge, fill="white", font=font_badge
            )

        # Value & Unit
        val_str = self._resolve_value(ctx)
        vb = draw.textbbox((0, 0), val_str, font=font_val)
        vw = vb[2] - vb[0]
        val_y = rect.y0 + (32 if rect.h >= 110 else 26)
        draw.text((rect.x0 + pad, val_y), val_str, fill="black", font=font_val)

        if self.unit:
            draw.text(
                (rect.x0 + pad + vw + 4, val_y + 16),
                self.unit,
                fill="#444444",
                font=font_unit,
            )

        # Subtitle
        if self.subtitle:
            sub_y = rect.y1 - pad - 12
            sub_text = ellipsize_to_width(draw, self.subtitle, font_sub, inner_w)
            draw.text((rect.x0 + pad, sub_y), sub_text, fill="#666666", font=font_sub)


class EntityListWidget(Widget):
    """
    Displays a vertical list of entity states (Home Assistant entities or sensor rows).
    Each row has an entity name, optional secondary label, and state pill.
    """

    def __init__(self, entities: list[dict[str, Any]], border: bool = True) -> None:
        self.entities = entities
        self.border = border

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=6,
                fill="white",
                outline="black",
                width=1,
            )

        if not self.entities:
            return

        pad = 10
        row_h = (rect.h - (pad * 2)) // len(self.entities)
        font_name = get_font(13, bold=True)
        font_state = get_font(12, bold=True)
        font_sub = get_font(10, bold=False)

        for i, ent in enumerate(self.entities):
            y0 = rect.y0 + pad + i * row_h
            if i > 0:
                draw.line(
                    [(rect.x0 + pad, y0), (rect.x1 - pad, y0)], fill="#e0e0e0", width=1
                )

            name = ent.get("name", "Unknown")
            sub = ent.get("subtitle", "")
            raw_state = ent.get("state")
            source = ent.get("source")
            if source and not raw_state:
                raw_state = resolve_field_path(ctx.custom_data, source)
            state_str = str(raw_state or "off").upper()

            # Name and subtitle
            draw.text((rect.x0 + pad, y0 + 4), name, fill="black", font=font_name)
            if sub:
                draw.text((rect.x0 + pad, y0 + 20), sub, fill="#666666", font=font_sub)

            # State pill
            sb = draw.textbbox((0, 0), state_str, font=font_state)
            sw = sb[2] - sb[0] + 16
            sx0 = rect.x1 - pad - sw
            is_active = state_str in ("ON", "OPEN", "ACTIVE", "RUNNING", "HOME")
            draw.rounded_rectangle(
                [sx0, y0 + 6, rect.x1 - pad, y0 + 24],
                radius=4,
                fill="black" if is_active else "#f0f0f0",
                outline="black" if is_active else "#cccccc",
                width=1,
            )
            draw.text(
                (sx0 + 8, y0 + 8),
                state_str,
                fill="white" if is_active else "black",
                font=font_state,
            )


class TextNoticeWidget(Widget):
    """Formatted text card for travel alerts, announcements, or custom notices."""

    def __init__(
        self,
        title: str,
        body: str,
        border: bool = True,
        badge: Optional[str] = "NOTICE",
    ) -> None:
        self.title = title
        self.body = body
        self.border = border
        self.badge = badge

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=6,
                fill="white",
                outline="black",
                width=1,
            )

        pad = 12
        font_title = get_font(14, bold=True)
        font_body = get_font(12, bold=False)

        y = rect.y0 + pad
        if self.badge:
            font_badge = get_font(10, bold=True)
            bb = draw.textbbox((0, 0), self.badge, font=font_badge)
            bw = bb[2] - bb[0] + 12
            draw.rounded_rectangle(
                [rect.x0 + pad, y, rect.x0 + pad + bw, y + 16],
                radius=4,
                fill="black",
            )
            draw.text(
                (rect.x0 + pad + 6, y + 2), self.badge, fill="white", font=font_badge
            )
            draw.text(
                (rect.x0 + pad + bw + 8, y), self.title, fill="black", font=font_title
            )
            y += 24
        else:
            draw.text((rect.x0 + pad, y), self.title, fill="black", font=font_title)
            y += 20

        # Body text (wrapped)
        body_wrapped = ellipsize_to_width(
            draw, self.body, font_body, rect.w - (pad * 2)
        )
        draw.text((rect.x0 + pad, y), body_wrapped, fill="#333333", font=font_body)


class ButtonBarWidget(Widget):
    """
    Bottom tactile button bar or dormant status strip.
    Dynamically renders buttons for the active view set when interactive,
    or overpaints the status strip when dormant/idle.
    """

    def __init__(self, buttons: Optional[list[tuple[str, str]]] = None) -> None:
        self.custom_buttons = buttons

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if ctx.presentation != "interactive":
            draw_status_strip(draw, ctx.width, ctx.height, note=ctx.status_note)
            return

        if self.custom_buttons:
            # Custom buttons provided
            btn_list = self.custom_buttons
        elif ctx.available_views and len(ctx.available_views) > 1:
            # Generate buttons based on available views
            btn_list = []
            for v in ctx.available_views[:2]:
                label = v.replace("_", " ").upper()
                btn_list.append((label, v))
            btn_list.append(("☼ LIGHT", "light"))
            btn_list.append(("↻ REFRESH", "refresh"))
        else:
            # Default to standard BUSES and CITI BIKE
            draw_bottom_button_bar(
                draw,
                ctx.width,
                ctx.height,
                active_view=ctx.active_view,
            )
            return

        # Render custom button set
        is_tall = ctx.height >= 580
        btn_y0 = ctx.height - (44 if is_tall else 38)
        btn_y1 = ctx.height - (10 if is_tall else 8)
        btn_h = btn_y1 - btn_y0
        draw.line(
            [(20, btn_y0 - 8), (ctx.width - 20, btn_y0 - 8)], fill="#bbbbbb", width=1
        )

        start_x = 20
        total_w = ctx.width - 40
        gap = 10
        font = get_font(12, bold=True)
        col_w = (total_w - (len(btn_list) - 1) * gap) // len(btn_list)

        for i, (label, target_view) in enumerate(btn_list):
            is_active = target_view == ctx.active_view
            display_label = f"● {label}" if is_active else label
            x0 = start_x + i * (col_w + gap)
            x1 = x0 + col_w
            bg = "black" if is_active else "#f4f4f4"
            fg = "white" if is_active else "black"
            draw.rounded_rectangle(
                [x0, btn_y0, x1, btn_y1], radius=6, fill=bg, outline="black", width=2
            )
            bbox = draw.textbbox((0, 0), display_label, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                (x0 + (col_w - tw) // 2, btn_y0 + (btn_h - th) // 2 - 1),
                display_label,
                fill=fg,
                font=font,
            )


# ---------------------------------------------------------------------------
# Weather Widgets
# ---------------------------------------------------------------------------


class WeatherHeroWidget(Widget):
    """Prominent weather card showing current temperature, conditions, and daily stats."""

    def __init__(self, show_details: bool = True, border: bool = True) -> None:
        self.show_details = show_details
        self.border = border

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=6,
                fill="white",
                outline="black",
                width=1,
            )

        wx = ctx.weather_data
        if wx is None:
            font_msg = get_font(14, bold=True)
            draw.text(
                (rect.x0 + 16, rect.y0 + 20),
                "Weather telemetry offline",
                fill="black",
                font=font_msg,
            )
            return

        pad = 16
        font_temp = get_font(68 if rect.h >= 140 else 54, bold=True)
        font_deg = get_font(26, bold=True)
        font_cond = get_font(18, bold=True)
        font_detail = get_font(13, bold=False)
        font_label = get_font(11, bold=True)

        # "CURRENT WEATHER" label
        draw.text(
            (rect.x0 + pad, rect.y0 + pad),
            "CURRENT WEATHER",
            fill="#555555",
            font=font_label,
        )

        # Big Temp
        temp_str = f"{wx.temp}"
        tb = draw.textbbox((0, 0), temp_str, font=font_temp)
        temp_w = tb[2] - tb[0]
        temp_y = rect.y0 + pad + 18
        draw.text((rect.x0 + pad, temp_y), temp_str, fill="black", font=font_temp)
        draw.text(
            (rect.x0 + pad + temp_w + 2, temp_y + 6), "°F", fill="black", font=font_deg
        )

        # Condition & High/Low right of temperature
        cond_x = rect.x0 + pad + temp_w + 48
        draw.text((cond_x, temp_y + 4), wx.condition, fill="black", font=font_cond)

        hl_str = f"High: {wx.temp_max}°  •  Low: {wx.temp_min}°"
        draw.text((cond_x, temp_y + 30), hl_str, fill="#333333", font=font_detail)

        feels_str = f"Feels like {wx.apparent_temp}°  •  Humidity: {wx.humidity}%  •  Wind: {wx.wind_speed} mph"
        draw.text((cond_x, temp_y + 50), feels_str, fill="#555555", font=font_detail)


class WeatherForecastWidget(Widget):
    """Horizontal forecast strip showing upcoming daily weather forecasts."""

    def __init__(self, days: int = 4, border: bool = True) -> None:
        self.days = days
        self.border = border

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if self.border:
            draw.rounded_rectangle(
                [rect.x0, rect.y0, rect.x1, rect.y1],
                radius=6,
                fill="white",
                outline="black",
                width=1,
            )

        wx = ctx.weather_data
        forecasts = wx.forecast if (wx and wx.forecast) else ()
        if not forecasts:
            return

        items = list(forecasts[: self.days])
        count = len(items)
        pad = 8
        card_w = (rect.w - (pad * (count + 1))) // count

        font_day = get_font(13, bold=True)
        font_cond = get_font(11, bold=False)
        font_hl = get_font(13, bold=True)
        font_precip = get_font(11, bold=True)

        for i, f in enumerate(items):
            x0 = rect.x0 + pad + i * (card_w + pad)
            x1 = x0 + card_w
            y0 = rect.y0 + pad
            y1 = rect.y1 - pad

            draw.rounded_rectangle(
                [x0, y0, x1, y1], radius=4, fill="#f9f9f9", outline="#cccccc", width=1
            )

            # Day
            draw.text(
                (x0 + 10, y0 + 8), f.day_name.upper(), fill="black", font=font_day
            )

            # High / Low
            hl_str = f"{f.temp_max}° / {f.temp_min}°"
            draw.text((x0 + 10, y0 + 28), hl_str, fill="black", font=font_hl)

            # Condition
            cond_short = ellipsize_to_width(draw, f.condition, font_cond, card_w - 20)
            draw.text((x0 + 10, y0 + 48), cond_short, fill="#444444", font=font_cond)

            # Rain chance
            if f.precip_probability > 0:
                draw.text(
                    (x0 + 10, y0 + 66),
                    f"{f.precip_probability}% rain",
                    fill="#111111",
                    font=font_precip,
                )


# ---------------------------------------------------------------------------
# Transit Widgets
# ---------------------------------------------------------------------------


class BusHeroWidget(Widget):
    """Prominent Route 126 bus departure countdown cards for specified stops."""

    def __init__(self, stops: Optional[list[str]] = None, route: str = "126") -> None:
        self.stops = stops or ["20512", "20494"]
        self.route = route

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        from evening_view import render_evening_view

        # Delegate to battle-tested evening bus hero layout
        render_evening_view(
            draw,
            ctx.stops_data,
            ctx.citibike_data,
            ctx.now,
            batt_level=ctx.batt_level,
            is_charging=ctx.is_charging,
            is_mock=ctx.is_mock,
            stop_status=ctx.stop_status,
            width=ctx.width,
            height=ctx.height,
        )


class BusBarWidget(Widget):
    """Compact horizontal bus arrivals bar."""

    def __init__(self, stops: Optional[list[str]] = None, route: str = "126") -> None:
        self.stops = stops or ["20512", "20494"]
        self.route = route

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        pad = 8
        draw.rounded_rectangle(
            [rect.x0, rect.y0, rect.x1, rect.y1],
            radius=6,
            fill="#f9f9f9",
            outline="black",
            width=1,
        )

        font_label = get_font(11, bold=True)
        font_stop = get_font(13, bold=True)
        font_eta = get_font(13, bold=True)

        draw.text(
            (rect.x0 + pad + 2, rect.y0 + 6),
            f"ROUTE {self.route} DEPARTURES TO NYC PORT AUTHORITY",
            fill="#555555",
            font=font_label,
        )

        col_w = (rect.w - (pad * 2)) // len(self.stops)
        for i, sid in enumerate(self.stops):
            cx0 = rect.x0 + pad + i * col_w
            stop_info = next(
                (s for s in STOPS if s["id"] == sid), {"name": f"Stop #{sid}"}
            )
            trips = ctx.stops_data.get(sid, [])
            eta_str = trips[0].get("eta", "No arrivals") if trips else "No arrivals"

            s_name = stop_info.get("name", sid)
            draw.text((cx0 + 2, rect.y0 + 26), s_name, fill="black", font=font_stop)
            draw.text((cx0 + 2, rect.y0 + 44), eta_str, fill="black", font=font_eta)


class CitiBikeHeroWidget(Widget):
    """Large Citi Bike cards showing available e-bikes, classic bikes, docks, and walk times."""

    def __init__(self, stations: Optional[list[str]] = None) -> None:
        self.stations = stations

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        from morning_view import render_morning_view

        # Delegate to battle-tested morning view hero layout
        render_morning_view(
            draw,
            ctx.stops_data,
            ctx.citibike_data,
            ctx.now,
            batt_level=ctx.batt_level,
            is_charging=ctx.is_charging,
            is_mock=ctx.is_mock,
            stop_status=ctx.stop_status,
            width=ctx.width,
            height=ctx.height,
        )


class CitiBikeBarWidget(Widget):
    """Compact horizontal strip of Citi Bike station availability."""

    def __init__(self, stations: Optional[list[str]] = None) -> None:
        self.stations = stations

    def render(self, draw: Any, rect: Rect, ctx: RenderContext) -> None:
        if not ctx.citibike_data:
            return

        pad = 8
        draw.rounded_rectangle(
            [rect.x0, rect.y0, rect.x1, rect.y1],
            radius=6,
            fill="#f9f9f9",
            outline="black",
            width=1,
        )

        font_label = get_font(11, bold=True)
        draw.text(
            (rect.x0 + pad + 2, rect.y0 + 6),
            "CITI BIKE NEARBY DOCKS & E-BIKE AVAILABILITY",
            fill="#555555",
            font=font_label,
        )

        stations = ctx.citibike_data[:3]
        col_w = (rect.w - (pad * 2)) // len(stations)
        font_name = get_font(12, bold=True)
        font_stat = get_font(12, bold=False)

        for i, st in enumerate(stations):
            cx0 = rect.x0 + pad + i * col_w
            name = st.get("name", "Station")
            ebikes = st.get("ebikes", 0)
            docks = st.get("docks", 0)
            draw.text((cx0 + 2, rect.y0 + 26), name, fill="black", font=font_name)
            stat_text = f"⚡ {ebikes} e-bikes  •  {docks} docks"
            draw.text((cx0 + 2, rect.y0 + 44), stat_text, fill="black", font=font_stat)

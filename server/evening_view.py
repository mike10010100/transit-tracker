import re
from datetime import datetime
from typing import Any, Optional

from canvas import (
    HEIGHT,
    STATUS_ERROR,
    STOPS,
    WIDTH,
    draw_battery_indicator,
    draw_bottom_button_bar,
    draw_header_badge,
    draw_mock_badge,
    ellipsize_to_width,
    get_font,
    mock_badge_width,
    parse_minutes,
)


def render_evening_view(
    draw: Any,
    stops_data: dict[str, list[dict[str, Any]]],
    citibike_data: Any,
    now: datetime,
    batt_level: Optional[int],
    is_charging: bool,
    is_mock: bool,
    stop_status: Optional[dict[str, str]] = None,
    width: int = WIDTH,
    height: int = HEIGHT,
):
    """
    Afternoon / Evening View:
    NJ Transit 126 bus arrivals are the primary hero display with large minute countdowns
    and decision badges. Citi Bike dock inventory is summarized across the bottom.
    """
    has_citibike = bool(citibike_data)
    is_tall = height >= 580

    font_title = get_font(21, bold=True)
    font_header_sub = get_font(12, bold=False)
    font_time = get_font(18, bold=True)
    font_stop_name = get_font(20, bold=True)
    font_walk = get_font(12, bold=True)
    font_countdown_num = get_font(62 if has_citibike else 74, bold=True)
    font_countdown_unit = get_font(22 if has_citibike else 24, bold=True)
    font_badge = get_font(12 if has_citibike else 13, bold=True)
    font_detail = get_font(13 if has_citibike else 14, bold=False)
    font_detail_bold = get_font(13 if has_citibike else 14, bold=True)
    font_footer = get_font(11 if has_citibike else 12, bold=False)

    font_cb_tag = get_font(11, bold=True)
    font_cb_name = get_font(14, bold=True)
    font_cb_stat = get_font(13, bold=True)
    font_cb_sub = get_font(11, bold=False)
    font_cb_walk = get_font(10, bold=True)

    now_time_str = now.strftime("%-I:%M %p")
    now_date_str = now.strftime("%A, %b %-d")

    b_x1 = draw_header_badge(draw, 20, 14, "126", font_title, pad_x=16)
    draw.text(
        (b_x1 + 12, 15), "HOBOKEN → NYC PORT AUTHORITY", fill="black", font=font_title
    )
    sub_title = (
        "NJ TRANSIT 126 & CITI BIKE LIVE TRACKER"
        if has_citibike
        else "NJ TRANSIT REAL-TIME TRACKER"
    )
    draw.text((b_x1 + 12, 39), sub_title, fill="#555555", font=font_header_sub)

    time_bbox = draw.textbbox((0, 0), now_time_str, font=font_time)
    time_w = time_bbox[2] - time_bbox[0]
    time_x = width - 20 - time_w
    draw.text((time_x, 15), now_time_str, fill="black", font=font_time)

    if batt_level is not None:
        font_batt = get_font(13, bold=True)
        label = f"{batt_level}%"
        bbox = draw.textbbox((0, 0), label, font=font_batt)
        label_w = bbox[2] - bbox[0]
        bolt_w = 12 if is_charging else 0
        total_batt_w = bolt_w + label_w + 6 + 28 + 3
        batt_x = int(time_x - total_batt_w - 18)
        draw_battery_indicator(
            draw, batt_x, 16, batt_level, is_charging=is_charging, font=font_batt
        )

    date_bbox = draw.textbbox((0, 0), now_date_str, font=font_header_sub)
    date_w = date_bbox[2] - date_bbox[0]
    draw.text(
        (width - 20 - date_w, 39), now_date_str, fill="#555555", font=font_header_sub
    )

    if is_mock:
        badge_font = get_font(11, bold=True)
        draw_mock_badge(
            draw,
            int(width - 20 - date_w - mock_badge_width(draw, badge_font) - 10),
            36,
            badge_font,
        )

    draw.line([(20, 62), (width - 20, 62)], fill="black", width=2)

    col_w = (width - 40 - 20) // 2
    col_h = (346 if is_tall else 276) if has_citibike else (475 if is_tall else 345)
    card_y = 70 if has_citibike else 80
    xs = [20, 20 + col_w + 20]

    for i, stop_cfg in enumerate(STOPS):
        stop_id = str(stop_cfg["id"])
        stop_name = str(stop_cfg["name"])
        stop_subtitle = str(stop_cfg["subtitle"])
        walk_min = int(stop_cfg["walk_min"])
        x0 = xs[i]
        x1 = x0 + col_w
        y0 = card_y
        y1 = y0 + col_h

        draw.rounded_rectangle(
            [x0, y0, x1, y1], radius=10, fill="white", outline="black", width=2
        )

        pill_h = 48 if is_tall else (44 if has_citibike else 52)
        draw.rounded_rectangle(
            [x0, y0, x1, y0 + pill_h],
            radius=10,
            fill="#f2f2f2",
            outline="black",
            width=2,
        )
        draw.rectangle([x0 + 1, y0 + pill_h - 12, x1 - 1, y0 + pill_h], fill="#f2f2f2")
        draw.line([(x0, y0 + pill_h), (x1, y0 + pill_h)], fill="black", width=2)

        draw.text(
            (x0 + 12, y0 + (6 if is_tall else (5 if has_citibike else 8))),
            stop_name.upper(),
            fill="black",
            font=font_stop_name,
        )
        draw.text(
            (x0 + 12, y0 + (28 if is_tall else (26 if has_citibike else 32))),
            stop_subtitle,
            fill="#555555",
            font=font_header_sub,
        )

        walk_text = f"{walk_min} MIN WALK"
        wb = draw.textbbox((0, 0), walk_text, font=font_walk)
        ww = wb[2] - wb[0]
        walk_btn_h = 24 if is_tall else (22 if has_citibike else 24)
        walk_btn_y = y0 + (12 if is_tall else (11 if has_citibike else 14))
        draw.rounded_rectangle(
            [x1 - ww - 20, walk_btn_y, x1 - 10, walk_btn_y + walk_btn_h],
            radius=5,
            fill="black",
        )
        draw.text(
            (x1 - ww - 15, walk_btn_y + (5 if is_tall else (4 if has_citibike else 5))),
            walk_text,
            fill="white",
            font=font_walk,
        )

        arrivals = stops_data.get(stop_id, [])
        if not arrivals:
            status = (stop_status or {}).get(stop_id)
            if status == STATUS_ERROR:
                headline, subtext, headline_color = (
                    "LIVE DATA UNAVAILABLE",
                    "Could not reach NJ Transit.\nRetrying automatically.",
                    "#aa0000",
                )
            else:
                headline, subtext, headline_color = (
                    "NO BUSES IN NEXT HOUR",
                    "Off-peak schedule active or\nno buses currently tracked.",
                    "#444444",
                )
            draw.text(
                (x0 + 24, y0 + (80 if is_tall else (70 if has_citibike else 120))),
                headline,
                fill=headline_color,
                font=font_stop_name if has_citibike else font_title,
            )
            draw.text(
                (x0 + 24, y0 + (115 if is_tall else (100 if has_citibike else 155))),
                subtext,
                fill=("#aa0000" if status == STATUS_ERROR else "#666666"),
                font=font_detail,
            )
            box_h0 = y1 - (88 if is_tall else (66 if has_citibike else 85))
            box_h1 = y1 - (12 if has_citibike else 20)
            draw.rounded_rectangle(
                [x0 + 14, box_h0, x1 - 14, box_h1],
                radius=7,
                fill="#fafafa",
                outline="#bbbbbb",
                width=1,
            )
            tip = (
                "Check your network / server connection"
                if status == STATUS_ERROR
                else "Tip: Check NJ Transit app for daily timetables"
            )
            draw.text(
                (x0 + 24, box_h0 + (18 if is_tall else (16 if has_citibike else 23))),
                tip,
                fill="#666666",
                font=font_footer,
            )
        else:
            first_bus = arrivals[0]
            first_eta_raw = first_bus.get("eta", "")
            first_min = parse_minutes(first_eta_raw)
            walk_min = stop_cfg["walk_min"]

            cy = y0 + (60 if is_tall else (54 if has_citibike else 68))
            if first_min is not None:
                if first_min == 0:
                    cd_str = "DUE"
                    unit_str = ""
                else:
                    cd_str = str(first_min)
                    unit_str = "MIN"
            else:
                time_match = re.search(
                    r"\b(\d{1,2}:\d{2}(?:\s*[AP]M)?)\b", first_eta_raw, re.IGNORECASE
                )
                if time_match:
                    cd_str = time_match.group(1)
                else:
                    cd_str = first_eta_raw[:7].strip() if first_eta_raw else "--"
                unit_str = ""

            draw.text((x0 + 14, cy), cd_str, fill="black", font=font_countdown_num)
            c_bbox = draw.textbbox((x0 + 14, cy), cd_str, font=font_countdown_num)
            unit_x = c_bbox[2] + 6

            if unit_str:
                draw.text(
                    (unit_x, cy + (34 if has_citibike else 40)),
                    unit_str,
                    fill="black",
                    font=font_countdown_unit,
                )

            badge_y = cy + (14 if has_citibike else 18)
            if first_min is not None:
                if (
                    "board" in first_eta_raw.lower()
                    or "all aboard" in first_eta_raw.lower()
                ):
                    badge_label = "ALL ABOARD"
                    badge_bg = "black"
                    badge_fg = "white"
                elif first_min <= walk_min:
                    badge_label = "RUN! LEAVING SOON"
                    badge_bg = "black"
                    badge_fg = "white"
                elif first_min <= walk_min + 3:
                    badge_label = "WALK NOW"
                    badge_bg = "black"
                    badge_fg = "white"
                elif first_min <= walk_min + 7:
                    badge_label = "GET READY"
                    badge_bg = "#f0f0f0"
                    badge_fg = "black"
                else:
                    badge_label = "ON TIME"
                    badge_bg = "#f0f0f0"
                    badge_fg = "black"

                bb = draw.textbbox((0, 0), badge_label, font=font_badge)
                bw = bb[2] - bb[0]
                badge_x = x1 - bw - 24
                badge_h = 24 if has_citibike else 28
                draw.rounded_rectangle(
                    [badge_x, badge_y, badge_x + bw + 14, badge_y + badge_h],
                    radius=5,
                    fill=badge_bg,
                    outline="black",
                    width=2,
                )
                draw.text(
                    (badge_x + 7, badge_y + (4 if has_citibike else 6)),
                    badge_label,
                    fill=badge_fg,
                    font=font_badge,
                )

            card_text_max_w = (x1 - 10) - (x0 + 16)
            sched_str = ellipsize_to_width(
                draw, f"Estimated: {first_eta_raw}", font_detail, card_text_max_w
            )
            draw.text(
                (x0 + 16, cy + (80 if is_tall else (74 if has_citibike else 82))),
                sched_str,
                fill="#333333",
                font=font_detail,
            )

            bus_num = first_bus.get("vehicle_id")
            load = first_bus.get("occupancy")
            if load == "EMPTY":
                load_clean = "Seats Available"
            elif load:
                load_clean = load.replace("_", " ").title()
            else:
                load_clean = None  # no live vehicle data (schedule-only)
            if bus_num and load_clean:
                bus_meta = f"Bus #{bus_num}  •  {load_clean}"
            elif bus_num:
                bus_meta = f"Bus #{bus_num}"
            elif load_clean:
                bus_meta = f"Status: {load_clean}"
            elif first_bus.get("live"):
                bus_meta = "Live prediction"
            else:
                bus_meta = "Scheduled — no live vehicle"
            bus_meta = ellipsize_to_width(draw, bus_meta, font_detail, card_text_max_w)
            draw.text(
                (x0 + 16, cy + (102 if is_tall else (94 if has_citibike else 104))),
                bus_meta,
                fill="#444444",
                font=font_detail,
            )

            box_y0 = y1 - (88 if is_tall else (66 if has_citibike else 85))
            box_y1 = y1 - (12 if has_citibike else 16)
            draw.rounded_rectangle(
                [x0 + 12, box_y0, x1 - 12, box_y1],
                radius=7,
                fill="#f8f8f8",
                outline="black",
                width=1,
            )

            # Text must stay inside the inner box (x0+12 .. x1-12), so clamp the
            # right edge to the box interior minus the left inset.
            inner_left = x0 + 20
            inner_right = x1 - 12
            inner_max_w = inner_right - inner_left - 4

            if len(arrivals) > 1:
                next_bus = arrivals[1]
                next_eta = next_bus.get("eta", "Scheduled")
                next_bus_num = (
                    f" (Bus #{next_bus['vehicle_id']})"
                    if next_bus.get("vehicle_id")
                    else ""
                )
                draw.text(
                    (x0 + 20, box_y0 + (8 if is_tall else (9 if has_citibike else 12))),
                    "UPCOMING BUSES:",
                    fill="#555555",
                    font=font_walk,
                )
                line2 = ellipsize_to_width(
                    draw,
                    f"126 to NYC → {next_eta}{next_bus_num}",
                    font_detail_bold,
                    inner_max_w,
                )
                draw.text(
                    (
                        x0 + 20,
                        box_y0 + (25 if is_tall else (27 if has_citibike else 32)),
                    ),
                    line2,
                    fill="black",
                    font=font_detail_bold,
                )
                if is_tall and len(arrivals) > 2:
                    third_bus = arrivals[2]
                    third_eta = third_bus.get("eta", "Scheduled")
                    third_bus_num = (
                        f" (#{third_bus['vehicle_id']})"
                        if third_bus.get("vehicle_id")
                        else ""
                    )
                    line3 = ellipsize_to_width(
                        draw,
                        f"Following → {third_eta}{third_bus_num}",
                        font_detail,
                        inner_max_w,
                    )
                    draw.text(
                        (x0 + 20, box_y0 + 46), line3, fill="#555555", font=font_detail
                    )
            else:
                draw.text(
                    (x0 + 20, box_y0 + (8 if is_tall else (9 if has_citibike else 12))),
                    "UPCOMING BUSES:",
                    fill="#555555",
                    font=font_walk,
                )
                draw.text(
                    (
                        x0 + 20,
                        box_y0 + (25 if is_tall else (27 if has_citibike else 32)),
                    ),
                    "No further buses in next 60 min",
                    fill="#666666",
                    font=font_detail,
                )

    if has_citibike:
        cb_x0, cb_y0, cb_x1, cb_y1 = (
            20,
            (422 if is_tall else 348),
            width - 20,
            (542 if is_tall else 424),
        )
        draw.rounded_rectangle(
            [cb_x0, cb_y0, cb_x1, cb_y1],
            radius=10,
            fill="white",
            outline="black",
            width=2,
        )

        header_h = 26 if is_tall else 24
        draw.rounded_rectangle(
            [cb_x0, cb_y0, cb_x1, cb_y0 + header_h],
            radius=10,
            fill="#f4f4f4",
            outline="black",
            width=2,
        )
        draw.rectangle(
            [cb_x0 + 1, cb_y0 + 14, cb_x1 - 1, cb_y0 + header_h], fill="#f4f4f4"
        )
        draw.line(
            [(cb_x0, cb_y0 + header_h), (cb_x1, cb_y0 + header_h)],
            fill="black",
            width=1,
        )
        draw.text(
            (cb_x0 + 14, cb_y0 + (6 if is_tall else 5)),
            "CITI BIKE • NEAREST E-BIKE DOCKS TO 919 PARK AVE",
            fill="#444444",
            font=font_cb_tag,
        )

        body_y0 = cb_y0 + header_h
        cb_display_data = citibike_data[:3]
        col_count = len(cb_display_data)
        col_w = (cb_x1 - cb_x0) // col_count

        # Row offsets below the section header. The compact (800x480) box body is
        # only 52px tall, so its rows sit tighter than the tall layout; the tall
        # offsets previously pushed the "Docks available" line past the border.
        if is_tall:
            r_name, r_stat, r_sub, r_badge = 10, 32, 52, 70
            badge_top, badge_bot, walk_top = 8, 24, 10
        else:
            r_name, r_stat, r_sub = 5, 23, 38
            badge_top, badge_bot, walk_top = 6, 22, 7

        for i, c in enumerate(cb_display_data):
            cx0 = cb_x0 + i * col_w
            cx1 = cx0 + col_w
            if i > 0:
                draw.line(
                    [(cx0, body_y0 + 6), (cx0, cb_y1 - 6)], fill="#dddddd", width=1
                )

            walk_str = f"{c.get('walk_min', 0)} MIN"
            wb = draw.textbbox((0, 0), walk_str, font=font_cb_walk)
            ww = wb[2] - wb[0]
            badge_x0 = cx1 - ww - 18
            draw.rounded_rectangle(
                [badge_x0, body_y0 + badge_top, cx1 - 10, body_y0 + badge_bot],
                radius=4,
                fill="#eeeeee",
                outline="black",
                width=1,
            )
            draw.text(
                (cx1 - ww - 14, body_y0 + walk_top),
                walk_str,
                fill="black",
                font=font_cb_walk,
            )

            cb_name_font = font_cb_name
            max_name_w = badge_x0 - (cx0 + 14) - 6
            tb = draw.textbbox((0, 0), c["name"].upper(), font=cb_name_font)
            if (tb[2] - tb[0]) > max_name_w:
                cb_name_font = get_font(12, bold=True)
            draw.text(
                (cx0 + 14, body_y0 + r_name),
                c["name"].upper(),
                fill="black",
                font=cb_name_font,
            )

            if c.get("is_offline"):
                draw.text(
                    (cx0 + 14, body_y0 + r_stat),
                    "STATION OFFLINE",
                    fill="#777777",
                    font=font_cb_stat,
                )
                draw.text(
                    (cx0 + 14, body_y0 + r_sub),
                    "Temporarily unavailable",
                    fill="#777777",
                    font=font_cb_sub,
                )
            else:
                ebikes_count = c.get("ebikes", 0)
                classic_count = c.get("classic", 0)
                docks_count = c.get("docks", 0)
                stat_str = f"{ebikes_count} Ebikes  •  {classic_count} Classic"
                draw.text(
                    (cx0 + 14, body_y0 + r_stat),
                    stat_str,
                    fill="black",
                    font=font_cb_stat,
                )
                docks_str = f"{docks_count} Docks available"
                draw.text(
                    (cx0 + 14, body_y0 + r_sub),
                    docks_str,
                    fill="#555555",
                    font=font_cb_sub,
                )
                if is_tall:
                    if ebikes_count >= 4:
                        cb_badge = "● GOOD AVAILABILITY"
                    elif ebikes_count > 0:
                        cb_badge = "● LIMITED E-BIKES"
                    elif docks_count == 0:
                        cb_badge = "● DOCKS FULL"
                    else:
                        cb_badge = "● CLASSIC ONLY"
                    draw.text(
                        (cx0 + 14, body_y0 + r_badge),
                        cb_badge,
                        fill="#444444",
                        font=font_cb_walk,
                    )

    draw_bottom_button_bar(draw, width, height, active_view="evening")

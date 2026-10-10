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
    empty_state_message,
    get_font,
    mock_badge_width,
)


def render_morning_view(
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
    Morning Commute View:
    Citi Bike dock status is the primary hero display (3 large cards for Clinton & 9th,
    Washington & 11th, and Washington & 8th).
    NJ Transit 126 bus arrivals are displayed in a compact bottom commute bar.
    """
    is_tall = height >= 580

    font_title = get_font(21, bold=True)
    font_header_sub = get_font(12, bold=False)
    font_time = get_font(18, bold=True)
    font_card_title = get_font(16 if is_tall else 15, bold=True)
    font_walk = get_font(11, bold=True)
    font_hero_num = get_font(58 if is_tall else 52, bold=True)
    font_hero_label = get_font(13, bold=True)
    font_sub_stat = get_font(14 if is_tall else 13, bold=False)
    font_badge = get_font(11, bold=True)
    font_bus_bar_title = get_font(11, bold=True)
    font_bus_stop = get_font(14 if is_tall else 13, bold=True)
    font_bus_eta = get_font(14 if is_tall else 13, bold=True)
    font_bus_meta = get_font(13 if is_tall else 12, bold=False)

    now_time_str = now.strftime("%-I:%M %p")
    now_date_str = now.strftime("%A, %b %-d")

    time_bbox = draw.textbbox((0, 0), now_time_str, font=font_time)
    time_w = time_bbox[2] - time_bbox[0]
    time_x = width - 20 - time_w
    draw.text((time_x, 15), now_time_str, fill="black", font=font_time)

    batt_x = time_x
    if batt_level is not None:
        font_batt = get_font(13, bold=True)
        label = f"{batt_level}%"
        bbox = draw.textbbox((0, 0), label, font=font_batt)
        label_w = bbox[2] - bbox[0]
        bolt_w = 12 if is_charging else 0
        total_batt_w = bolt_w + label_w + 6 + 28 + 3
        batt_x = time_x - total_batt_w - 18
        draw_battery_indicator(
            draw, batt_x, 16, batt_level, is_charging=is_charging, font=font_batt
        )

    b_x1 = draw_header_badge(draw, 20, 14, "CITI BIKE", font_title, pad_x=12)
    morning_title = "HOBOKEN MORNING DOCKS"
    max_title_w = (batt_x - 16) - (b_x1 + 12)
    tb = draw.textbbox((0, 0), morning_title, font=font_title)
    if (tb[2] - tb[0]) > max_title_w:
        morning_title = "MORNING DOCKS"
    draw.text((b_x1 + 12, 15), morning_title, fill="black", font=font_title)
    draw.text(
        (b_x1 + 12, 39),
        "919 PARK AVE • E-BIKE PRIORITY & 126 BUS",
        fill="#555555",
        font=font_header_sub,
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
            width - 20 - date_w - mock_badge_width(draw, badge_font) - 10,
            36,
            badge_font,
        )

    draw.line([(20, 62), (width - 20, 62)], fill="black", width=2)

    cb_y0 = 70
    cb_y1 = 416 if is_tall else 346
    col_w = (width - 40 - 22) // 3
    gap = 11

    for i, c in enumerate(citibike_data[:3]):
        cx0 = 20 + i * (col_w + gap)
        cx1 = cx0 + col_w

        draw.rounded_rectangle(
            [cx0, cb_y0, cx1, cb_y1], radius=10, fill="white", outline="black", width=2
        )

        pill_h = 44
        draw.rounded_rectangle(
            [cx0, cb_y0, cx1, cb_y0 + pill_h],
            radius=10,
            fill="#f2f2f2",
            outline="black",
            width=2,
        )
        draw.rectangle(
            [cx0 + 1, cb_y0 + pill_h - 12, cx1 - 1, cb_y0 + pill_h], fill="#f2f2f2"
        )
        draw.line([(cx0, cb_y0 + pill_h), (cx1, cb_y0 + pill_h)], fill="black", width=2)

        walk_text = f"{c.get('walk_min', 0)} MIN"
        wb = draw.textbbox((0, 0), walk_text, font=font_walk)
        ww = wb[2] - wb[0]
        badge_x0 = cx1 - ww - 16
        draw.rounded_rectangle(
            [badge_x0, cb_y0 + 11, cx1 - 8, cb_y0 + 33], radius=4, fill="black"
        )
        draw.text((cx1 - ww - 12, cb_y0 + 15), walk_text, fill="white", font=font_walk)

        card_title_font = font_card_title
        max_title_w = badge_x0 - (cx0 + 10) - 6
        for size in [16 if is_tall else 15, 14, 13, 12]:
            candidate_font = get_font(size, bold=True)
            tb = draw.textbbox((0, 0), c["name"].upper(), font=candidate_font)
            if (tb[2] - tb[0]) <= max_title_w:
                card_title_font = candidate_font
                break
        else:
            card_title_font = get_font(11, bold=True)

        title_line = ellipsize_to_width(
            draw, c["name"].upper(), card_title_font, max_title_w
        )
        draw.text((cx0 + 10, cb_y0 + 6), title_line, fill="black", font=card_title_font)

        if c.get("is_offline"):
            draw.text(
                (cx0 + 14, cb_y0 + (90 if is_tall else 80)),
                "STATION OFFLINE",
                fill="#666666",
                font=font_card_title,
            )
            draw.text(
                (cx0 + 14, cb_y0 + (125 if is_tall else 110)),
                "Temporarily not renting",
                fill="#888888",
                font=font_sub_stat,
            )
        else:
            stat_y = cb_y0 + (64 if is_tall else 54)
            draw.text(
                (cx0 + 12, stat_y), str(c["ebikes"]), fill="black", font=font_hero_num
            )
            num_box = draw.textbbox(
                (cx0 + 12, stat_y), str(c["ebikes"]), font=font_hero_num
            )
            draw.text(
                (num_box[2] + 8, stat_y + 16),
                "E-BIKES",
                fill="black",
                font=font_hero_label,
            )
            draw.text(
                (num_box[2] + 8, stat_y + 34),
                "AVAILABLE",
                fill="#555555",
                font=font_walk,
            )

            div_y = stat_y + (86 if is_tall else 70)
            draw.line([(cx0 + 10, div_y), (cx1 - 10, div_y)], fill="#e0e0e0", width=1)

            draw.text(
                (cx0 + 12, div_y + (16 if is_tall else 12)),
                f"{c.get('classic', 0)} Classic Bikes",
                fill="#333333",
                font=font_sub_stat,
            )
            draw.text(
                (cx0 + 12, div_y + (40 if is_tall else 32)),
                f"{c.get('docks', 0)} Open Docks",
                fill="#333333",
                font=font_sub_stat,
            )

            badge_box_y0 = cb_y1 - (46 if is_tall else 42)
            badge_box_y1 = cb_y1 - (12 if is_tall else 12)
            draw.rounded_rectangle(
                [cx0 + 10, badge_box_y0, cx1 - 10, badge_box_y1],
                radius=6,
                fill="#f8f8f8",
                outline="black",
                width=1,
            )
            if c["ebikes"] >= 4:
                status_label = "E-BIKES READY"
            elif c["ebikes"] > 0:
                status_label = "LOW E-BIKES"
            elif c["docks"] == 0:
                status_label = "STATION FULL"
            else:
                status_label = "NO E-BIKES"
            draw.text(
                (cx0 + 18, badge_box_y0 + (9 if is_tall else 8)),
                status_label,
                fill="black",
                font=font_badge,
            )

    bus_y0 = 422 if is_tall else 348
    bus_y1 = 542 if is_tall else 424
    draw.rounded_rectangle(
        [20, bus_y0, width - 20, bus_y1],
        radius=10,
        fill="white",
        outline="black",
        width=2,
    )

    header_h = 26 if is_tall else 24
    draw.rounded_rectangle(
        [20, bus_y0, width - 20, bus_y0 + header_h],
        radius=10,
        fill="#f4f4f4",
        outline="black",
        width=2,
    )
    draw.rectangle([21, bus_y0 + 14, width - 21, bus_y0 + header_h], fill="#f4f4f4")
    draw.line(
        [(20, bus_y0 + header_h), (width - 20, bus_y0 + header_h)],
        fill="black",
        width=1,
    )
    draw.text(
        (34, bus_y0 + (6 if is_tall else 5)),
        "NJ TRANSIT 126 BUS • UPCOMING PORT AUTHORITY ARRIVALS",
        fill="#444444",
        font=font_bus_bar_title,
    )

    # Row offsets below the section header. The compact (800x480) box has only
    # 52px of content height, so its rows must sit tighter than the tall one;
    # using the tall offsets here pushed the "Following" line past the border.
    content_top = bus_y0 + header_h
    if is_tall:
        row_name, row_next, row_follow, row_third, row_empty = 10, 32, 50, 68, 34
    else:
        row_name, row_next, row_follow, row_empty = 2, 17, 34, 20

    bus_col_w = (width - 40) // 2
    for i, stop_cfg in enumerate(STOPS):
        sid = str(stop_cfg["id"])
        stop_name = str(stop_cfg["name"])
        walk_min = int(stop_cfg["walk_min"])
        bx0 = 20 + i * bus_col_w
        bx1 = bx0 + bus_col_w
        if i > 0:
            draw.line(
                [(bx0, bus_y0 + header_h + 6), (bx0, bus_y1 - 6)],
                fill="#dddddd",
                width=1,
            )

        stop_name_y = content_top + row_name
        draw.text(
            (bx0 + 14, stop_name_y),
            stop_name.upper(),
            fill="black",
            font=font_bus_stop,
        )

        walk_badge = f"{walk_min}m walk"
        wb = draw.textbbox((0, 0), walk_badge, font=font_walk)
        ww = wb[2] - wb[0]
        draw.rounded_rectangle(
            [bx1 - ww - 18, stop_name_y - 2, bx1 - 10, stop_name_y + 14],
            radius=4,
            fill="#eeeeee",
            outline="black",
            width=1,
        )
        draw.text(
            (bx1 - ww - 14, stop_name_y), walk_badge, fill="black", font=font_walk
        )

        # Column interior; text must not cross into the neighbouring column.
        col_max_w = (bx1 - 12) - (bx0 + 14)

        arrivals = stops_data.get(sid, [])
        if arrivals:
            first_bus = arrivals[0]
            eta = first_bus.get("eta", "")
            b_num = (
                f" (Bus #{first_bus['vehicle_id']})"
                if first_bus.get("vehicle_id")
                else ""
            )
            next_line = ellipsize_to_width(
                draw, f"Next: {eta}{b_num}", font_bus_eta, col_max_w
            )
            draw.text(
                (bx0 + 14, content_top + row_next),
                next_line,
                fill="black",
                font=font_bus_eta,
            )
            if len(arrivals) > 1:
                next_eta = arrivals[1].get("eta", "")
                follow_line = ellipsize_to_width(
                    draw, f"Following: {next_eta}", font_bus_meta, col_max_w
                )
                draw.text(
                    (bx0 + 14, content_top + row_follow),
                    follow_line,
                    fill="#555555",
                    font=font_bus_meta,
                )
            if is_tall and len(arrivals) > 2:
                third_eta = arrivals[2].get("eta", "")
                third_line = ellipsize_to_width(
                    draw, f"Upcoming: {third_eta}", font_bus_meta, col_max_w
                )
                draw.text(
                    (bx0 + 14, content_top + row_third),
                    third_line,
                    fill="#777777",
                    font=font_bus_meta,
                )
        else:
            status = (stop_status or {}).get(sid)
            msg, color = empty_state_message(status)
            msg_font = font_bus_meta if status == STATUS_ERROR else font_bus_eta
            msg = ellipsize_to_width(draw, msg, msg_font, col_max_w)
            draw.text(
                (bx0 + 14, content_top + row_empty), msg, fill=color, font=msg_font
            )

    draw_bottom_button_bar(draw, width, height, active_view="morning")

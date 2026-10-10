"""
Unit tests for dashboard rendering: badge thresholds, offline stations, tall
layouts, countdown parsing, mock badge, font fallback, text ellipsizing, and a
comprehensive containment harness that asserts every glyph stays inside the box
it was drawn into across both views, both heights, and a matrix of data shapes.
"""

import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import render_dashboard as rd
from PIL import Image, ImageDraw, ImageFont


def _stops(counts=None):
    """Builds stops_data with N arrivals per stop (default 3)."""
    counts = counts or {"20512": 3, "20494": 3}
    data = {}
    for sid, n in counts.items():
        data[sid] = [
            {
                "route": "126",
                "destination": "126 NEW YORK",
                "eta": "in 9 mins (11:36 PM)",
                "occupancy": "SEATS_AVAILABLE",
                "vehicle_id": str(1000 + i),
            }
            for i in range(n)
        ]
    return data


def _cb(offline=False):
    return [
        {
            "id": "x",
            "name": "Clinton & 9th",
            "full_name": "Clinton & 9th",
            "walk_min": 3,
            "distance_m": 246,
            "ebikes": 5,
            "classic": 12,
            "total_bikes": 17,
            "docks": 9,
            "is_offline": offline,
            "is_returning": True,
        }
    ]


class TestFontFallback(unittest.TestCase):
    def test_returns_default_when_no_paths_exist(self):
        with patch.object(rd.os.path, "exists", return_value=False):
            font = rd.get_font(12)
            self.assertIsNotNone(font)

    def test_returns_none_when_load_default_raises(self):
        with patch.object(rd.os.path, "exists", return_value=False):
            with patch.object(
                rd.ImageFont, "load_default", side_effect=Exception("boom")
            ):
                self.assertIsNone(rd.get_font(12))

    def test_truetype_failure_falls_through(self):
        with patch.object(rd.os.path, "exists", return_value=True):
            with patch.object(
                rd.ImageFont, "truetype", side_effect=Exception("bad font")
            ):
                with patch.object(
                    rd.ImageFont, "load_default", return_value="default-font"
                ):
                    self.assertEqual(rd.get_font(12), "default-font")


class TestParseMinutes(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(rd.parse_minutes(""))
        self.assertIsNone(rd.parse_minutes(None))

    def test_zero_phrases(self):
        for phrase in ["APPROACHING", "due now", "All Aboard", "now boarding", "BOARD"]:
            with self.subTest(phrase=phrase):
                self.assertEqual(rd.parse_minutes(phrase), 0, phrase)

    def test_numeric(self):
        self.assertEqual(rd.parse_minutes("in 9 mins (11:36 PM)"), 9)
        self.assertEqual(rd.parse_minutes("3 min"), 3)

    def test_unparseable(self):
        self.assertIsNone(rd.parse_minutes("Scheduled"))


class TestDrawHelpers(unittest.TestCase):
    def _draw(self):
        img = Image.new("RGB", (400, 200), "white")
        return ImageDraw.Draw(img)

    def test_draw_header_badge_returns_right_edge(self):
        d = self._draw()
        x1 = rd.draw_header_badge(d, 20, 14, "126", rd.get_font(18, bold=True))
        self.assertGreater(x1, 20)

    def test_mock_badge_width_positive(self):
        d = self._draw()
        self.assertGreater(rd.mock_badge_width(d, rd.get_font(11, bold=True)), 0)
        self.assertGreater(rd.draw_mock_badge(d, 10, 10), 10)

    def test_battery_indicator_none_returns_zero(self):
        d = self._draw()
        self.assertEqual(rd.draw_battery_indicator(d, 0, 0, None), 0)
        self.assertEqual(rd.draw_battery_indicator(d, 0, 0, -1), 0)

    def test_battery_indicator_charging_draws(self):
        d = self._draw()
        self.assertGreater(
            rd.draw_battery_indicator(
                d, 0, 0, 55, is_charging=True, font=rd.get_font(12)
            ),
            0,
        )


class TestStatusPresentation(unittest.TestCase):
    def test_draw_status_strip_paints_bottom(self):
        img = Image.new("RGB", (800, 480), "white")
        draw = ImageDraw.Draw(img)
        rd.draw_status_strip(draw, 800, 480, note="PRESS POWER BUTTON TO INTERACT")
        # The bottom strip must have been painted (no longer pure white).
        self.assertNotEqual(img.getpixel((400, 460)), (255, 255, 255))

    def test_non_interactive_render_differs_from_interactive(self):
        live_out = "/tmp/test_interactive_pres.png"
        idle_out = "/tmp/test_idle_pres.png"
        dormant_out = "/tmp/test_dormant_pres.png"
        try:
            rd.render_dashboard(
                _stops(),
                citibike_data=_cb(),
                output_path=live_out,
                view="evening",
                is_mock=True,
                width=800,
                height=480,
                presentation="interactive",
            )
            rd.render_dashboard(
                _stops(),
                citibike_data=_cb(),
                output_path=idle_out,
                view="evening",
                is_mock=True,
                width=800,
                height=480,
                presentation="idle",
                status_note="PRESS POWER BUTTON TO INTERACT",
            )
            rd.render_dashboard(
                _stops(),
                citibike_data=_cb(),
                output_path=dormant_out,
                view="evening",
                is_mock=True,
                width=800,
                height=480,
                presentation="dormant",
                status_note="SLEEPING — back at 6:00 AM",
            )
            with (
                Image.open(live_out) as live,
                Image.open(idle_out) as idle,
                Image.open(dormant_out) as dormant,
            ):
                self.assertEqual(live.size, idle.size)
                self.assertNotEqual(live.tobytes(), idle.tobytes())
                self.assertNotEqual(idle.tobytes(), dormant.tobytes())
        finally:
            for p in (live_out, idle_out, dormant_out):
                if os.path.exists(p):
                    os.remove(p)


class TestEmptyStateMessage(unittest.TestCase):
    def test_error_vs_empty_vs_ok(self):
        err, err_c = rd.empty_state_message(rd.STATUS_ERROR)
        empty, empty_c = rd.empty_state_message(rd.STATUS_EMPTY)
        ok, ok_c = rd.empty_state_message(rd.STATUS_OK)
        self.assertIn("unavailable", err.lower())
        self.assertEqual(empty, ok)
        self.assertNotEqual(err, empty)
        self.assertEqual(empty_c, ok_c)
        self.assertNotEqual(err_c, empty_c)


class TestRenderingBranches(unittest.TestCase):
    def setUp(self):
        self.out = "/tmp/test_render_branch.png"

    def tearDown(self):
        if os.path.exists(self.out):
            os.remove(self.out)

    def test_morning_tall_with_offline_station(self):
        rd.render_dashboard(
            _stops({"20512": 3, "20494": 3}),
            citibike_data=_cb(offline=True),
            output_path=self.out,
            view="morning",
            is_mock=True,
            batt_level=90,
            is_charging=True,
            width=800,
            height=600,
        )
        with Image.open(self.out) as img:
            self.assertEqual(img.size, (800, 600))

    def test_morning_wide_without_tall(self):
        rd.render_dashboard(
            _stops(),
            citibike_data=_cb(),
            output_path=self.out,
            view="morning",
            is_mock=True,
            width=800,
            height=480,
        )
        with Image.open(self.out) as img:
            self.assertEqual(img.size, (800, 480))

    def test_morning_error_empty_states(self):
        rd.render_dashboard(
            {"20512": [], "20494": []},
            citibike_data=_cb(),
            output_path=self.out,
            view="morning",
            stop_status={"20512": rd.STATUS_ERROR, "20494": rd.STATUS_EMPTY},
        )
        self.assertTrue(os.path.exists(self.out))

    def test_evening_tall_with_all_badge_types(self):
        # Build arrivals producing each countdown/badge class.
        cases = {
            "20512": [
                {"eta": "in 1 mins", "vehicle_id": "1", "occupancy": "EMPTY"},
                {"eta": "in 12 mins", "vehicle_id": "2", "occupancy": "HALF_EMPTY"},
                {
                    "eta": "in 30 mins",
                    "vehicle_id": "3",
                    "occupancy": "SEATS_AVAILABLE",
                },
            ],
            "20494": [
                {"eta": "ALL ABOARD", "vehicle_id": "4", "occupancy": "EMPTY"},
            ],
        }
        rd.render_dashboard(
            cases,
            citibike_data=_cb(),
            output_path=self.out,
            view="evening",
            is_mock=True,
            batt_level=42,
            is_charging=False,
            width=800,
            height=600,
        )
        with Image.open(self.out) as img:
            self.assertEqual(img.size, (800, 600))

    def test_evening_time_eta_fallback(self):
        cases = {
            "20512": [{"eta": "Scheduled", "vehicle_id": None, "occupancy": None}],
            "20494": [{"eta": "8:35 AM", "vehicle_id": None, "occupancy": None}],
        }
        rd.render_dashboard(
            cases,
            citibike_data=_cb(),
            output_path=self.out,
            view="evening",
        )
        self.assertTrue(os.path.exists(self.out))

    def test_evening_without_citibike_uses_full_height(self):
        rd.render_dashboard(
            _stops(),
            citibike_data=[],
            output_path=self.out,
            view="evening",
            width=800,
            height=480,
        )
        self.assertTrue(os.path.exists(self.out))

    def test_evening_offline_citibike(self):
        rd.render_dashboard(
            _stops(),
            citibike_data=_cb(offline=True),
            output_path=self.out,
            view="evening",
            is_mock=True,
            width=800,
            height=600,
        )
        self.assertTrue(os.path.exists(self.out))

    def test_evening_citibike_badge_thresholds(self):
        for ebikes, docks in [(5, 9), (2, 9), (0, 0), (0, 4)]:
            with self.subTest(ebikes=ebikes, docks=docks):
                cb = _cb()
                cb[0]["ebikes"], cb[0]["docks"] = ebikes, docks
                rd.render_dashboard(
                    _stops(),
                    citibike_data=cb,
                    output_path=self.out,
                    view="evening",
                    is_mock=True,
                    width=800,
                    height=600,
                )
                self.assertTrue(os.path.exists(self.out))
                os.remove(self.out)

    def test_resolve_view_default_hour_branch(self):
        # Forces the datetime.now() branch (hour=None).
        self.assertIn(rd.resolve_view("auto"), ("morning", "evening"))
        self.assertEqual(rd.resolve_view("MORNING"), "morning")
        self.assertEqual(rd.resolve_view("  BUS  "), "evening")

    def test_render_autodetects_citibike_when_none(self):
        with patch.object(rd.CitiBikeTracker, "get_station_status", return_value=_cb()):
            rd.render_dashboard(
                _stops(), citibike_data=None, output_path=self.out, view="morning"
            )
        self.assertTrue(os.path.exists(self.out))


class TestTextStaysInsideContainers(unittest.TestCase):
    """
    Comprehensive containment harness.

    Every render is instrumented to capture each drawn text bounding box and
    every rounded-rectangle (container) it was drawn into. For each text we find
    its innermost enclosing container and assert the glyph box stays fully
    inside it. This is data-driven across both views, both heights, and a matrix
    of arrival/station permutations, so any layout that pushes text outside a box
    fails the suite (this is the class of bug that produced the reported
    'Following' overflow).
    """

    CONTAINER_CALLS = []
    TEXT_CALLS = []
    _orig_text = None
    _orig_rrect = None

    @classmethod
    def setUpClass(cls):
        from PIL import ImageDraw

        cls._orig_text = ImageDraw.ImageDraw.text
        cls._orig_rrect = ImageDraw.ImageDraw.rounded_rectangle

        def cap_rrect(self, xy, *a, **k):
            cls.CONTAINER_CALLS.append(tuple(xy))
            return cls._orig_rrect(self, xy, *a, **k)

        def cap_text(self, xy, text, *a, **k):
            try:
                bbox = self.textbbox(xy, text, font=k.get("font"))
            except Exception:
                bbox = None
            cls.TEXT_CALLS.append((xy, text, bbox))
            return cls._orig_text(self, xy, text, *a, **k)

        ImageDraw.ImageDraw.rounded_rectangle = cap_rrect
        ImageDraw.ImageDraw.text = cap_text

    @classmethod
    def tearDownClass(cls):
        from PIL import ImageDraw

        ImageDraw.ImageDraw.text = cls._orig_text
        ImageDraw.ImageDraw.rounded_rectangle = cls._orig_rrect

    def _render_and_collect(self, stops, cb, view, height, scale=1.0, **kwargs):
        type(self).CONTAINER_CALLS = []
        type(self).TEXT_CALLS = []
        rd.render_dashboard(
            stops,
            citibike_data=cb,
            output_path="/tmp/_containment.png",
            view=view,
            is_mock=True,
            batt_level=77,
            width=800,
            height=height,
            scale=scale,
            **kwargs,
        )
        return list(self.CONTAINER_CALLS), list(self.TEXT_CALLS)

    def _assert_all_contained(self, containers, texts, context):
        for xy, text, bbox in texts:
            if not bbox:
                continue
            left, top, right, bottom = bbox
            tx, ty = xy
            enclosing = [
                c for c in containers if c[0] <= tx <= c[2] and c[1] <= ty <= c[3]
            ]
            if not enclosing:
                continue
            c = min(enclosing, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
            self.assertLessEqual(
                bottom,
                c[3],
                f"[{context}] text {text[:50]!r} bottom {bottom} exceeds container {c}",
            )
            self.assertLessEqual(
                right,
                c[2] - 1,
                f"[{context}] text {text[:50]!r} right {right} exceeds container {c}",
            )
            self.assertGreaterEqual(
                left, c[0], f"[{context}] text {text[:50]!r} left {left} < {c}"
            )
            self.assertGreaterEqual(
                top, c[1], f"[{context}] text {text[:50]!r} top {top} < {c}"
            )

    @staticmethod
    def _arrivals(n, eta, dest, with_vid=True):
        return [
            {
                "route": "126",
                "destination": dest,
                "eta": eta,
                "occupancy": "SEATS_AVAILABLE",
                "vehicle_id": (str(20000 + i) if with_vid else None),
            }
            for i in range(n)
        ]

    @staticmethod
    def _cbset(offline=False, zero=False):
        from citibike import CitiBikeTracker

        base = CitiBikeTracker().get_mock_data()
        names = [
            "Clinton & 9th",
            "Washington & 11th EXTREMELY LONG STATION NAME",
            "Grand & 6th",
        ]
        for i, nm in enumerate(names):
            base[i]["name"] = nm
        if offline:
            base[0] = {
                **base[0],
                "is_offline": True,
                "ebikes": 0,
                "classic": 0,
                "docks": 0,
            }
        if zero:
            base[0] = {**base[0], "ebikes": 0, "classic": 0, "docks": 0}
        return base

    def test_all_boxes_contain_their_text(self):
        permutations = {
            "normal": ("126 NEW YORK", "in 17 mins (8:47 AM)", 3, True),
            "long_eta": (
                "126 NEW YORK VIA LINCOLN TUNNEL",
                "APPROACHING (11:47 PM)",
                3,
                True,
            ),
            "long_dest": (
                "126 NEW YORK VIA CLINTON & LINCOLN TUNNEL EXTRA",
                "in 5 mins",
                1,
                True,
            ),
            "many": ("126 NEW YORK", "in 5 mins", 5, True),
            "no_vid": ("126 NEW YORK", "in 5 mins", 3, False),
            "single": ("126 NEW YORK", "in 5 mins", 1, True),
            "huge": (
                "126 NEW YORK VIA EVERYTHING",
                "APPROACHING NOW BOARDING (11:47 PM)",
                4,
                True,
            ),
            "scheduled": ("", "Scheduled", 2, True),
        }
        cb_perms = {
            "normal": (False, False),
            "offline": (True, False),
            "zero": (False, True),
        }

        for view in ("morning", "evening"):
            for height in (480, 600):
                for pname, (dest, eta, n, vid) in permutations.items():
                    for cbname, (offline, zero) in cb_perms.items():
                        stops = {
                            "20512": self._arrivals(n, eta, dest, vid),
                            "20494": self._arrivals(n, eta, dest, vid),
                        }
                        ctx = f"{view} h={height} {pname}/{cbname}"
                        containers, texts = self._render_and_collect(
                            stops,
                            self._cbset(offline, zero),
                            view,
                            height,
                        )
                        # Guard against a vacuous pass: ensure instrumentation
                        # actually observed boxes and glyphs for every render.
                        self.assertGreater(
                            len(containers), 3, f"[{ctx}] no containers captured"
                        )
                        self.assertGreater(len(texts), 3, f"[{ctx}] no text captured")
                        self._assert_all_contained(containers, texts, ctx)

                # Empty/error states
                ctx = f"{view} h={height} empty/error"
                containers, texts = self._render_and_collect(
                    {"20512": [], "20494": []},
                    self._cbset(),
                    view,
                    height,
                    stop_status={"20512": rd.STATUS_ERROR, "20494": rd.STATUS_EMPTY},
                )
                self._assert_all_contained(containers, texts, ctx)

                # Evening with no Citi Bike data (full-height bus cards)
                if view == "evening":
                    ctx = f"{view} h={height} no-citibike"
                    containers, texts = self._render_and_collect(
                        {
                            "20512": self._arrivals(3, "in 5 mins", "126 NEW YORK"),
                            "20494": self._arrivals(3, "in 5 mins", "126 NEW YORK"),
                        },
                        [],
                        view,
                        height,
                    )
                    self._assert_all_contained(containers, texts, ctx)

    def test_containment_holds_at_native_kindle_scale(self):
        """
        Native Kindle rendering scales all geometry and fonts by ~2.06 instead
        of resampling. The containment invariant must still hold in native
        coordinate space (where ScaledDraw forwards to the real ImageDraw).
        """
        scale = 1648 / 800
        stops = {
            "20512": self._arrivals(
                3,
                "APPROACHING NOW BOARDING (11:47 PM)",
                "126 NEW YORK VIA LINCOLN TUNNEL",
            ),
            "20494": self._arrivals(
                4, "in 17 mins (8:47 AM)", "126 NEW YORK VIA CLINTON"
            ),
        }
        for view in ("morning", "evening"):
            for height in (600,):
                ctx = f"native {view} h={height}"
                containers, texts = self._render_and_collect(
                    stops,
                    self._cbset(),
                    view,
                    height,
                    scale=scale,
                )
                self.assertGreater(
                    len(containers), 3, f"[{ctx}] no containers captured"
                )
                self.assertGreater(len(texts), 3, f"[{ctx}] no text captured")
                self._assert_all_contained(containers, texts, ctx)


class TestScaledDraw(unittest.TestCase):
    def _proxy(self, scale):
        img = Image.new("RGB", (1000, 1000), "white")
        real = ImageDraw.Draw(img)
        return img, rd.ScaledDraw(real, scale), real

    def test_scale_one_is_identity(self):
        img, sd, real = self._proxy(1.0)
        sd.text((10, 20), "Hi", fill="black", font=rd.get_font(12))
        a1 = real.textbbox((0, 0), "Hi", font=rd.get_font(12))
        a2 = sd.textbbox((0, 0), "Hi", font=rd.get_font(12))
        self.assertEqual(a1, a2)

    def test_textbbox_is_returned_in_logical_units(self):
        img, sd, real = self._proxy(2.0)
        font = rd.get_font(20)
        logical = sd.textbbox((0, 0), "Hello", font=font)
        # Logical bbox should be about half the native glyph size.
        native = real.textbbox((0, 0), "Hello", font=sd._font(font))
        self.assertLess(logical[2] - logical[0], native[2] - native[0])
        self.assertAlmostEqual(
            (native[2] - native[0]) / 2, logical[2] - logical[0], delta=3
        )

    def test_fonts_are_reconstructed_larger(self):
        img, sd, real = self._proxy(3.0)
        font = rd.get_font(10)
        scaled = sd._font(font)
        self.assertGreater(scaled.size, font.size)

    def test_accepts_flat_and_nested_boxes(self):
        img, sd, real = self._proxy(2.0)
        sd.rectangle([0, 0, 10, 10], outline="black")
        sd.rectangle([(0, 0), (10, 10)], outline="black")
        sd.rounded_rectangle([0, 0, 10, 10], radius=2, outline="black")
        sd.rounded_rectangle([(0, 0), (10, 10)], radius=2, outline="black")

    def test_native_canvas_is_scaled(self):
        img, sd, real = self._proxy(2.0)
        sd.line([(0, 0), (50, 50)], fill="black", width=2)
        # A logical (50,50) endpoint maps to native (100,100).
        self.assertEqual(sd._pt((50, 50)), (100.0, 100.0))

    def test_untagged_font_is_passed_through(self):
        # A font without the transit tags cannot be rescaled; it is used as-is.
        img, sd, real = self._proxy(2.0)
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ]
        plain = None
        for p in font_paths:
            if os.path.exists(p):
                try:
                    plain = ImageFont.truetype(p, 12)
                    break
                except Exception:
                    continue
        if plain is None:
            plain = ImageFont.load_default()
        self.assertIs(sd._font(plain), plain)

    def test_none_font_is_passed_through(self):
        img, sd, real = self._proxy(2.0)
        self.assertIsNone(sd._font(None))

    def test_font_cache_returns_same_object(self):
        img, sd, real = self._proxy(2.0)
        font = rd.get_font(12)
        self.assertIs(sd._font(font), sd._font(font))

    def test_outline_none_and_scaling(self):
        img, sd, real = self._proxy(2.0)
        self.assertIsNone(sd._outline(None))
        self.assertEqual(sd._outline(2), 4)
        self.assertEqual(sd._outline(1), 2)

    def test_textlength_is_logical(self):
        img, sd, real = self._proxy(2.0)
        font = rd.get_font(14)
        logical = sd.textlength("Hello", font=font)
        native = real.textlength("Hello", font=sd._font(font))
        self.assertAlmostEqual(native / 2, logical, delta=2)

    def test_polygon_scales(self):
        img, sd, real = self._proxy(2.0)
        sd.polygon([(0, 0), (10, 0), (5, 10)], fill="black")


class TestEllipsizeToWidth(unittest.TestCase):
    def _draw(self):
        return ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def test_short_text_unchanged(self):
        d = self._draw()
        self.assertEqual(rd.ellipsize_to_width(d, "OK", rd.get_font(12), 200), "OK")

    def test_long_text_truncated_within_budget(self):
        d = self._draw()
        font = rd.get_font(14, bold=True)
        text = "126 to NYC → APPROACHING NOW BOARDING (11:47 PM) (Bus #25248)"
        budget = 120
        result = rd.ellipsize_to_width(d, text, font, budget)
        self.assertLessEqual(d.textbbox((0, 0), result, font=font)[2], budget)
        self.assertTrue(result.endswith("…"))
        self.assertLess(len(result), len(text))

    def test_zero_or_negative_budget_returns_original(self):
        d = self._draw()
        self.assertEqual(rd.ellipsize_to_width(d, "abc", rd.get_font(12), 0), "abc")
        self.assertEqual(rd.ellipsize_to_width(d, "abc", rd.get_font(12), -5), "abc")

    def test_empty_text(self):
        d = self._draw()
        self.assertEqual(rd.ellipsize_to_width(d, "", rd.get_font(12), 100), "")

    def test_ellipsis_exceeds_budget_returns_empty(self):
        d = self._draw()
        font = rd.get_font(20, bold=True)
        self.assertEqual(rd.ellipsize_to_width(d, "hello", font, 1), "")


class TestUncoveredDashboardBranches(unittest.TestCase):
    def test_modular_view_and_canvas_imports(self):
        import canvas
        import evening_view
        import morning_view

        self.assertEqual(canvas.WIDTH, 800)
        self.assertTrue(callable(morning_view.render_morning_view))
        self.assertTrue(callable(evening_view.render_evening_view))

    def test_draw_battery_indicator_without_font(self):
        img = Image.new("RGB", (100, 100), "white")
        draw = ImageDraw.Draw(img)
        w = rd.draw_battery_indicator(draw, 10, 10, 75, is_charging=False, font=None)
        self.assertGreater(w, 0)

    def test_draw_bottom_button_bar_default_font(self):
        img = Image.new("RGB", (800, 480), "white")
        draw = ImageDraw.Draw(img)
        rd.draw_bottom_button_bar(draw, 800, 480, font=None)

    def test_scaled_draw_font_exception(self):
        img = Image.new("RGB", (100, 100), "white")
        sd = rd.ScaledDraw(ImageDraw.Draw(img), 1.5)
        mock_font = MagicMock()
        mock_font._transit_path = "/nonexistent/fake.ttf"
        mock_font._transit_size = 14
        f = sd._font(mock_font)
        self.assertEqual(f, mock_font)

    def test_render_morning_view_narrow_title_and_no_ebikes_status(self):
        img = Image.new("RGB", (600, 480), "white")
        draw = rd.ScaledDraw(ImageDraw.Draw(img), 1.0)
        citi_data = [
            {
                "name": "9th & Clinton",
                "walk_min": 3,
                "ebikes": 0,
                "classic": 10,
                "total_bikes": 10,
                "docks": 5,
                "is_offline": False,
                "is_returning": True,
            }
        ]
        stops_data = {"20512": []}
        now = datetime(2026, 10, 10, 8, 30)
        rd.render_morning_view(
            draw,
            stops_data,
            citi_data,
            now,
            batt_level=80,
            is_charging=False,
            is_mock=False,
            width=400,
            height=480,
        )

    def test_render_evening_view_bus_meta_variations(self):
        img = Image.new("RGB", (800, 480), "white")
        draw = rd.ScaledDraw(ImageDraw.Draw(img), 1.0)
        now = datetime(2026, 10, 10, 18, 30)
        stops_data_1 = {
            "20512": [
                {
                    "route": "126",
                    "destination": "126 NEW YORK",
                    "eta": "in 5 mins",
                    "live": True,
                    "vehicle_id": "9999",
                    "occupancy": None,
                }
            ]
        }
        rd.render_evening_view(
            draw,
            stops_data_1,
            citibike_data=[],
            now=now,
            batt_level=80,
            is_charging=False,
            is_mock=False,
            width=800,
            height=480,
        )

        stops_data_2 = {
            "20512": [
                {
                    "route": "126",
                    "destination": "126 NEW YORK",
                    "eta": "in 8 mins",
                    "live": True,
                    "vehicle_id": None,
                    "occupancy": None,
                }
            ]
        }
        rd.render_evening_view(
            draw,
            stops_data_2,
            citibike_data=[],
            now=now,
            batt_level=80,
            is_charging=False,
            is_mock=False,
            width=800,
            height=480,
        )

    def test_render_dashboard_citibike_fetch_exception(self):
        out_path = "/tmp/test_citibike_err.png"
        with patch(
            "citibike.CitiBikeTracker.get_station_status",
            side_effect=Exception("GBFS down"),
        ):
            with patch(
                "citibike.CitiBikeTracker.get_mock_data",
                side_effect=Exception("Mock down"),
            ):
                res = rd.render_dashboard(
                    stops_data={"20512": []},
                    citibike_data=None,
                    output_path=out_path,
                    is_mock=False,
                )
                self.assertEqual(res, out_path)
                if os.path.exists(out_path):
                    os.remove(out_path)


if __name__ == "__main__":
    unittest.main()

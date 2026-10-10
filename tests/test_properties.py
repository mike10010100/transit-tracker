import io
import os
import unittest

import identity
import render_dashboard
import schedule
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

# Configure hypothesis profiles for fast local testing and robust CI testing
settings.register_profile("ci", max_examples=50, deadline=None)
settings.register_profile("dev", max_examples=20, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))


class TestDashboardProperties(unittest.TestCase):
    @given(
        dest=st.text(min_size=0, max_size=200),
        route=st.text(min_size=0, max_size=50),
        eta=st.one_of(
            st.integers(min_value=-1000, max_value=10000),
            st.text(min_size=0, max_size=100),
        ),
        view=st.sampled_from(["auto", "morning", "evening", "other"]),
        presentation=st.sampled_from(["interactive", "idle", "dormant"]),
        batt_level=st.one_of(st.none(), st.integers(min_value=-50, max_value=150)),
        is_charging=st.booleans(),
        status_note=st.text(min_size=0, max_size=100),
    )
    def test_render_dashboard_invariants(
        self,
        dest: str,
        route: str,
        eta: object,
        view: str,
        presentation: str,
        batt_level: object,
        is_charging: bool,
        status_note: str,
    ):
        """Property: render_dashboard must never crash or raise unhandled exceptions across arbitrary input parameters."""
        eta_str = str(eta)
        stops_data = {
            "20495": [
                {
                    "route": route,
                    "destination": dest,
                    "eta": eta_str,
                    "occupancy": "MANY_SEATS_AVAILABLE",
                    "vehicle_id": "1234",
                }
            ],
            "20494": [],
        }
        citibike_data = [
            {
                "name": dest,
                "bikes": 5,
                "ebikes": 2,
                "docks": 10,
            }
        ]
        buf = io.BytesIO()
        render_dashboard.render_dashboard(
            stops_data=stops_data,
            citibike_data=citibike_data,
            output_path=buf,
            view=view,
            presentation=presentation,
            batt_level=batt_level if isinstance(batt_level, int) else None,
            is_charging=is_charging,
            status_note=status_note,
        )

        # Output must be a valid non-empty PNG
        buf.seek(0)
        img = Image.open(buf)
        self.assertEqual(img.size, (render_dashboard.WIDTH, render_dashboard.HEIGHT))


class TestIdentityProperties(unittest.TestCase):
    @given(nonce=st.text(min_size=0, max_size=100))
    def test_nonce_validation_property(self, nonce: str):
        """Property: is_valid_nonce is true IF AND ONLY IF nonce is 32 lowercase hex characters."""
        result = identity.is_valid_nonce(nonce)
        expected = len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce)
        self.assertEqual(result, expected)

    @given(
        nonce=st.from_regex(r"\A[0-9a-f]{32}\Z"),
        path=st.text(min_size=1, max_size=50).filter(
            lambda p: "\r" not in p and "\n" not in p
        ),
        status=st.integers(min_value=100, max_value=599),
        body=st.binary(min_size=0, max_size=1024),
        headers=st.dictionaries(
            keys=st.text(min_size=1, max_size=30).filter(
                lambda k: "\r" not in k and "\n" not in k and ":" not in k
            ),
            values=st.text(min_size=0, max_size=100).filter(
                lambda v: "\r" not in v and "\n" not in v
            ),
            max_size=10,
        ),
    )
    def test_response_message_deterministic_property(
        self,
        nonce: str,
        path: str,
        status: int,
        body: bytes,
        headers: dict,
    ):
        """Property: build_response_message is deterministic and contains required §3 elements."""
        msg1 = identity.build_response_message(nonce, path, status, body, headers)
        msg2 = identity.build_response_message(nonce, path, status, body, headers)
        self.assertEqual(msg1, msg2)
        lines = msg1.decode("utf-8").split("\n")
        self.assertEqual(lines[0], identity.RESP_FORMAT)
        self.assertEqual(lines[1], f"nonce={nonce}")
        self.assertEqual(lines[2], f"path={path}")
        self.assertEqual(lines[3], f"status={status}")

    @given(
        crlf=st.sampled_from(["\r", "\n", "\r\n"]),
        text=st.text(min_size=1, max_size=20),
    )
    def test_crlf_rejection_property(self, crlf: str, text: str):
        """Property: CR or LF in nonce, path, or headers is always rejected with IdentityError."""
        bad_val = f"{text}{crlf}{text}"
        with self.assertRaises(identity.IdentityError):
            identity.build_response_message(bad_val, "/path", 200, b"", {})
        with self.assertRaises(identity.IdentityError):
            identity.build_response_message("0" * 32, bad_val, 200, b"", {})
        with self.assertRaises(identity.IdentityError):
            identity.build_response_message(
                "0" * 32, "/path", 200, b"", {"x-tracker-view": bad_val}
            )


class TestScheduleProperties(unittest.TestCase):
    @given(
        now_hour=st.integers(min_value=0, max_value=23),
        now_minute=st.integers(min_value=0, max_value=59),
        now_weekday=st.integers(min_value=0, max_value=6),
    )
    def test_schedule_interval_bounds_property(
        self,
        now_hour: int,
        now_minute: int,
        now_weekday: int,
    ):
        """Property: Schedule resolution always maps to valid interval and expected schedule buckets."""
        # Check resolve_view
        v1 = render_dashboard.resolve_view("auto", hour=now_hour)
        self.assertIn(v1, ("morning", "evening"))
        v2 = render_dashboard.resolve_view("morning", hour=now_hour)
        self.assertEqual(v2, "morning")
        v3 = render_dashboard.resolve_view("evening", hour=now_hour)
        self.assertEqual(v3, "evening")

        # Check is_peak_commute_hours and commute lighting
        from datetime import datetime

        dt = datetime(2026, 10, 12 + (now_weekday % 7), now_hour, now_minute)
        peak = schedule.is_peak_commute_hours(dt)
        self.assertIsInstance(peak, bool)

        lighting = schedule.get_commute_lighting(dt)
        self.assertGreaterEqual(lighting.brightness, 0)
        self.assertGreaterEqual(lighting.warmth, 0)

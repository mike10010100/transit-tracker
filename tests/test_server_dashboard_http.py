"""
Integration tests for the dashboard route, OTA tracker-arm endpoint, and
discovery/image formatting helpers.
"""

import base64
import hashlib
import io
import json
import os
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import discovery
import ota
import schedule
from PIL import Image
from state_machine import ConfigStore
from test_server_http import ServerHTTPTestBase, _auth_headers, _http_get

import server
from server import format_for_kindle, sha256_file


class TestDashboardRoute(ServerHTTPTestBase):
    def test_mock_dashboard_standard(self):
        status, headers, body = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertIn("X-Kindle-Poll-Interval", headers)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (800, 480))

    def test_mock_dashboard_kindle_rotation(self):
        status, headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5&rotate=90"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Resolved-View") in ("morning", "evening"), True)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (1236, 1648))
        self.assertEqual(img.mode, "L")

    def test_kindle_custom_panel_dimensions_reported_natively(self):
        status, _headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5&w=1648&h=1236&rotate=90"
        )
        self.assertEqual(status, 200)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (1236, 1648))
        self.assertEqual(img.mode, "L")

    def test_kindle_smaller_panel(self):
        status, _headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5&w=1448&h=1072&rotate=90"
        )
        self.assertEqual(status, 200)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (1072, 1448))

    def test_kindle_bad_dimensions_fall_back(self):
        status, _headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5&w=abc&h=xyz&rotate=90"
        )
        self.assertEqual(status, 200)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (1236, 1648))

    def test_kindle_buffer_sized_dimensions_rejected(self):
        status, _headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5&w=3296&h=1248&rotate=90"
        )
        self.assertEqual(status, 200)
        img = Image.open(io.BytesIO(body))
        self.assertEqual(img.size, (1236, 1648))
        self.assertEqual(server.sanitize_kindle_panel(3296, 1248), server.PW5_LANDSCAPE)

    def test_dashboard_carries_version_and_sha_headers(self):
        cand1 = os.path.join(os.path.dirname(server.__file__), "tracker-arm")
        cand2 = os.path.join(os.path.dirname(server.__file__), "..", "tracker-arm")
        binary = (
            cand1
            if os.path.exists(cand1)
            else (cand2 if os.path.exists(cand2) else cand1)
        )
        manifest = os.path.join(os.path.dirname(binary), "tracker-arm.manifest.json")
        existed = os.path.exists(binary)
        m_existed = os.path.exists(manifest)
        if not existed:
            with open(binary, "wb") as f:
                f.write(b"FAKEARM")
        if not m_existed:
            man_content = json.dumps(
                {
                    "format": "transit-tracker-ota-v1",
                    "version": server.SERVER_VERSION,
                    "sha256": server.sha256_file(binary),
                    "size": os.path.getsize(binary),
                    "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
                }
            )
            with open(manifest, "w", encoding="utf-8") as f:
                f.write(man_content)
        ota.clear_caches()
        try:
            status, headers, _body = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5"
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Tracker-Version"), server.SERVER_VERSION)
            self.assertEqual(
                headers.get("X-Tracker-SHA256"), server.sha256_file(binary)
            )
        finally:
            if not m_existed and os.path.exists(manifest):
                os.remove(manifest)
            if not existed and os.path.exists(binary):
                os.remove(binary)
            ota.clear_caches()

    def test_dashboard_etag_and_304(self):
        status, headers, _body = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertEqual(status, 200)
        etag = headers.get("ETag")
        self.assertTrue(etag)

        status2, headers2, body2 = _http_get(
            self.port, "/dashboard.png?mock=1", headers={"If-None-Match": etag}
        )
        self.assertEqual(status2, 304)
        self.assertEqual(body2, b"")
        self.assertEqual(headers2.get("ETag"), etag)
        self.assertIn("X-Kindle-Poll-Interval", headers2)

    def test_battery_clamped(self):
        status, _headers, body = _http_get(self.port, "/dashboard.png?mock=1&batt=999")
        self.assertEqual(status, 200)
        self.assertTrue(len(body) > 0)

    def test_battery_valid_in_range_accepted(self):
        status, _headers, body = _http_get(
            self.port, "/dashboard.png?mock=1&batt=77&charging=1"
        )
        self.assertEqual(status, 200)
        self.assertTrue(len(body) > 0)

    def test_view_via_header(self):
        status, headers, _body = _http_get(
            self.port, "/dashboard.png?mock=1", headers={"X-Tracker-View": "evening"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Tracker-View"), "evening")

    def test_bus_png_alias(self):
        status, _headers, _body = _http_get(self.port, "/bus.png?mock=1")
        self.assertEqual(status, 200)


class TestTrackerArmRoute(ServerHTTPTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cand1 = os.path.join(os.path.dirname(server.__file__), "tracker-arm")
        cand2 = os.path.join(os.path.dirname(server.__file__), "..", "tracker-arm")
        cls.binary = (
            cand1
            if os.path.exists(cand1)
            else (cand2 if os.path.exists(cand2) else cand1)
        )
        cls._pre_existing = os.path.exists(cls.binary)
        if not cls._pre_existing:
            with open(cls.binary, "wb") as f:
                f.write(b"FAKEARM" * 100)

    @classmethod
    def tearDownClass(cls):
        if not cls._pre_existing and os.path.exists(cls.binary):
            os.remove(cls.binary)
        super().tearDownClass()

    def test_get_binary_headers(self):
        status, headers, body = _http_get(self.port, "/tracker-arm")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/octet-stream")
        self.assertEqual(headers.get("X-Tracker-Version"), server.SERVER_VERSION)
        self.assertEqual(headers.get("X-Tracker-SHA256"), sha256_file(self.binary))
        self.assertEqual(len(body), os.path.getsize(self.binary))
        with open(self.binary, "rb") as f:
            self.assertEqual(body, f.read())

    def test_missing_binary_returns_404(self):
        cand1 = os.path.join(os.path.dirname(server.__file__), "tracker-arm")
        cand2 = os.path.join(os.path.dirname(server.__file__), "..", "tracker-arm")
        renamed = []
        for p in (cand1, cand2):
            if os.path.exists(p):
                os.rename(p, p + ".bak")
                renamed.append(p)
        try:
            status, _headers, _body = _http_get(self.port, "/tracker-arm")
            self.assertEqual(status, 404)
        finally:
            for p in renamed:
                os.rename(p + ".bak", p)

    def test_conditional_request_304(self):
        _status, headers, _body = _http_get(self.port, "/tracker-arm")
        last_mod = headers.get("Last-Modified")
        status, _headers, _body = _http_get(
            self.port, "/tracker-arm", headers={"If-Modified-Since": last_mod}
        )
        self.assertEqual(status, 304)


class TestDiscoveryAndLighting(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"payload-bytes")
            path = f.name
        try:
            self.assertEqual(
                sha256_file(path), hashlib.sha256(b"payload-bytes").hexdigest()
            )
        finally:
            os.remove(path)

    def test_get_local_ip_returns_string(self):
        ip = server.get_local_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(len(ip) > 0)

    def test_peak_commute_boundaries(self):
        from datetime import datetime

        self.assertTrue(server.is_peak_commute_hours(datetime(2026, 1, 1, 7, 30)))
        self.assertFalse(server.is_peak_commute_hours(datetime(2026, 1, 1, 9, 30)))
        self.assertTrue(server.is_peak_commute_hours(datetime(2026, 1, 1, 16, 30)))
        self.assertFalse(server.is_peak_commute_hours(datetime(2026, 1, 1, 19, 0)))

    def test_lighting_and_poll_interval_pair(self):
        from datetime import datetime

        peak = datetime(2026, 1, 1, 8, 0)
        off = datetime(2026, 1, 1, 13, 0)
        self.assertEqual(server.get_commute_lighting(peak), (8, 12))
        self.assertEqual(server.get_target_poll_interval(peak), 60)
        self.assertEqual(server.get_commute_lighting(off), (0, 0))
        self.assertEqual(server.get_target_poll_interval(off), 600)

    def test_presentation_by_schedule(self):
        from datetime import datetime

        with patch.object(schedule, "STORE", _store()):
            self.assertEqual(
                server.get_presentation(datetime(2026, 1, 1, 8, 0)), "interactive"
            )
            self.assertEqual(
                server.get_presentation(datetime(2026, 1, 1, 13, 0)), "idle"
            )
            self.assertEqual(
                server.get_presentation(datetime(2026, 1, 1, 23, 0)), "dormant"
            )
            self.assertEqual(
                server.get_presentation(datetime(2026, 1, 1, 2, 0)), "dormant"
            )

    def test_status_notes(self):
        self.assertIn("SLEEPING", server.get_status_note("dormant"))
        self.assertIn("press power", server.get_status_note("dormant").lower())
        self.assertEqual(
            server.get_status_note("idle"), "PRESS POWER BUTTON TO INTERACT"
        )
        self.assertEqual(server.get_status_note("interactive"), "")


def _store(**env):
    """A ConfigStore with built-in defaults plus ``env`` (no file, no host env)."""
    return ConfigStore(path="", env=env, log=lambda *_: None)


class TestPresentationOverHTTP(ServerHTTPTestBase):
    def test_dashboard_advertises_presentation_header(self):
        with patch.object(schedule, "STORE", _store()):
            status, headers, _ = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5"
            )
            self.assertEqual(status, 200)
            self.assertIn("X-Tracker-Presentation", headers)
            self.assertIn(
                headers["X-Tracker-Presentation"], ("interactive", "idle", "dormant")
            )

    def test_dashboard_present_override_is_interactive(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="overnight")):
            status, headers, _ = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5&present=interactive"
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Tracker-Presentation"), "interactive")

    def test_dashboard_renders_dormant_when_overnight(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="overnight")):
            status, headers, body = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5"
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Tracker-Presentation"), "dormant")
            self.assertEqual(headers.get("X-Kindle-Poll-Interval"), "3600")
            self.assertEqual(headers.get("X-Kindle-Brightness"), "0")
            self.assertTrue(len(body) > 0)

    def test_phase_parameters_drive_headers(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="peak")):
            status, headers, _ = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5"
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Tracker-Presentation"), "interactive")
            self.assertEqual(headers.get("X-Kindle-Poll-Interval"), "60")
            self.assertEqual(headers.get("X-Kindle-Brightness"), "8")
            self.assertEqual(headers.get("X-Kindle-Warmth"), "12")

    def test_policy_header_present(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="peak")):
            _s, headers, _ = _http_get(self.port, "/dashboard.png?mock=1&kindle=pw5")
            self.assertEqual(
                headers.get("X-Tracker-Policy"),
                "v=1;phase=peak;suspend=0;session=90;fast=600;hold=2700;sl=8,12",
            )

    def test_policy_header_on_304(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="offpeak")):
            _s, first, _ = _http_get(self.port, "/dashboard.png?mock=1")
            status, second, _ = _http_get(
                self.port,
                "/dashboard.png?mock=1",
                headers={"If-None-Match": first["ETag"]},
            )
            self.assertEqual(status, 304)
            self.assertIn("phase=offpeak", second.get("X-Tracker-Policy", ""))
            self.assertIn("suspend=1", second.get("X-Tracker-Policy", ""))

    def test_auto_view_follows_window_view(self):
        cfg_dir = tempfile.mkdtemp()
        path = os.path.join(cfg_dir, "schedule.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "windows": [
                        {
                            "phase": "peak",
                            "start": "00:00",
                            "end": "24:00",
                            "view": "morning",
                        }
                    ]
                },
                f,
            )
        store = ConfigStore(path=path, env={}, log=lambda *_: None)
        try:
            with patch.object(schedule, "STORE", store):
                _s, auto_headers, _ = _http_get(
                    self.port, "/dashboard.png?mock=1&kindle=pw5&view=auto"
                )
                self.assertEqual(auto_headers.get("X-Resolved-View"), "morning")
                self.assertEqual(auto_headers.get("X-Tracker-View"), "auto")
                # An explicit view still wins over the window's view.
                _s, explicit, _ = _http_get(
                    self.port, "/dashboard.png?mock=1&kindle=pw5&view=evening"
                )
                self.assertEqual(explicit.get("X-Resolved-View"), "evening")
        finally:
            os.remove(path)
            os.rmdir(cfg_dir)


class TestScheduleEndpoint(ServerHTTPTestBase):
    def test_requires_token(self):
        status, _headers, _body = _http_get(self.port, "/schedule")
        self.assertEqual(status, 403)
        status, _headers, _body = _http_get(
            self.port, "/schedule", headers={"X-Tracker-Token": "wrong"}
        )
        self.assertEqual(status, 403)

    def test_report_contents(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="overnight")):
            status, headers, body = _http_get(
                self.port, "/schedule", headers=_auth_headers()
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        report = json.loads(body)
        self.assertEqual(report["current"]["phase"], "overnight")
        self.assertEqual(report["current"]["presentation"], "dormant")
        self.assertIsNone(report["current"]["until"])
        self.assertEqual(report["overrides"]["force_phase"], "overnight")
        self.assertEqual(report["transitions"], [])
        self.assertIn("peak", report["config"]["phases"])
        self.assertIsNone(report["error"])

    def test_panel_shows_phase(self):
        with patch.object(schedule, "STORE", _store(FORCE_PHASE="peak")):
            status, _headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Phase: <strong>peak</strong>", body)


class TestMdnsAdvertiser(unittest.TestCase):
    def test_returns_none_when_zeroconf_unavailable(self):
        original = discovery.ZEROCONF_AVAILABLE
        discovery.ZEROCONF_AVAILABLE = False
        try:
            zc, info = discovery.start_mdns_advertiser(http_port=8000)
            self.assertIsNone(zc)
            self.assertIsNone(info)
        finally:
            discovery.ZEROCONF_AVAILABLE = original

    def test_registers_service_when_available(self):
        original_flag = discovery.ZEROCONF_AVAILABLE
        original_zc = getattr(discovery, "Zeroconf", None)
        original_info = getattr(discovery, "ServiceInfo", None)

        registered = {}

        class FakeZC:
            def register_service(self, info):
                registered["info"] = info

            def unregister_service(self, info):
                registered["unregistered"] = info

            def close(self):
                registered["closed"] = True

        discovery.ZEROCONF_AVAILABLE = True
        discovery.Zeroconf = FakeZC
        discovery.ServiceInfo = lambda *a, **k: {"args": a, "kwargs": k}
        with patch("discovery.get_local_ip", return_value="192.168.1.100"):
            try:
                zc, info = discovery.start_mdns_advertiser(
                    http_port=8000, version="1.2.3"
                )
                self.assertIsInstance(zc, FakeZC)
                self.assertIsNotNone(info)
                self.assertIn("info", registered)
                self.assertEqual(
                    registered["info"]["kwargs"]["server"], "transittracker.local."
                )
            finally:
                discovery.ZEROCONF_AVAILABLE = original_flag
                if original_zc is None:
                    if hasattr(discovery, "Zeroconf"):
                        delattr(discovery, "Zeroconf")
                else:
                    discovery.Zeroconf = original_zc
                if original_info is None:
                    if hasattr(discovery, "ServiceInfo"):
                        delattr(discovery, "ServiceInfo")
                else:
                    discovery.ServiceInfo = original_info


class TestDiscoveryResponderFailure(unittest.TestCase):
    def test_bind_failure_is_handled(self):
        original = discovery.socket.socket

        def fake_socket(*args, **kwargs):
            raise OSError("bind fail")

        discovery.socket.socket = fake_socket
        try:
            t = discovery.start_discovery_responder(http_port=8000, version="1.0.0")
            t.join(timeout=1)
            self.assertFalse(t.is_alive())
        finally:
            discovery.socket.socket = original


class TestMdnsFailure(unittest.TestCase):
    def test_registration_exception_returns_none(self):
        original_flag = discovery.ZEROCONF_AVAILABLE
        original_zc = getattr(discovery, "Zeroconf", None)
        original_info = getattr(discovery, "ServiceInfo", None)

        discovery.ZEROCONF_AVAILABLE = True
        discovery.ServiceInfo = lambda *a, **k: object()

        class FailingZC:
            def register_service(self, info):
                raise OSError("cannot bind mdns")

        discovery.Zeroconf = FailingZC
        with patch("discovery.get_local_ip", return_value="192.168.1.100"):
            try:
                zc, info = discovery.start_mdns_advertiser(
                    http_port=8000, version="1.0.0"
                )
                self.assertIsNone(zc)
                self.assertIsNone(info)
            finally:
                discovery.ZEROCONF_AVAILABLE = original_flag
                if original_zc is None:
                    if hasattr(discovery, "Zeroconf"):
                        delattr(discovery, "Zeroconf")
                else:
                    discovery.Zeroconf = original_zc
                if original_info is None:
                    if hasattr(discovery, "ServiceInfo"):
                        delattr(discovery, "ServiceInfo")
                else:
                    discovery.ServiceInfo = original_info


class TestUDPDiscoveryResponder(unittest.TestCase):
    def test_responder_answers_probe(self):
        original_port = discovery.DISCOVERY_PORT
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        discovery.DISCOVERY_PORT = port
        try:
            t = discovery.start_discovery_responder(
                http_port=server.PORT, version="9.9.9", port=port
            )
            self.assertTrue(t.daemon)
            time.sleep(0.2)

            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(3)
            try:
                client.sendto(b"TRANSIT_TRACKER_DISCOVER\n", ("127.0.0.1", port))
                data, _addr = client.recvfrom(1024)
                self.assertIn(b"TRANSIT_TRACKER_OFFER", data)
                self.assertIn(b"9.9.9", data)
            finally:
                client.close()
        finally:
            discovery.DISCOVERY_PORT = original_port
            server.DISCOVERY_PORT = original_port


class TestSanitizeKindlePanel(unittest.TestCase):
    def test_valid_panels_pass_through(self):
        self.assertEqual(server.sanitize_kindle_panel(1648, 1236), (1648, 1236))
        self.assertEqual(server.sanitize_kindle_panel(1448, 1072), (1448, 1072))

    def test_buffer_sized_falls_back(self):
        self.assertEqual(server.sanitize_kindle_panel(3296, 1248), server.PW5_LANDSCAPE)

    def test_out_of_range_falls_back(self):
        self.assertEqual(server.sanitize_kindle_panel(100, 100), server.PW5_LANDSCAPE)
        self.assertEqual(server.sanitize_kindle_panel(5000, 4000), server.PW5_LANDSCAPE)

    def test_non_numeric_falls_back(self):
        self.assertEqual(
            server.sanitize_kindle_panel("abc", "xyz"), server.PW5_LANDSCAPE
        )
        self.assertEqual(server.sanitize_kindle_panel(None, None), server.PW5_LANDSCAPE)

    def test_bad_aspect_ratio_falls_back(self):
        self.assertEqual(server.sanitize_kindle_panel(1000, 1000), server.PW5_LANDSCAPE)


class TestFormatForKindle(unittest.TestCase):
    def test_landscape_rotation_and_grayscale(self):
        base = Image.new("RGB", (800, 480), "white")
        out = format_for_kindle(base, orientation="landscape", rotation=90)
        self.assertEqual(out.size, (1236, 1648))
        self.assertEqual(out.mode, "L")

    def test_non_landscape_is_grayscale(self):
        base = Image.new("RGB", (800, 480), "white")
        out = format_for_kindle(base, orientation="portrait")
        self.assertEqual(out.mode, "L")

    def test_native_size_is_not_resampled(self):
        base = Image.new("RGB", (1648, 1236), "white")
        out = format_for_kindle(base, orientation="landscape", rotation=90)
        self.assertEqual(out.size, (1236, 1648))

    def test_custom_target_size(self):
        base = Image.new("RGB", (1448, 1072), "white")
        out = format_for_kindle(
            base, orientation="landscape", rotation=90, target=(1448, 1072)
        )
        self.assertEqual(out.size, (1072, 1448))

    def test_native_render_scale(self):
        self.assertAlmostEqual(
            server.native_render_scale(1648, 1236, 800), 2.06, places=2
        )
        self.assertAlmostEqual(
            server.native_render_scale(1448, 1072, 800), 1.81, places=2
        )


if __name__ == "__main__":
    unittest.main()

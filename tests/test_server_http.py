"""
Integration tests that exercise the real HTTP handlers over a loopback socket.
The server is bound to an ephemeral port; the dashboard route is served from
mock data so no external network calls are made.
"""

import copy
import datetime
import json
import os
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import server
from server import DashboardHandler


def _http_get(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _http(method, port, path, headers=None, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _auth_headers(extra=None):
    tok = getattr(server, "CONTROL_TOKEN", "test-token-1234") or "test-token-1234"
    hdrs = {"X-Tracker-Token": tok}
    if extra:
        hdrs.update(extra)
    return hdrs


class ServerHTTPTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_token = server.CONTROL_TOKEN
        server.CONTROL_TOKEN = "test-token-1234"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        server.CONTROL_TOKEN = cls._orig_token
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        super().setUp()
        server.CONTROL_TOKEN = "test-token-1234"


class TestHealthAndRoot(ServerHTTPTestBase):
    def test_healthz(self):
        status, _headers, body = _http_get(self.port, "/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], server.SERVER_VERSION)
        self.assertIn("stopped", payload)

    def test_root_html(self):
        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Dashboard", body)

    def test_root_html_does_not_leak_control_token(self):
        original = server.CONTROL_TOKEN
        server.CONTROL_TOKEN = "s3cret"
        try:
            status, headers, body = _http_get(self.port, "/")
            self.assertEqual(status, 200)
            self.assertNotIn(b"s3cret", body)
            self.assertIn("Content-Security-Policy", headers)
            self.assertIn("nonce-", headers["Content-Security-Policy"])
        finally:
            server.CONTROL_TOKEN = original

    def test_unknown_path_404(self):
        status, _headers, _body = _http_get(self.port, "/does-not-exist")
        self.assertEqual(status, 404)

    def test_head_request(self):
        status, _headers, _body = _http(method="HEAD", port=self.port, path="/healthz")
        self.assertEqual(status, 200)


class TestDiagnosticsEndpoints(ServerHTTPTestBase):
    def setUp(self):
        super().setUp()
        with server._diag_lock:
            server._last_diagnostics["text"] = ""
            server._last_diagnostics["time"] = 0.0
            server._diag_requested = ""

    def tearDown(self):
        with server._diag_lock:
            server._last_diagnostics["text"] = ""
            server._diag_requested = ""
        super().tearDown()

    def test_get_diag_404_before_upload(self):
        status, _headers, _body = _http_get(self.port, "/diag")
        self.assertEqual(status, 403)
        status, _headers, _body = _http_get(self.port, "/diag", headers=_auth_headers())
        self.assertEqual(status, 404)

    def test_parse_diag_battery(self):
        cases = [
            ("battery_level=83 charging=true", 83, True),
            ("battery_level=42 charging=false", 42, False),
            ("no battery here", None, None),
            ("battery_level=-1 charging=false", None, None),
        ]
        for text, exp_level, exp_charging in cases:
            with self.subTest(text=text):
                res = server.parse_diag_battery(text)
                self.assertEqual(res, (exp_level, exp_charging))
                self.assertEqual(res.level, exp_level)
                self.assertEqual(res.charging, exp_charging)

    def test_post_diag_extracts_and_exposes_battery(self):
        body = (
            b"--- Kindle Diagnostics ---\nbattery_level=87 charging=true\nfw=5.16.21\n"
        )
        status, _headers, _body = _http(
            method="POST", port=self.port, path="/diag", body=body
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._last_diagnostics["battery"], 87)
        self.assertEqual(server._last_diagnostics["charging"], True)

    def test_post_then_get_diag(self):
        body = b"sample diagnostics payload"
        _http(method="POST", port=self.port, path="/diag", body=body)
        status, headers, text = _http_get(self.port, "/diag", headers=_auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertIn(b"sample diagnostics payload", text)

    def test_diag_request_flagged_and_consumed_by_dashboard(self):
        status, _headers, _body = _http(
            method="POST",
            port=self.port,
            path="/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"level": "1"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._diag_requested, "1")

        status, headers, _body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Tracker-Diag"), "1")
        self.assertEqual(server._diag_requested, "")

        status, headers, _body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5"
        )
        self.assertNotIn("X-Tracker-Diag", headers)

    def test_web_request_does_not_consume_diag_flag(self):
        _http(
            method="POST",
            port=self.port,
            path="/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"level": "1"}',
        )
        self.assertEqual(server._diag_requested, "1")

        status, headers, _body = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Tracker-Diag", headers)
        self.assertEqual(server._diag_requested, "1")

    def test_diag_full_request_forwarded(self):
        _http(
            method="POST",
            port=self.port,
            path="/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"level": "full"}',
        )
        self.assertEqual(server._diag_requested, "full")
        _status, headers, _body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5"
        )
        self.assertEqual(headers.get("X-Tracker-Diag"), "full")
        self.assertEqual(server._diag_requested, "")

    def test_diag_flag_also_sent_on_304(self):
        _status, headers, _body = _http_get(
            self.port, "/dashboard.png?mock=1&kindle=pw5"
        )
        etag = headers.get("ETag")
        self.assertTrue(etag)

        _http(
            method="POST",
            port=self.port,
            path="/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"level": "1"}',
        )
        self.assertEqual(server._diag_requested, "1")

        status, headers, _body = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(status, 304)
        self.assertEqual(headers.get("X-Tracker-Diag"), "1")
        self.assertEqual(server._diag_requested, "")


class TestRunModeEndpoint(ServerHTTPTestBase):
    def setUp(self):
        super().setUp()
        with server._diag_lock:
            server._mode_requested = ""

    def tearDown(self):
        with server._diag_lock:
            server._mode_requested = ""
        super().tearDown()

    def test_mode_get_reports_valid_and_pending(self):
        status, headers, body = _http_get(self.port, "/mode", headers=_auth_headers())
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        data = json.loads(body)
        self.assertEqual(data["pending"], "")
        self.assertIn("resident", data["valid"])
        self.assertIn("oneshot", data["valid"])
        self.assertIn("sleep", data["valid"])
        self.assertIn("sleep-suspend", data["valid"])

    def test_mode_set_and_forwarded_on_kindle_poll(self):
        status, _, _ = _http(
            method="POST",
            port=self.port,
            path="/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"set": "sleep"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._mode_requested, "sleep")

        _status, headers_web, _ = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertNotIn("X-Tracker-Mode", headers_web)

        _status, headers_k, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Mode": "oneshot"},
        )
        self.assertEqual(headers_k.get("X-Tracker-Mode"), "sleep")

    def test_mode_is_sticky_across_client_restarts(self):
        _http(
            method="POST",
            port=self.port,
            path="/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"set": "sleep"}',
        )

        _s, h1, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Mode": "oneshot"},
        )
        self.assertEqual(h1.get("X-Tracker-Mode"), "sleep")

        _s, h2, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Mode": "sleep"},
        )
        self.assertNotIn("X-Tracker-Mode", h2)

        _s, h3, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Mode": "oneshot"},
        )
        self.assertEqual(h3.get("X-Tracker-Mode"), "sleep")
        self.assertEqual(server._mode_requested, "sleep")

    def test_mode_invalid_value_ignored(self):
        status, _headers, body = _http(
            method="POST",
            port=self.port,
            path="/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"set": "bogus"}',
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["pending"], "")

    def test_mode_accepts_sleep_suspend(self):
        status, _, _ = _http(
            method="POST",
            port=self.port,
            path="/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"set": "sleep-suspend"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._mode_requested, "sleep-suspend")

    def test_action_queued_and_forwarded_to_kindle(self):
        with server._diag_lock:
            server._device_action = ""

        status, _headers, body = _http(
            method="POST",
            port=self.port,
            path="/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"do": "disable-ads"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._device_action, "disable-ads")

        _s, hw, _ = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertNotIn("X-Tracker-Action", hw)

        _s, hk, _ = _http_get(self.port, "/dashboard.png?mock=1&kindle=pw5")
        self.assertEqual(hk.get("X-Tracker-Action"), "disable-ads")
        self.assertEqual(server._device_action, "")

        _s, hk2, _ = _http_get(self.port, "/dashboard.png?mock=1&kindle=pw5")
        self.assertNotIn("X-Tracker-Action", hk2)

    def test_mode_forwarded_on_304(self):
        _s, h, _ = _http_get(self.port, "/dashboard.png?mock=1&kindle=pw5")
        etag = h.get("ETag")

        _http(
            method="POST",
            port=self.port,
            path="/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"set": "oneshot"}',
        )

        s304, h304, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"If-None-Match": etag, "X-Tracker-Mode": "sleep"},
        )
        self.assertEqual(s304, 304)
        self.assertEqual(h304.get("X-Tracker-Mode"), "oneshot")


class TestKeepAlive(ServerHTTPTestBase):
    def test_multiple_requests_on_one_connection(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", self.port))
        try:
            for _ in range(3):
                req = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
                s.sendall(req)

                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = s.recv(1024)
                    self.assertTrue(chunk, "connection closed prematurely by server")
                    buf += chunk
                header_part, rest = buf.split(b"\r\n\r\n", 1)

                content_length = 0
                for line in header_part.decode().split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())

                while len(rest) < content_length:
                    rest += s.recv(1024)

                payload = json.loads(rest[:content_length].decode())
                self.assertEqual(payload["status"], "ok")
        finally:
            s.close()

    def test_head_has_no_body_and_keeps_connection_usable(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", self.port))
        try:
            req = b"HEAD /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
            s.sendall(req)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(1024)
                self.assertTrue(chunk)
                buf += chunk
            header_part, rest = buf.split(b"\r\n\r\n", 1)
            self.assertEqual(rest, b"")
            self.assertIn(b"200 OK", header_part)

            req2 = (
                b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            s.sendall(req2)
            buf2 = b""
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    break
                buf2 += chunk
            self.assertIn(b"200 OK", buf2)
            self.assertIn(b'"status": "ok"', buf2)
        finally:
            s.close()


class TestLogEndpoint(ServerHTTPTestBase):
    def test_post_log_returns_200(self):
        status, _headers, _body = _http(
            method="POST", port=self.port, path="/log", body=b"hello from kindle\n"
        )
        self.assertEqual(status, 200)

    def test_post_unknown_path_404(self):
        status, _headers, _body = _http(
            method="POST", port=self.port, path="/nonexistent", body=b"xyz"
        )
        self.assertEqual(status, 404)

    def test_get_log_with_query(self):
        status, _headers, _body = _http_get(self.port, "/log?msg=test-message")
        self.assertIn(status, (404, 405))


class TestControlEndpoints(ServerHTTPTestBase):
    def setUp(self):
        super().setUp()
        server.tracker_stopped = False
        server.CONTROL_TOKEN = "test-token"

    def tearDown(self):
        server.tracker_stopped = False
        server.CONTROL_TOKEN = ""
        super().tearDown()

    def test_stop_then_resume_from_loopback(self):
        tok = "control123"
        server.CONTROL_TOKEN = tok
        status, _headers, body = _http(
            "POST", self.port, "/stop", headers={"X-Tracker-Token": tok}
        )
        self.assertEqual(status, 200)
        self.assertTrue(server.tracker_stopped)
        self.assertIn(b"stopped", body)

        status, _headers, _body = _http_get(self.port, "/dashboard.png?mock=1")
        self.assertEqual(status, 205)

        status, _headers, _body = _http(
            "POST", self.port, "/resume", headers={"X-Tracker-Token": tok}
        )
        self.assertEqual(status, 200)
        self.assertFalse(server.tracker_stopped)

    def test_start_alias_resumes(self):
        server.tracker_stopped = True
        tok = "control123"
        server.CONTROL_TOKEN = tok
        status, _headers, _body = _http(
            "POST", self.port, "/start", headers={"X-Tracker-Token": tok}
        )
        self.assertEqual(status, 200)
        self.assertFalse(server.tracker_stopped)

    def test_stop_denied_with_token_configured_and_wrong_token(self):
        server.CONTROL_TOKEN = "topsecret"
        status, _headers, body = _http(
            "POST", self.port, "/stop", headers={"X-Tracker-Token": "wrong"}
        )
        self.assertEqual(status, 403)
        self.assertFalse(server.tracker_stopped)
        self.assertIn(b"Forbidden", body)

    def test_stop_denied_via_get(self):
        server.CONTROL_TOKEN = "topsecret"
        status, _headers, _body = _http_get(
            self.port, "/stop", headers={"X-Tracker-Token": "topsecret"}
        )
        self.assertEqual(status, 405)

    def test_stop_allowed_with_correct_header_token(self):
        server.CONTROL_TOKEN = "topsecret"
        status, _headers, _body = _http(
            "POST", self.port, "/stop", headers={"X-Tracker-Token": "topsecret"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(server.tracker_stopped)

    def test_resume_denied_without_token(self):
        server.tracker_stopped = True
        server.CONTROL_TOKEN = "topsecret"
        status, _headers, _body = _http("POST", self.port, "/resume")
        self.assertEqual(status, 403)
        self.assertTrue(server.tracker_stopped)

    def test_token_links_present_when_stopped_and_configured(self):
        server.CONTROL_TOKEN = "topsecret"
        server.tracker_stopped = True
        status, _headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"STOPPED", body)
        self.assertNotIn(b"topsecret", body)


class TestForbiddenResponse(unittest.TestCase):
    def test_send_forbidden_renders_403(self):
        class FakeHandler:
            command = "GET"
            sent_headers = {}

            def send_response(self, code):
                self.code = code

            def send_header(self, k, v):
                self.sent_headers[k] = v

            def end_headers(self):
                pass

            class Out:
                def __init__(self):
                    self.data = b""

                def write(self, b):
                    self.data += b

            def __init__(self):
                self.wfile = self.Out()

        h = FakeHandler()
        DashboardHandler._send_forbidden(h)
        self.assertEqual(h.code, 403)
        self.assertIn("text/html", h.sent_headers["Content-Type"])
        self.assertIn(b"403 Forbidden", h.wfile.data)


class TestServerCoverageAdditions(ServerHTTPTestBase):
    def test_warm_up_gtfs(self):
        mock_gtfs = MagicMock()
        orig = server.gtfs_tracker
        try:
            server.gtfs_tracker = mock_gtfs
            server.warm_up_gtfs()
            mock_gtfs.ensure_index.assert_called_once()

            mock_gtfs.ensure_index.side_effect = Exception("warmup failed")
            server.warm_up_gtfs()
        finally:
            server.gtfs_tracker = orig

    def test_get_fresh_dashboard_image_cached(self):
        img1 = server.get_fresh_dashboard_image(use_mock=True, width=800, height=480)
        img2 = server.get_fresh_dashboard_image(use_mock=True, width=800, height=480)
        self.assertIsNotNone(img1)
        self.assertIsNotNone(img2)

    def test_parse_hour_env_invalid(self):
        with patch.dict("os.environ", {"TEST_INVALID_HOUR": "not-a-float"}):
            self.assertEqual(server._parse_hour_env("TEST_INVALID_HOUR", 8.5), 8.5)

    def test_is_overnight_hours_ordered(self):
        import schedule
        from state_machine import ConfigStore

        store = ConfigStore(
            path="",
            env={"OVERNIGHT_START": "1.0", "OVERNIGHT_END": "5.0"},
            log=lambda *_: None,
        )
        with patch.object(schedule, "STORE", store):
            dt_in = datetime.datetime(2026, 10, 10, 3, 0)
            dt_out = datetime.datetime(2026, 10, 10, 6, 0)
            self.assertTrue(server.is_overnight_hours(dt_in))
            self.assertFalse(server.is_overnight_hours(dt_out))

    def test_get_presentation_force_fast_poll(self):
        import schedule
        from state_machine import ConfigStore

        store = ConfigStore(path="", env={"FORCE_FAST_POLL": "1"}, log=lambda *_: None)
        with patch.object(schedule, "STORE", store):
            self.assertEqual(server.get_presentation(), "interactive")

    def test_action_endpoint_without_do(self):
        status, _headers, body = _http_get(
            self.port, "/action", headers=_auth_headers()
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("pending", data)

    def test_tracker_arm_head_request(self):
        cand1 = os.path.join(os.path.dirname(server.__file__), "tracker-arm")
        cand2 = os.path.join(os.path.dirname(server.__file__), "..", "tracker-arm")
        binary = (
            cand1
            if os.path.exists(cand1)
            else (cand2 if os.path.exists(cand2) else cand1)
        )
        created = False
        if not os.path.exists(binary):
            with open(binary, "wb") as f:
                f.write(b"FAKEARM" * 100)
            created = True
        try:
            status, headers, body = _http("HEAD", self.port, "/tracker-arm")
            self.assertEqual(status, 200)
            self.assertEqual(len(body), 0)
            self.assertIn("X-Tracker-Version", headers)
        finally:
            if created and os.path.exists(binary):
                os.remove(binary)

    def test_dashboard_304_with_action_header(self):
        status, headers, _ = _http_get(self.port, "/dashboard.png?mock=1&kindle=pw5")
        self.assertEqual(status, 200)
        etag = headers.get("ETag")
        self.assertIsNotNone(etag)

        with server._diag_lock:
            server._device_action = "disable-ads"
        try:
            status_304, headers_304, _ = _http_get(
                self.port,
                "/dashboard.png?mock=1&kindle=pw5",
                headers={"If-None-Match": etag},
            )
            self.assertEqual(status_304, 304)
            self.assertEqual(headers_304.get("X-Tracker-Action"), "disable-ads")
        finally:
            with server._diag_lock:
                server._device_action = ""

    def test_send_forbidden_on_head(self):
        with patch("server.check_control_auth", return_value=False):
            status, _headers, body = _http("HEAD", self.port, "/mode")
            self.assertEqual(status, 403)
            self.assertEqual(len(body), 0)

    def test_get_devices_requires_auth(self):
        status_unauth, _, _ = _http("GET", self.port, "/devices")
        self.assertEqual(status_unauth, 403)

        status_auth, headers, body = _http(
            "GET", self.port, "/devices", headers=_auth_headers()
        )
        self.assertEqual(status_auth, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        data = json.loads(body.decode("utf-8"))
        self.assertIn("devices", data)

    def test_post_log_and_diag_read_errors(self):
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.command = "POST"
        handler.path = "/log"
        handler.headers = {"Content-Length": "10"}
        mock_rfile = MagicMock()
        mock_rfile.read.side_effect = OSError("socket closed")
        handler.rfile = mock_rfile
        handler._send_empty = MagicMock()

        handler.do_POST()
        handler._send_empty.assert_called_with(400)

        handler.path = "/diag"
        handler._send_empty.reset_mock()
        handler.do_POST()
        handler._send_empty.assert_called_with(400)

    def test_get_fresh_data_live_gtfs_exception_fallback(self):
        mock_gtfs = MagicMock()
        mock_gtfs.get_upcoming.side_effect = Exception("GTFS failure")
        mock_njt = MagicMock()
        mock_njt.get_arrivals_with_status.return_value = ("ok", [])
        mock_snap = MagicMock()
        mock_snap.status = "ok"
        mock_snap.stations = [
            {
                "name": "Mock Station",
                "ebikes": 2,
                "classic": 1,
                "docks": 5,
                "walk_min": 2,
                "is_offline": False,
            }
        ]
        mock_cb = MagicMock()
        mock_cb.get_snapshot.return_value = mock_snap
        mock_cb.get_mock_data.return_value = []
        orig_gtfs = server.gtfs_tracker
        orig_njt = server.tracker
        orig_cb = server.cb_tracker

        with server._data_lock:
            orig_cache = copy.deepcopy(server._data_cache)

        orig_connect = socket.socket.connect

        def hermetic_connect(sock, address):
            if sock.type == socket.SOCK_STREAM and address[0] not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                raise AssertionError(
                    f"Hermetic isolation violation: outbound WAN connection attempted to {address}"
                )
            return orig_connect(sock, address)

        try:
            server.gtfs_tracker = mock_gtfs
            server.tracker = mock_njt
            server.cb_tracker = mock_cb
            with server._data_lock:
                server._data_cache["time"] = 0
            with patch.object(socket.socket, "connect", hermetic_connect):
                stops, status, cb_data = server.get_fresh_data(use_mock=False)
            mock_njt.get_arrivals_with_status.assert_called()
            mock_cb.get_snapshot.assert_called_once()
            self.assertEqual(cb_data, mock_snap.stations)
        finally:
            server.gtfs_tracker = orig_gtfs
            server.tracker = orig_njt
            server.cb_tracker = orig_cb
            with server._data_lock:
                server._data_cache.clear()
                server._data_cache.update(orig_cache)


class TestMultiDeviceWebInterface(ServerHTTPTestBase):
    def setUp(self):
        super().setUp()
        server.get_device_registry().clear()
        with server._diag_lock:
            server._device_action = ""
            server._diag_requested = ""
            server._mode_requested = ""
            server._last_diagnostics = {
                "text": "",
                "time": 0.0,
                "battery": None,
                "charging": None,
            }

    def test_fleet_overview_empty_registry(self):
        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        body_text = body.decode("utf-8")
        self.assertIn("Fleet Overview:", body_text)
        self.assertIn("Total: <strong>0</strong>", body_text)
        self.assertIn("Online: <strong>0</strong>", body_text)
        self.assertIn("Offline: <strong>0</strong>", body_text)
        self.assertIn("No registered devices", body_text)
        self.assertIn("Broadcast Controls:", body_text)

    def test_fleet_overview_multi_device_rendering(self):
        registry = server.get_device_registry()
        # Device 1: online, 85% battery, charging, v1.2 / 5.14, resident mode
        registry.update_telemetry(
            client_id="kindle-dev-001",
            remote_ip="192.168.1.101",
            battery=85.0,
            charging=True,
            client_mode="resident",
            client_version="v1.2",
            firmware_version="5.14.2",
        )
        # Device 2: offline (seen 1 day ago), 15% battery, not charging, v1.0 / 5.12, sleep mode with target oneshot
        d2 = registry.update_telemetry(
            client_id="kindle-dev-002",
            remote_ip="192.168.1.102",
            battery=15.0,
            charging=False,
            client_mode="sleep",
            client_version="v1.0",
            firmware_version="5.12.1",
        )
        d2.last_seen = time.time() - 86400.0
        registry.set_mode("kindle-dev-002", "oneshot")

        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        body_text = body.decode("utf-8")

        # Summary
        self.assertIn("Total: <strong>2</strong>", body_text)
        self.assertIn("Online: <strong>1</strong>", body_text)
        self.assertIn("Offline: <strong>1</strong>", body_text)

        # Device 1 fields
        self.assertIn("ONLINE", body_text)
        self.assertIn("kindle-dev-001", body_text)
        self.assertIn("192.168.1.101", body_text)
        self.assertIn("⚡ 85%", body_text)
        self.assertIn("v1.2 / 5.14.2", body_text)
        self.assertIn('data-client-id="kindle-dev-001"', body_text)

        # Device 2 fields
        self.assertIn("OFFLINE", body_text)
        self.assertIn("kindle-dev-002", body_text)
        self.assertIn("192.168.1.102", body_text)
        self.assertIn("15%", body_text)
        self.assertNotIn("⚡ 15%", body_text)
        self.assertIn("v1.0 / 5.12.1", body_text)
        self.assertIn("target: oneshot", body_text)
        self.assertIn('data-client-id="kindle-dev-002"', body_text)

        # Controls
        self.assertIn("broadcastActionBtn", body_text)
        self.assertIn("broadcastDiagBtn", body_text)
        self.assertIn("broadcastModeBtn", body_text)
        self.assertIn("device-action-select", body_text)
        self.assertIn("device-diag-select", body_text)
        self.assertIn("device-mode-select", body_text)

    def test_fleet_overview_xss_escaping(self):
        registry = server.get_device_registry()
        # Direct injection into fields
        dev = registry.get_or_register("xss_safe_dev")
        dev.remote_ip = "<script>alert('ip-xss')</script>"
        dev.client_version = "v<script>alert('ver-xss')</script>"
        dev.firmware_version = "<img src=x onerror=alert('fw-xss')>"
        dev.client_mode = "<b>bold</b>"
        dev.target_mode = "<i>italic</i>"
        dev.last_seen = time.time()

        status, _, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        body_text = body.decode("utf-8")

        # Unescaped XSS payloads must NOT be present
        self.assertNotIn("<script>alert('ip-xss')</script>", body_text)
        self.assertNotIn("<script>alert('ver-xss')</script>", body_text)
        self.assertNotIn("<img src=x onerror=alert('fw-xss')>", body_text)
        self.assertNotIn("<b>bold</b>", body_text)
        self.assertNotIn("<i>italic</i>", body_text)

        # Escaped versions MUST be present
        self.assertIn(
            "&lt;script&gt;alert(&#x27;ip-xss&#x27;)&lt;/script&gt;", body_text
        )
        self.assertIn("&lt;img src=x onerror=alert(&#x27;fw-xss&#x27;)&gt;", body_text)

    def test_existing_dashboard_functionality_operational(self):
        # Default view
        status, _, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<img src="/dashboard.png?view=auto&amp;t=', body)
        self.assertIn(b'Status: <span style="color:#51cf66;">ACTIVE</span>', body)
        self.assertIn(b"Auto (AM Citi / PM Bus)", body)
        self.assertIn(b"Morning (Citi Bike Hero)", body)
        self.assertIn(b"Evening (Bus Hero)", body)
        self.assertIn(b"Standard (800x480)", body)
        self.assertIn(b"Kindle PW5 (Rotated 90\xc2\xb0)", body)

        # View switcher parameter propagation
        status_m, _, body_m = _http_get(self.port, "/?view=morning")
        self.assertEqual(status_m, 200)
        self.assertIn(b'<img src="/dashboard.png?view=morning&amp;t=', body_m)

        status_e, _, body_e = _http_get(self.port, "/index.html?view=evening")
        self.assertEqual(status_e, 200)
        self.assertIn(b'<img src="/dashboard.png?view=evening&amp;t=', body_e)

    def test_csp_nonce_and_script_security(self):
        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self' 'nonce-", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

        body_text = body.decode("utf-8")
        match_csp = re.search(r"nonce-([a-f0-9]+)", csp)
        self.assertIsNotNone(match_csp)
        csp_nonce = match_csp.group(1)

        match_script = re.search(r'<script nonce="([a-f0-9]+)">', body_text)
        self.assertIsNotNone(match_script)
        script_nonce = match_script.group(1)

        self.assertEqual(csp_nonce, script_nonce)

        self.assertNotIn("onclick=", body_text)
        self.assertNotIn("onload=", body_text)

    def test_per_device_action_and_broadcast_controls_end_to_end(self):
        registry = server.get_device_registry()
        registry.get_or_register("device-alpha")
        registry.get_or_register("device-beta")

        # 1. Targeted Action to device-alpha
        status, _, body = _http(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"action": "restart", "client_id": "device-alpha"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], "restart")

        # device-beta polling does NOT receive action
        _s, h_beta, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-beta"},
        )
        self.assertNotIn("X-Tracker-Action", h_beta)

        # device-alpha polling receives restart action
        _s, h_alpha, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-alpha"},
        )
        self.assertEqual(h_alpha.get("X-Tracker-Action"), "restart")

        # Second poll of device-alpha -> action already popped
        _s, h_alpha2, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-alpha"},
        )
        self.assertNotIn("X-Tracker-Action", h_alpha2)

        # 2. Broadcast Action to 'all'
        status, _, body = _http(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"action": "update", "client_id": "all"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)

        # Both devices receive update action
        _s, h_alpha_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-alpha"},
        )
        self.assertEqual(h_alpha_bc.get("X-Tracker-Action"), "update")

        _s, h_beta_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-beta"},
        )
        self.assertEqual(h_beta_bc.get("X-Tracker-Action"), "update")

        # 3. Targeted Diag to device-beta
        status, _, body = _http(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"level": "full", "client_id": "device-beta"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, 200)

        _s, h_alpha_diag, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-alpha"},
        )
        self.assertNotIn("X-Tracker-Diag", h_alpha_diag)

        _s, h_beta_diag, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-beta"},
        )
        self.assertEqual(h_beta_diag.get("X-Tracker-Diag"), "full")

        # 4. Broadcast Diag to 'all'
        status, _, body = _http(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"level": "1", "client_id": "all"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)

        _s, h_alpha_diag_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-alpha"},
        )
        self.assertEqual(h_alpha_diag_bc.get("X-Tracker-Diag"), "1")

        _s, h_beta_diag_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-beta"},
        )
        self.assertEqual(h_beta_diag_bc.get("X-Tracker-Diag"), "1")

        # 5. Targeted Mode to device-alpha
        status, _, body = _http(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps(
                {"mode": "sleep-suspend", "client_id": "device-alpha"}
            ).encode("utf-8"),
        )
        self.assertEqual(status, 200)

        _s, h_alpha_mode, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "device-alpha",
                "X-Tracker-Mode": "resident",
            },
        )
        self.assertEqual(h_alpha_mode.get("X-Tracker-Mode"), "sleep-suspend")

        _s, h_beta_mode, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "device-beta",
                "X-Tracker-Mode": "resident",
            },
        )
        self.assertNotIn("X-Tracker-Mode", h_beta_mode)

        # 6. Broadcast Mode to 'all'
        status, _, body = _http(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"mode": "oneshot", "client_id": "all"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)

        _s, h_alpha_mode_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "device-alpha",
                "X-Tracker-Mode": "resident",
            },
        )
        self.assertEqual(h_alpha_mode_bc.get("X-Tracker-Mode"), "oneshot")

        _s, h_beta_mode_bc, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "device-beta",
                "X-Tracker-Mode": "resident",
            },
        )
        self.assertEqual(h_beta_mode_bc.get("X-Tracker-Mode"), "oneshot")

        # 7. Auth failure tests
        status_unauth, _, _ = _http(
            "POST",
            self.port,
            "/action",
            headers={"Content-Type": "application/json"},
            body=b'{"action": "restart"}',
        )
        self.assertEqual(status_unauth, 403)

        status_unauth2, _, _ = _http(
            "POST",
            self.port,
            "/diag/request",
            headers={"Content-Type": "application/json"},
            body=b'{"level": "1"}',
        )
        self.assertEqual(status_unauth2, 403)

        status_unauth3, _, _ = _http(
            "POST",
            self.port,
            "/mode",
            headers={"Content-Type": "application/json"},
            body=b'{"mode": "sleep"}',
        )
        self.assertEqual(status_unauth3, 403)


class TestFleetTelemetryAndBrowserIsolation(ServerHTTPTestBase):
    def setUp(self):
        super().setUp()
        server.get_device_registry().clear()

    def test_browser_preview_does_not_register_device(self):
        # 1. Web browser loads HTML viewer
        status, _, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(len(server.get_device_registry().list_devices()), 0)

        # 2. Web browser loads embedded dashboard image
        status_img, _, _ = _http_get(
            self.port, "/dashboard.png?view=auto&t=1791665695&mock=1"
        )
        self.assertEqual(status_img, 200)
        self.assertEqual(len(server.get_device_registry().list_devices()), 0)

        # 3. Reloading HTML viewer still shows 0 devices
        status2, _, body2 = _http_get(self.port, "/")
        self.assertEqual(status2, 200)
        body_text = body2.decode("utf-8")
        self.assertIn("Total: <strong>0</strong>", body_text)
        self.assertIn("No registered devices", body_text)

    def test_kindle_request_auto_registers_with_telemetry_headers(self):
        status, _, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5&batt=95&charging=1",
            headers={
                "X-Tracker-Client-ID": "kindle-pw5-fleet-test",
                "X-Tracker-Client-Version": "1.35.10",
                "X-Tracker-Firmware": "Kindle 5.18.6",
                "X-Tracker-Mode": "sleep-suspend",
                "X-Kindle-Battery": "95",
                "X-Kindle-Charging": "1",
            },
        )
        self.assertEqual(status, 200)

        reg = server.get_device_registry()
        dev = reg.get_device("kindle-pw5-fleet-test")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.client_version, "1.35.10")
        self.assertEqual(dev.firmware_version, "Kindle 5.18.6")
        self.assertEqual(dev.client_mode, "sleep-suspend")
        self.assertEqual(dev.battery, 95.0)
        self.assertTrue(dev.charging)

        status_view, _, body_view = _http_get(self.port, "/")
        self.assertEqual(status_view, 200)
        body_text = body_view.decode("utf-8")
        self.assertIn("kindle-pw5-fleet-test", body_text)
        self.assertIn("1.35.10 / Kindle 5.18.6", body_text)
        self.assertIn("sleep-suspend", body_text)
        self.assertIn("⚡ 95%", body_text)

    def test_parse_diag_versions_helper(self):
        text1 = (
            "=== DIAGNOSTICS v1.35.10 ===\n"
            "runtime: linux/arm\n"
            "/etc/prettyversion.txt:\n"
            "  Kindle 5.18.6 (~~otaVersion~~)\n"
        )
        c_ver, fw_ver = server.parse_diag_versions(text1)
        self.assertEqual(c_ver, "1.35.10")
        self.assertEqual(fw_ver, "Kindle 5.18.6")

        # Fallback to /etc/version
        text2 = (
            "=== DIAGNOSTICS v1.2.3 ===\n"
            "/etc/prettyversion.txt:\n"
            "  <missing>\n"
            "/etc/version:\n"
            "  5.14.2\n"
        )
        c_ver2, fw_ver2 = server.parse_diag_versions(text2)
        self.assertEqual(c_ver2, "1.2.3")
        self.assertEqual(fw_ver2, "5.14.2")

        # Empty / missing
        c_empty, fw_empty = server.parse_diag_versions("no version info here")
        self.assertEqual(c_empty, "")
        self.assertEqual(fw_empty, "")

    def test_post_diag_populates_parsed_versions(self):
        diag_body = (
            "=== DIAGNOSTICS v1.35.10 ===\n"
            "battery_level=87 charging=false\n"
            "/etc/prettyversion.txt:\n"
            "  Kindle 5.18.6 (ota-123)\n"
        )
        status, _, _ = _http(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "diag-ver-test"},
            body=diag_body.encode("utf-8"),
        )
        self.assertEqual(status, 200)

        reg = server.get_device_registry()
        dev = reg.get_device("diag-ver-test")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.client_version, "1.35.10")
        self.assertEqual(dev.firmware_version, "Kindle 5.18.6")
        self.assertEqual(dev.battery, 87.0)
        self.assertFalse(dev.charging)


if __name__ == "__main__":
    unittest.main()

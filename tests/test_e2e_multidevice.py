"""
End-to-end multi-device integration tests for the Transit Tracker system.

Validates multi-device client registration, per-device server state isolation,
independent action/diagnostic queue dispatch, broadcast controls, diagnostics
and log persistence isolation, Web UI fleet rendering, and control plane API.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

from device_registry import get_device_registry, reset_device_registry

import server
from server import TransitTrackerHandler


def _http_get(port: int, path: str, headers: dict = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _http_post(port: int, path: str, headers: dict = None, body: bytes = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body if body is not None else b"",
        headers=headers or {},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _auth_headers(extra: dict = None) -> dict:
    tok = getattr(server, "CONTROL_TOKEN", "test-token-1234") or "test-token-1234"
    hdrs = {"X-Tracker-Token": tok}
    if extra:
        hdrs.update(extra)
    return hdrs


class TestE2EMultiDevice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_token = getattr(server, "CONTROL_TOKEN", "")
        server.CONTROL_TOKEN = "test-token-1234"
        cls._orig_cache_dir = os.environ.get("CACHE_DIR")
        cls.temp_dir = tempfile.mkdtemp(prefix="tt_e2e_cache_")
        os.environ["CACHE_DIR"] = cls.temp_dir

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), TransitTrackerHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        server.CONTROL_TOKEN = cls._orig_token
        if cls._orig_cache_dir is not None:
            os.environ["CACHE_DIR"] = cls._orig_cache_dir
        else:
            os.environ.pop("CACHE_DIR", None)

        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        server.CONTROL_TOKEN = "test-token-1234"
        reset_device_registry()
        with server._diag_lock:
            server._diag_requested = ""
            server._mode_requested = ""
            server._device_action = ""
            server._last_diagnostics = {
                "text": "",
                "time": 0.0,
                "battery": None,
                "charging": None,
            }
        server.tracker_stopped = False

    def test_auto_registration_and_header_telemetry(self):
        """
        Verify unknown client IDs auto-register into DeviceRegistry upon first request
        and telemetry headers (battery, charging, mode, view) are properly recorded.
        """
        registry = get_device_registry()
        self.assertEqual(len(registry.list_devices()), 0)

        clients = [
            ("kindle_alpha", "85", "1", "morning", "resident"),
            ("kindle_beta", "42", "0", "evening", "oneshot"),
            ("kindle_gamma", "99", "1", "auto", "sleep"),
        ]

        for cid, batt, chg, view, mode in clients:
            headers = {
                "X-Tracker-Client-ID": cid,
                "X-Kindle-Battery": batt,
                "X-Kindle-Charging": chg,
                "X-Tracker-View": view,
                "X-Tracker-Mode": mode,
            }
            status, resp_headers, body = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5", headers=headers
            )
            self.assertEqual(status, 200)
            self.assertEqual(resp_headers.get("Content-Type"), "image/png")
            self.assertTrue(len(body) > 0)

        devices = {d.client_id: d for d in registry.list_devices()}
        self.assertIn("kindle_alpha", devices)
        self.assertIn("kindle_beta", devices)
        self.assertIn("kindle_gamma", devices)

        alpha = devices["kindle_alpha"]
        self.assertEqual(alpha.battery, 85.0)
        self.assertTrue(alpha.charging)
        self.assertEqual(alpha.client_mode, "resident")
        self.assertTrue(alpha.last_seen > 0)
        self.assertTrue(alpha.is_online())

        beta = devices["kindle_beta"]
        self.assertEqual(beta.battery, 42.0)
        self.assertFalse(beta.charging)
        self.assertEqual(beta.client_mode, "oneshot")

        gamma = devices["kindle_gamma"]
        self.assertEqual(gamma.battery, 99.0)
        self.assertTrue(gamma.charging)
        self.assertEqual(gamma.client_mode, "sleep")

    def test_queue_isolation(self):
        """
        Verify isolated queue dispatch:
        - Admin enqueues action 'restart' for kindle_alpha.
        - Admin enqueues diag 'quick' for kindle_beta.
        - kindle_beta receives X-Tracker-Diag: quick, and NO X-Tracker-Action.
        - kindle_alpha receives X-Tracker-Action: restart, and NO X-Tracker-Diag.
        - kindle_gamma receives neither header.
        """
        # Register the devices
        for cid in ["kindle_alpha", "kindle_beta", "kindle_gamma"]:
            _http_get(
                self.port, "/dashboard.png?mock=1", headers={"X-Tracker-Client-ID": cid}
            )

        # 1. Admin enqueues action 'restart' targeted to kindle_alpha
        status, _, body = _http_post(
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"action": "restart", "client_id": "kindle_alpha"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("pending"), "restart")

        # 2. Admin enqueues diag 'quick' targeted to kindle_beta
        status, _, body = _http_post(
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"mode": "quick", "client_id": "kindle_beta"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("requested"), "quick")

        # 3. Client kindle_beta polls /dashboard.png
        s_beta, h_beta, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "kindle_beta"},
        )
        self.assertEqual(s_beta, 200)
        self.assertEqual(h_beta.get("X-Tracker-Diag"), "quick")
        self.assertNotIn("X-Tracker-Action", h_beta)

        # 4. Client kindle_alpha polls /dashboard.png
        s_alpha, h_alpha, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "kindle_alpha"},
        )
        self.assertEqual(s_alpha, 200)
        self.assertEqual(h_alpha.get("X-Tracker-Action"), "restart")
        self.assertNotIn("X-Tracker-Diag", h_alpha)

        # 5. Client kindle_gamma polls /dashboard.png
        s_gamma, h_gamma, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "kindle_gamma"},
        )
        self.assertEqual(s_gamma, 200)
        self.assertNotIn("X-Tracker-Action", h_gamma)
        self.assertNotIn("X-Tracker-Diag", h_gamma)

        # 6. Verify queues are consumed on subsequent polls
        _, h_beta_2, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "kindle_beta"},
        )
        self.assertNotIn("X-Tracker-Diag", h_beta_2)

        _, h_alpha_2, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "kindle_alpha"},
        )
        self.assertNotIn("X-Tracker-Action", h_alpha_2)

    def test_broadcast_dispatch(self):
        """
        Verify broadcast dispatch:
        - Admin enqueues action 'reboot' with client_id='all'.
        - All clients poll and each independently receives X-Tracker-Action: reboot.
        """
        clients = ["kindle_alpha", "kindle_beta", "kindle_gamma"]
        for cid in clients:
            _http_get(
                self.port, "/dashboard.png?mock=1", headers={"X-Tracker-Client-ID": cid}
            )

        # Admin enqueues action reboot for 'all'
        status, _, body = _http_post(
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"action": "reboot", "client_id": "all"}).encode("utf-8"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("pending"), "reboot")

        # Each client independently receives reboot
        for cid in clients:
            s, h, _ = _http_get(
                self.port,
                "/dashboard.png?mock=1&kindle=pw5",
                headers={"X-Tracker-Client-ID": cid},
            )
            self.assertEqual(s, 200)
            self.assertEqual(
                h.get("X-Tracker-Action"),
                "reboot",
                f"Client {cid} did not receive broadcast action",
            )

        # Subsequent poll has consumed the action
        for cid in clients:
            _, h2, _ = _http_get(
                self.port,
                "/dashboard.png?mock=1&kindle=pw5",
                headers={"X-Tracker-Client-ID": cid},
            )
            self.assertNotIn(
                "X-Tracker-Action",
                h2,
                f"Client {cid} queue was not cleared after consumption",
            )

    def test_diagnostics_isolation(self):
        """
        Verify diagnostics dumps are saved under isolated paths:
        <cache_dir>/devices/<client_id>/diagnostics.txt without overwriting.
        GET /diag?client_id=kindle_alpha returns alpha's diagnostics.
        GET /diag?client_id=kindle_gamma returns 404 (no leak).
        """
        alpha_dump = b"KINDLE_ALPHA_DIAG_DUMP\nbattery=87%\nlipc=ok\n"
        beta_dump = b"KINDLE_BETA_DIAG_DUMP\nbattery=33%\nnetwork=ok\n"

        # POST diagnostics from kindle_alpha
        s_a, _, _ = _http_post(
            self.port,
            "/diag",
            headers={
                "X-Tracker-Client-ID": "kindle_alpha",
                "Content-Type": "text/plain",
            },
            body=alpha_dump,
        )
        self.assertEqual(s_a, 200)

        # POST diagnostics from kindle_beta
        s_b, _, _ = _http_post(
            self.port,
            "/diag",
            headers={
                "X-Tracker-Client-ID": "kindle_beta",
                "Content-Type": "text/plain",
            },
            body=beta_dump,
        )
        self.assertEqual(s_b, 200)

        # Verify disk files exist and are isolated
        alpha_file = os.path.join(
            self.temp_dir, "devices", "kindle_alpha", "diagnostics.txt"
        )
        beta_file = os.path.join(
            self.temp_dir, "devices", "kindle_beta", "diagnostics.txt"
        )
        self.assertTrue(os.path.exists(alpha_file), f"Missing file {alpha_file}")
        self.assertTrue(os.path.exists(beta_file), f"Missing file {beta_file}")

        with open(alpha_file, "rb") as f:
            content_a = f.read()
        with open(beta_file, "rb") as f:
            content_b = f.read()

        self.assertEqual(content_a, alpha_dump)
        self.assertEqual(content_b, beta_dump)

        # GET /diag?client_id=kindle_alpha returns alpha's content
        s_get_a, _, body_a = _http_get(
            self.port,
            "/diag?client_id=kindle_alpha",
            headers=_auth_headers(),
        )
        self.assertEqual(s_get_a, 200)
        self.assertIn(b"KINDLE_ALPHA_DIAG_DUMP", body_a)
        self.assertNotIn(b"KINDLE_BETA_DIAG_DUMP", body_a)

        # GET /diag?client_id=kindle_gamma returns 404 (no cross-device leakage)
        s_get_g, _, _ = _http_get(
            self.port,
            "/diag?client_id=kindle_gamma",
            headers=_auth_headers(),
        )
        self.assertEqual(s_get_g, 404)

    def test_per_device_logs(self):
        """
        Verify clients posting logs to /log have them appended to
        <cache_dir>/devices/<client_id>/client.log in isolation.
        """
        s1, _, _ = _http_post(
            self.port,
            "/log",
            headers={
                "X-Tracker-Client-ID": "kindle_alpha",
                "Content-Type": "text/plain",
            },
            body=b"alpha log line 1\n",
        )
        self.assertEqual(s1, 200)

        s2, _, _ = _http_post(
            self.port,
            "/log",
            headers={
                "X-Tracker-Client-ID": "kindle_alpha",
                "Content-Type": "text/plain",
            },
            body=b"alpha log line 2\n",
        )
        self.assertEqual(s2, 200)

        s3, _, _ = _http_post(
            self.port,
            "/log",
            headers={
                "X-Tracker-Client-ID": "kindle_beta",
                "Content-Type": "text/plain",
            },
            body=b"beta log message single\n",
        )
        self.assertEqual(s3, 200)

        alpha_log = os.path.join(self.temp_dir, "devices", "kindle_alpha", "client.log")
        beta_log = os.path.join(self.temp_dir, "devices", "kindle_beta", "client.log")
        gamma_log = os.path.join(self.temp_dir, "devices", "kindle_gamma", "client.log")

        self.assertTrue(os.path.exists(alpha_log))
        self.assertTrue(os.path.exists(beta_log))
        self.assertFalse(os.path.exists(gamma_log))

        with open(alpha_log, encoding="utf-8") as f:
            lines_a = f.read()
        with open(beta_log, encoding="utf-8") as f:
            lines_b = f.read()

        self.assertIn("alpha log line 1", lines_a)
        self.assertIn("alpha log line 2", lines_a)
        self.assertNotIn("beta log message", lines_a)

        self.assertIn("beta log message single", lines_b)
        self.assertNotIn("alpha log line", lines_b)

    def test_web_ui_fleet_status_and_xss_protection(self):
        """
        Verify GET / renders table/cards containing all registered devices,
        online/offline badges, battery percentages, charging indicators,
        and proper HTML escaping to prevent XSS.
        """
        # Register devices with varied metadata
        _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "kindle_alpha",
                "X-Kindle-Battery": "85",
                "X-Kindle-Charging": "1",
                "X-Tracker-Mode": "resident",
            },
        )
        _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "kindle_beta",
                "X-Kindle-Battery": "42",
                "X-Kindle-Charging": "0",
                "X-Tracker-Mode": "oneshot",
            },
        )
        _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "kindle_gamma",
                "X-Kindle-Battery": "99",
                "X-Kindle-Charging": "1",
                "X-Tracker-Mode": "sleep",
            },
        )

        # Inject potentially malicious client metadata to test XSS escaping
        _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "kindle_xss",
                "X-Kindle-Battery": "50",
                "X-Tracker-Client-Version": "<script>alert('xss_ver')</script>",
                "X-Tracker-Mode": "<b>inject</b>",
            },
        )

        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html_text = body.decode("utf-8")

        # Verify device IDs are present
        self.assertIn("kindle_alpha", html_text)
        self.assertIn("kindle_beta", html_text)
        self.assertIn("kindle_gamma", html_text)

        # Verify online status badge
        self.assertIn("ONLINE", html_text)

        # Verify battery and charging symbols
        self.assertIn("⚡ 85%", html_text)
        self.assertIn("42%", html_text)
        self.assertIn("⚡ 99%", html_text)

        # Verify last seen timestamp text
        self.assertIn("ago", html_text)

        # Verify XSS escaping
        self.assertNotIn("<script>alert('xss_ver')</script>", html_text)
        self.assertIn(
            "&lt;script&gt;alert(&#x27;xss_ver&#x27;)&lt;/script&gt;", html_text
        )
        self.assertNotIn("<b>inject</b>", html_text)
        self.assertIn("&lt;b&gt;inject&lt;/b&gt;", html_text)

    def test_control_plane_api(self):
        """
        Verify control plane endpoints:
        - GET /devices returns JSON with registered devices and accurate metadata.
        - POST /action, POST /diag/request, POST /mode with client_id target specific devices.
        """
        # Register kindle_alpha
        _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "kindle_alpha",
                "X-Kindle-Battery": "88",
                "X-Kindle-Charging": "1",
                "X-Tracker-Mode": "resident",
            },
        )

        # GET /devices
        s_unauth, _, _ = _http_get(self.port, "/devices")
        self.assertEqual(s_unauth, 403)

        status, _, body = _http_get(self.port, "/devices", headers=_auth_headers())
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("devices", data)
        dev_map = {d["client_id"]: d for d in data["devices"]}
        self.assertIn("kindle_alpha", dev_map)
        alpha_info = dev_map["kindle_alpha"]
        self.assertEqual(alpha_info["battery"], 88.0)
        self.assertTrue(alpha_info["charging"])
        self.assertEqual(alpha_info["client_mode"], "resident")
        self.assertTrue(alpha_info["online"])

        # Targeted POST /action
        s_act, _, b_act = _http_post(
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps(
                {"action": "clear_backup", "client_id": "kindle_alpha"}
            ).encode("utf-8"),
        )
        self.assertEqual(s_act, 200)
        self.assertEqual(json.loads(b_act)["pending"], "clear_backup")

        # Targeted POST /diag/request
        s_diag, _, b_diag = _http_post(
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"mode": "full", "client_id": "kindle_alpha"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(s_diag, 200)
        self.assertEqual(json.loads(b_diag)["requested"], "full")

        # Targeted POST /mode
        s_mode, _, b_mode = _http_post(
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps(
                {"mode": "sleep-suspend", "client_id": "kindle_alpha"}
            ).encode("utf-8"),
        )
        self.assertEqual(s_mode, 200)
        self.assertEqual(json.loads(b_mode)["pending"], "sleep-suspend")

        # Verify device record holds targeted directives
        rec = get_device_registry().get_device("kindle_alpha")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.pending_action, "clear_backup")
        self.assertEqual(rec.pending_diag, "full")
        self.assertEqual(rec.target_mode, "sleep-suspend")

    def test_concurrent_clients_simulation(self):
        """
        Simulate concurrent polling from multiple Kindle clients simultaneously,
        ensuring thread safety and no race conditions or state corruption.
        """
        client_ids = [f"kindle_client_{i}" for i in range(10)]

        def poll_client(cid: str):
            headers = {
                "X-Tracker-Client-ID": cid,
                "X-Kindle-Battery": "75",
                "X-Kindle-Charging": "0",
                "X-Tracker-View": "morning",
                "X-Tracker-Mode": "resident",
            }
            code, resp_h, data = _http_get(
                self.port, "/dashboard.png?mock=1&kindle=pw5", headers=headers
            )
            return code, len(data)

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(poll_client, client_ids))

        for code, length in results:
            self.assertEqual(code, 200)
            self.assertTrue(length > 0)

        registry = get_device_registry()
        reg_devices = {d.client_id for d in registry.list_devices()}
        for cid in client_ids:
            self.assertIn(cid, reg_devices)


if __name__ == "__main__":
    unittest.main()

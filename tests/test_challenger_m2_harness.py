"""
Empirical Challenge & Stress Harness for Milestone 2.
Tests auto-registration, state isolation, broadcast, diagnostics/log persistence,
304 Not Modified delivery, concurrent multi-device safety, and edge-case sanitization.
"""

import concurrent.futures
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from device_registry import (
    get_device_registry,
    reset_device_registry,
    sanitize_client_id,
)

import server
from server import DashboardHandler


def _http_req(
    method: str, port: int, path: str, headers: dict = None, body: bytes = None
):
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


def _auth_headers(extra: dict = None):
    tok = (
        getattr(server, "CONTROL_TOKEN", "test-token-challenger")
        or "test-token-challenger"
    )
    hdrs = {"X-Tracker-Token": tok}
    if extra:
        hdrs.update(extra)
    return hdrs


class ChallengerM2StressHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_cache = tempfile.mkdtemp(prefix="m2_cache_")
        cls._orig_cache_env = os.environ.get("CACHE_DIR")
        os.environ["CACHE_DIR"] = cls.test_cache
        cls._orig_token = server.CONTROL_TOKEN
        server.CONTROL_TOKEN = "test-token-challenger"

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True
        )
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        server.CONTROL_TOKEN = cls._orig_token
        if cls._orig_cache_env is not None:
            os.environ["CACHE_DIR"] = cls._orig_cache_env
        else:
            os.environ.pop("CACHE_DIR", None)
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join(timeout=5)
        shutil.rmtree(cls.test_cache, ignore_errors=True)

    def setUp(self):
        super().setUp()
        server.CONTROL_TOKEN = "test-token-challenger"
        reset_device_registry()
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

    # --------------------------------------------------------------------------
    # 1. AUTO-REGISTRATION EMPIRICAL VERIFICATION
    # --------------------------------------------------------------------------
    def test_auto_registration_dashboard_png(self):
        """Unknown client polling /dashboard.png must auto-register immediately."""
        status, _, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1&batt=88&charging=1",
            headers={
                "X-Tracker-Client-ID": "kindle-autoreg-dash",
                "X-Tracker-Mode": "resident",
                "X-Tracker-Client-Version": "2.0.0",
                "X-Tracker-Firmware": "5.16.2",
            },
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        dev = reg.get_device("kindle-autoreg-dash")
        self.assertIsNotNone(dev, "Client must be auto-registered in registry")
        self.assertEqual(dev.client_id, "kindle-autoreg-dash")
        self.assertEqual(dev.battery, 88.0)
        self.assertTrue(dev.charging)
        self.assertEqual(dev.client_mode, "resident")
        self.assertEqual(dev.client_version, "2.0.0")
        self.assertEqual(dev.firmware_version, "5.16.2")
        self.assertGreater(dev.last_seen, 0.0)
        self.assertTrue(dev.is_online())

    def test_auto_registration_bus_png(self):
        """Unknown client polling /bus.png must auto-register immediately."""
        status, _, _ = _http_req(
            "GET",
            self.port,
            "/bus.png?mock=1&batt=72&charging=0",
            headers={"X-Tracker-Client-ID": "kindle-autoreg-bus"},
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        dev = reg.get_device("kindle-autoreg-bus")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.battery, 72.0)
        self.assertFalse(dev.charging)

    def test_auto_registration_post_log(self):
        """Unknown client posting to /log must auto-register immediately."""
        status, _, _ = _http_req(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": "kindle-autoreg-log"},
            body=b"System boot complete at 12:00:00\n",
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        dev = reg.get_device("kindle-autoreg-log")
        self.assertIsNotNone(dev)
        self.assertIn("System boot complete at 12:00:00", dev.recent_logs)

    def test_auto_registration_post_diag(self):
        """Unknown client posting to /diag must auto-register immediately."""
        diag_body = "battery_level=95 charging=true\nKindle Paperwhite 5\n"
        status, _, _ = _http_req(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "kindle-autoreg-diag"},
            body=diag_body.encode("utf-8"),
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        dev = reg.get_device("kindle-autoreg-diag")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.battery, 95.0)
        self.assertTrue(dev.charging)
        self.assertEqual(dev.last_diagnostics_text, diag_body)

    def test_auto_registration_admin_actions(self):
        """Admin dispatching action/diag/mode to unknown client auto-registers it with pending state."""
        # 1. Action to unknown client
        s1, _, _ = _http_req(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"action": "restart", "client_id": "kindle-autoreg-act"}',
        )
        self.assertEqual(s1, 200)

        # 2. Diag to unknown client
        s2, _, _ = _http_req(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "full", "client_id": "kindle-autoreg-diagreq"}',
        )
        self.assertEqual(s2, 200)

        # 3. Mode to unknown client
        s3, _, _ = _http_req(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "sleep", "client_id": "kindle-autoreg-modereq"}',
        )
        self.assertEqual(s3, 200)

        reg = get_device_registry()
        dev1 = reg.get_device("kindle-autoreg-act")
        self.assertIsNotNone(dev1)
        self.assertEqual(dev1.pending_action, "restart")

        dev2 = reg.get_device("kindle-autoreg-diagreq")
        self.assertIsNotNone(dev2)
        self.assertEqual(dev2.pending_diag, "full")

        dev3 = reg.get_device("kindle-autoreg-modereq")
        self.assertIsNotNone(dev3)
        self.assertEqual(dev3.target_mode, "sleep")

    # --------------------------------------------------------------------------
    # 2. STATE ISOLATION & TARGETED ADMIN ACTIONS
    # --------------------------------------------------------------------------
    def test_per_device_action_queue_isolation(self):
        """Action queued for devA must NOT be popped by devB, and devA pop must not affect devB."""
        # Register devA and devB
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devB"},
        )

        # Queue action specifically for devA
        _http_req(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"action": "reboot", "client_id": "devA"}',
        )

        # Poll devB -> must NOT receive reboot
        s_b, h_b, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devB"},
        )
        self.assertEqual(s_b, 200)
        self.assertNotIn("X-Tracker-Action", h_b)

        # Poll devA -> must receive reboot
        s_a, h_a, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA"},
        )
        self.assertEqual(s_a, 200)
        self.assertEqual(h_a.get("X-Tracker-Action"), "reboot")

        # Second poll devA -> queue must be empty
        s_a2, h_a2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA"},
        )
        self.assertEqual(s_a2, 200)
        self.assertNotIn("X-Tracker-Action", h_a2)

    def test_per_device_diag_request_isolation(self):
        """Diag request queued for devB must NOT be popped by devA."""
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devB"},
        )

        _http_req(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "full", "client_id": "devB"}',
        )

        # devA polls: must NOT receive diag request
        _, h_a, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA"},
        )
        self.assertNotIn("X-Tracker-Diag", h_a)

        # devB polls: must receive diag request
        _, h_b, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devB"},
        )
        self.assertEqual(h_b.get("X-Tracker-Diag"), "full")

        # devB second poll: must be cleared
        _, h_b2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devB"},
        )
        self.assertNotIn("X-Tracker-Diag", h_b2)

    def test_per_device_mode_isolation(self):
        """Mode set for devC must NOT be sent to devA or devB."""
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA", "X-Tracker-Mode": "resident"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devC", "X-Tracker-Mode": "resident"},
        )

        _http_req(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "sleep", "client_id": "devC"}',
        )

        # devA polls with resident: must NOT receive sleep mode header
        _, h_a, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devA", "X-Tracker-Mode": "resident"},
        )
        self.assertNotIn("X-Tracker-Mode", h_a)

        # devC polls with resident: must receive sleep mode header
        _, h_c, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "devC", "X-Tracker-Mode": "resident"},
        )
        self.assertEqual(h_c.get("X-Tracker-Mode"), "sleep")

    # --------------------------------------------------------------------------
    # 3. BROADCAST ("all") VERIFICATION
    # --------------------------------------------------------------------------
    def test_broadcast_action_independent_consumption(self):
        """Broadcast action enqueues to all registered devices; one popping does not starve another."""
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev1"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev2"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev3"},
        )

        _http_req(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"action": "update", "client_id": "all"}',
        )

        # dev1 pops
        _, h1, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev1"},
        )
        self.assertEqual(h1.get("X-Tracker-Action"), "update")

        # dev1 second poll is cleared
        _, h1_2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev1"},
        )
        self.assertNotIn("X-Tracker-Action", h1_2)

        # dev2 still has its action!
        _, h2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev2"},
        )
        self.assertEqual(h2.get("X-Tracker-Action"), "update")

        # dev3 still has its action!
        _, h3, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-dev3"},
        )
        self.assertEqual(h3.get("X-Tracker-Action"), "update")

    def test_broadcast_diag_independent_consumption(self):
        """Broadcast diag request enqueues to all registered devices independently."""
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-d1"},
        )
        _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-d2"},
        )

        _http_req(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "full", "client_id": "all"}',
        )

        _, hd1, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-d1"},
        )
        self.assertEqual(hd1.get("X-Tracker-Diag"), "full")

        _, hd2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "bc-d2"},
        )
        self.assertEqual(hd2.get("X-Tracker-Diag"), "full")

    # --------------------------------------------------------------------------
    # 4. DIAGNOSTICS & LOG PERSISTENCE UNDER cache/devices/<client_id>/
    # --------------------------------------------------------------------------
    def test_diagnostics_disk_isolation(self):
        """Each device's diagnostics dump is stored in its own directory without collision."""
        cache_dir = os.environ.get("CACHE_DIR")
        self.assertTrue(os.path.isdir(cache_dir))

        diag_content_1 = "DEV_ONE_DIAGNOSTICS_DATA_XYZ"
        diag_content_2 = "DEV_TWO_DIAGNOSTICS_DATA_ABC"

        _http_req(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "persist-dev-1"},
            body=diag_content_1.encode("utf-8"),
        )
        _http_req(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "persist-dev-2"},
            body=diag_content_2.encode("utf-8"),
        )

        p1 = os.path.join(cache_dir, "devices", "persist-dev-1", "diagnostics.txt")
        p2 = os.path.join(cache_dir, "devices", "persist-dev-2", "diagnostics.txt")

        self.assertTrue(os.path.isfile(p1), f"Expected diagnostics file at {p1}")
        self.assertTrue(os.path.isfile(p2), f"Expected diagnostics file at {p2}")

        with open(p1, encoding="utf-8") as f:
            c1 = f.read()
        with open(p2, encoding="utf-8") as f:
            c2 = f.read()

        self.assertEqual(c1, diag_content_1)
        self.assertEqual(c2, diag_content_2)
        self.assertNotEqual(c1, c2)

    def test_get_diag_for_device_without_diagnostics_returns_404(self):
        """When querying GET /diag?client_id=target, if target has never uploaded diagnostics, it must NOT return another device's diagnostics."""
        # 1. Dev A uploads diagnostics
        diag_a = "DIAGNOSTICS_FOR_DEV_A_PRIVATE"
        _http_req(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "dev-diag-A"},
            body=diag_a.encode("utf-8"),
        )

        # 2. Query GET /diag for Dev B (which has never uploaded diagnostics)
        status, _, body = _http_req(
            "GET",
            self.port,
            "/diag?client_id=dev-diag-B",
            headers=_auth_headers(),
        )
        # Should return 404 because Dev B has no diagnostics.
        # It must NOT return Dev A's diagnostics!
        self.assertNotIn(b"DIAGNOSTICS_FOR_DEV_A_PRIVATE", body)
        self.assertEqual(status, 404)

    def test_client_log_disk_isolation_and_appending(self):
        """Each device's logs are appended to its own client.log file."""
        cache_dir = os.environ.get("CACHE_DIR")

        _http_req(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": "log-dev-A"},
            body=b"line A1\n",
        )
        _http_req(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": "log-dev-B"},
            body=b"line B1\n",
        )
        _http_req(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": "log-dev-A"},
            body=b"line A2\n",
        )

        pA = os.path.join(cache_dir, "devices", "log-dev-A", "client.log")
        pB = os.path.join(cache_dir, "devices", "log-dev-B", "client.log")

        with open(pA, encoding="utf-8") as f:
            contentA = f.read()
        with open(pB, encoding="utf-8") as f:
            contentB = f.read()

        self.assertEqual(contentA, "line A1\nline A2\n")
        self.assertEqual(contentB, "line B1\n")

    # --------------------------------------------------------------------------
    # 5. 304 NOT MODIFIED CONDITIONAL GET
    # --------------------------------------------------------------------------
    def test_304_delivers_and_pops_headers_for_specific_device(self):
        """When a device polls with matching ETag (304), action and diag headers are delivered and popped."""
        # 1. First poll to obtain ETag
        s1, h1, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": "etag-dev"},
        )
        self.assertEqual(s1, 200)
        etag = h1.get("ETag")
        self.assertTrue(etag, "ETag must be present in response")

        # 2. Queue an action and diag
        reg = get_device_registry()
        reg.set_action("etag-dev", "clear_backup")
        reg.set_diag("etag-dev", "1")

        # 3. Second poll with If-None-Match: etag -> returns 304 Not Modified
        s2, h2, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={
                "X-Tracker-Client-ID": "etag-dev",
                "If-None-Match": etag,
            },
        )
        self.assertEqual(s2, 304)
        self.assertEqual(h2.get("X-Tracker-Action"), "clear_backup")
        self.assertEqual(h2.get("X-Tracker-Diag"), "1")

        # 4. Third poll with If-None-Match -> headers already popped, must not be present
        s3, h3, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={
                "X-Tracker-Client-ID": "etag-dev",
                "If-None-Match": etag,
            },
        )
        self.assertEqual(s3, 304)
        self.assertNotIn("X-Tracker-Action", h3)
        self.assertNotIn("X-Tracker-Diag", h3)

    # --------------------------------------------------------------------------
    # 6. ADVERSARIAL & SECURITY EDGE CASES
    # --------------------------------------------------------------------------
    def test_adversarial_path_traversal_in_client_id(self):
        """Client ID with directory traversal attempts must never write outside cache/devices/."""
        cache_dir = os.environ.get("CACHE_DIR")
        evil_id = "../../../../tmp/malicious_escape"

        _http_req(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": evil_id},
            body=b"escape payload\n",
        )

        reg = get_device_registry()
        clean_id = sanitize_client_id(evil_id)
        self.assertEqual(clean_id, "tmpmalicious_escape")
        self.assertIn("tmpmalicious_escape", reg._devices)

        # File must be inside cache_dir/devices/tmpmalicious_escape, NOT in /tmp/malicious_escape
        expected_path = os.path.join(
            cache_dir, "devices", "tmpmalicious_escape", "client.log"
        )
        self.assertTrue(os.path.isfile(expected_path))
        self.assertFalse(os.path.exists("/tmp/malicious_escape"))

    def test_adversarial_null_bytes_and_newlines_in_client_id(self):
        """Client ID with null bytes and control chars is cleanly neutralized."""
        evil_id = "device\x00\r\nadmin"
        clean_id = sanitize_client_id(evil_id)
        self.assertEqual(clean_id, "deviceadmin")

        status, _, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1&client_id=device%00%0D%0Aadmin",
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        self.assertIn("deviceadmin", reg._devices)

    def test_adversarial_oversized_client_id(self):
        """Client ID of 5,000 characters is cleanly truncated to 64 chars."""
        huge_id = "k" * 5000
        clean_id = sanitize_client_id(huge_id)
        self.assertEqual(len(clean_id), 64)
        self.assertEqual(clean_id, "k" * 64)

        status, _, _ = _http_req(
            "GET",
            self.port,
            "/dashboard.png?mock=1",
            headers={"X-Tracker-Client-ID": huge_id},
        )
        self.assertEqual(status, 200)

        reg = get_device_registry()
        self.assertIn("k" * 64, reg._devices)

    # --------------------------------------------------------------------------
    # 7. CONCURRENT MULTI-DEVICE STRESS TEST
    # --------------------------------------------------------------------------
    def test_concurrent_multi_device_stress(self):
        """20 concurrent devices polling, posting logs, updating telemetry, and popping actions."""
        errors = []

        def client_worker(device_idx: int):
            cid = f"stress-device-{device_idx:02d}"
            try:
                # 1. Initial poll
                s, _, _ = _http_req(
                    "GET",
                    self.port,
                    f"/dashboard.png?mock=1&batt={50 + device_idx}&charging={device_idx % 2}",
                    headers={
                        "X-Tracker-Client-ID": cid,
                        "X-Tracker-Mode": "resident",
                    },
                )
                if s != 200:
                    errors.append(f"Client {cid} poll 1 failed with {s}")

                # 2. Post logs
                s_log, _, _ = _http_req(
                    "POST",
                    self.port,
                    "/log",
                    headers={"X-Tracker-Client-ID": cid},
                    body=f"log message 1 from {cid}\nlog message 2 from {cid}\n".encode(),
                )
                if s_log != 200:
                    errors.append(f"Client {cid} log post failed with {s_log}")

                # 3. Post diagnostics
                s_diag, _, _ = _http_req(
                    "POST",
                    self.port,
                    "/diag",
                    headers={"X-Tracker-Client-ID": cid},
                    body=f"battery_level={50 + device_idx} charging={device_idx % 2}\ndiag dump for {cid}\n".encode(),
                )
                if s_diag != 200:
                    errors.append(f"Client {cid} diag post failed with {s_diag}")

                # 4. Queue action targeted to this device
                s_act, _, _ = _http_req(
                    "POST",
                    self.port,
                    "/action",
                    headers=_auth_headers({"Content-Type": "application/json"}),
                    body=json.dumps({"action": f"act-{cid}", "client_id": cid}).encode(
                        "utf-8"
                    ),
                )
                if s_act != 200:
                    errors.append(f"Client {cid} action post failed with {s_act}")

                # 5. Poll to consume action
                s_poll2, h_poll2, _ = _http_req(
                    "GET",
                    self.port,
                    "/dashboard.png?mock=1",
                    headers={"X-Tracker-Client-ID": cid},
                )
                if s_poll2 != 200:
                    errors.append(f"Client {cid} poll 2 failed with {s_poll2}")
                if h_poll2.get("X-Tracker-Action") != f"act-{cid}":
                    errors.append(
                        f"Client {cid} expected act-{cid}, got {h_poll2.get('X-Tracker-Action')}"
                    )

            except Exception as e:
                errors.append(f"Client {cid} exception: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(client_worker, i) for i in range(20)]
            concurrent.futures.wait(futures)

        self.assertEqual(len(errors), 0, f"Concurrent stress test errors: {errors}")

        reg = get_device_registry()
        devices = reg.list_devices()
        self.assertEqual(len(devices), 20)

        # Verify GET /devices requires auth and returns all 20 devices
        s_unauth, _, _ = _http_req("GET", self.port, "/devices")
        self.assertEqual(s_unauth, 403)

        status, headers, body = _http_req(
            "GET", self.port, "/devices", headers=_auth_headers()
        )
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["devices"]), 20)


if __name__ == "__main__":
    unittest.main()

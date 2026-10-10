"""
Unit and integration tests for DeviceRegistry, DeviceRecord, and multi-device state isolation.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from device_registry import (
    DeviceRecord,
    DeviceRegistry,
    get_device_registry,
    reset_device_registry,
    sanitize_client_id,
)

import server
from server import DashboardHandler


def _http_get(port: int, path: str, headers: dict = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _http(method: str, port: int, path: str, headers: dict = None, body: bytes = None):
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
    tok = getattr(server, "CONTROL_TOKEN", "test-token-1234") or "test-token-1234"
    hdrs = {"X-Tracker-Token": tok}
    if extra:
        hdrs.update(extra)
    return hdrs


class TestSanitizeClientID(unittest.TestCase):
    def test_valid_alphanumeric_and_special(self):
        self.assertEqual(sanitize_client_id("kindle_pw5-01"), "kindle_pw5-01")
        self.assertEqual(sanitize_client_id("B01234567890ABCD"), "B01234567890ABCD")
        self.assertEqual(sanitize_client_id("client-123_abc"), "client-123_abc")

    def test_whitespace_trimming(self):
        self.assertEqual(sanitize_client_id("  pw5-01  \n"), "pw5-01")

    def test_max_length_truncation(self):
        long_id = "a" * 100
        sanitized = sanitize_client_id(long_id)
        self.assertEqual(len(sanitized), 64)
        self.assertEqual(sanitized, "a" * 64)

    def test_empty_and_none(self):
        self.assertEqual(sanitize_client_id(""), "default")
        self.assertEqual(sanitize_client_id("   "), "default")
        self.assertEqual(sanitize_client_id(None), "default")
        self.assertEqual(sanitize_client_id(123), "123")

    def test_path_traversal_and_malicious_chars(self):
        self.assertEqual(sanitize_client_id("../../etc/passwd"), "etcpasswd")
        self.assertEqual(sanitize_client_id("../../../"), "default")
        self.assertEqual(sanitize_client_id("device/../../other"), "deviceother")
        self.assertEqual(sanitize_client_id("device;rm -rf /"), "devicerm-rf")
        self.assertEqual(sanitize_client_id("dev!@#$%^&*()ice"), "device")


class TestDeviceRecord(unittest.TestCase):
    def test_record_init_and_defaults(self):
        rec = DeviceRecord(client_id="dev-1")
        self.assertEqual(rec.client_id, "dev-1")
        self.assertEqual(rec.remote_ip, "")
        self.assertEqual(rec.last_seen, 0.0)
        self.assertIsNone(rec.battery)
        self.assertIsNone(rec.charging)
        self.assertEqual(rec.pending_action, "")
        self.assertEqual(rec.pending_diag, "")
        self.assertEqual(rec.target_mode, "")
        self.assertEqual(len(rec.recent_logs), 0)
        self.assertFalse(rec.is_online())

    def test_is_online_calculation(self):
        rec = DeviceRecord(client_id="dev-2")
        rec.last_seen = 0.0
        self.assertFalse(rec.is_online(60.0))

        # Just seen -> online
        rec.last_seen = time.time()
        self.assertTrue(rec.is_online(60.0))

        # Seen 100s ago, poll interval 60 -> cutoff is max(120, 150) = 150s -> online
        rec.last_seen = time.time() - 100.0
        self.assertTrue(rec.is_online(60.0))

        # Seen 200s ago, poll interval 60 -> cutoff is 150s -> offline
        rec.last_seen = time.time() - 200.0
        self.assertFalse(rec.is_online(60.0))

        # Seen 200s ago, poll interval 120 -> cutoff is max(120, 300) = 300s -> online
        self.assertTrue(rec.is_online(120.0))

    def test_to_dict_structure(self):
        rec = DeviceRecord(
            client_id="kindle-1",
            remote_ip="192.168.1.50",
            last_seen=time.time(),
            battery=85.0,
            charging=True,
            client_mode="resident",
            client_version="1.2.0",
            firmware_version="5.14.2",
            pending_action="restart",
            pending_diag="full",
            target_mode="sleep",
        )
        rec.recent_logs.append("Boot log 1")
        rec.recent_logs.append("Boot log 2")

        d = rec.to_dict()
        self.assertEqual(d["client_id"], "kindle-1")
        self.assertEqual(d["remote_ip"], "192.168.1.50")
        self.assertEqual(d["ip"], "192.168.1.50")
        self.assertEqual(d["battery"], 85.0)
        self.assertTrue(d["charging"])
        self.assertEqual(d["client_mode"], "resident")
        self.assertEqual(d["client_version"], "1.2.0")
        self.assertEqual(d["firmware_version"], "5.14.2")
        self.assertEqual(d["pending_action"], "restart")
        self.assertEqual(d["pending_diag"], "full")
        self.assertEqual(d["target_mode"], "sleep")
        self.assertEqual(d["recent_logs"], ["Boot log 1", "Boot log 2"])
        self.assertTrue(d["is_online"])
        self.assertTrue(d["online"])
        self.assertEqual(d["status"], "online")


class TestDeviceRegistryUnit(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_auto_registration(self):
        rec1 = self.registry.get_or_register("kindle_pw5_a", "192.168.1.10")
        self.assertEqual(rec1.client_id, "kindle_pw5_a")
        self.assertEqual(rec1.remote_ip, "192.168.1.10")

        # Second lookup returns same instance
        rec2 = self.registry.get_or_register("kindle_pw5_a", "192.168.1.20")
        self.assertIs(rec1, rec2)
        self.assertEqual(rec2.remote_ip, "192.168.1.20")

    def test_update_telemetry(self):
        rec = self.registry.update_telemetry(
            client_id="kindle_pw5_a",
            remote_ip="10.0.0.5",
            battery=92.5,
            charging="true",
            client_mode="resident",
            client_version="1.0.1",
            firmware_version="5.16",
        )
        self.assertGreater(rec.last_seen, 0.0)
        self.assertEqual(rec.battery, 92.5)
        self.assertTrue(rec.charging)
        self.assertEqual(rec.client_mode, "resident")
        self.assertEqual(rec.client_version, "1.0.1")
        self.assertEqual(rec.firmware_version, "5.16")

    def test_isolated_action_and_diag_queues(self):
        # Register device A and B
        self.registry.get_or_register("dev-a")
        self.registry.get_or_register("dev-b")

        # Set action and diag targeted to device A only
        self.registry.set_action("dev-a", "restart")
        self.registry.set_diag("dev-a", "full")

        # Verify device B queue is unaffected
        self.assertEqual(self.registry.pop_action("dev-b"), "")
        self.assertEqual(self.registry.pop_diag("dev-b"), "")

        # Verify device A receives action and diag
        self.assertEqual(self.registry.pop_action("dev-a"), "restart")
        self.assertEqual(self.registry.pop_diag("dev-a"), "full")

        # Popping again clears device A's queue
        self.assertEqual(self.registry.pop_action("dev-a"), "")
        self.assertEqual(self.registry.pop_diag("dev-a"), "")

    def test_broadcast_action_and_diag(self):
        self.registry.get_or_register("dev-1")
        self.registry.get_or_register("dev-2")

        # Broadcast action and diag
        self.registry.set_action("all", "clear_backup")
        self.registry.set_diag("all", "1")
        self.registry.set_mode("all", "sleep")

        rec1 = self.registry.get_device("dev-1")
        rec2 = self.registry.get_device("dev-2")

        self.assertEqual(rec1.target_mode, "sleep")
        self.assertEqual(rec2.target_mode, "sleep")

        self.assertEqual(self.registry.pop_action("dev-1"), "clear_backup")
        self.assertEqual(self.registry.pop_diag("dev-1"), "1")

        self.assertEqual(self.registry.pop_action("dev-2"), "clear_backup")
        self.assertEqual(self.registry.pop_diag("dev-2"), "1")

    def test_broadcast_when_empty_registers_default(self):
        # Empty registry broadcast targets default record
        self.registry.set_action("all", "reboot")
        self.assertEqual(self.registry.pop_action("default"), "reboot")

        self.registry.set_diag("all", "full")
        self.assertEqual(self.registry.pop_diag("default"), "full")

        self.registry.set_mode("all", "oneshot")
        rec = self.registry.get_device("default")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.target_mode, "oneshot")

    def test_diagnostics_file_persistence(self):
        diag_content = "=== Kindle Diagnostics ===\nBattery: 88%\nUptime: 12345s\n"
        self.registry.save_diagnostics("kindle-alpha", diag_content, self.temp_dir)

        rec = self.registry.get_device("kindle-alpha")
        self.assertEqual(rec.last_diagnostics_text, diag_content)
        self.assertGreater(rec.last_diagnostics_time, 0.0)

        diag_path = os.path.join(
            self.temp_dir, "devices", "kindle-alpha", "diagnostics.txt"
        )
        self.assertTrue(os.path.exists(diag_path))
        with open(diag_path, encoding="utf-8") as f:
            saved = f.read()
        self.assertEqual(saved, diag_content)

    def test_append_log_persistence_and_bounding(self):
        for i in range(250):
            self.registry.append_log("kindle-beta", f"Log line {i}\n", self.temp_dir)

        rec = self.registry.get_device("kindle-beta")
        # In-memory deque bounded at 200 items
        self.assertEqual(len(rec.recent_logs), 200)
        self.assertEqual(rec.recent_logs[0], "Log line 50")
        self.assertEqual(rec.recent_logs[-1], "Log line 249")

        log_path = os.path.join(self.temp_dir, "devices", "kindle-beta", "client.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 250)

    def test_registry_max_devices_evicts_oldest_offline(self):
        reg = DeviceRegistry(max_devices=3)
        reg.get_or_register("dev-1")
        reg.get_or_register("dev-2")
        reg.get_or_register("dev-3")

        # Mark dev-1 offline (last seen 500s ago)
        reg.get_device("dev-1").last_seen = time.time() - 500.0
        reg.get_device("dev-2").last_seen = time.time() - 10.0
        reg.get_device("dev-3").last_seen = time.time() - 5.0

        # Adding dev-4 exceeds limit 3 -> must evict dev-1
        reg.get_or_register("dev-4")
        self.assertLessEqual(len(reg._devices), 3)
        self.assertIsNone(reg.get_device("dev-1"))
        self.assertIsNotNone(reg.get_device("dev-4"))

    def test_log_file_truncation_when_exceeding_max_size(self):
        import device_registry as dreg

        orig_max = dreg.MAX_LOG_FILE_SIZE
        try:
            dreg.MAX_LOG_FILE_SIZE = 1000  # 1 KB
            # Append enough data to trigger rotation
            chunk = "A" * 600 + "\n"
            self.registry.append_log("rot-dev", chunk, self.temp_dir)
            self.registry.append_log("rot-dev", chunk, self.temp_dir)
            # File is now 1202 bytes, exceeding 1000 limit.
            # Next append will truncate before appending.
            self.registry.append_log("rot-dev", "B" * 200 + "\n", self.temp_dir)
            log_path = os.path.join(self.temp_dir, "devices", "rot-dev", "client.log")
            size = os.path.getsize(log_path)
            self.assertLessEqual(size, 800)
        finally:
            dreg.MAX_LOG_FILE_SIZE = orig_max

    def test_save_diagnostics_size_bounding(self):
        import device_registry as dreg

        orig_max = dreg.MAX_DIAG_FILE_SIZE
        try:
            dreg.MAX_DIAG_FILE_SIZE = 1000  # 1 KB
            big_text = "D" * 5000
            self.registry.save_diagnostics("diag-dev", big_text, self.temp_dir)
            rec = self.registry.get_device("diag-dev")
            self.assertLessEqual(len(rec.last_diagnostics_text), 1000)
            diag_path = os.path.join(
                self.temp_dir, "devices", "diag-dev", "diagnostics.txt"
            )
            self.assertLessEqual(os.path.getsize(diag_path), 1000)
        finally:
            dreg.MAX_DIAG_FILE_SIZE = orig_max

    def test_list_devices_sorting(self):
        now = time.time()
        r1 = self.registry.get_or_register("older")
        r1.last_seen = now - 50.0

        r2 = self.registry.get_or_register("newer")
        r2.last_seen = now - 10.0

        r3 = self.registry.get_or_register("newest")
        r3.last_seen = now

        devs = self.registry.list_devices()
        ids = [d.client_id for d in devs]
        self.assertEqual(ids, ["newest", "newer", "older"])

    def test_concurrent_access_thread_safety(self):
        def worker(w_id: int):
            for i in range(50):
                cid = f"worker-{w_id % 5}"
                self.registry.update_telemetry(
                    cid, f"192.168.1.{w_id}", battery=float(i)
                )
                self.registry.set_action(cid, f"act-{i}")
                self.registry.pop_action(cid)
                self.registry.append_log(cid, f"log-{i}", self.temp_dir)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(len(self.registry.list_devices()), 5)

    def test_file_io_error_handling(self):
        # Point to an uncreatable directory path to test OSError handling
        bad_dir = "/proc/nonexistent_cache_test"
        # Should not raise exception
        self.registry.save_diagnostics("dev-fail", "some text", bad_dir)
        self.registry.append_log("dev-fail", "some log line", bad_dir)
        rec = self.registry.get_device("dev-fail")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.last_diagnostics_text, "some text")
        self.assertIn("some log line", rec.recent_logs)

    def test_pop_action_and_diag_unknown_device(self):
        self.assertEqual(self.registry.pop_action("nonexistent-dev"), "")
        self.assertEqual(self.registry.pop_diag("nonexistent-dev"), "")

    def test_update_telemetry_edge_cases(self):
        # Battery not a valid float -> ignored without exception
        rec = self.registry.update_telemetry(
            "dev-edge", battery="not-a-number", charging="invalid-bool"
        )
        self.assertIsNone(rec.battery)
        self.assertFalse(rec.charging)

        # Empty string broadcast in set_action and set_diag
        self.registry.set_action("", "action-broadcast")
        self.assertEqual(rec.pending_action, "action-broadcast")

        self.registry.set_diag("   ", "diag-broadcast")
        self.assertEqual(rec.pending_diag, "diag-broadcast")

        self.registry.set_mode("", "mode-broadcast")
        self.assertEqual(rec.target_mode, "mode-broadcast")

    def test_clear_registry(self):
        self.registry.get_or_register("dev-clear-1")
        self.registry.get_or_register("dev-clear-2")
        self.assertEqual(len(self.registry.list_devices()), 2)
        self.registry.clear()
        self.assertEqual(len(self.registry.list_devices()), 0)


class TestDeviceRegistryHTTPIntegration(unittest.TestCase):
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

    def test_dashboard_poll_with_client_id_auto_registers(self):
        status, headers, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5&batt=82&charging=1",
            headers={"X-Tracker-Client-ID": "kindle-device-1"},
        )
        self.assertEqual(status, 200)

        registry = get_device_registry()
        dev = registry.get_device("kindle-device-1")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.battery, 82.0)
        self.assertTrue(dev.charging)
        self.assertTrue(dev.is_online())

    def test_queue_isolation_between_clients_http(self):
        # Target action and diag specifically to device-A
        status_act, _, _ = _http(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"action": "reboot", "client_id": "device-A"}).encode(
                "utf-8"
            ),
        )
        self.assertEqual(status_act, 200)

        status_diag, _, _ = _http(
            "POST",
            self.port,
            "/diag/request",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=json.dumps({"mode": "full", "client_id": "device-A"}).encode("utf-8"),
        )
        self.assertEqual(status_diag, 200)

        # Device-B polls: MUST NOT receive device-A's action or diag
        _s, hb, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-B"},
        )
        self.assertNotIn("X-Tracker-Action", hb)
        self.assertNotIn("X-Tracker-Diag", hb)

        # Device-A polls: MUST receive its targeted action and diag
        _s, ha, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-A"},
        )
        self.assertEqual(ha.get("X-Tracker-Action"), "reboot")
        self.assertEqual(ha.get("X-Tracker-Diag"), "full")

        # Device-A second poll: queue is popped, no action or diag returned
        _s, ha2, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "device-A"},
        )
        self.assertNotIn("X-Tracker-Action", ha2)
        self.assertNotIn("X-Tracker-Diag", ha2)

    def test_broadcast_action_http(self):
        # Pre-register two devices
        _http_get(
            self.port, "/dashboard.png?mock=1", headers={"X-Tracker-Client-ID": "dev-x"}
        )
        _http_get(
            self.port, "/dashboard.png?mock=1", headers={"X-Tracker-Client-ID": "dev-y"}
        )

        # Broadcast action to all devices
        status, _, _ = _http(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"action": "update", "client_id": "all"}',
        )
        self.assertEqual(status, 200)

        # Both devices receive the action
        _s, hx, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "dev-x"},
        )
        self.assertEqual(hx.get("X-Tracker-Action"), "update")

        _s, hy, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "dev-y"},
        )
        self.assertEqual(hy.get("X-Tracker-Action"), "update")

    def test_post_log_per_device(self):
        status, _, _ = _http(
            "POST",
            self.port,
            "/log",
            headers={"X-Tracker-Client-ID": "kindle-logger"},
            body=b"client initialized successfully\nbattery level 94%\n",
        )
        self.assertEqual(status, 200)

        registry = get_device_registry()
        dev = registry.get_device("kindle-logger")
        self.assertIsNotNone(dev)
        self.assertIn("client initialized successfully", dev.recent_logs)
        self.assertIn("battery level 94%", dev.recent_logs)

    def test_post_diag_per_device(self):
        diag_body = (
            "=== Diagnostics ===\nbattery_level=76 charging=true\nSystem: kindle-pw5\n"
        )
        status, _, _ = _http(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "kindle-diag-device"},
            body=diag_body.encode("utf-8"),
        )
        self.assertEqual(status, 200)

        registry = get_device_registry()
        dev = registry.get_device("kindle-diag-device")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.battery, 76.0)
        self.assertTrue(dev.charging)
        self.assertEqual(dev.last_diagnostics_text, diag_body)

    def test_get_devices_api(self):
        # Register two devices with telemetry
        _http_get(
            self.port,
            "/dashboard.png?mock=1&batt=80&charging=0",
            headers={"X-Tracker-Client-ID": "fleet-kindle-1"},
        )
        _http_get(
            self.port,
            "/dashboard.png?mock=1&batt=45&charging=1",
            headers={"X-Tracker-Client-ID": "fleet-kindle-2"},
        )

        status_unauth, _, _ = _http_get(self.port, "/devices")
        self.assertEqual(status_unauth, 403)

        status, headers, body = _http_get(
            self.port, "/devices", headers=_auth_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json")

        payload = json.loads(body)
        self.assertIn("devices", payload)
        devs = {d["client_id"]: d for d in payload["devices"]}

        self.assertIn("fleet-kindle-1", devs)
        self.assertEqual(devs["fleet-kindle-1"]["battery"], 80.0)
        self.assertFalse(devs["fleet-kindle-1"]["charging"])
        self.assertTrue(devs["fleet-kindle-1"]["online"])

        self.assertIn("fleet-kindle-2", devs)
        self.assertEqual(devs["fleet-kindle-2"]["battery"], 45.0)
        self.assertTrue(devs["fleet-kindle-2"]["charging"])
        self.assertTrue(devs["fleet-kindle-2"]["online"])

    def test_post_mode_targeted_and_forwarded(self):
        status, _, _ = _http(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "sleep", "client_id": "mode-client"}',
        )
        self.assertEqual(status, 200)

        registry = get_device_registry()
        dev = registry.get_device("mode-client")
        self.assertEqual(dev.target_mode, "sleep")

        # When mode-client polls with resident mode, it receives sleep header
        _s, headers, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Client-ID": "mode-client",
                "X-Tracker-Mode": "resident",
            },
        )
        self.assertEqual(headers.get("X-Tracker-Mode"), "sleep")

    def test_get_diag_with_client_id(self):
        # Post diagnostics for a specific client
        diag_text = "battery_level=90 charging=false\nkindle diagnostics report test"
        _http(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "dev-diag-get"},
            body=diag_text.encode("utf-8"),
        )

        status, _headers, body = _http_get(
            self.port,
            "/diag?client_id=dev-diag-get",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertIn(b"kindle diagnostics report test", body)

    def test_get_action_and_mode_with_client_id(self):
        # GET /action?do=restart&client_id=target-device
        status, _, body = _http_get(
            self.port,
            "/action?do=restart&client_id=target-device",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], "restart")

        registry = get_device_registry()
        dev = registry.get_device("target-device")
        self.assertIsNotNone(dev)
        self.assertEqual(dev.pending_action, "restart")

        # GET /mode?set=oneshot&client_id=target-device
        status, _, body = _http_get(
            self.port,
            "/mode?set=oneshot&client_id=target-device",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], "oneshot")
        self.assertEqual(dev.target_mode, "oneshot")

    def test_get_diag_isolated_device_returns_404_when_no_diagnostics(self):
        # Post diagnostics for device A
        diag_a = "DIAGNOSTICS_FOR_DEV_A_PRIVATE"
        _http(
            "POST",
            self.port,
            "/diag",
            headers={"X-Tracker-Client-ID": "dev-diag-A-secret"},
            body=diag_a.encode("utf-8"),
        )
        # Device B has never uploaded diagnostics -> GET /diag?client_id=dev-diag-B must 404
        status, _, body = _http_get(
            self.port,
            "/diag?client_id=dev-diag-B-empty",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 404)
        self.assertNotIn(b"DIAGNOSTICS_FOR_DEV_A_PRIVATE", body)

    def test_post_action_and_mode_response_body_with_client_id(self):
        # POST /action with client_id
        status, _, body = _http(
            "POST",
            self.port,
            "/action",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"action": "reboot", "client_id": "target-post-dev"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], "reboot")

        # POST /mode with client_id
        status, _, body = _http(
            "POST",
            self.port,
            "/mode",
            headers=_auth_headers({"Content-Type": "application/json"}),
            body=b'{"mode": "sleep", "client_id": "target-post-dev"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], "sleep")

    def test_path_traversal_client_id_http(self):
        # Client sends malicious path traversal in header
        status, _, _ = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Client-ID": "../../evil_escape"},
        )
        self.assertEqual(status, 200)

        registry = get_device_registry()
        # Should be stored under sanitized key 'evil_escape', never the raw unsanitized string
        self.assertIn("evil_escape", registry._devices)
        self.assertNotIn("../../evil_escape", registry._devices)

    def test_root_html_renders_registered_fleet_table(self):
        registry = get_device_registry()
        registry.update_telemetry(
            client_id="fleet-view-kindle-1",
            remote_ip="10.0.0.50",
            battery=77.0,
            charging=True,
            client_mode="resident",
            client_version="v2.0",
            firmware_version="5.15.1",
        )
        registry.update_telemetry(
            client_id="fleet-view-kindle-2",
            remote_ip="10.0.0.51",
            battery=33.0,
            charging=False,
            client_mode="sleep",
            client_version="v2.0",
            firmware_version="5.15.1",
        )

        status, headers, body = _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        text = body.decode("utf-8")

        self.assertIn("Fleet Overview:", text)
        self.assertIn("Total: <strong>2</strong>", text)
        self.assertIn("Online: <strong>2</strong>", text)
        self.assertIn("fleet-view-kindle-1", text)
        self.assertIn("10.0.0.50", text)
        self.assertIn("⚡ 77%", text)
        self.assertIn("fleet-view-kindle-2", text)
        self.assertIn("10.0.0.51", text)
        self.assertIn("33%", text)
        self.assertIn("Broadcast Controls:", text)

    def test_broadcast_controls_via_get_endpoints(self):
        registry = get_device_registry()
        registry.get_or_register("bc-dev-1")
        registry.get_or_register("bc-dev-2")

        # Broadcast action via GET
        status, _, body = _http_get(
            self.port,
            "/action?do=reboot&client_id=all",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(registry.get_device("bc-dev-1").pending_action, "reboot")
        self.assertEqual(registry.get_device("bc-dev-2").pending_action, "reboot")

        # Broadcast mode via GET
        status, _, body = _http_get(
            self.port,
            "/mode?set=sleep&client_id=all",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(registry.get_device("bc-dev-1").target_mode, "sleep")
        self.assertEqual(registry.get_device("bc-dev-2").target_mode, "sleep")

        # Broadcast diag via GET
        with server._diag_lock:
            server._last_diagnostics["text"] = "sample diagnostics data"
            server._last_diagnostics["time"] = time.time()
        status, _, body = _http_get(
            self.port,
            "/diag?request=full&client_id=all",
            headers=_auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(registry.get_device("bc-dev-1").pending_diag, "full")
        self.assertEqual(registry.get_device("bc-dev-2").pending_diag, "full")


if __name__ == "__main__":
    unittest.main()

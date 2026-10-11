"""
Comprehensive tests for security architecture:
- Ed25519 identity and response signing against vectors.json (§2, §3)
- Release manifest validation and TOCTOU-safe OTA binary loading (§1, §4)
- Control auth, token requirements, rate/body caps, input validation (§9)
- Paths, log sanitization, and discovery network safety (§6, §7)
"""

import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import ota
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from identity import (
    CERT_FORMAT,
    RESP_FORMAT,
    SIGNED_HEADERS,
    IdentityError,
    ServerIdentity,
    b64decode_strict,
    build_response_message,
    is_valid_nonce,
    load_identity,
)
from logsafe import redact, safe_log_lines, strip_controls
from paths import artifact_candidates, find_artifact, resolve_cache_dir
from test_server_http import _http, _http_get

import server
from server import DashboardHandler


class TestVectorsAndCrypto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vec_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "client-go",
            "internal",
            "otasig",
            "testdata",
            "vectors.json",
        )
        with open(vec_path, encoding="utf-8") as f:
            cls.vectors = json.load(f)

    def test_response_message_builder_against_vectors(self):
        vec_resp = self.vectors["response"]
        msg = build_response_message(
            nonce=vec_resp["nonce"],
            path=vec_resp["path"],
            status=vec_resp["status"],
            body=vec_resp["body"].encode("utf-8"),
            headers=vec_resp["headers"],
        )
        self.assertEqual(msg.decode("utf-8"), vec_resp["message"])
        self.assertTrue(vec_resp["message"].startswith(RESP_FORMAT + "\n"))
        self.assertTrue(
            vec_resp["message"].endswith(
                "\nx-tracker-policy=" + vec_resp["headers"]["x-tracker-policy"]
            )
        )
        priv = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.vectors["server_seed_hex"])
        )
        self.assertEqual(
            base64.b64encode(priv.sign(msg)).decode("ascii"), vec_resp["signature"]
        )

    def test_response_format_covers_policy(self):
        self.assertEqual(RESP_FORMAT, "transit-tracker-resp-v2")
        self.assertEqual(len(SIGNED_HEADERS), 13)
        self.assertIn("x-tracker-policy", SIGNED_HEADERS)
        vec = self.vectors["response"]
        msg = build_response_message(
            nonce=vec["nonce"],
            path=vec["path"],
            status=vec["status"],
            body=vec["body"].encode("utf-8"),
            headers=vec["headers"],
        )
        self.assertEqual(msg.decode("utf-8"), vec["message"])

    def test_response_format_v1_and_v2_backward_compatibility(self):
        from identity import (
            RESP_FORMAT_V1,
            RESP_FORMAT_V2,
            SIGNED_HEADERS_V1,
            SIGNED_HEADERS_V2,
        )

        self.assertEqual(len(SIGNED_HEADERS_V1), 12)
        self.assertNotIn("x-tracker-policy", SIGNED_HEADERS_V1)
        self.assertEqual(len(SIGNED_HEADERS_V2), 13)
        self.assertIn("x-tracker-policy", SIGNED_HEADERS_V2)

        nonce = "000102030405060708090a0b0c0d0e0f"
        headers = {
            "etag": '"abc"',
            "x-kindle-poll-interval": "60",
            "x-tracker-policy": "v=1;phase=peak",
        }

        # v1 message
        msg_v1 = build_response_message(
            nonce, "/dashboard.png", 200, b"PNG", headers, resp_format=RESP_FORMAT_V1
        )
        lines_v1 = msg_v1.decode("utf-8").split("\n")
        self.assertEqual(lines_v1[0], "transit-tracker-resp-v1")
        self.assertEqual(len(lines_v1), 5 + 12)  # 5 preamble + 12 headers
        self.assertFalse(any(line.startswith("x-tracker-policy=") for line in lines_v1))

        # v2 message
        msg_v2 = build_response_message(
            nonce, "/dashboard.png", 200, b"PNG", headers, resp_format=RESP_FORMAT_V2
        )
        lines_v2 = msg_v2.decode("utf-8").split("\n")
        self.assertEqual(lines_v2[0], "transit-tracker-resp-v2")
        self.assertEqual(len(lines_v2), 5 + 13)  # 5 preamble + 13 headers
        self.assertIn("x-tracker-policy=v=1;phase=peak", lines_v2)

    def test_server_resolves_v1_format_for_legacy_clients(self):
        h = DashboardHandler.__new__(DashboardHandler)
        h.headers = {"X-Tracker-Client-Version": "1.35.10"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v1")

        h.headers = {"X-Tracker-Client-Version": "1.35.0"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v1")

        h.headers = {"X-Tracker-Client-Version": "1.36.0"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v2")

        h.headers = {"X-Tracker-Client-Version": "1.37.0"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v2")

        h.headers = {"X-Tracker-Resp-Format": "transit-tracker-resp-v1"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v1")

        h.headers = {"X-Tracker-Resp-Format": "transit-tracker-resp-v2"}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v2")

        h.headers = {}
        self.assertEqual(h._resolve_resp_format(), "transit-tracker-resp-v2")

    def test_response_signature_against_vectors(self):
        vec_resp = self.vectors["response"]
        server_seed = bytes.fromhex(self.vectors["server_seed_hex"])
        server_priv = Ed25519PrivateKey.from_private_bytes(server_seed)

        msg = vec_resp["message"].encode("utf-8")
        sig = server_priv.sign(msg)
        sig_b64 = base64.b64encode(sig).decode("ascii")
        self.assertEqual(sig_b64, vec_resp["signature"])

        # Also verify with public key
        pub_bytes = b64decode_strict(self.vectors["server_public_key"])
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig, msg)

    def test_cert_and_manifest_signatures_against_vectors(self):
        release_pub_bytes = b64decode_strict(self.vectors["release_public_key"])
        release_pub = Ed25519PublicKey.from_public_bytes(release_pub_bytes)

        # Cert
        cert_msg = self.vectors["cert"]["message"].encode("utf-8")
        cert_sig = b64decode_strict(self.vectors["cert"]["signature"])
        release_pub.verify(cert_sig, cert_msg)

        # Manifest
        man_msg = self.vectors["manifest"]["message"].encode("utf-8")
        man_sig = b64decode_strict(self.vectors["manifest"]["signature"])
        release_pub.verify(man_sig, man_msg)

    def test_is_valid_nonce(self):
        self.assertTrue(is_valid_nonce("0123456789abcdef0123456789abcdef"))
        self.assertFalse(
            is_valid_nonce("0123456789ABCDEF0123456789ABCDEF")
        )  # uppercase rejected
        self.assertFalse(is_valid_nonce("short"))
        self.assertFalse(
            is_valid_nonce("0123456789abcdef0123456789abcdef0")
        )  # 33 chars
        self.assertFalse(is_valid_nonce(None))
        self.assertFalse(is_valid_nonce(123))

    def test_b64decode_strict(self):
        good = base64.b64encode(b"hello world").decode("ascii")
        self.assertEqual(b64decode_strict(good), b"hello world")
        with self.assertRaises(IdentityError):
            b64decode_strict("")
        with self.assertRaises(IdentityError):
            b64decode_strict(None)
        with self.assertRaises(IdentityError):
            b64decode_strict("not base64!!!")


class TestIdentityModule(unittest.TestCase):
    def test_server_identity_lifecycle(self):
        seed = bytes(range(32))
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii")

        cert_dict = {
            "format": CERT_FORMAT,
            "public_key": pub_b64,
            "issued_at": int(time.time()),
            "signature": base64.b64encode(b"\x11" * 64).decode("ascii"),
        }
        cert_json = json.dumps(cert_dict).encode("utf-8")

        identity = ServerIdentity(priv, cert_json)
        self.assertEqual(identity.public_key_b64, pub_b64)

        hdrs = dict(
            identity.sign_response(
                nonce="0123456789abcdef0123456789abcdef",
                path="/dashboard.png",
                status=200,
                body=b"TESTPNG",
                headers={"etag": '"xyz"'},
            )
        )
        self.assertIn("X-Tracker-Cert", hdrs)
        self.assertIn("X-Tracker-Auth", hdrs)
        self.assertEqual(hdrs["X-Tracker-Cert"], identity.cert_header)

        # Verify signature
        auth_bytes = b64decode_strict(hdrs["X-Tracker-Auth"])
        expected_msg = build_response_message(
            nonce="0123456789abcdef0123456789abcdef",
            path="/dashboard.png",
            status=200,
            body=b"TESTPNG",
            headers={"etag": '"xyz"'},
        )
        priv.public_key().verify(auth_bytes, expected_msg)

    def test_load_identity_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(
                "os.environ",
                {
                    "IDENTITY_KEY_PATH": os.path.join(td, "nonexistent.key"),
                    "IDENTITY_CERT_PATH": os.path.join(td, "nonexistent.cert"),
                },
            ):
                ident, status = load_identity()
                self.assertIsNone(ident)
                self.assertIn("not found", status)

    def test_load_identity_invalid_key_or_cert(self):
        with tempfile.TemporaryDirectory() as td:
            key_path = os.path.join(td, "bad.key")
            cert_path = os.path.join(td, "bad.cert")
            with open(key_path, "wb") as f:
                f.write(b"NOT A PEM KEY")
            with open(cert_path, "wb") as f:
                f.write(b'{"bad": "cert"}')

            with patch.dict(
                "os.environ",
                {
                    "IDENTITY_KEY_PATH": key_path,
                    "IDENTITY_CERT_PATH": cert_path,
                },
            ):
                ident, status = load_identity()
                self.assertIsNone(ident)
                self.assertIn("unusable", status)


class TestOTAManifestAndBinary(unittest.TestCase):
    def setUp(self):
        ota.clear_caches()

    def tearDown(self):
        ota.clear_caches()

    def test_parse_manifest_validation(self):
        valid = {
            "format": "transit-tracker-ota-v1",
            "version": "1.0.0",
            "sha256": "a" * 64,
            "size": 1024,
            "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
        }
        info = ota.parse_manifest(json.dumps(valid).encode("utf-8"))
        self.assertEqual(info.version, "1.0.0")
        self.assertEqual(info.sha256, "a" * 64)
        self.assertEqual(info.size, 1024)

        # Oversized
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(b"{" + b" " * 5000 + b"}")

        # Invalid JSON
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(b"not json")

        # Wrong format
        bad = dict(valid, format="bad-format")
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))

        # Bad version regex
        bad = dict(valid, version="1.0")
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))

        # Bad sha256
        bad = dict(valid, sha256="abc")
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))

        # Bad size
        bad = dict(valid, size=0)
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))
        bad = dict(valid, size=True)
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))

        # Bad signature
        bad = dict(valid, signature="short")
        with self.assertRaises(ota.ManifestError):
            ota.parse_manifest(json.dumps(bad).encode("utf-8"))

    def test_load_binary_and_manifest_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "tracker-arm")
            man_path = os.path.join(td, "tracker-arm.manifest.json")
            with open(bin_path, "wb") as f:
                f.write(b"REALBINARY")
            real_sha = hashlib.sha256(b"REALBINARY").hexdigest()

            # Manifest with mismatched SHA
            man_data = {
                "format": "transit-tracker-ota-v1",
                "version": "1.0.0",
                "sha256": "f" * 64,
                "size": len(b"REALBINARY"),
                "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
            }
            with open(man_path, "w") as f:
                json.dump(man_data, f)

            with patch.object(ota, "BINARY_SEARCH_PATHS", [bin_path]):
                # Manifest is rejected because of sha mismatch
                self.assertIsNone(ota.get_valid_manifest(bin_path))

                # Now fix sha
                man_data["sha256"] = real_sha
                with open(man_path, "w") as f:
                    json.dump(man_data, f)
                ota.clear_caches()
                val_man = ota.get_valid_manifest(bin_path)
                self.assertIsNotNone(val_man)
                self.assertEqual(val_man.version, "1.0.0")

    def test_load_binary_oversized(self):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "tracker-arm")
            with open(bin_path, "wb") as f:
                f.write(b"X" * 100)
            with patch("os.fstat") as mock_fstat:
                st = MagicMock()
                st.st_size = 40 * 1024 * 1024  # > 32 MiB
                st.st_mtime = time.time()
                mock_fstat.return_value = st
                self.assertIsNone(ota.load_binary(bin_path))


class TestPathsAndLogsafe(unittest.TestCase):
    def test_resolve_cache_dir(self):
        with patch.dict("os.environ", {"CACHE_DIR": "/custom/cache"}):
            self.assertEqual(resolve_cache_dir(), "/custom/cache")

        with patch.dict("os.environ", {"CACHE_DIR": ""}):
            with patch("os.makedirs"):
                with patch("os.access", return_value=True):
                    self.assertEqual(resolve_cache_dir(), "/app/cache")
                with patch("os.access", return_value=False):
                    self.assertTrue(resolve_cache_dir().endswith("cache"))

    def test_artifact_candidates_and_find(self):
        cands = artifact_candidates("test_art")
        self.assertEqual(len(cands), 2)
        self.assertIsNone(find_artifact("definitely_does_not_exist_xyz"))

    def test_logsafe_redact_and_strip(self):
        self.assertEqual(
            redact("GET /stop?token=secret123 HTTP/1.1"),
            "GET /stop?token=REDACTED HTTP/1.1",
        )
        self.assertEqual(
            redact("http://host/?foo=1&token=abc&bar=2"),
            "http://host/?foo=1&token=REDACTED&bar=2",
        )

        dirty = "\x1b[31mRed text\x1b[0m\x07\x08clean"
        clean = strip_controls(dirty)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\x07", clean)

        lines = safe_log_lines("a\nb\r\nc\n" + "x\n" * 1500, max_lines=50)
        self.assertLessEqual(len(lines), 51)


class TestAuthenticatedEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create a mock identity for server
        seed = bytes(range(32))
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii")
        cert_dict = {
            "format": CERT_FORMAT,
            "public_key": pub_b64,
            "issued_at": int(time.time()),
            "signature": base64.b64encode(b"\x22" * 64).decode("ascii"),
        }
        cert_json = json.dumps(cert_dict).encode("utf-8")
        cls.test_identity = ServerIdentity(priv, cert_json)
        server._identity = cls.test_identity

        server.CONTROL_TOKEN = "sec-tok"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server._identity = None

    def test_identity_endpoint(self):
        # Without nonce -> 200 but no X-Tracker-Auth
        status, headers, body = _http_get(self.port, "/identity")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Tracker-Auth", headers)
        data = json.loads(body)
        self.assertEqual(data["service"], "transit-tracker")

        # Valid nonce -> 200 with signed auth header
        nonce = "aabbccddeeff00112233445566778899"
        status, headers, body = _http_get(
            self.port, "/identity", headers={"X-Tracker-Nonce": nonce}
        )
        self.assertEqual(status, 200)
        self.assertIn("X-Tracker-Cert", headers)
        self.assertIn("X-Tracker-Auth", headers)

        data = json.loads(body)
        self.assertEqual(data["service"], "transit-tracker")
        self.assertEqual(data["version"], server.SERVER_VERSION)

        # Verify signature
        sig = b64decode_strict(headers["X-Tracker-Auth"])
        expected_msg = build_response_message(
            nonce=nonce,
            path="/identity",
            status=200,
            body=body,
            headers={},
        )
        pub = Ed25519PublicKey.from_public_bytes(
            b64decode_strict(self.test_identity.public_key_b64)
        )
        pub.verify(sig, expected_msg)

    def test_signed_dashboard_200_and_304(self):
        nonce = "11223344556677889900aabbccddeeff"
        status, headers, body = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Nonce": nonce},
        )
        self.assertEqual(status, 200)
        self.assertIn("X-Tracker-Auth", headers)
        etag = headers.get("ETag")

        # 304 Not Modified
        nonce304 = "99887766554433221100ffeeddccbbaa"
        status304, headers304, body304 = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Nonce": nonce304, "If-None-Match": etag},
        )
        self.assertEqual(status304, 304)
        self.assertIn("X-Tracker-Auth", headers304)

    def test_signed_dashboard_205_when_stopped(self):
        server.tracker_stopped = True
        try:
            nonce = "00112233445566778899aabbccddeeff"
            status, headers, body = _http_get(
                self.port, "/dashboard.png?mock=1", headers={"X-Tracker-Nonce": nonce}
            )
            self.assertEqual(status, 205)
            self.assertIn("X-Tracker-Auth", headers)
        finally:
            server.tracker_stopped = False

    def _verify(self, nonce, path, status, body, headers):
        """Rebuilds the message from the received headers and verifies it."""
        hdr_map = {k.lower(): v for k, v in headers.items()}
        msg = build_response_message(
            nonce=nonce,
            path=path,
            status=status,
            body=body,
            headers=hdr_map,
        )
        pub = Ed25519PublicKey.from_public_bytes(
            b64decode_strict(self.test_identity.public_key_b64)
        )
        pub.verify(b64decode_strict(headers["X-Tracker-Auth"]), msg)

    def test_dashboard_signs_policy_200_and_304(self):
        nonce = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        status, headers, body = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={"X-Tracker-Nonce": nonce},
        )
        self.assertEqual(status, 200)
        self.assertIn("X-Tracker-Policy", headers)
        self._verify(nonce, "/dashboard.png", 200, body, headers)
        # Tampering with the policy breaks the signature.
        tampered = dict(headers)
        tampered["X-Tracker-Policy"] = headers["X-Tracker-Policy"].replace(
            "session=", "session=9"
        )
        with self.assertRaises(InvalidSignature):
            self._verify(nonce, "/dashboard.png", 200, body, tampered)

        nonce304 = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
        status, headers304, body304 = _http_get(
            self.port,
            "/dashboard.png?mock=1&kindle=pw5",
            headers={
                "X-Tracker-Nonce": nonce304,
                "If-None-Match": headers["ETag"],
            },
        )
        self.assertEqual(status, 304)
        self.assertIn("X-Tracker-Policy", headers304)
        self._verify(nonce304, "/dashboard.png", 304, body304, headers304)

    def test_205_and_identity_signed(self):
        server.tracker_stopped = True
        try:
            nonce = "abcdefabcdefabcdefabcdefabcdefab"
            status, headers, body = _http_get(
                self.port,
                "/dashboard.png?mock=1",
                headers={"X-Tracker-Nonce": nonce},
            )
            self.assertEqual(status, 205)
            self._verify(nonce, "/dashboard.png", 205, b"", {**headers})
        finally:
            server.tracker_stopped = False

        nonce = "fedcbafedcbafedcbafedcbafedcbafe"
        status, headers, body = _http_get(
            self.port, "/identity", headers={"X-Tracker-Nonce": nonce}
        )
        self.assertEqual(status, 200)
        sig_headers = {
            "X-Tracker-Auth": headers["X-Tracker-Auth"],
            "content-type": "application/json",
            "content-length": str(len(body)),
        }
        self._verify(nonce, "/identity", 200, body, sig_headers)

    def test_rotate_validation_400(self):
        status, _, _ = _http_get(self.port, "/dashboard.png?mock=1&rotate=45")
        self.assertEqual(status, 400)

    def test_view_sanitization(self):
        status, headers, _ = _http_get(
            self.port, "/dashboard.png?mock=1&view=<script>alert(1)</script>"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Tracker-View"), "auto")

    def test_post_log_and_diag_body_limits(self):
        # 413 on payload > 64 KiB
        oversized = b"A" * (65536 + 10)
        status, _, _ = _http("POST", self.port, "/log", body=oversized)
        self.assertEqual(status, 413)

        status, _, _ = _http("POST", self.port, "/diag", body=oversized)
        self.assertEqual(status, 413)

        # Missing Content-Length -> 411
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", self.port))
        try:
            s.sendall(b"POST /log HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            resp = s.recv(1024)
            self.assertIn(b"411", resp)
        finally:
            s.close()

    def test_post_urlencoded_bodies(self):
        # Mode urlencoded
        status, _, body = _http(
            "POST",
            self.port,
            "/mode",
            headers={
                "X-Tracker-Token": server.CONTROL_TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"set=oneshot",
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._mode_requested, "oneshot")

        # Action urlencoded
        status, _, body = _http(
            "POST",
            self.port,
            "/action",
            headers={
                "X-Tracker-Token": server.CONTROL_TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"do=disable-ads",
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._device_action, "disable-ads")

        # Diag request urlencoded
        status, _, body = _http(
            "POST",
            self.port,
            "/diag/request",
            headers={
                "X-Tracker-Token": server.CONTROL_TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"level=full",
        )
        self.assertEqual(status, 200)
        self.assertEqual(server._diag_requested, "full")

    def test_tracker_arm_manifest_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "tracker-arm")
            man_path = os.path.join(td, "tracker-arm.manifest.json")
            with open(bin_path, "wb") as f:
                f.write(b"SAMPLE")
            man_data = {
                "format": "transit-tracker-ota-v1",
                "version": "2.0.0",
                "sha256": hashlib.sha256(b"SAMPLE").hexdigest(),
                "size": 6,
                "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
            }
            with open(man_path, "w") as f:
                json.dump(man_data, f)

            with patch.object(ota, "BINARY_SEARCH_PATHS", [bin_path]):
                ota.clear_caches()
                status, headers, body = _http_get(self.port, "/tracker-arm.manifest")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "application/json")
                resp_json = json.loads(body)
                self.assertEqual(resp_json["version"], "2.0.0")

    def test_parse_cert_errors_and_verify(self):
        from identity import parse_cert, verify_cert

        # Invalid JSON
        with self.assertRaises(IdentityError):
            parse_cert(b"not-json")

        # Non-dict
        with self.assertRaises(IdentityError):
            parse_cert(b"[]")

        # Wrong format
        with self.assertRaises(IdentityError):
            parse_cert(b'{"format": "wrong"}')

        # Bad public key
        with self.assertRaises(IdentityError):
            parse_cert(
                b'{"format": "transit-tracker-server-v1", "public_key": "short"}'
            )

        # Bad issued_at
        good_pub = base64.b64encode(b"\x00" * 32).decode("ascii")
        with self.assertRaises(IdentityError):
            parse_cert(
                json.dumps(
                    {
                        "format": "transit-tracker-server-v1",
                        "public_key": good_pub,
                        "issued_at": -1,
                    }
                ).encode("utf-8")
            )

        # Bad signature length
        with self.assertRaises(IdentityError):
            parse_cert(
                json.dumps(
                    {
                        "format": "transit-tracker-server-v1",
                        "public_key": good_pub,
                        "issued_at": 100,
                        "signature": "short",
                    }
                ).encode("utf-8")
            )

        # Verify cert helper failure
        self.assertFalse(verify_cert({}, good_pub))

        # ServerIdentity mismatched pubkey
        priv = Ed25519PrivateKey.generate()
        other_pub = base64.b64encode(b"\x99" * 32).decode("ascii")
        bad_cert = json.dumps(
            {
                "format": "transit-tracker-server-v1",
                "public_key": other_pub,
                "issued_at": 100,
                "signature": base64.b64encode(b"\x00" * 64).decode("ascii"),
            }
        ).encode("utf-8")
        with self.assertRaises(IdentityError):
            ServerIdentity(priv, bad_cert)


if __name__ == "__main__":
    unittest.main()

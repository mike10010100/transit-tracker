import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple, Optional

from PIL import Image

try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:
    Zeroconf = None  # type: ignore[assignment,misc]
    ServiceInfo = None  # type: ignore[assignment,misc]

import state_machine
from bus_tracker import NJTransitBusTracker, describe_base_url, normalize_arrival
from citibike import (
    CB_STATUS_ERROR,
    CitiBikeTracker,
)
from device_registry import (
    get_device_registry,
    sanitize_client_id,
)
from discovery import (
    DISCOVERY_PORT,
    get_local_ip,
    is_private_address,
    start_discovery_responder,
    start_mdns_advertiser,
)
from gtfs_bus import GTFSBusTracker
from identity import (
    NONCE_HEADER,
    RESP_FORMAT_V1,
    RESP_FORMAT_V2,
    is_valid_nonce,
    load_identity,
)
from kindle_image import (
    PW5_LANDSCAPE,
    format_for_kindle,
    native_render_scale,
    sanitize_kindle_panel,
)
from logsafe import redact, safe_log_lines
from ota import (
    get_valid_manifest,
    load_binary,
    sha256_file,
)
from paths import resolve_cache_dir
from render_dashboard import STOPS, WIDTH, get_mock_data, render_dashboard, resolve_view
from schedule import (
    _parse_hour_env,
    clear_schedule_override,
    get_commute_lighting,
    get_policy_header,
    get_presentation,
    get_schedule_report,
    get_schedule_state,
    get_status_note,
    get_target_poll_interval,
    is_overnight_hours,
    is_peak_commute_hours,
    reset_schedule_config,
    save_schedule_config,
    set_schedule_override,
)
from version import VERSION

__all__ = [
    "PORT",
    "SERVER_VERSION",
    "_parse_hour_env",
    "format_for_kindle",
    "get_commute_lighting",
    "get_presentation",
    "get_schedule_state",
    "get_status_note",
    "get_target_poll_interval",
    "is_overnight_hours",
    "is_peak_commute_hours",
    "is_private_address",
    "sha256_file",
]

PORT = int(os.environ.get("PORT", 8000))
INTERACTIVE_TTL = 30
SERVER_VERSION = VERSION


# Control token management (security spec §9)
def init_control_token() -> str:
    env_tok = os.environ.get("TRACKER_CONTROL_TOKEN", "").strip()
    if env_tok:
        return env_tok
    cache_dir = resolve_cache_dir()
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        pass
    token_file = os.path.join(cache_dir, "control_token")
    if os.path.exists(token_file):
        try:
            with open(token_file, encoding="utf-8") as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
    tok = secrets.token_hex(16)
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


CONTROL_TOKEN = init_control_token()
_identity, _identity_status = load_identity()

tracker = None
gtfs_tracker = None
cb_tracker = CitiBikeTracker(cache_ttl=30)


def check_control_auth(handler) -> bool:
    """
    Authorizes a control request (security spec §9).
    Requires matching X-Tracker-Token header. No query-param or private-IP bypass.
    """
    supplied = handler.headers.get("X-Tracker-Token", "").strip()
    return bool(CONTROL_TOKEN) and hmac.compare_digest(supplied, CONTROL_TOKEN)


# Last device diagnostics report uploaded by a client
_diag_lock = threading.Lock()
_last_diagnostics: dict[str, Any] = {
    "text": "",
    "time": 0.0,
    "battery": None,
    "charging": None,
}
_diag_requested = ""

VALID_RUN_MODES = ("resident", "oneshot", "sleep", "sleep-suspend")
_mode_requested = os.environ.get("TRACKER_DEFAULT_MODE", "").strip().lower()
if _mode_requested not in VALID_RUN_MODES:
    _mode_requested = ""

_device_action = ""


class BatteryDiag(NamedTuple):
    level: Optional[int]
    charging: Optional[bool]


def parse_diag_battery(text: str) -> BatteryDiag:
    """
    Extracts (level, charging) from a diagnostics report's machine-readable
    'battery_level=<n> charging=<bool>' line. Returns (None, None) if absent.
    """
    m = re.search(r"battery_level=(-?\d+)\s+charging=(true|false)", text)
    if not m:
        return BatteryDiag(None, None)
    level = int(m.group(1))
    if level < 0:
        return BatteryDiag(None, None)
    return BatteryDiag(level, m.group(2) == "true")


def parse_diag_versions(text: str) -> tuple[str, str]:
    """
    Extracts (client_version, firmware_version) from diagnostics text report.
    Returns ("", "") if not detected.
    """
    client_ver = ""
    fw_ver = ""
    m_ver = re.search(r"^=== DIAGNOSTICS v([^\s=]+) ===", text, re.MULTILINE)
    if m_ver:
        client_ver = m_ver.group(1).strip()
    m_fw = re.search(r"/etc/prettyversion\.txt:\s*\n\s*([^\n(]+)", text)
    if m_fw and "<missing>" not in m_fw.group(1) and "<directory>" not in m_fw.group(1):
        fw_ver = m_fw.group(1).strip()
    if not fw_ver:
        m_fw2 = re.search(r"/etc/version:\s*\n\s*([^\n]+)", text)
        if (
            m_fw2
            and "<missing>" not in m_fw2.group(1)
            and "<directory>" not in m_fw2.group(1)
        ):
            fw_ver = m_fw2.group(1).strip()
    return client_ver, fw_ver


# Upstream data cache (bus arrivals + Citi Bike status), decoupled from render.
_data_lock = threading.Lock()
_data_fetch_lock = threading.Lock()
_data_cache: dict[str, Any] = {"time": 0.0, "stops": None, "status": {}, "cb": None}
_render_cache: dict[Any, bytes] = {}
_render_lock = threading.Lock()


def data_cache_ttl(interactive: bool = False) -> int:
    if interactive:
        return INTERACTIVE_TTL
    return get_target_poll_interval()


def get_fresh_data(
    use_mock: bool = False, interactive: bool = False
) -> tuple[dict[str, Any], dict[str, str], Any]:
    """
    Returns (stops_data, stop_status, cb_data), refreshing upstream sources at
    most once per data_cache_ttl(). Shared by all render requests.
    Mock data requests never poison the shared cache.
    """
    if use_mock:
        return (
            get_mock_data(),
            {str(stop["id"]): NJTransitBusTracker.STATUS_OK for stop in STOPS},
            cb_tracker.get_mock_data(),
        )

    now = time.time()
    with _data_lock:
        fresh = _data_cache["stops"] is not None and (
            now - float(_data_cache["time"]) < data_cache_ttl(interactive)
        )
        if fresh:
            return _data_cache["stops"], _data_cache["status"], _data_cache["cb"]

    # Single-flight upstream fetch so concurrent misses wait for a single fetch
    with _data_fetch_lock:
        now = time.time()
        with _data_lock:
            fresh = _data_cache["stops"] is not None and (
                now - float(_data_cache["time"]) < data_cache_ttl(interactive)
            )
            if fresh:
                return _data_cache["stops"], _data_cache["status"], _data_cache["cb"]

        stops_data: dict[str, Any] = {}
        stop_status: dict[str, str] = {}
        global tracker, gtfs_tracker
        if tracker is None:
            tracker = NJTransitBusTracker()
        if gtfs_tracker is None:
            gtfs_tracker = GTFSBusTracker(
                route="126", stops=[str(stop["id"]) for stop in STOPS]
            )

        allow_realtime = get_schedule_state().realtime
        for stop in STOPS:
            sid = str(stop["id"])
            arrivals = None
            try:
                arrivals = gtfs_tracker.get_upcoming(
                    sid, limit=3, allow_realtime=allow_realtime
                )
            except Exception as e:
                print(
                    f"[Server] GTFS-BUS fetch error ({redact(e)}); falling back to public API."
                )
            if arrivals:
                stops_data[sid] = arrivals
                stop_status[sid] = NJTransitBusTracker.STATUS_OK
            else:
                status, trips = tracker.get_arrivals_with_status(
                    stop_id=sid, route="126"
                )
                stops_data[sid] = [dict(normalize_arrival(t)) for t in trips]
                stop_status[sid] = status

        try:
            snap = cb_tracker.get_snapshot()
            if snap.status == CB_STATUS_ERROR:
                cb_data = []
            else:
                cb_data = snap.stations
        except Exception as e:
            print(f"[Server] Citi Bike fetch error ({redact(e)})")
            cb_data = []

        with _data_lock:
            _data_cache["time"] = now
            _data_cache["stops"] = stops_data
            _data_cache["status"] = stop_status
            _data_cache["cb"] = cb_data

        return stops_data, stop_status, cb_data


def warm_up_gtfs() -> None:
    """Builds the GTFS static index ahead of the first client render."""
    global gtfs_tracker
    try:
        if gtfs_tracker is None:
            gtfs_tracker = GTFSBusTracker(
                route="126", stops=[str(stop["id"]) for stop in STOPS]
            )
        gtfs_tracker.ensure_index(wait=True)
    except Exception as e:
        print(f"[GTFS] warm-up failed ({redact(e)})")


def get_fresh_dashboard_image(
    use_mock: bool = False,
    batt_level: Optional[int] = None,
    is_charging: bool = False,
    view: str = "auto",
    width: int = 800,
    height: int = 480,
    scale: float = 1.0,
    presentation: str = "interactive",
    status_note: str = "",
    interactive: bool = False,
) -> Image.Image:
    stops_data, stop_status, cb_data = get_fresh_data(
        use_mock=use_mock, interactive=interactive
    )

    with _data_lock:
        data_time = _data_cache["time"] if not use_mock else 0

    cache_key = (
        use_mock,
        view,
        width,
        height,
        scale,
        batt_level,
        is_charging,
        presentation,
        status_note,
        data_time,
    )
    with _render_lock:
        cached = _render_cache.get(cache_key)
        if cached is not None:
            return Image.open(io.BytesIO(cached))

    buf = io.BytesIO()
    render_dashboard(
        stops_data,
        citibike_data=cb_data,
        output_path=buf,
        view=view,
        is_mock=use_mock,
        batt_level=batt_level,
        is_charging=is_charging,
        stop_status=stop_status,
        width=width,
        height=height,
        scale=scale,
        presentation=presentation,
        status_note=status_note,
    )

    img_bytes = buf.getvalue()
    with _render_lock:
        if len(_render_cache) > 16:
            _render_cache.clear()
        _render_cache[cache_key] = img_bytes

    return Image.open(io.BytesIO(img_bytes))


tracker_stopped = False


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def send_header(self, keyword: str, value: Any) -> None:
        str_val = str(value)
        if "\r" in str_val or "\n" in str_val:
            raise ValueError(f"CR/LF detected in header {keyword}: {str_val!r}")
        try:
            str_val.encode("latin-1")
        except UnicodeEncodeError as e:
            raise ValueError(
                f"Non-latin1 character in header {keyword}: {str_val!r}"
            ) from e
        super().send_header(keyword, str_val)

    def _send_forbidden(self) -> None:
        msg = b"<h1>403 Forbidden</h1><p>Control endpoint requires a valid X-Tracker-Token header.</p>"
        self.close_connection = True
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(msg)

    def _send_empty(self, code: int) -> None:
        self.send_response(code)
        if code >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_tracker_headers(
        self, status: int, headers: list[tuple[str, str]]
    ) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()

    def _write_body(self, data: bytes) -> None:
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _send_json(self, code: int, data: Any) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._write_body(payload)

    def _read_body(self, max_bytes: int = 65536) -> Optional[bytes]:
        try:
            length_hdr = self.headers.get("Content-Length")
            if length_hdr is None:
                self.close_connection = True
                self._send_empty(411)  # Length Required
                return None
            length = int(length_hdr)
            if length < 0:
                self.close_connection = True
                self._send_empty(400)
                return None
            if length > max_bytes:
                self.close_connection = True
                self._send_empty(413)  # Payload Too Large
                return None
            return self.rfile.read(length)
        except (ValueError, OSError):
            self.close_connection = True
            self._send_empty(400)
            return None

    def _extract_client_id(self, params: Optional[dict[str, list[str]]] = None) -> str:
        cid = self.headers.get("X-Tracker-Client-ID", "").strip()
        if not cid and params:
            cid = params.get("client_id", params.get("id", [""]))[0].strip()
        return sanitize_client_id(cid)

    def _is_client_device(self, params: Optional[dict[str, list[str]]] = None) -> bool:
        """
        Determines if an incoming request originated from an active transit-tracker
        client device (e.g. Kindle Paperwhite ARM client or embedded polling script)
        rather than a standard web browser previewing /dashboard.png.
        """
        if bool(self.headers.get("X-Tracker-Client-ID")):
            return True
        if bool(self.headers.get("X-Tracker-Mode")):
            return True
        if bool(self.headers.get("X-Tracker-Client-Version")):
            return True
        if bool(self.headers.get("X-Tracker-Firmware")):
            return True
        if bool(self.headers.get("X-Kindle-Battery")):
            return True
        if params:
            if "client_id" in params or "id" in params:
                return True
            if "kindle" in params or "batt" in params or "battery" in params:
                return True
        return False

    def _remote_ip(self) -> str:
        if hasattr(self, "client_address") and self.client_address:
            return str(self.client_address[0])
        try:
            return self.address_string()
        except Exception:
            return ""

    def _resolve_resp_format(self) -> str:
        req_fmt = self.headers.get("X-Tracker-Resp-Format", "").strip()
        if req_fmt in (RESP_FORMAT_V1, RESP_FORMAT_V2):
            return req_fmt
        client_ver = self.headers.get("X-Tracker-Client-Version", "").strip()
        if client_ver:
            try:
                parts = [int(p) for p in client_ver.split(".")[:3]]
                if len(parts) == 3 and parts < [1, 36, 0]:
                    return RESP_FORMAT_V1
            except (ValueError, IndexError):
                pass
        return RESP_FORMAT_V2

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        client_id = self._extract_client_id(params)
        remote_ip = self._remote_ip()
        registry = get_device_registry()
        if client_id != "default" or parsed.path in ["/log", "/diag"]:
            registry.get_or_register(client_id, remote_ip)

        if parsed.path == "/stop":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            global tracker_stopped
            tracker_stopped = True
            payload = json.dumps({"status": "stopped"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path in ["/resume", "/start"]:
            if not check_control_auth(self):
                self._send_forbidden()
                return
            tracker_stopped = False
            payload = json.dumps({"status": "resumed"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/mode":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            body = self._read_body(4096)
            if body is None:
                return
            want = ""
            target_id = ""
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    want = str(data.get("set", data.get("mode", ""))).lower().strip()
                    target_id = str(data.get("client_id", data.get("id", ""))).strip()
            except Exception:
                qs = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))
                want = qs.get("set", qs.get("mode", [""]))[0].lower().strip()
                target_id = qs.get("client_id", qs.get("id", [""]))[0].strip()
            if not target_id:
                target_id = (
                    params.get("client_id", params.get("id", ["all"]))[0].strip()
                    or "all"
                )
            global _mode_requested
            if want in VALID_RUN_MODES:
                registry.set_mode(target_id, want)
                with _diag_lock:
                    if target_id.lower() == "all" or target_id == "default":
                        _mode_requested = want
                print(
                    f"[Mode] requested client mode '{want}' for {target_id} on the next poll"
                )
            if target_id and target_id.lower() != "all":
                rec = registry.get_device(target_id)
                pending = (
                    rec.target_mode
                    if rec
                    else (want if want in VALID_RUN_MODES else "")
                )
            else:
                with _diag_lock:
                    pending = _mode_requested
            payload = json.dumps(
                {"pending": pending, "valid": list(VALID_RUN_MODES)}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/action":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            body = self._read_body(4096)
            if body is None:
                return
            do = ""
            target_id = ""
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    do = str(data.get("do", data.get("action", ""))).strip().lower()
                    target_id = str(data.get("client_id", data.get("id", ""))).strip()
            except Exception:
                qs = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))
                do = qs.get("do", qs.get("action", [""]))[0].strip().lower()
                target_id = qs.get("client_id", qs.get("id", [""]))[0].strip()
            if not target_id:
                target_id = (
                    params.get("client_id", params.get("id", ["all"]))[0].strip()
                    or "all"
                )
            global _device_action
            if do:
                registry.set_action(target_id, do)
                with _diag_lock:
                    if target_id.lower() == "all" or target_id == "default":
                        _device_action = do
                print(
                    f"[Action] queued device action '{do}' for {target_id} for the next poll"
                )
            if target_id and target_id.lower() != "all":
                rec = registry.get_device(target_id)
                pending = rec.pending_action if rec else do
            else:
                with _diag_lock:
                    pending = _device_action
            payload = json.dumps({"pending": pending}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/diag/request":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            body = self._read_body(4096)
            if body is None:
                return
            level = "1"
            target_id = ""
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    level = (
                        str(data.get("level", data.get("mode", "1"))).strip().lower()
                    )
                    target_id = str(data.get("client_id", data.get("id", ""))).strip()
            except Exception:
                qs = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))
                level = qs.get("level", qs.get("mode", ["1"]))[0].strip().lower()
                target_id = qs.get("client_id", qs.get("id", [""]))[0].strip()
            if not target_id:
                target_id = (
                    params.get("client_id", params.get("id", ["all"]))[0].strip()
                    or "all"
                )
            req = "full" if level == "full" else ("quick" if level == "quick" else "1")
            global _diag_requested
            registry.set_diag(target_id, req)
            with _diag_lock:
                if target_id.lower() == "all" or target_id == "default":
                    _diag_requested = req
            print(
                f"[Diagnostics] requested a '{req}' dump for {target_id} from the next poll"
            )
            payload = json.dumps({"requested": req}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/log":
            body = self._read_body(65536)
            if body is None:
                return
            text = body.decode("utf-8", errors="replace")
            registry.append_log(client_id, text, resolve_cache_dir())
            for line in safe_log_lines(text):
                print(f"[Kindle Log] {line}")
            self._send_empty(200)
            return

        if parsed.path == "/diag":
            body = self._read_body(65536)
            if body is None:
                return
            text = body.decode("utf-8", errors="replace")
            batt_lvl, charging = parse_diag_battery(text)
            diag_client_ver, diag_fw_ver = parse_diag_versions(text)
            registry.save_diagnostics(client_id, text, resolve_cache_dir())
            client_mode = self.headers.get("X-Tracker-Mode", "").strip()
            client_version = (
                self.headers.get("X-Tracker-Client-Version", "").strip()
                or diag_client_ver
            )
            firmware_version = (
                self.headers.get("X-Tracker-Firmware", "").strip() or diag_fw_ver
            )
            registry.update_telemetry(
                client_id=client_id,
                remote_ip=remote_ip,
                battery=float(batt_lvl) if batt_lvl is not None else None,
                charging=charging,
                client_mode=client_mode,
                client_version=client_version,
                firmware_version=firmware_version,
            )
            with _diag_lock:
                _last_diagnostics["text"] = text
                _last_diagnostics["time"] = time.time()
                if batt_lvl is not None:
                    _last_diagnostics["battery"] = batt_lvl
                    _last_diagnostics["charging"] = charging
            extra = (
                f" battery={batt_lvl}%{'⚡' if charging else ''}"
                if batt_lvl is not None
                else ""
            )
            print(
                f"[Diagnostics] received {len(body)} bytes from {self.address_string()}{extra}"
            )
            self._send_empty(200)
            return

        if parsed.path == "/schedule":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            body = self._read_body(65536)
            if body is None:
                return
            data = {}
            if body and body.strip():
                try:
                    data = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as json_err:
                    ct = self.headers.get("Content-Type", "")
                    if "form-urlencoded" in ct and b"=" in body:
                        qs = urllib.parse.parse_qs(
                            body.decode("utf-8", errors="ignore")
                        )
                        data = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
                    else:
                        self._send_json(400, {"error": f"invalid JSON: {json_err}"})
                        return
            if not isinstance(data, dict):
                self._send_json(400, {"error": "expected a JSON object"})
                return

            action = str(data.get("action", "")).strip().lower()
            try:
                if action == "reset":
                    reset_schedule_config()
                elif action == "clear_override":
                    clear_schedule_override()
                elif (
                    action == "override"
                    or "force_phase" in data
                    or "force_fast_poll" in data
                ):
                    force_phase = data.get("force_phase")
                    force_fast_poll = data.get("force_fast_poll")
                    if isinstance(force_phase, str) and force_phase.lower() in (
                        "auto",
                        "none",
                        "clear",
                        "",
                    ):
                        force_phase = ""
                    if isinstance(force_fast_poll, str):
                        force_fast_poll = force_fast_poll.lower() in (
                            "1",
                            "true",
                            "yes",
                            "on",
                        )
                    set_schedule_override(
                        force_phase=force_phase,
                        force_fast_poll=force_fast_poll,
                    )
                elif (
                    action == "save"
                    or "config" in data
                    or "phases" in data
                    or "windows" in data
                ):
                    cfg_data = data.get("config", data)
                    if not isinstance(cfg_data, dict):
                        self._send_json(400, {"error": "config must be an object"})
                        return
                    save_schedule_config(cfg_data)
                else:
                    err_msg = (
                        f"unknown schedule action {action!r}"
                        if action
                        else "no valid schedule action or configuration provided"
                    )
                    self._send_json(400, {"error": err_msg})
                    return

                report = get_schedule_report()
                self._send_json(200, report)
            except state_machine.ConfigError as err:
                self._send_json(400, {"error": str(err)})
            except Exception as err:
                self._send_json(500, {"error": str(err)})
            return

        self._send_empty(404)

    def do_GET(self):
        global tracker_stopped, _diag_requested, _mode_requested, _device_action
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        client_id = self._extract_client_id(params)
        remote_ip = self._remote_ip()
        registry = get_device_registry()
        if client_id != "default" or (
            parsed.path in ["/dashboard.png", "/bus.png"]
            and self._is_client_device(params)
        ):
            registry.get_or_register(client_id, remote_ip)

        if parsed.path in ["/healthz", "/health"]:
            payload = json.dumps(
                {
                    "status": "ok",
                    "version": SERVER_VERSION,
                    "stopped": tracker_stopped,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/identity":
            nonce = self.headers.get(NONCE_HEADER, "").strip()
            payload = json.dumps(
                {
                    "service": "transit-tracker",
                    "version": SERVER_VERSION,
                }
            ).encode("utf-8")
            headers = [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
                ("Cache-Control", "no-cache"),
            ]
            if is_valid_nonce(nonce) and _identity is not None:
                resp_fmt = self._resolve_resp_format()
                headers.append(("X-Tracker-Resp-Format", resp_fmt))
                auth_hdrs = _identity.sign_response(
                    nonce=nonce,
                    path="/identity",
                    status=200,
                    body=payload,
                    headers={
                        "content-type": "application/json",
                        "content-length": str(len(payload)),
                    },
                    resp_format=resp_fmt,
                )
                headers.extend(auth_hdrs)
            self._send_tracker_headers(200, headers)
            self._write_body(payload)
            return

        if parsed.path == "/devices":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            device_dicts = [d.to_dict() for d in registry.list_devices()]
            payload = json.dumps({"devices": device_dicts}, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/schedule":
            # Read-only, but gated like every other control endpoint (§9).
            if not check_control_auth(self):
                self._send_forbidden()
                return
            payload = json.dumps(get_schedule_report(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/tracker-arm.manifest":
            info = get_valid_manifest()
            if info is None:
                self._send_empty(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(info.raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._write_body(info.raw)
            return

        if parsed.path == "/diag":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            target_id = (
                params.get("client_id", params.get("id", ["all"]))[0].strip() or "all"
            )
            req = params.get("request", params.get("level", params.get("mode", ["0"])))[
                0
            ].lower()
            if req in ("1", "true", "yes", "full", "quick"):
                diag_mode = (
                    "full" if req == "full" else ("quick" if req == "quick" else "1")
                )
                registry.set_diag(target_id, diag_mode)
                with _diag_lock:
                    if target_id.lower() == "all" or target_id == "default":
                        _diag_requested = diag_mode
                print(
                    f"[Diagnostics] requested a '{diag_mode}' dump for {target_id} from the next poll"
                )
            with _diag_lock:
                text = str(_last_diagnostics.get("text", "") or "")
                ts = float(_last_diagnostics.get("time", 0.0) or 0.0)
            dev_target = params.get("client_id", params.get("id", [""]))[0].strip()
            if dev_target and dev_target.lower() != "all":
                dev_rec = registry.get_device(dev_target)
                if dev_rec and dev_rec.last_diagnostics_text:
                    text = dev_rec.last_diagnostics_text
                    ts = dev_rec.last_diagnostics_time
                else:
                    text = ""
                    ts = 0.0
            if not text:
                self._send_empty(404)
                return
            safe_text = "\n".join(safe_log_lines(str(text), max_lines=1000))
            payload = f"# diagnostics captured {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))}\n{safe_text}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/mode":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            target_id = (
                params.get("client_id", params.get("id", ["all"]))[0].strip() or "all"
            )
            want = params.get("set", params.get("mode", [""]))[0].lower()
            if want in VALID_RUN_MODES:
                registry.set_mode(target_id, want)
                with _diag_lock:
                    if target_id.lower() == "all" or target_id == "default":
                        _mode_requested = want
                print(
                    f"[Mode] requested client mode '{want}' for {target_id} on the next poll"
                )
            if target_id and target_id.lower() != "all":
                rec = registry.get_device(target_id)
                pending = (
                    rec.target_mode
                    if rec
                    else (want if want in VALID_RUN_MODES else "")
                )
            else:
                with _diag_lock:
                    pending = _mode_requested
            payload = json.dumps(
                {"pending": pending, "valid": list(VALID_RUN_MODES)}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path == "/action":
            if not check_control_auth(self):
                self._send_forbidden()
                return
            target_id = (
                params.get("client_id", params.get("id", ["all"]))[0].strip() or "all"
            )
            do = params.get("do", params.get("action", [""]))[0].strip().lower()
            if do:
                registry.set_action(target_id, do)
                with _diag_lock:
                    if target_id.lower() == "all" or target_id == "default":
                        _device_action = do
                print(
                    f"[Action] queued device action '{do}' for {target_id} for the next poll"
                )
            if target_id and target_id.lower() != "all":
                rec = registry.get_device(target_id)
                pending = rec.pending_action if rec else do
            else:
                with _diag_lock:
                    pending = _device_action
            payload = json.dumps({"pending": pending}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)
            return

        if parsed.path in ["/stop", "/resume", "/start"]:
            # State-changing endpoints are POST-only
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path == "/tracker-arm":
            blob = load_binary()
            if blob is None:
                self._send_empty(404)
                return

            last_mod = time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(blob.mtime)
            )
            ims = self.headers.get("If-Modified-Since")
            if ims == last_mod:
                self._send_empty(304)
                return

            try:
                local_ip = self.connection.getsockname()[0]
            except Exception:
                local_ip = get_local_ip()

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(blob.size))
            self.send_header("Last-Modified", last_mod)
            self.send_header("X-Tracker-Version", SERVER_VERSION)
            self.send_header("X-Tracker-Server", f"http://{local_ip}:{PORT}")
            self.send_header("X-Tracker-SHA256", blob.sha256)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            if self.command == "GET":
                self._write_body(blob.data)
            return

        if parsed.path in ["/dashboard.png", "/bus.png"]:
            nonce = self.headers.get(NONCE_HEADER, "").strip()

            if tracker_stopped:
                resp_headers = [("Content-Length", "0")]
                if is_valid_nonce(nonce) and _identity is not None:
                    resp_fmt = self._resolve_resp_format()
                    resp_headers.append(("X-Tracker-Resp-Format", resp_fmt))
                    auth_hdrs = _identity.sign_response(
                        nonce=nonce,
                        path=parsed.path,
                        status=205,
                        body=b"",
                        headers={"content-length": "0"},
                        resp_format=resp_fmt,
                    )
                    resp_headers.extend(auth_hdrs)
                self.send_response(205)
                for k, v in resp_headers:
                    self.send_header(k, v)
                self.end_headers()
                return

            use_mock = "mock" in params
            kindle_mode = params.get("kindle", [None])[0]

            rot_str = params.get("rotate", ["90"])[0]
            try:
                rot_val = int(rot_str)
                if rot_val not in (0, 90, 180, 270):
                    raise ValueError()
            except ValueError:
                self._send_empty(400)
                return

            raw_view = (
                params.get("view", [self.headers.get("X-Tracker-View", "auto")])[0]
                .lower()
                .strip()
            )
            if raw_view in ("morning", "citi", "citibike", "am"):
                view_param = "morning"
            elif raw_view in ("evening", "bus", "pm", "afternoon", "night"):
                view_param = "evening"
            else:
                view_param = "auto"

            batt_param = (
                params.get("batt", [None])[0]
                or params.get("battery", [None])[0]
                or self.headers.get("X-Kindle-Battery")
            )
            charging_param = params.get("charging", [None])[0] or self.headers.get(
                "X-Kindle-Charging"
            )

            batt_level = None
            if batt_param and str(batt_param).strip().lstrip("-").isdigit():
                val = int(batt_param)
                if 0 <= val <= 100:
                    batt_level = val

            is_charging = str(charging_param).lower() in ["1", "true", "yes"]
            is_kindle = kindle_mode == "pw5" or "kindle" in params
            interactive_override = params.get("present", [""])[0] == "interactive"
            sched = get_schedule_state()
            presentation = (
                "interactive"
                if (interactive_override or not is_kindle)
                else sched.presentation
            )
            status_note = "" if presentation == "interactive" else sched.status_note
            # An "auto" view follows the active window's view when it sets one,
            # falling back to the time-of-day rule in resolve_view().
            render_view = (
                sched.view if (view_param == "auto" and sched.view) else view_param
            )
            render_w = 800
            render_h = 480

            if is_kindle:
                land_w, land_h = PW5_LANDSCAPE
                if "w" in params and "h" in params:
                    land_w, land_h = sanitize_kindle_panel(
                        params["w"][0], params["h"][0]
                    )

                logical_w = WIDTH
                logical_h = max(1, int(round(logical_w * land_h / land_w)))
                scale = native_render_scale(land_w, land_h, logical_w)
                render_w, render_h = logical_w, logical_h

                img = get_fresh_dashboard_image(
                    use_mock=use_mock,
                    batt_level=batt_level,
                    is_charging=is_charging,
                    view=render_view,
                    width=render_w,
                    height=render_h,
                    scale=scale,
                    presentation=presentation,
                    status_note=status_note,
                    interactive=interactive_override,
                )
                img = format_for_kindle(
                    img,
                    orientation="landscape",
                    rotation=rot_val,
                    target=(land_w, land_h),
                )
            else:
                img = get_fresh_dashboard_image(
                    use_mock=use_mock,
                    batt_level=batt_level,
                    is_charging=is_charging,
                    view=render_view,
                    width=render_w,
                    height=render_h,
                    presentation=presentation,
                    status_note=status_note,
                )

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            etag = f'"{hashlib.sha256(img_bytes).hexdigest()[:16]}"'
            brightness = sched.lighting.brightness
            warmth = sched.lighting.warmth
            poll_interval = sched.poll_interval

            client_mode = self.headers.get("X-Tracker-Mode", "")
            client_version = self.headers.get("X-Tracker-Client-Version", "")
            firmware_version = self.headers.get("X-Tracker-Firmware", "")

            if client_id != "default" or self._is_client_device(params):
                registry.update_telemetry(
                    client_id=client_id,
                    remote_ip=remote_ip,
                    battery=float(batt_level) if batt_level is not None else None,
                    charging=is_charging if charging_param is not None else None,
                    client_mode=client_mode,
                    client_version=client_version,
                    firmware_version=firmware_version,
                )

            diag_header = ""
            mode_header = ""
            action_header = ""
            if is_kindle or bool(self.headers.get("X-Tracker-Client-ID")):
                with _diag_lock:
                    action_header = registry.pop_action(client_id)
                    if client_id == "default":
                        if not action_header and _device_action:
                            action_header = _device_action
                        _device_action = ""

                    diag_header = registry.pop_diag(client_id)
                    if client_id == "default":
                        if not diag_header and _diag_requested:
                            diag_header = _diag_requested
                        _diag_requested = ""

                    rec = registry.get_device(client_id)
                    tgt_mode = (
                        rec.target_mode
                        if (rec and rec.target_mode)
                        else _mode_requested
                    )
                    if tgt_mode and client_mode != tgt_mode:
                        mode_header = tgt_mode

            try:
                local_ip = self.connection.getsockname()[0]
            except Exception:
                local_ip = get_local_ip()

            resp_fmt = self._resolve_resp_format()
            common_headers: list[tuple[str, str]] = [
                ("ETag", etag),
                ("X-Kindle-Poll-Interval", str(poll_interval)),
                ("X-Tracker-Presentation", presentation),
                ("X-Tracker-Server", f"http://{local_ip}:{PORT}"),
                ("X-Resolved-View", resolve_view(render_view)),
                ("X-Tracker-View", view_param),
                ("X-Tracker-Policy", get_policy_header(sched)),
                ("X-Tracker-Resp-Format", resp_fmt),
            ]

            valid_m = get_valid_manifest()
            if valid_m is not None:
                common_headers.append(("X-Tracker-Version", valid_m.version))
                common_headers.append(("X-Tracker-SHA256", valid_m.sha256))

            if diag_header:
                common_headers.append(("X-Tracker-Diag", diag_header))
            if mode_header:
                common_headers.append(("X-Tracker-Mode", mode_header))
            if action_header:
                common_headers.append(("X-Tracker-Action", action_header))

            # Not Modified check (304)
            if self.headers.get("If-None-Match") == etag:
                resp_304_headers = list(common_headers)
                resp_304_headers.append(("Content-Length", "0"))
                if is_valid_nonce(nonce) and _identity is not None:
                    hdr_map = {k.lower(): v for k, v in resp_304_headers}
                    auth_hdrs = _identity.sign_response(
                        nonce=nonce,
                        path=parsed.path,
                        status=304,
                        body=b"",
                        headers=hdr_map,
                        resp_format=resp_fmt,
                    )
                    resp_304_headers.extend(auth_hdrs)
                self._send_tracker_headers(304, resp_304_headers)
                return

            full_headers = (
                [
                    ("Content-Type", "image/png"),
                    ("Content-Length", str(len(img_bytes))),
                ]
                + common_headers
                + [
                    ("X-Kindle-Brightness", str(brightness)),
                    ("X-Kindle-Warmth", str(warmth)),
                    ("Cache-Control", "no-cache, no-store, must-revalidate"),
                ]
            )

            if is_valid_nonce(nonce) and _identity is not None:
                hdr_map = {k.lower(): v for k, v in full_headers}
                auth_hdrs = _identity.sign_response(
                    nonce=nonce,
                    path=parsed.path,
                    status=200,
                    body=img_bytes,
                    headers=hdr_map,
                    resp_format=resp_fmt,
                )
                full_headers.extend(auth_hdrs)

            self._send_tracker_headers(200, full_headers)
            self._write_body(img_bytes)

        elif parsed.path in ["/", "/index.html"]:
            current_view = html.escape(params.get("view", ["auto"])[0])
            status_badge = (
                '<span style="color:#ff6b6b;">STOPPED</span>'
                if tracker_stopped
                else '<span style="color:#51cf66;">ACTIVE</span>'
            )

            with _diag_lock:
                batt_val = _last_diagnostics.get("battery")
                batt_level = int(batt_val) if batt_val is not None else None
                batt_charging = _last_diagnostics.get("charging")
            if batt_level is not None:
                bolt = "⚡ " if batt_charging else ""
                batt_html = f" | Kindle: {bolt}<strong>{int(batt_level)}%</strong>"
            else:
                batt_html = " | Kindle: <em>no report</em>"

            panel_state = get_schedule_state()
            poll_interval = panel_state.poll_interval
            phase_until = (
                f" until {panel_state.until.strftime('%a %H:%M')}"
                if panel_state.until is not None
                else ""
            )
            phase_html = (
                f" | Phase: <strong>{html.escape(panel_state.phase)}</strong>"
                f"{html.escape(phase_until)}"
            )
            registered_devices = registry.list_devices()
            total_count = len(registered_devices)
            online_count = sum(
                1 for d in registered_devices if d.is_online(poll_interval)
            )
            offline_count = total_count - online_count

            now_ts = time.time()
            rows_html = []
            for d in registered_devices:
                is_on = d.is_online(poll_interval)
                status_color = "#2b8a3e" if is_on else "#c92a2a"
                status_label = "ONLINE" if is_on else "OFFLINE"
                dev_status = (
                    f'<span class="badge" style="background:{status_color};color:#fff;'
                    f'padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">'
                    f"{status_label}</span>"
                )
                safe_id = html.escape(d.client_id)
                safe_ip = html.escape(d.remote_ip) if d.remote_ip else "-"

                if d.battery is not None:
                    b_bolt = "⚡ " if d.charging else ""
                    batt_str = f"{b_bolt}{int(d.battery)}%"
                else:
                    batt_str = "-"
                safe_batt = html.escape(batt_str)

                if d.last_seen > 0:
                    dt_str = datetime.fromtimestamp(d.last_seen).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    elapsed = max(0, int(now_ts - d.last_seen))
                    if elapsed < 60:
                        rel = f"{elapsed}s ago"
                    elif elapsed < 3600:
                        rel = f"{elapsed // 60}m ago"
                    else:
                        rel = f"{elapsed // 3600}h ago"
                    seen_str = f"{dt_str} ({rel})"
                else:
                    seen_str = "never"
                safe_seen = html.escape(seen_str)

                safe_client_ver = (
                    html.escape(d.client_version) if d.client_version else "-"
                )
                safe_fw_ver = (
                    html.escape(d.firmware_version) if d.firmware_version else "-"
                )
                ver_display = f"{safe_client_ver} / {safe_fw_ver}"

                safe_mode = html.escape(d.client_mode) if d.client_mode else "-"
                if d.target_mode:
                    safe_mode += f" (target: {html.escape(d.target_mode)})"

                rows_html.append(f"""<tr>
                    <td>{dev_status}</td>
                    <td><strong>{safe_id}</strong></td>
                    <td>{safe_ip}</td>
                    <td>{safe_batt}</td>
                    <td>{safe_seen}</td>
                    <td>{ver_display}</td>
                    <td>{safe_mode}</td>
                    <td>
                        <div class="ctrl-group">
                            <select class="device-action-select" aria-label="Action for {safe_id}">
                                <option value="restart">restart</option>
                                <option value="reboot">reboot</option>
                                <option value="update">update</option>
                                <option value="clear_backup">clear_backup</option>
                            </select>
                            <button class="btn-small btn-device-action" data-client-id="{safe_id}">Action</button>
                        </div>
                        <div class="ctrl-group">
                            <select class="device-diag-select" aria-label="Diagnostics for {safe_id}">
                                <option value="1">quick</option>
                                <option value="full">full</option>
                            </select>
                            <button class="btn-small btn-device-diag" data-client-id="{safe_id}">Diag</button>
                            <button class="btn-small btn-device-view-diag" data-client-id="{safe_id}">View Diag</button>
                        </div>
                        <div class="ctrl-group">
                            <select class="device-mode-select" aria-label="Run Mode for {safe_id}">
                                <option value="resident">resident</option>
                                <option value="oneshot">oneshot</option>
                                <option value="sleep">sleep</option>
                                <option value="sleep-suspend">sleep-suspend</option>
                            </select>
                            <button class="btn-small btn-device-mode" data-client-id="{safe_id}">Mode</button>
                        </div>
                    </td>
                </tr>""")

            if not rows_html:
                fleet_table_body = '<tr><td colspan="8" style="text-align:center;color:#888;padding:16px;">No registered devices. Kindle devices will appear automatically upon first poll.</td></tr>'
            else:
                fleet_table_body = "\n".join(rows_html)

            sched_report = get_schedule_report()
            sched_cfg = sched_report.get("config", {})
            sched_overrides = sched_report.get("overrides", {})
            active_force_phase = sched_overrides.get("force_phase") or ""
            active_force_fast = bool(sched_overrides.get("force_fast_poll"))

            all_phases = sorted(sched_cfg.get("phases", {}).keys())
            phase_options_html = ['<option value="">Auto (Follow Schedule)</option>']
            for pname in all_phases:
                sel = ' selected="selected"' if active_force_phase == pname else ""
                phase_options_html.append(
                    f'<option value="{html.escape(pname)}"{sel}>Force {html.escape(pname)}</option>'
                )
            phase_select_html = "\n".join(phase_options_html)

            windows_list = sched_cfg.get("windows", [])
            window_rows = []
            for w in windows_list:
                w_phase = html.escape(str(w.get("phase", "")))
                days_list = [d.capitalize() for d in w.get("days", [])]
                w_days = html.escape(", ".join(days_list) if days_list else "All Days")
                w_time = f"{html.escape(str(w.get('start', '')))} – {html.escape(str(w.get('end', '')))}"
                w_view = html.escape(str(w.get("view", "-")))
                window_rows.append(
                    f"<tr><td><strong>{w_phase}</strong></td><td>{w_days}</td><td>{w_time}</td><td>{w_view}</td></tr>"
                )
            if not window_rows:
                sched_windows_tbody = '<tr><td colspan="4" style="text-align:center;color:#888;padding:12px;">No windows defined (default phase active at all times).</td></tr>'
            else:
                sched_windows_tbody = "\n".join(window_rows)

            trans_items = []
            for t in sched_report.get("transitions", []):
                t_time = html.escape(str(t.get("at", "")))
                t_phase = html.escape(str(t.get("phase", "")))
                trans_items.append(
                    f'<span class="badge" style="background:#333;color:#bbb;padding:2px 8px;border-radius:4px;font-size:11px;border:1px solid #555;">{t_time} &rarr; <strong style="color:#fff;">{t_phase}</strong></span>'
                )
            transitions_html = (
                " ".join(trans_items)
                if trans_items
                else "<em>No transitions scheduled in next 24h</em>"
            )

            sched_json_pretty = html.escape(json.dumps(sched_cfg, indent=2))
            sched_source_safe = html.escape(str(sched_report.get("source", "defaults")))
            override_badge = (
                f'<span class="badge" style="background:#d9480f;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">OVERRIDE: {html.escape(active_force_phase)}</span>'
                if active_force_phase
                else '<span class="badge" style="background:#2b8a3e;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;">AUTO (SCHEDULED)</span>'
            )

            script_nonce = secrets.token_hex(16)

            csp = (
                "default-src 'self'; "
                "img-src 'self' data:; "
                f"script-src 'self' 'nonce-{script_nonce}'; "
                "style-src 'unsafe-inline'; "
                "frame-ancestors 'none'; "
                "object-src 'none'; "
                "base-uri 'none'"
            )

            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>NJ Transit 126 &amp; Citi Bike Tracker</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body {{
            background: #222;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            min-height: 100vh;
            margin: 0;
            padding: 20px 0;
            box-sizing: border-box;
            font-family: -apple-system, sans-serif;
            color: #ddd;
        }}
        img {{
            max-width: 95vw;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
            border-radius: 8px;
        }}
        .status {{
            margin-top: 12px;
            font-size: 16px;
        }}
        .links {{
            margin-top: 10px;
            font-size: 14px;
        }}
        .controls {{
            margin-top: 14px;
            display: flex;
            gap: 8px;
            align-items: center;
        }}
        button {{
            background: #444;
            color: #fff;
            border: 1px solid #666;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
        }}
        button:hover {{ background: #555; }}
        input[type="password"] {{
            background: #333;
            color: #fff;
            border: 1px solid #555;
            padding: 6px;
            border-radius: 4px;
        }}
        a {{ color: #4da6ff; text-decoration: none; margin: 0 8px; }}
        a:hover {{ text-decoration: underline; }}
        .fleet-section {{
            width: 95vw;
            max-width: 1100px;
            margin-top: 24px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 16px;
            box-sizing: border-box;
        }}
        .fleet-summary {{
            display: flex;
            gap: 16px;
            align-items: center;
            flex-wrap: wrap;
            font-size: 15px;
            padding-bottom: 12px;
            border-bottom: 1px solid #444;
        }}
        .broadcast-toolbar {{
            display: flex;
            gap: 12px;
            align-items: center;
            flex-wrap: wrap;
            margin-top: 12px;
            padding: 10px;
            background: #222;
            border-radius: 6px;
            font-size: 13px;
        }}
        .fleet-table-container {{
            overflow-x: auto;
            margin-top: 14px;
        }}
        table.fleet-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }}
        table.fleet-table th, table.fleet-table td {{
            padding: 8px 10px;
            border-bottom: 1px solid #3a3a3a;
            white-space: nowrap;
        }}
        table.fleet-table th {{
            background: #333;
            color: #bbb;
            font-weight: 600;
        }}
        table.fleet-table tr:hover {{
            background: rgba(255, 255, 255, 0.04);
        }}
        .ctrl-group {{
            display: inline-flex;
            gap: 4px;
            align-items: center;
            margin-right: 6px;
        }}
        select {{
            background: #333;
            color: #fff;
            border: 1px solid #555;
            padding: 4px 6px;
            border-radius: 4px;
            font-size: 12px;
        }}
        button.btn-small {{
            padding: 4px 8px;
            font-size: 12px;
        }}
        .schedule-section {{
            width: 95vw;
            max-width: 1100px;
            margin-top: 24px;
            background: #2a2a2a;
            border: 1px solid #444;
            border-radius: 8px;
            padding: 16px;
            box-sizing: border-box;
        }}
        .sched-summary {{
            display: flex;
            gap: 16px;
            align-items: center;
            flex-wrap: wrap;
            font-size: 15px;
            padding-bottom: 12px;
            border-bottom: 1px solid #444;
        }}
        .sched-toolbar {{
            display: flex;
            gap: 12px;
            align-items: center;
            flex-wrap: wrap;
            margin-top: 12px;
            padding: 10px;
            background: #222;
            border-radius: 6px;
            font-size: 13px;
        }}
        .sched-table-container {{
            overflow-x: auto;
            margin-top: 14px;
        }}
        table.sched-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }}
        table.sched-table th, table.sched-table td {{
            padding: 8px 10px;
            border-bottom: 1px solid #3a3a3a;
            white-space: nowrap;
        }}
        table.sched-table th {{
            background: #333;
            color: #bbb;
            font-weight: 600;
        }}
        table.sched-table tr:hover {{
            background: rgba(255, 255, 255, 0.04);
        }}
        .sched-editor-container {{
            margin-top: 14px;
            background: #1e1e1e;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 12px;
        }}
        .sched-editor-container textarea {{
            width: 100%;
            box-sizing: border-box;
            background: #181818;
            color: #a8d1ff;
            font-family: monospace;
            font-size: 12px;
            border: 1px solid #3a3a3a;
            border-radius: 4px;
            padding: 8px;
            resize: vertical;
        }}
    </style>
</head>

<body>
    <img src="/dashboard.png?view={current_view}&amp;t={int(time.time())}" alt="Transit Dashboard" />
    <div class="status">Status: {status_badge}{batt_html}{phase_html}</div>
    <div class="controls">
        <input type="password" id="tokenInput" placeholder="Control Token" />
        <button id="saveTokenBtn">Save Token</button>
        <button id="toggleBtn">{"Resume Tracker" if tracker_stopped else "Stop Kindle Tracker"}</button>
    </div>
    <div class="links">
        <a href="/diag" id="diagLink">View Diagnostics</a>
    </div>
    <div class="links">
        <strong>View Mode:</strong>
        <a href="/?view=auto">Auto (AM Citi / PM Bus)</a> |
        <a href="/?view=morning">Morning (Citi Bike Hero)</a> |
        <a href="/?view=evening">Evening (Bus Hero)</a>
    </div>
    <div class="links">
        <a href="/dashboard.png?view={current_view}" target="_blank">Standard (800x480)</a> |
        <a href="/dashboard.png?kindle=pw5&amp;rotate=90&amp;view={current_view}" target="_blank">Kindle PW5 (Rotated 90°)</a> |
        <a href="/dashboard.png?mock=1&amp;view={current_view}" target="_blank">Mock Preview</a>
    </div>

    <div class="schedule-section">
        <div class="sched-summary">
            <strong>State Machine Schedule:</strong>
            <span>Active Phase: <strong>{html.escape(panel_state.phase)}</strong></span>
            <span>Cadence: <strong>{poll_interval}s</strong></span>
            <span>Source: <code>{sched_source_safe}</code></span>
            <span>Status: {override_badge}</span>
        </div>
        <div class="sched-toolbar">
            <span style="font-weight:600;">Quick Override:</span>
            <div class="ctrl-group">
                <select id="schedPhaseSelect" aria-label="Schedule Phase Override">
                    {phase_select_html}
                </select>
                <label style="display:flex;align-items:center;gap:4px;font-size:12px;margin:0 4px;cursor:pointer;">
                    <input type="checkbox" id="schedFastPollCheck" {"checked" if active_force_fast else ""}> Fast Poll (60s)
                </label>
                <button class="btn-small" id="applySchedOverrideBtn">Apply Override</button>
                <button class="btn-small" id="clearSchedOverrideBtn">Clear Override</button>
            </div>
        </div>
        <div class="sched-table-container">
            <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:#bbb;">Active Windows:</div>
            <table class="sched-table">
                <thead>
                    <tr>
                        <th>Phase</th>
                        <th>Days</th>
                        <th>Time Window</th>
                        <th>View Mode</th>
                    </tr>
                </thead>
                <tbody>
                    {sched_windows_tbody}
                </tbody>
            </table>
        </div>
        <div style="margin-top:10px;font-size:12px;color:#aaa;">
            <strong>Upcoming Transitions (24h):</strong> {transitions_html}
        </div>
        <div class="sched-editor-container">
            <details id="schedEditorDetails">
                <summary style="cursor:pointer;font-weight:600;font-size:13px;color:#4da6ff;outline:none;">
                    &#9998; Edit Schedule JSON Configuration
                </summary>
                <div style="margin-top:10px;">
                    <textarea id="schedConfigText" rows="16" spellcheck="false">{sched_json_pretty}</textarea>
                    <div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;">
                        <button class="btn-small" id="saveSchedBtn" style="background:#2b8a3e;border-color:#2b8a3e;font-weight:600;">Save &amp; Apply Schedule</button>
                        <button class="btn-small" id="resetSchedBtn">Reset to Defaults</button>
                        <button class="btn-small" id="reloadSchedBtn">Reload</button>
                    </div>
                    <div id="schedStatusMsg" style="display:none;margin-top:8px;padding:8px 12px;border-radius:4px;font-size:12px;"></div>
                </div>
            </details>
        </div>
    </div>

    <div class="fleet-section">
        <div class="fleet-summary">
            <strong>Fleet Overview:</strong>
            <span id="fleetTotal">Total: <strong>{total_count}</strong></span>
            <span id="fleetOnline" style="color:#51cf66;">Online: <strong>{online_count}</strong></span>
            <span id="fleetOffline" style="color:#adb5bd;">Offline: <strong>{offline_count}</strong></span>
        </div>
        <div class="broadcast-toolbar">
            <span style="font-weight:600;">Broadcast Controls:</span>
            <div class="ctrl-group">
                <select id="broadcastActionSelect" aria-label="Broadcast Action">
                    <option value="restart">restart</option>
                    <option value="reboot">reboot</option>
                    <option value="update">update</option>
                    <option value="clear_backup">clear_backup</option>
                </select>
                <button class="btn-small" id="broadcastActionBtn">Broadcast Action</button>
            </div>
            <div class="ctrl-group">
                <select id="broadcastDiagSelect" aria-label="Broadcast Diagnostics">
                    <option value="1">quick</option>
                    <option value="full">full</option>
                </select>
                <button class="btn-small" id="broadcastDiagBtn">Broadcast Diag</button>
            </div>
            <div class="ctrl-group">
                <select id="broadcastModeSelect" aria-label="Broadcast Run Mode">
                    <option value="resident">resident</option>
                    <option value="oneshot">oneshot</option>
                    <option value="sleep">sleep</option>
                    <option value="sleep-suspend">sleep-suspend</option>
                </select>
                <button class="btn-small" id="broadcastModeBtn">Broadcast Mode</button>
            </div>
        </div>
        <div class="fleet-table-container">
            <table class="fleet-table">
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Device ID</th>
                        <th>IP Address</th>
                        <th>Battery</th>
                        <th>Last Seen</th>
                        <th>Client / FW</th>
                        <th>Mode</th>
                        <th>Per-Device Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {fleet_table_body}
                </tbody>
            </table>
        </div>
    </div>

    <script nonce="{script_nonce}">
        const tokenInput = document.getElementById("tokenInput");
        const savedToken = localStorage.getItem("tracker_token") || "";
        tokenInput.value = savedToken;
        document.getElementById("saveTokenBtn").onclick = () => {{
            localStorage.setItem("tracker_token", tokenInput.value.trim());
            alert("Token saved in browser.");
        }};
        document.getElementById("toggleBtn").onclick = async () => {{
            const tok = tokenInput.value.trim();
            const action = "{"resume" if tracker_stopped else "stop"}";
            const res = await fetch("/" + action, {{
                method: "POST",
                headers: {{ "X-Tracker-Token": tok }}
            }});
            if (res.ok) {{
                window.location.reload();
            }} else {{
                alert("Action failed: HTTP " + res.status);
            }}
        }};
        document.getElementById("diagLink").onclick = async (e) => {{
            e.preventDefault();
            const tok = tokenInput.value.trim();
            const res = await fetch("/diag", {{
                headers: {{ "X-Tracker-Token": tok }}
            }});
            if (res.ok) {{
                const text = await res.text();
                const w = window.open();
                if (w) {{
                    w.document.open();
                    w.document.write("<pre>" + text.replace(/&/g,"&amp;").replace(/</g,"&lt;") + "</pre>");
                    w.document.close();
                }} else {{
                    alert("Pop-up blocked. Open /diag manually.");
                }}
            }} else {{
                alert("Diagnostics access denied: HTTP " + res.status);
            }}
        }};

        function getAuthToken() {{
            return tokenInput.value.trim();
        }}

        async function apiPost(endpoint, data) {{
            const tok = getAuthToken();
            try {{
                const res = await fetch(endpoint, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-Tracker-Token": tok
                    }},
                    body: JSON.stringify(data)
                }});
                if (res.ok) {{
                    alert("Command sent successfully.");
                    window.location.reload();
                }} else {{
                    alert("Command failed: HTTP " + res.status);
                }}
            }} catch (err) {{
                alert("Network error: " + err.message);
            }}
        }}

        document.querySelectorAll(".btn-device-action").forEach(btn => {{
            btn.addEventListener("click", () => {{
                const cid = btn.getAttribute("data-client-id");
                const row = btn.closest("tr") || btn.parentElement;
                const sel = row ? row.querySelector(".device-action-select") : null;
                if (!sel || !sel.value) {{
                    alert("Please select an action.");
                    return;
                }}
                apiPost("/action", {{ action: sel.value, client_id: cid }});
            }});
        }});

        document.querySelectorAll(".btn-device-diag").forEach(btn => {{
            btn.addEventListener("click", () => {{
                const cid = btn.getAttribute("data-client-id");
                const row = btn.closest("tr") || btn.parentElement;
                const sel = row ? row.querySelector(".device-diag-select") : null;
                const lvl = sel ? sel.value : "1";
                apiPost("/diag/request", {{ level: lvl, client_id: cid }});
            }});
        }});

        document.querySelectorAll(".btn-device-mode").forEach(btn => {{
            btn.addEventListener("click", () => {{
                const cid = btn.getAttribute("data-client-id");
                const row = btn.closest("tr") || btn.parentElement;
                const sel = row ? row.querySelector(".device-mode-select") : null;
                if (!sel || !sel.value) {{
                    alert("Please select a mode.");
                    return;
                }}
                apiPost("/mode", {{ mode: sel.value, client_id: cid }});
            }});
        }});

        document.querySelectorAll(".btn-device-view-diag").forEach(btn => {{
            btn.addEventListener("click", async () => {{
                const cid = btn.getAttribute("data-client-id");
                const tok = getAuthToken();
                const url = "/diag?client_id=" + encodeURIComponent(cid);
                try {{
                    const res = await fetch(url, {{
                        headers: {{ "X-Tracker-Token": tok }}
                    }});
                    if (res.ok) {{
                        const text = await res.text();
                        const w = window.open();
                        if (w) {{
                            w.document.open();
                            w.document.write("<pre>" + text.replace(/&/g,"&amp;").replace(/</g,"&lt;") + "</pre>");
                            w.document.close();
                        }} else {{
                            alert("Pop-up blocked. Open " + url + " manually.");
                        }}
                    }} else if (res.status === 404) {{
                        alert("No diagnostics available for " + cid);
                    }} else {{
                        alert("Diagnostics access denied: HTTP " + res.status);
                    }}
                }} catch (err) {{
                    alert("Error loading diagnostics: " + err.message);
                }}
            }});
        }});

        const bActionBtn = document.getElementById("broadcastActionBtn");
        if (bActionBtn) {{
            bActionBtn.addEventListener("click", () => {{
                const sel = document.getElementById("broadcastActionSelect");
                if (sel && sel.value) {{
                    apiPost("/action", {{ action: sel.value, client_id: "all" }});
                }}
            }});
        }}

        const bDiagBtn = document.getElementById("broadcastDiagBtn");
        if (bDiagBtn) {{
            bDiagBtn.addEventListener("click", () => {{
                const sel = document.getElementById("broadcastDiagSelect");
                const lvl = sel ? sel.value : "1";
                apiPost("/diag/request", {{ level: lvl, client_id: "all" }});
            }});
        }}

        const bModeBtn = document.getElementById("broadcastModeBtn");
        if (bModeBtn) {{
            bModeBtn.addEventListener("click", () => {{
                const sel = document.getElementById("broadcastModeSelect");
                if (sel && sel.value) {{
                    apiPost("/mode", {{ mode: sel.value, client_id: "all" }});
                }}
            }});
        }}

        const schedPhaseSelect = document.getElementById("schedPhaseSelect");
        const schedFastPollCheck = document.getElementById("schedFastPollCheck");
        const schedStatusMsg = document.getElementById("schedStatusMsg");
        const schedConfigText = document.getElementById("schedConfigText");

        function showSchedMsg(text, isError) {{
            if (!schedStatusMsg) return;
            schedStatusMsg.style.display = "block";
            schedStatusMsg.style.background = isError ? "#5c1d1d" : "#1d5c2b";
            schedStatusMsg.style.color = "#fff";
            schedStatusMsg.textContent = text;
        }}

        const applyOverrideBtn = document.getElementById("applySchedOverrideBtn");
        if (applyOverrideBtn) {{
            applyOverrideBtn.addEventListener("click", async () => {{
                const phase = schedPhaseSelect ? schedPhaseSelect.value : "";
                const fast = schedFastPollCheck ? schedFastPollCheck.checked : false;
                const tok = getAuthToken();
                try {{
                    const res = await fetch("/schedule", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/json",
                            "X-Tracker-Token": tok
                        }},
                        body: JSON.stringify({{
                            action: "override",
                            force_phase: phase,
                            force_fast_poll: fast
                        }})
                    }});
                    const data = await res.json().catch(() => ({{}}));
                    if (res.ok) {{
                        showSchedMsg("Schedule override applied successfully.", false);
                        setTimeout(() => window.location.reload(), 600);
                    }} else {{
                        showSchedMsg("Override failed: " + (data.error || ("HTTP " + res.status)), true);
                    }}
                }} catch (err) {{
                    showSchedMsg("Network error: " + err.message, true);
                }}
            }});
        }}

        const clearOverrideBtn = document.getElementById("clearSchedOverrideBtn");
        if (clearOverrideBtn) {{
            clearOverrideBtn.addEventListener("click", async () => {{
                const tok = getAuthToken();
                try {{
                    const res = await fetch("/schedule", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/json",
                            "X-Tracker-Token": tok
                        }},
                        body: JSON.stringify({{ action: "clear_override" }})
                    }});
                    const data = await res.json().catch(() => ({{}}));
                    if (res.ok) {{
                        showSchedMsg("Schedule overrides cleared.", false);
                        setTimeout(() => window.location.reload(), 600);
                    }} else {{
                        showSchedMsg("Clear failed: " + (data.error || ("HTTP " + res.status)), true);
                    }}
                }} catch (err) {{
                    showSchedMsg("Network error: " + err.message, true);
                }}
            }});
        }}

        const saveSchedBtn = document.getElementById("saveSchedBtn");
        if (saveSchedBtn) {{
            saveSchedBtn.addEventListener("click", async () => {{
                const raw = schedConfigText ? schedConfigText.value : "";
                let parsedConfig;
                try {{
                    parsedConfig = JSON.parse(raw);
                }} catch (err) {{
                    showSchedMsg("JSON parse error: " + err.message, true);
                    return;
                }}
                const tok = getAuthToken();
                try {{
                    const res = await fetch("/schedule", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/json",
                            "X-Tracker-Token": tok
                        }},
                        body: JSON.stringify({{ action: "save", config: parsedConfig }})
                    }});
                    const data = await res.json().catch(() => ({{}}));
                    if (res.ok) {{
                        showSchedMsg("Schedule saved & applied successfully.", false);
                        setTimeout(() => window.location.reload(), 800);
                    }} else {{
                        showSchedMsg("Validation / Save error: " + (data.error || ("HTTP " + res.status)), true);
                    }}
                }} catch (err) {{
                    showSchedMsg("Network error: " + err.message, true);
                }}
            }});
        }}

        const resetSchedBtn = document.getElementById("resetSchedBtn");
        if (resetSchedBtn) {{
            resetSchedBtn.addEventListener("click", async () => {{
                if (!confirm("Are you sure you want to reset schedule to defaults?")) return;
                const tok = getAuthToken();
                try {{
                    const res = await fetch("/schedule", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/json",
                            "X-Tracker-Token": tok
                        }},
                        body: JSON.stringify({{ action: "reset" }})
                    }});
                    const data = await res.json().catch(() => ({{}}));
                    if (res.ok) {{
                        showSchedMsg("Schedule reset to defaults.", false);
                        setTimeout(() => window.location.reload(), 600);
                    }} else {{
                        showSchedMsg("Reset failed: " + (data.error || ("HTTP " + res.status)), true);
                    }}
                }} catch (err) {{
                    showSchedMsg("Network error: " + err.message, true);
                }}
            }});
        }}

        const reloadSchedBtn = document.getElementById("reloadSchedBtn");
        if (reloadSchedBtn) {{
            reloadSchedBtn.addEventListener("click", async () => {{
                const tok = getAuthToken();
                try {{
                    const res = await fetch("/schedule", {{
                        headers: {{ "X-Tracker-Token": tok }}
                    }});
                    if (res.ok) {{
                        const data = await res.json();
                        if (schedConfigText && data.config) {{
                            schedConfigText.value = JSON.stringify(data.config, null, 2);
                            showSchedMsg("Schedule configuration reloaded.", false);
                        }}
                    }} else {{
                        showSchedMsg("Reload failed: HTTP " + res.status, true);
                    }}
                }} catch (err) {{
                    showSchedMsg("Error: " + err.message, true);
                }}
            }});
        }}
    </script>

</body>
</html>"""
            payload = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_body(payload)

        else:
            self._send_empty(404)

    def log_message(self, format, *args):
        print(f"[Server] {self.address_string()} - {args[0]}")


TransitTrackerHandler = DashboardHandler


if __name__ == "__main__":
    server_address = ("", PORT)
    httpd = ThreadingHTTPServer(server_address, DashboardHandler)
    local_ip = get_local_ip()
    print("==================================================")
    print(f"  Hoboken Transit Tracker Server v{SERVER_VERSION} on Port {PORT}")
    print(f"  Local View:      http://localhost:{PORT}")
    print(f"  Kindle Endpoint: http://{local_ip}:{PORT}/dashboard.png?kindle=pw5")
    print(
        f"  Auto-Discovery:  UDP Port {DISCOVERY_PORT} & mDNS (_transittracker._tcp.local)"
    )
    print(f"  Identity status: {_identity_status}")
    print(f"  Control token:   {CONTROL_TOKEN}")
    print(f"  {describe_base_url()}")
    print("==================================================")

    start_discovery_responder(http_port=PORT, version=SERVER_VERSION)
    zc, mdns_info = start_mdns_advertiser(http_port=PORT, version=SERVER_VERSION)

    threading.Thread(target=warm_up_gtfs, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        if zc and mdns_info:
            try:
                zc.unregister_service(mdns_info)
                zc.close()
            except Exception:
                pass
        httpd.server_close()

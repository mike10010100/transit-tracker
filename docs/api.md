# HTTP API & Protocol Reference

This document provides the formal specification for all endpoints exposed by the Transit Tracker Server (`server/server.py`).

---

## 1. Client Telemetry & Rendering Endpoints

### 1.1 `GET /dashboard.png`
Renders and delivers the transit dashboard PNG image tailored to the requesting device.

#### Query Parameters
| Parameter | Type | Default | Description |
|---|---|---|---|
| `kindle` | `string` | `"standard"` | Target profile (`pw5` or `standard`). `pw5` sets landscape native resolution (1648×1236). |
| `view` | `string` | `"auto"` | View selection: `auto`, `morning` (Citi Bike), or `evening` (NJ Transit Bus). |
| `rotate` | `int` | `0` | Image rotation in degrees: `0`, `90`, `180`, `270`. |
| `scale` | `float` | `1.0` | High-DPI render scaling factor. |
| `mock` | `int` | `0` | If `1`, renders synthetic mock departures for testing. |
| `present`| `string`| `""` | Client presentation override (`interactive`, `idle`, `dormant`). |

#### Request Headers
| Header | Example | Description |
|---|---|---|
| `If-None-Match` | `"d41d8cd98f00b204"` | ETag from previous cycle for conditional 304 caching. |
| `X-Tracker-Client-ID` | `G001LG0123456789` | Unique hardware serial or persistent UUID. |
| `X-Kindle-Battery` | `87` | Current battery percentage reported by Kindle `lipc`. |
| `X-Kindle-Charging` | `1` | Charging status (`1` = charging, `0` = battery). |
| `X-Tracker-Mode` | `sleep` | Current client execution mode (`resident`, `sleep`, `oneshot`). |
| `X-Tracker-View` | `morning` | Explicit client view mode requested by button tap. |
| `X-Tracker-Nonce` | `a1b2c3d4e5f6...` | 16-byte cryptographic challenge for response signing. |

#### Response Headers
| Header | Description |
|---|---|
| `ETag` | SHA-256 digest prefix of the PNG image bytes. |
| `X-Kindle-Poll-Interval` | Recommended interval in seconds until next poll (e.g. `60`, `600`, `3600`). |
| `X-Kindle-Brightness` | Recommended frontlight brightness (`0` to `24`). |
| `X-Kindle-Warmth` | Recommended frontlight color warmth (`0` to `24`). |
| `X-Tracker-Presentation`| Presentation state: `interactive`, `idle`, or `dormant`. |
| `X-Tracker-Policy` | Schedule and interaction policy as `;`-separated `key=value` pairs: `v` (policy version), `phase`, `until` (epoch seconds of next phase change), `suspend` (`0`/`1`), `session`, `fast`, `hold` (seconds), `sl` (session lighting `brightness,warmth`). |
| `X-Tracker-Mode` | Run mode directive targeted at the client. |
| `X-Tracker-Action` | Queued action popped for this client (`restart`, `update`, etc.). |
| `X-Tracker-Diag` | Diagnostics dump request popped for this client (`quick` or `full`). |
| `X-Tracker-Auth` | Ed25519 signature over response headers, body hash, and client nonce. |
| `X-Tracker-Cert` | Escaped JSON server identity certificate. |

#### Status Codes
- `200 OK`: New dashboard image rendered and returned as `image/png`.
- `304 Not Modified`: Image is byte-identical to `If-None-Match`; client reuses cached screen buffer without refreshing the e-ink display.

---

### 1.2 `POST /diag`
Receives diagnostic dumps uploaded by a Kindle client.

#### Request Headers
- `X-Tracker-Client-ID: <string>` (required for per-device routing).
- `Content-Type: text/plain; charset=utf-8`

#### Request Body
Plaintext system diagnostics including `dmesg`, `lipc` power/battery status, memory usage (`/proc/meminfo`), and process listings.

#### Response
- `200 OK`: Diagnostics accepted and written to `<cache_dir>/devices/<client_id>/diagnostics.txt`.

---

### 1.3 `POST /log`
Receives streaming runtime logs from the Kindle client.

#### Request Headers
- `X-Tracker-Client-ID: <string>`
- `Content-Type: text/plain; charset=utf-8`

#### Request Body
Newline-delimited UTF-8 client log messages.

---

## 2. Discovery & System Information

### 2.1 `GET /healthz`
Health check endpoint used by Docker health checks and LAN discovery sweeps.

#### Response (`200 OK`, `application/json`)
```json
{
  "status": "ok",
  "version": "1.38.0",
  "time": 1791653581,
  "service": "transit-tracker"
}
```

### 2.2 `GET /identity`
Returns server cryptographic public key and certificate for LAN identity verification.

#### Response (`200 OK`, `application/json`)
```json
{
  "service": "transit-tracker",
  "version": "1.38.0",
  "public_key": "<server-public-key>",
  "certificate": {
    "public_key": "<server-public-key>",
    "issuer": "transit-tracker-release",
    "issued_at": 1791653581,
    "signature": "<root-signature>"
  }
}
```

---

## 3. OTA Firmware Distribution

### 3.1 `GET /tracker-arm`
Delivers the cross-compiled static ARMv7 Go binary for Kindle Paperwhite.
- Supports `HEAD` requests for checking modification time and length.
- Verifies conditional `If-Modified-Since` requests (returns `304 Not Modified`).

### 3.2 `GET /tracker-arm.manifest` (or `.manifest.json`)
Returns the signed release manifest.
```json
{
  "version": "1.38.0",
  "sha256": "0109ef3df247a368977e1d8fcb307788578ae3b7a7c2edd1b1642fc6f9fa5429",
  "size": 6750370,
  "signature": "<base64-ed25519-signature>"
}
```

---

## 4. Schedule State Machine Endpoints

Both schedule endpoints require authentication via `X-Tracker-Token`.

### 4.1 `GET /schedule`
Returns the active schedule state machine configuration, current phase status, upcoming 24h transitions, and active overrides.

#### Request
```bash
curl -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" http://localhost:8000/schedule
```

#### Response (`200 OK`, `application/json`)
```json
{
  "source": "/app/config/schedule.json",
  "path": "/app/config/schedule.json",
  "error": null,
  "now": "2026-10-10T08:15:00",
  "current": {
    "phase": "peak",
    "presentation": "interactive",
    "status_note": "",
    "poll_interval": 60,
    "lighting": {"brightness": 8, "warmth": 12},
    "realtime": true,
    "suspend": false,
    "view": "morning",
    "until": "2026-10-10T09:30:00"
  },
  "transitions": [
    {"at": "2026-10-10T09:30:00", "phase": "offpeak"},
    {"at": "2026-10-10T16:30:00", "phase": "peak"},
    {"at": "2026-10-10T19:00:00", "phase": "offpeak"},
    {"at": "2026-10-10T22:00:00", "phase": "overnight"}
  ],
  "config": {
    "version": 1,
    "default_phase": "offpeak",
    "phases": { ... },
    "windows": [ ... ]
  },
  "overrides": {
    "force_phase": null,
    "force_fast_poll": false
  }
}
```

### 4.2 `POST /schedule`
Mutates schedule state machine configuration or sets runtime overrides.

#### Actions

- **Set Phase / Fast-Poll Override**:
  ```bash
  curl -X POST -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"action": "override", "force_phase": "peak", "force_fast_poll": true}' \
    http://localhost:8000/schedule
  ```
  *(Pass `force_phase: ""` or `"auto"` to clear the phase override.)*

- **Clear All Overrides**:
  ```bash
  curl -X POST -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"action": "clear_override"}' \
    http://localhost:8000/schedule
  ```

- **Save Configuration**:
  ```bash
  curl -X POST -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"action": "save", "config": { ... }}' \
    http://localhost:8000/schedule
  ```
  *(Atomically persists to `config/schedule.json` or fallback cache directory.)*

- **Reset Configuration to Factory Defaults**:
  ```bash
  curl -X POST -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"action": "reset"}' \
    http://localhost:8000/schedule
  ```

---

## 5. Control Plane & Fleet Administration

All endpoints below require authentication with the `X-Tracker-Token` header.

### 5.1 `GET /devices`
Lists all registered devices and real-time telemetry.

#### Response (`200 OK`, `application/json`)
```json
{
  "devices": [
    {
      "client_id": "G001LG0123456789",
      "remote_ip": "192.168.1.142",
      "battery": 87,
      "charging": true,
      "last_seen": 1791653580.4,
      "client_version": "1.38.0",
      "firmware_version": "5.16.2.1",
      "client_mode": "sleep",
      "target_mode": "",
      "online": true
    }
  ]
}
```

### 5.2 `POST /action`
Queues a hardware or software action for a specific client or the entire fleet.

#### Parameters (JSON body, Form, or Query)
- `action`: Supported commands:
  - `restart`: Gracefully restarts the Go client process.
  - `reboot`: Reboots the Kindle operating system via `lipc-set-prop com.lab126.powerd reboot 1`.
  - `update`: Triggers an immediate OTA check and binary upgrade.
  - `clear_backup`: Removes `/tmp/tracker-arm.bak` to free storage.
  - `disable-ads`: Disables lockscreen special offers on Kindle.
  - `stop-framework`: Suspends Kindle native GUI framework (`stop lab126_gui`).
  - `start-framework`: Restores Kindle native GUI framework.
  - `framework-state`: Inspects framework status.
  - `sleep-test`: Verifies low-power RTC sleep cycle.
  - `rtc-suspend`: Initiates RTC alarm suspend cycle.
  - `input-wake-probe`: Tests wake capability on touch.
- `client_id` (optional): Target device serial / UUID, or `all` to broadcast.

#### Example
```bash
curl -X POST -H "X-Tracker-Token: $TRACKER_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "update", "client_id": "all"}' \
  http://localhost:8000/action
```

### 5.3 `POST /mode`
Configures the execution mode for devices.

#### Parameters
- `mode`: Target execution mode:
  - `resident`: Keep running continuously in memory with fast periodic polling.
  - `sleep`: Low-power periodic sleep with Wi-Fi power toggling.
  - `sleep-suspend`: Deep low-power sleep using Linux kernel suspend-to-RAM (`/sys/power/state`).
  - `oneshot`: Render once and exit.
- `client_id` (optional): Target client ID or `all`.

### 5.4 `POST /diag/request`
Requests that devices upload a diagnostic dump on their next poll.

#### Parameters
- `mode`: `quick` or `full`.
- `client_id` (optional): Target client ID or `all`.

### 5.5 `POST /stop` and `POST /resume`
Temporarily halts or resumes client polling fleet-wide.

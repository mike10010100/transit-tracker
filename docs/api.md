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
| `X-Tracker-Policy` | Schedule and interaction policy as `;`-separated `key=value` pairs: `v` (policy version), `phase`, `until` (epoch seconds of the next phase change; omitted if none), `suspend` (`0`/`1`), `session`, `fast`, `hold` (seconds), `sl` (session lighting `brightness,warmth`). Clients must ignore unknown keys. See [architecture §2.3](architecture.md#23-schedule-state-machine-serverstate_machinepy-serverschedulepy). |
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
  "version": "1.35.5",
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
  "version": "1.35.5",
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
  "version": "1.35.5",
  "sha256": "0109ef3df247a368977e1d8fcb307788578ae3b7a7c2edd1b1642fc6f9fa5429",
  "size": 6750370,
  "signature": "<base64-ed25519-signature>"
}
```

---

## 4. Control Plane & Administration Endpoints

All endpoints below require authentication with the `X-Tracker-Token` header.

### 4.1 `GET /devices`
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
      "client_version": "1.35.5",
      "firmware_version": "5.16.2.1",
      "client_mode": "sleep",
      "target_mode": "",
      "online": true
    }
  ]
}
```

### 4.1a `GET /schedule`
Read-only view of the schedule state machine, for debugging `schedule.json`. Like the other control endpoints, it requires `X-Tracker-Token`.

#### Response (`200 OK`, `application/json`)
```json
{
  "source": "/app/config/schedule.json",
  "path": "/app/config/schedule.json",
  "error": null,
  "now": "2026-10-08T08:15:00",
  "current": {
    "phase": "peak", "presentation": "interactive", "status_note": "",
    "poll_interval": 60, "lighting": {"brightness": 8, "warmth": 12},
    "realtime": true, "suspend": false, "view": "morning",
    "until": "2026-10-08T09:30:00"
  },
  "transitions": [{"at": "2026-10-08T09:30:00", "phase": "offpeak"}],
  "config": { "...": "effective config in schedule.json shape" },
  "overrides": {"force_phase": null, "force_fast_poll": false}
}
```
`error` contains the most recent rejected-file message, while the last good config remains active. `transitions` covers the next 24 hours.

### 4.2 `POST /action`
Queues a hardware or software action for a specific client or the entire fleet.

#### Parameters (JSON body, Form, or Query)
- `action`: `restart`, `reboot`, `update`, `clear_backup`, `disable-ads`, `stop-framework`, `start-framework`, `framework-state`, `sleep-test`, `rtc-suspend`, `input-wake-probe`, `touch-wake-test`, `touch-wake-probe`.
- `client_id` (optional): Specific client ID, or `all` to broadcast.

### 4.3 `POST /mode`
Configures the execution mode for devices.

#### Parameters
- `mode`: `resident`, `oneshot`, `sleep`, `sleep-suspend`.
- `client_id` (optional): Target client ID or `all`.

### 4.4 `POST /diag/request`
Requests that devices upload a diagnostic dump on their next poll.

#### Parameters
- `mode`: `quick` or `full`.
- `client_id` (optional): Target client ID or `all`.

### 4.5 `POST /stop` and `POST /resume`
Temporarily halts or resumes client polling fleet-wide.

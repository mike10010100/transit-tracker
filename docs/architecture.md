# Architecture & System Design

The **Hoboken Transit Tracker** is a multi-tier system engineered to deliver real-time transit and micro-mobility telemetry to ultra-low-power e-ink displays (specifically jailbroken Amazon Kindle Paperwhite devices) with minimal energy consumption and cryptographic verification.

---

## 1. System Topology

```mermaid
flowchart TD
    subgraph External["External Telemetry Feeds"]
        NJT_DV["NJ Transit DepartureVision (BUSDV2 API)"]
        NJT_GQL["NJ Transit GraphQL Public Fallback"]
        GBFS["Citi Bike GBFS Live Feed"]
    end

    subgraph Server["Host Server (server/)"]
        direction TB
        Ingest["Telemetry Ingestion\n(bus_tracker.py, citibike.py, gtfs_bus.py)"]
        Layout["Canvas Rendering Engine\n(canvas.py, morning_view.py, evening_view.py)"]
        ScheduleEngine["Schedule & Lighting Governor\n(schedule.py)"]
        FleetReg["Device Registry & State Store\n(device_registry.py)"]
        HttpServer["HTTP / Telemetry / OTA Server\n(server.py on Port 8000)"]
        DiscoveryResp["Discovery Responder\n(discovery.py on Port 8001 / mDNS)"]

        Ingest --> Layout
        ScheduleEngine --> HttpServer
        FleetReg --> HttpServer
        Layout --> HttpServer
    end

    subgraph Client["Kindle Paperwhite Client (client-go/)"]
        direction TB
        Bootstrap["TransitTracker.sh\n(Cascading LAN Discovery & Supervisor)"]
        GoBinary["tracker-arm Native Binary"]
        
        subgraph Subsystems["tracker-arm Subsystems"]
            Disc["discovery.go (mDNS, UDP, /24 Subnet Sweep)"]
            Http["client.go (Conditional HTTP Polling, ETag Cache)"]
            Sec["security.go (Ed25519 & Constant-Time SHA-256)"]
            Ev["input.go (Evdev Touch & Power Key Dispatcher)"]
            Disp["display.go (E-Ink Driver & eips Framebuffer)"]
        end

        Bootstrap -->|Launch & Hot-Reload| GoBinary
        GoBinary --> Subsystems
    end

    subgraph HW["Kindle Hardware"]
        FB["E-Ink Display (/dev/fb0)"]
        Touch["pt_mt Digitizer (/dev/input/event1)"]
        Pwr["bd71828-pwrkey (/dev/input/event0)"]
        Light["Frontlight Controller (lipc / powerd)"]
    end

    External --> Ingest
    HttpServer -->|dashboard.png, OTA updates| Http
    Disc --> DiscoveryResp
    Disp --> FB
    Touch --> Ev
    Pwr --> Ev
    Http --> Light
```

---

## 2. Server Architecture (`server/`)

The server runs on standard Linux/macOS hosts, home servers, or Raspberry Pis using Python 3.11+.

### 2.1 Dual-Redundancy Arrival Engine
- **Primary Source**: `NJTransitBusTracker` queries the NJ Transit DepartureVision BUSDV2 XML endpoint for Route 126 arrivals at designated stop IDs (`#20512` at Washington & 9th; `#20494` at Clinton & 9th).
- **Secondary Fallback**: If BUSDV2 encounters network timeouts, schema changes, or HTTP errors, the tracker automatically falls back to NJ Transit's public GraphQL endpoint without interrupting the client.
- **GTFS Bus Tracker**: `GTFSBusTracker` (`server/gtfs_bus.py`) indexes static GTFS timetables with GTFS-RT protocol buffers to compute ETA predictions even when primary feeds are impaired.
- **Citi Bike Telemetry**: `CitiBikeTracker` (`server/citibike.py`) consumes public GBFS JSON feeds for live station availability and prioritizes e-bikes and open docks across 6 key neighborhood hubs.

### 2.2 Dynamic Layout & Resolution Engine
- **Resolution Native Rendering**: The server renders images at the client's native panel resolution (e.g. 1648×1236 for Kindle Paperwhite 5) rather than upscaling an 800px bitmap. Text and vector glyphs are rendered with subpixel font metrics directly to the target canvas.
- **Morning vs. Evening Hero Views**:
  - `morning` (5:00 AM – 12:00 PM): Hero presentation focused on Citi Bike availability for the morning NYC commute.
  - `evening` (12:00 PM – 5:00 AM): Hero presentation prioritizing Route 126 inbound/outbound bus departures.
  - `auto`: Automatically selects the optimal view based on current local time.
- **`ScaledDraw` Proxy**: Located in `server/canvas.py`, wraps Pillow's `ImageDraw` to scale coordinates, bounding boxes, and font sizes linearly according to the requested display scale.

### 2.3 Schedule State Machine (`server/state_machine.py`, `server/schedule.py`)

The schedule is a configurable two-layer state machine. The server owns
**layer 1**, the time-driven *phase*. The client owns **layer 2**, the
event-driven *interaction overlay* (`idle → session → hold`). Its timeouts are
configured on the server and shipped in the signed `X-Tracker-Policy` header.

```mermaid
stateDiagram-v2
    direction LR
    offpeak --> peak: window start
    peak --> offpeak: window end
    offpeak --> overnight: window start
    overnight --> offpeak: window end
```

Each phase sets everything that used to be scattered across `schedule.py`:

| Phase key | Meaning | `peak` | `offpeak` | `overnight` |
|---|---|---|---|---|
| `poll_interval` | `X-Kindle-Poll-Interval` (30–7200 s) | 60 | 600 | 3600 |
| `presentation` | `interactive` / `idle` / `dormant` | interactive | idle | dormant |
| `status_note` | Bottom-strip text for non-interactive faces; `{until}` is replaced with the phase end (e.g. `6:00 AM`) | – | "PRESS POWER…" | "SLEEPING — back at {until}…" |
| `lighting` | Frontlight `brightness` / `warmth` (0–24) | 8 / 12 | 0 / 0 | 0 / 0 |
| `realtime` | Allow GTFS-RT / live feeds (else static schedule only) | ✓ | ✓ | ✗ |
| `suspend` | Client may deep-suspend between polls | ✗ | ✓ | ✓ |

**Windows** select the active phase. They are evaluated **in order and the first match wins**. `days` defaults to every day. A window with `start > end` wraps past midnight and belongs to the day it *starts* on. Outside every window, `default_phase` applies. A window may also set `view` (`morning` / `evening`), which an `auto` view request then uses. Outside such windows, `auto` falls back to the 5:00–12:00 morning rule. The defaults are weekday peaks 07:30–09:30 (morning view) and 16:30–19:00 (evening view), plus overnight 22:00–06:00 every day.

**Configuration** lives in `schedule.json` at `$SCHEDULE_CONFIG` (default `/app/config/schedule.json`). [`config/schedule.example.json`](../config/schedule.example.json) spells out the built-in defaults. The merge rules are:
- `phases` merge by name and key. You can override a single value, such as `{"phases": {"peak": {"lighting": {"brightness": 4}}}}`, or add a new phase.
- `windows` replaces the default windows entirely.
- `interaction` merges by key.

Precedence, from lowest to highest: built-in defaults, then `schedule.json`, then the legacy env vars (`PEAK_*`, `OVERNIGHT_*`, `OFFPEAK_INTERVAL`, `OVERNIGHT_INTERVAL`). Empty env vars count as unset. The window env vars are ignored, with a warning, when the file defines its own `windows`.

**Fail-safe hot reload:** the file is re-read whenever its mtime changes. Validation is strict: unknown keys, wrong types, out-of-range values, bad `HH:MM` times and undefined phase references all reject the **whole file**. The error is logged and shown by `GET /schedule`, and the last good config stays in effect.

**Testing aids:** `FORCE_PHASE=<name>` pins a phase. `FORCE_FAST_POLL=1` forces an interactive face with 60 s polling.

**Interaction overlay parameters** (`interaction`): `session_timeout` (90 s; how long a power-button session stays awake without touches), `session_lighting` (8 / 12), `fast_poll_hold` (600 s) and `hold` (2700 s; how long manual view and frontlight choices persist). Clients receive these, plus the current phase, its end (`until`, epoch seconds) and `suspend`, in the signed `X-Tracker-Policy` header:

```
X-Tracker-Policy: v=1;phase=peak;until=1760103000;suspend=0;session=90;fast=600;hold=2700;sl=8,12
```

### 2.4 Multi-Device Fleet Registry (`server/device_registry.py`)
- **Auto-Registration**: Any incoming request containing `X-Tracker-Client-ID` is automatically registered without prior manual provisioning.
- **Isolated Per-Device State**:
  - Telemetry: IP address, battery percentage, charging state, client version, firmware version, and last-seen timestamp.
  - Command Queues: Isolated queues for `X-Tracker-Action` (e.g. `restart`, `reboot`, `update`) and `X-Tracker-Diag` (`quick`, `full`).
  - Storage: Per-device diagnostic logs and dumps stored under `<cache_dir>/devices/<client_id>/`.

---

## 3. Client Architecture (`client-go/`)

The Kindle client is a statically linked Go binary compiled for Linux ARMv7 (`CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7`).

### 3.1 Subsystems Breakdown

| Subsystem | Source File | Responsibility |
|---|---|---|
| **Discovery** | `client-go/discovery.go` | Implements LAN autodiscovery via mDNS, UDP broadcast (`TRANSIT_TRACKER_DISCOVER` on port 8001), and `/24` subnet sweeps. Rejects non-private or public addresses. |
| **HTTP Engine** | `client-go/dashboard.go` | Manages conditional polling loop (`If-None-Match` / `ETag`), keep-alive connections, exponential backoff, response auth verification, and header injection. |
| **Security** | `client-go/internal/otasig` | Implements Ed25519 response signature verification (`transit-tracker-resp-v2`), server identity certificate validation, and manifest checks. |
| **Event Router** | `client-go/input.go` | Asynchronously reads Linux evdev input streams (`/dev/input/event1` touch, `/dev/input/event0` power key). Decodes single taps, double taps (< 380ms), and button bar coordinates. |
| **Interaction Overlay** | `client-go/interaction.go` | Implements client-owned interaction overlay (`idle -> session -> hold`), policy parsing, timeout tracking, and fast-poll holds. |
| **Power Management** | `client-go/power.go` | Controls low-power sleep loops, Wi-Fi radio toggling, RTC wakealarm programming clamped to phase boundaries, and suspend-to-RAM (`/sys/power/state`). |
| **Display & Lighting** | `client-go/kindle.go` | Framebuffer geometry detection, Amazon UI suspension, eips rendering, and frontlight intensity/warmth cycling. |
| **OTA Supervisor** | `client-go/dashboard.go` | Detects new versions advertised by the server, validates the signed manifest against the embedded release key, writes to disk, and replaces the running process via `syscall.Exec`. |

### 3.2 Client Identification
The Go client establishes identity in `client-go/client_id.go`:
1. Queries Kindle hardware serial via `lipc-get-prop com.lab126.system serialNumber`.
2. Falls back to a persistent RFC 4122 v4 UUID in `/mnt/us/documents/tracker_client_id.txt` (or `/tmp/tracker_client_id.txt` on non-Kindle systems).
3. Attaches `X-Tracker-Client-ID: <id>` to every HTTP communication.

### 3.3 Interaction Overlay & Low-Power Sleep Machine
The client runs an event-driven interaction overlay that coordinates user input with the server's schedule policy:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Idle
    Idle --> Session: EvPowerWake
    Session --> Session: EvTouch (extends hold)
    Session --> Hold: EvSessionEnd (timeout)
    Session --> Hold: EvDataTap / EvViewTap
    Hold --> Session: EvPowerWake
    Hold --> Idle: EvPhaseChanged (clears overrides)
    Hold --> Idle: EvTick (timers expired)
```

1. **Overlay States**:
   - **`idle`**: The server's phase settings apply unchanged.
   - **`session`**: Active user interaction. Requests `present=interactive`, keeps device awake, lights panel to `session_lighting`, and resets session timer on every touch.
   - **`hold`**: Manual view and frontlight choices persist, and resident-mode polling stays at fast cadence for `fast_poll_hold`. In low-power suspend mode, this does not keep the device awake. Phase transitions immediately reset `hold` to `idle` to avoid leaking manual choices across phases (e.g. overnight).
2. **Boundary-Clamped RTC Wakes**:
   - In low-power sleep mode (`client-go/power.go`), when the device suspends to RAM, the RTC alarm is clamped to `min(interval, until - now)`.
   - This ensures the device wakes promptly when a schedule phase transitions (e.g., at the start of a morning commute peak) rather than oversleeping through phase changes.
   - If the phase boundary is closer than `minSuspendInterval` (120s), the device stays awake on wall-clock sleep instead of incurring suspend/resume latency.

---

## 4. Bootstrap Launcher (`client-go/launcher/TransitTracker.sh`)

A POSIX-compliant shell script that acts as the supervisor on the Kindle device:
1. **Network Initialization**: Waits for Wi-Fi interface connectivity (`wlan0`).
2. **Cascading LAN Discovery**:
   - Tier 1: Probes `transit-tracker.local:8000` via mDNS.
   - Tier 2: Queries the default gateway IP from `/proc/net/route` on port 8000.
   - Tier 3: Executes a parallel `/24` subnet sweep probing `/healthz` and `/identity`.
3. **Atomic Persistence**: Persists discovered server URL to `/mnt/us/documents/tracker_server.txt`.
4. **Binary Bootstrap & Recovery**: Downloads `tracker-arm`, validates the ELF magic bytes (`\x7fELF`), maintains a verified backup copy, and invokes the binary. If a new binary fails, it rolls back automatically to the backup copy.

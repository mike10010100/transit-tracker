# Transit Tracker Documentation Hub

Welcome to the **Hoboken Transit Tracker** technical documentation. This documentation covers everything from quick local exploration and Kindle deployment to in-depth architecture, cryptographic security, and developer guidelines.

---

## 🗺️ Documentation Directory

| Document | Target Audience | Primary Focus |
|---|---|---|
| **[Architecture & System Design](architecture.md)** | System Engineers & Maintainers | System topology, dual-redundant arrival engine, native canvas rendering, schedule governor, and fleet management. |
| **[Hardware & Kindle Paperwhite Guide](hardware_kindle.md)** | Kindle Deployers & Hardware Users | PW5 hardware specs, jailbreak prerequisites, bootstrap launcher, touch gestures, power management, and troubleshooting. |
| **[HTTP API & Protocol Reference](api.md)** | Developers & Integrators | Public endpoints, telemetry headers, JSON schemas, schedule configuration API, and control plane commands. |
| **[Security Specification & Cryptographic Model](security.md)** | Security Auditors & Developers | Ed25519 root trust chain, signed OTA manifests, server identity certificates, constant-time verification, and CSP. |
| **[Developer Guide & Quality Verification](development.md)** | Contributors & CI/CD Engineers | Tooling prerequisites, Makefile targets, linters, unit/integration test suites, coverage gates, and release builds. |

---

## 🎯 Navigating by Persona

### 🚀 "I just want to run the dashboard and see it working."
1. Follow the **[Quick Start Guide](../README.md#⚡-quick-start--2-minutes)** to launch the server via Docker or Python.
2. Open [`http://localhost:8000`](http://localhost:8000) in your web browser to view the interactive dashboard.
3. Open [`http://localhost:8000/dashboard.png?kindle=pw5`](http://localhost:8000/dashboard.png?kindle=pw5) to inspect the 300 PPI native Kindle e-ink rendering.

### 📱 "I have a Kindle Paperwhite and want to set it up."
1. Ensure your device is jailbroken (FW 5.14.x–5.16.x).
2. Follow the 3-step setup in **[Kindle Installation Guide](hardware_kindle.md#3-installation-via-launcher-script)**.
3. Review the **[Touch Controls Cheatsheet](hardware_kindle.md#4-touch-gestures--button-zones)** to learn about the bottom button bar and shortcuts.
4. If your Kindle cannot locate the server automatically, consult **[Kindle Troubleshooting](hardware_kindle.md#7-troubleshooting--faq)**.

### ⚙️ "I want to adjust commute hours, screen dimming, or poll rates."
1. Open the Web Dashboard at `http://localhost:8000` and scroll to **Schedule Management**.
2. Use the in-browser JSON editor to customize phases and time windows, then click **Save & Apply**.
3. For file-based configuration, copy `config/schedule.example.json` to `config/schedule.json` and edit it directly (changes hot-reload automatically).
4. Detailed state machine behavior is documented in **[Schedule State Machine](architecture.md#23-schedule-state-machine-serverstate_machinepy-serverschedulepy)**.

### 🔐 "I want to inspect or audit the security model."
1. Read the **[Cryptographic Trust Chain](security.md#1-cryptographic-trust-chain)** to understand how Ed25519 keys authenticate OTA updates and server responses.
2. Review the **[Constant-Time Cryptography Defenses](security.md#4-constant-time-timing-attack-defenses)** to verify timing side-channel mitigations.
3. Inspect the **[Zero-Bypass Policy](security.md#5-control-plane-authentication--web-ui-hardening)** governing administrative tokens and Content Security Policy (CSP).

### 🛠️ "I want to contribute code or run verification tests."
1. Read the **[Prerequisites & Workflows](development.md#1-prerequisites)**.
2. Run `make check` to execute format checks, linters, tests, and coverage checks across all stacks.
3. Review the strict non-negotiables in [`AGENTS.md`](../AGENTS.md).

---

## 💡 Key Concepts & Terminology

- **Dual-Redundancy Arrival Engine**: The server queries NJ Transit's DepartureVision (BUSDV2) XML feed first; if it is slow or down, it automatically queries the public GraphQL API or static GTFS schedules without interrupting the display.
- **Hero Presentation Views**:
  - `morning` (5:00 AM – 12:00 PM): Hero presentation highlighting Citi Bike dock availability and e-bikes for commuters heading into Manhattan.
  - `evening` (12:00 PM – 5:00 AM): Hero presentation highlighting Route 126 NYC-bound and return bus departures.
- **Conditional Polling (ETag 304)**: The Kindle client requests images using `If-None-Match`. If transit predictions have not changed, the server returns `304 Not Modified`, saving Wi-Fi bandwidth and battery by preventing unnecessary e-ink flashing.
- **Cascading Discovery**: The Kindle client locates the host server on the local network automatically using a 3-tier cascade:
  1. *Tier 1*: mDNS hostname lookup (`transit-tracker.local`).
  2. *Tier 2*: Default network gateway IP probe from `/proc/net/route`.
  3. *Tier 3*: Parallel `/24` local subnet sweep.
- **Interaction Overlay**: The Kindle's client-side state machine (`idle` $\rightarrow$ `session` $\rightarrow$ `hold`) that manages frontlight illumination, button debounce, and temporary fast-polling when a user interacts with the physical device.

---

## ❓ Frequently Asked Questions

### Do I need a Kindle to use this project?
No. You can view the dashboard in any desktop or mobile browser at `http://localhost:8000`, or embed the `/dashboard.png` image into Home Assistant or any other smart display.

### Where is the control token stored?
If `TRACKER_CONTROL_TOKEN` is not explicitly set in your `.env` file, the server generates a cryptographically secure random token on startup and logs it to stdout. The token is also stored with `0600` permissions in the cache directory (`.cache/tracker_token.txt`).

### What Kindle models are supported?
The client is tested on the **Kindle Paperwhite 5 (11th Gen, 2021)** running Linux ARMv7. The server supports rendering profiles for standard 800×600 displays as well as the native 1648×1236 PW5 panel.

### How does the Kindle update itself?
The Go binary embedded on the Kindle polls the server and checks the `X-Tracker-Version` header. When a newer signed release is available, it downloads the binary, verifies its Ed25519 signature and SHA-256 hash, and atomically hot-reloads via `syscall.Exec`.

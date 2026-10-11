# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.37.1] - 2026-10-10

### Fixed

- **Response Signing Backward Compatibility (`transit-tracker-resp-v1` & `v2`)**: Added server-side protocol negotiation and dual signature support in `server/identity.py` and `server/server.py`. Older deployed clients (such as `v1.35.x`) that only know `transit-tracker-resp-v1` (12 signed headers) are authenticated with `v1` signatures rather than rejected, allowing them to render dashboards and receive OTA updates seamlessly.
- **Client Dual Format Verification**: Updated `client-go/internal/otasig/otasig.go` to verify against `transit-tracker-resp-v2` first, gracefully falling back to `transit-tracker-resp-v1` for backward compatibility.
- **Kindle Library File Hiding & Migration**: Migrated Kindle client ID storage in `client-go/client_id.go` to `/mnt/us/documents/.tracker_client_id.txt` (hidden dotfile) so the Kindle ebook indexer no longer indexes the UUID as a document book on the Home/Library screen. Automatically migrates and removes legacy unhidden `/mnt/us/documents/tracker_client_id.txt` on startup.

---

## [1.37.0] - 2026-10-10

### Added

- **Client Interaction State Machine** (`client-go/interaction.go`): Formalized client-side interaction modes into an event-driven overlay state machine (`idle -> session -> hold`). Pure transition function with complete test matrix and 100% statement coverage.
- **Unified Response Format `transit-tracker-resp-v2` Client Verification**: Client verifies response signatures under `transit-tracker-resp-v2`, authenticating all response headers including `X-Tracker-Policy` for end-to-end cryptographic integrity.
- **Strict Policy Parsing & Clamping**: `parsePolicy` decodes `X-Tracker-Policy` with fail-safe bounds clamping on session timeout, fast poll hold, manual hold, and frontlight intensity/warmth. Covered by continuous native Go fuzzing (`FuzzParsePolicy`).
- **Phase-Boundary Clamped RTC Wakes**: In sleep mode (`runSleepLoop`), the hardware RTC wakealarm is clamped to `min(interval, until - now)` based on the active policy's phase transition deadline, ensuring the device wakes promptly when a phase transitions (e.g. morning peak) without oversleeping.
- **Policy-Governed Suspend**: Device suspend-to-RAM in sleep mode is explicitly gated by the server's policy (`suspend=0` stays awake on wall clock).

### Changed

- Refactored scattered interaction flags (`interacting`, `lastDataInteraction`, `manualLightTime`, `manualViewTime`) into the centralized interaction overlay.
- Removed hardcoded commute hours fallback in `client-go/schedule.go` in favor of last-known policy state and exponential backoff retry.
- Session auto-lighting and hold suppression directly integrate with overlay state and policy tunables.

---

## [1.36.0] - 2026-10-10

### Added

- **Configurable Schedule State Machine** (`server/state_machine.py`): the peak/off-peak/overnight schedule is now a declarative state machine. Named phases set the poll interval, presentation, status-strip text (with an `{until}` placeholder), frontlight, realtime feeds and suspend permission. Ordered, day-of-week-aware windows select the phase (first match wins; windows that wrap past midnight belong to the day they start on). Windows can also set the `auto` view.
- **`schedule.json` Configuration**: loaded from `$SCHEDULE_CONFIG` (default `/app/config/schedule.json`) and deep-merged onto the built-in defaults, so only changed keys are needed. It is hot-reloaded when its mtime changes. Validation is strict and fail-safe: an invalid file is rejected as a whole and the last good config stays active. Includes `config/schedule.example.json`.
- **`GET /schedule`** (requires `X-Tracker-Token`): config source, any load error, the current phase and its end, the next 24 h of transitions, and the effective config.
- **Response Format `transit-tracker-resp-v2` and `X-Tracker-Policy`**: responses include a cryptographically signed `X-Tracker-Policy` header carrying the phase, next transition timestamp (`until`), suspend permission, and interaction-overlay timeouts. Shared response test vector updated.
- **`FORCE_PHASE`** testing override to pin a phase.
- Control panel shows the current phase and when it ends.

### Changed

- `server/schedule.py` is now a backward-compatible facade over the state machine. Presentation, poll interval, lighting, status note and realtime gating for a request all come from one resolved state. The `sys.modules["server"]` monkeypatch hooks and the module-level `FORCE_FAST_POLL` / `OVERNIGHT_*` constants are removed.
- `docker-compose.yml` passes the legacy schedule env vars through only when they are set. Previously their hardcoded defaults would have overridden `schedule.json`. The compose file also mounts `./config` read-only.
- The legacy env vars (`PEAK_*`, `OVERNIGHT_*`, `OFFPEAK_INTERVAL`, `OVERNIGHT_INTERVAL`) still work and take precedence over the file. A property test checks them against the historical logic.

---

## [1.35.13] - 2026-10-10

### Fixed

- **Hermetic Multi-Device Web Interface Unit Test**: Fixed a time-of-day flake in `tests/test_server_http.py` (`test_fleet_overview_multi_device_rendering`) where devices seen 1 hour ago were classified as online during overnight hours (22:00–06:00 UTC) due to the dynamic 3600s deep-eco poll interval (2.5x cutoff = 9000s). Devices intended to be offline are now set to 24 hours in the past, ensuring test hermeticity across all timezones and times of day.
- **GTFS Index Rebuild Thread Cleanup in Tests**: In `tests/test_gtfs_bus.py` (`test_ensure_index_branches`), explicitly join the background rebuild thread before temporary directory context cleanup to eliminate directory deletion race conditions.

## [1.35.12] - 2026-10-10

### Changed

- **Architecture Overview Diagram Layout**: Redesigned the Mermaid architecture diagram in `README.md` into a vertically aligned 3-tier structure (External Transit Telemetry -> Host Python Server -> Kindle Paperwhite 5). Eliminates side-by-side subgraph crowding, crooked edges, and overlapping labels for a clean, readable presentation.

---

## [1.35.11] - 2026-10-10

### Added

- **Automated Mermaid Diagram Validation**: Added standalone validator `scripts/check_mermaid.py` and unit test suite `tests/test_docs_mermaid.py` ensuring all Markdown documentation diagrams (`README.md`, `docs/`) conform to Mermaid syntax rules (preventing unquoted delimiters in edge labels, unquoted nested delimiters in node labels, unclosed subgraphs, and mismatched blocks).
- **Mermaid Makefile & CI Quality Gates**: Added `make check-mermaid` target, integrated into `make lint`, and added automated validation step in `.github/workflows/ci.yml`.

### Fixed

- **README Architecture Overview Diagram**: Wrapped edge labels containing parentheses and special characters (`(Signed Binary & Manifest)`) in double quotes, fixing GitHub rich render parser error (`got 'PS'`).
- **Cross-Platform Test Font & Stress Resilience**: Updated font fallback paths in `tests/test_render_dashboard.py` and connection retry backlog handling in `tests/test_challenger_m2_harness.py` for macOS compatibility.

---

## [1.35.10] - 2026-10-10

### Added

- **Client Version & Firmware Telemetry**: Go client now attaches `X-Tracker-Client-Version: <Version>` and `X-Tracker-Firmware: <ReadFirmwareVersion>` headers across all polls, logs, diagnostics, and OTA requests.
- **Diagnostics Version Ingestion Fallback**: Server now extracts client and Kindle OS firmware versions from diagnostic uploads (`/diag`) even when request headers are omitted.

### Fixed

- **Fleet Overview Device Isolation**: Excluded web browser image previews (`/dashboard.png` with no Kindle headers or client parameters) from auto-registering in the device registry as `default`, preventing browser traffic from polluting the fleet overview.
- **Kindle OS Firmware Detection**: Added cached firmware version extraction in `client-go` parsing `/etc/prettyversion.txt` and `/etc/version`, stripping build metadata hashes like `(~~otaVersion~~)`.

## [1.35.9] - 2026-10-10

### Added

- **Go Client Native Fuzzing (`testing.F`)**: Implemented zero-dependency native Go fuzz test suites covering Linux evdev input event decoding (`FuzzParseInputEvents`) and cryptographic OTA signature/manifest/cert/semver validation (`FuzzVerifyManifest`, `FuzzVerifyCert`, `FuzzParseSemver`, `FuzzVerifyResponse`). Added `make fuzz` target and CI smoke test stage.
- **Python Property-Based Testing (`hypothesis`)**: Added randomized property-based test harness (`tests/test_properties.py`) validating dashboard rendering layout invariants, schedule/commute lighting bounds, and nonce/header deterministic protocol envelopes.
- **Targeted Mutation Testing (`mutmut`)**: Integrated scoped mutation testing for the server security and identity layer (`server/identity.py`), achieving high test kill rates with zero mutant survival in nonce validation. Added `make mutate-py` target and CI quality gate.

### Fixed

- **Defensive Evdev Buffer Bounds**: Clamped length bounds in `client-go/events.go` to prevent out-of-bounds slice indexing on malformed or truncated input buffers.
- **Resilient View Field Extraction**: Handled missing `walk_min`, `classic`, `ebikes`, and `docks` station keys gracefully in `server/morning_view.py` and `server/evening_view.py`.
- **CRLF Injection Prevention**: Enforced strict CR and LF rejection in `build_response_message` in `server/identity.py` to match Go client verification and spec §3 requirements.

## [1.35.8] - 2026-10-10

### Changed

- **CI Action Upgrades**: Upgraded `actions/setup-go` to `@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e` (v7.0.0) and `hadolint/hadolint-action` to `@06be81baf89a55ffd0e24b8f04a4185738dd3387` (v3.5.0) via grouped Dependabot update.

## [1.35.7] - 2026-10-10

### Changed

- **CI Workflow Modernization**: Upgraded `actions/checkout` to `@3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1) and `actions/setup-python` to `@5fda3b95a4ea91299a34e894583c3862153e4b97` (v7.0.0), resolving Node.js 20 deprecation warnings on GitHub Actions runners while preserving commit SHA cryptographic pinning.
- **Dependabot Batching & Strategy**: Added update groups (`github-actions`, `go-dependencies`, `python-dependencies`) and `versioning-strategy: increase-if-necessary` to `.github/dependabot.yml` to prevent automated PR spam and eliminate conflicting multi-PR dependency races.

## [1.35.6] - 2026-10-10

### Security

- **Launcher Integrity Verification (SEC-01 / ADV-01)**: Mandated SHA-256 digest validation against `tracker-arm.manifest` before binary execution in `TransitTracker.sh`, rejecting unverified or missing manifests.
- **Control Plane Authentication (SEC-02 / H1)**: Enforced `X-Tracker-Token` authorization check on `GET /devices` endpoint, eliminating unauthenticated fleet telemetry exposure.
- **Dependency Cleansing & Lockfile Purge (SEC-03 / H2)**: Removed unused and vulnerable `requirements.lock` from the repository root.
- **Dependency Vulnerability Audits & govulncheck (SEC-04 / H3)**: Updated Python dependencies to patched minimums, integrated `govulncheck` in Go CI pipeline and `Makefile`, and hardened audit gates.
- **Bounded Registry Memory & Log Storage (SEC-05 / M1 / ADV-03)**: Capped device registry capacity to `MAX_REGISTERED_DEVICES` (64) with oldest-offline eviction, and capped per-device log and diagnostic files with automatic tail-rotation.
- **HTTP Smuggling & Connection Desync Protection (SEC-06 / M2)**: Set explicit `Connection: close` and `close_connection = True` on 4xx/403 errors and body truncation conditions in `server.py`.
- **Default Production BUSDV2 Endpoint (SEC-07 / M3)**: Changed `DEFAULT_BASE_URL` from `testpcsdata.njtransit.com` to production `https://pcsdata.njtransit.com` to prevent credential exposure to test environments.
- **Secure File Permissions (SEC-09 / M5)**: Enforced `chmod 600` for `.env` files and updated setup documentation.
- **Strict Content Security Policy & Security Headers (SEC-12 / L5 / ADV-05)**: Added `frame-ancestors 'none'`, `object-src 'none'`, and `base-uri 'none'` to CSP; enforced `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff`.
- **Cache Thread Safety (SEC-12 / L1)**: Synchronized `_data_cache["time"]` access under `_data_lock` when constructing render cache keys.

### Added

- **Fleet Maintenance Actions (SEC-11 / ADV-04)**: Implemented `restart`, `reboot`, `update`, and `clear_backup` maintenance actions in Kindle Go client `deviceActions`.

### Changed

- **Non-Blocking GTFS Cold-Start (SEC-10 / M6)**: Updated `ensure_index(wait=False)` to asynchronously trigger background rebuilds rather than blocking incoming HTTP threads on cold start.
- **Commute Lighting on HTTP 304 (SEC-12 / L3)**: Refactored `processControlHeaders` in `client-go` to apply frontlight brightness, warmth, and control commands even when visual dashboard content is unchanged (HTTP 304).
- **API & Architectural Documentation Alignment (SEC-08 / M4)**: Reconciled `README.md` and `docs/api.md` with `/devices` JSON schema, authentication requirements, and available device actions.

## [1.35.5] - 2026-10-10

### Added

- **Unified Verification Pipeline**: Added automated verification scripts and gates across Go (93% statement coverage gate) and Python (92% branch coverage gate).
- **Consolidated Documentation**: Organized full technical documentation architecture under `docs/` (`architecture.md`, `security.md`, `api.md`, `hardware_kindle.md`, `development.md`).
- **Comprehensive Quality Tooling**: Enforced `gofmt`, `go vet`, `golangci-lint`, `mypy`, `ruff`, `shellcheck`, and `hadolint` across all codebases.
- **Armv7 Signed Release Verification**: Cryptographic Ed25519 signing validation pipeline for OTA manifests and server identity certificates.

### Changed

- **CI Workflow Modernization**: Upgraded GitHub Actions verification suite to Go 1.26 toolchain and hardened container smoke tests.
- **Docker Non-Root Hardening**: Runtime execution under unprivileged `tracker` system user and strict format compatibility in `docker top` inspections.
- **Gesture and Power Management Cleanup**: Cleaned up gesture detector callbacks, uptime parsers, and powerd suspend helpers for zero linter warnings.

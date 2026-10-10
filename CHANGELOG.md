# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.35.12] - 2026-10-10

### Changed

- **Architecture Overview Diagram Layout**: Redesigned the Mermaid architecture diagram in `README.md` into a vertically aligned 3-tier structure (External Transit Telemetry -> Host Python Server -> Kindle Paperwhite 5). Eliminates side-by-side subgraph crowding, crooked edges, and overlapping labels for a clean, readable presentation.

## [1.35.11] - 2026-10-10

### Added

- **Automated Mermaid Diagram Validation**: Added standalone validator `scripts/check_mermaid.py` and unit test suite `tests/test_docs_mermaid.py` ensuring all Markdown documentation diagrams (`README.md`, `docs/`) conform to Mermaid syntax rules (preventing unquoted delimiters in edge labels, unquoted nested delimiters in node labels, unclosed subgraphs, and mismatched blocks).
- **Mermaid Makefile & CI Quality Gates**: Added `make check-mermaid` target, integrated into `make lint`, and added automated validation step in `.github/workflows/ci.yml`.

### Fixed

- **README Architecture Overview Diagram**: Wrapped edge labels containing parentheses and special characters (`(Signed Binary & Manifest)`) in double quotes, fixing GitHub rich render parser error (`got 'PS'`).
- **Cross-Platform Test Font & Stress Resilience**: Updated font fallback paths in `tests/test_render_dashboard.py` and connection retry backlog handling in `tests/test_challenger_m2_harness.py` for macOS compatibility.

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

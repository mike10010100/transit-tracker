# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

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

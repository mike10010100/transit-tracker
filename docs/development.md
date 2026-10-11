# Developer Guide & Quality Verification

This guide outlines developer workflows, static analysis standards, testing procedures, release builds, and CI/CD pipelines for the Transit Tracker repository.

---

## 1. Prerequisites

- **Go**: 1.22+ (tested up to 1.26 on Linux and macOS).
- **Python**: 3.11+ (Python 3.11+ recommended and required for Docker builds).
- **Shell**: Bash 4+ and POSIX `/bin/sh`.
- **System Tools**: `make`, `curl`, `openssl`, `shellcheck`.

Install development Python dependencies:
```bash
pip install -r requirements-dev.txt
```

---

## 2. Makefile Automation Reference

The repository uses a unified [`Makefile`](file:///home/mike10010100/git/transit-tracker/Makefile) orchestrating tasks across all stacks.

| Target | Description | Underlying Tools |
|---|---|---|
| `make check` | Full verification pipeline. Runs formatting checks, linters, test suites, vulnerability audits, and coverage gates. | All verification tools |
| `make test` | Runs all test suites (Go, Python, Shell). | `go test`, `unittest`, `bash` |
| `make test-go` | Runs Go unit tests with the data race detector enabled. | `go test -v -race ./...` |
| `make test-py` | Runs Python unit tests. | `python -m unittest discover` |
| `make test-sh` | Runs the 10-tier Shell launcher integration test suite and version bump checks. | `tests/test_launcher.sh`, `tests/test_version_bump.sh` |
| `make lint` | Runs all static analysis tools across Go, Python, Shell, and documentation Mermaid diagrams. | `golangci-lint`, `ruff`, `mypy`, `shellcheck`, `check_mermaid.py` |
| `make lint-go` | Runs Go vet and golangci-lint. | `go vet`, `golangci-lint` |
| `make lint-py` | Runs Python type checking (Mypy) and linting (Ruff). | `mypy`, `ruff check` |
| `make check-sh` | Runs shell syntax checks and strict ShellCheck. | `bash -n`, `sh -n`, `shellcheck` |
| `make check-mermaid` | Validates Mermaid diagram syntax in Markdown documentation. | `scripts/check_mermaid.py` |
| `make fmt` | Formats all codebases in-place. | `gofmt -s -w`, `ruff format` |
| `make fmt-check` | Verifies formatting without altering files. | `gofmt -s -d`, `ruff format --check` |
| `make audit` | Runs dependency vulnerability auditing. | `govulncheck`, `pip-audit` |
| `make coverage` | Enforces coverage gates across Go (93%) and Python (92%). | `scripts/check_coverage_go.sh`, `coverage.py` |
| `make keygen` | Generates a new Ed25519 root release signing key. | `otasign keygen` |
| `make build` | Cross-compiles static ARMv7 binary, signs release manifest, and mints server cert. | `go build`, `otasign` |
| `make verify-release`| Validates manifest and binary signatures against public key. | `otasign verify` |
| `make clean` | Removes build artifacts, cached test databases, and temporary files. | `rm` |

---

## 3. Code Standards & Linters

### 3.1 Go Tooling
- **Formatting**: `gofmt -s` simplifies composite literals and slice operations.
- **Static Analysis**: Configured via [`.golangci.yml`](file:///home/mike10010100/git/transit-tracker/.golangci.yml):
  - `govet`: Standard compiler sanity analysis.
  - `errcheck`: Ensures checked error returns.
  - `staticcheck`: Advanced Go code analysis.
  - `revive`: Modern drop-in replacement for golint.
  - `gocritic`: Idiomatic micro-optimizations and style checks.
- **Race Safety**: Every test runs under `-race`. Zero data races are permitted.
- **Zero External Dependencies**: The client is built using 100% Go standard library. Do not import third-party packages into `client-go/`.

### 3.2 Python Tooling
- **Configuration**: All settings consolidated in [`pyproject.toml`](file:///home/mike10010100/git/transit-tracker/pyproject.toml).
- **Formatting**: `ruff format` enforces standard 88-character Black-compliant layout.
- **Linting**: `ruff check` evaluates Pyflakes (`F`), Pycodestyle (`E`/`W`), isort (`I`), pep8-naming (`N`), and flake8-bugbear (`B`).
- **Static Typing**: `mypy server` checks all 18 server modules with strict type analysis.

### 3.3 Shell Tooling
- **Strict POSIX Compliance**: Launcher script runs under `/bin/sh` using `set -eu` without non-portable bash extensions.
- **ShellCheck**: Configured via [`.shellcheckrc`](file:///home/mike10010100/git/transit-tracker/.shellcheckrc).

### 3.4 Docker Tooling
- **Hadolint**: Validates [`Dockerfile`](file:///home/mike10010100/git/transit-tracker/Dockerfile) in CI to ensure secure multi-stage build patterns, unprivileged users, and build cache optimization.

---

## 4. Test Suites & Coverage Gates

### 4.1 Go Coverage Gate (93%)
- Evaluated via `scripts/check_coverage_go.sh 93`.
- Generates a merged coverprofile across `client-go/`, `client-go/cmd/otasign`, and `client-go/internal/otasig`.
- Statement coverage status: **93.5%** (gate: **93%**).

### 4.2 Python Branch Coverage Gate (92%)
- Evaluated via `coverage.py` using `--fail-under=92`.
- Evaluates branch coverage across 414 unit tests in 18 source modules.
- Branch coverage status: **94.0%+** (gate: **92%**).

### 4.3 Shell Launcher Integration Tests
- Script: [`tests/test_launcher.sh`](file:///home/mike10010100/git/transit-tracker/tests/test_launcher.sh).
- Exercises 10 realistic integration scenarios using mock servers, temporary directory sandboxes, and fake ELF binaries:
  1. Tier 1 LAN discovery (mDNS hostname probe).
  2. Tier 2 LAN discovery (Gateway IP probe from `/proc/net/route`).
  3. Preconfigured URL bypass without discovery latency.
  4. Tier 3 LAN discovery (parallel `/24` subnet sweep).
  5. Corrupted / non-ELF payload rejection.
  6. 4-byte pseudo-ELF header rejection and recovery.
  7. Premature persistence rollback on download failure.
  8. Strict service verification rejecting foreign IoT services.
  9. Binary download rejection on SHA-256 checksum mismatch.
  10. Binary download rejection when release manifest is missing.

---

## 5. Release Builds & Cryptographic Signing

### 5.1 Generating a Production Key
Run once to initialize your private signing key:
```bash
make keygen
```
This generates `secrets/ota_ed25519.key` with permissions `0600`. **Never commit this file to git.**

### 5.2 Compiling a Signed Release
```bash
make build
```
This performs the complete release pipeline:
1. Cross-compiles `client-go` for `linux/arm/v7` with embedded version and public key.
2. Generates `tracker-arm.manifest.json` containing SHA-256 and Ed25519 signature.
3. Mints `server_identity.cert.json` and `server_identity.key`.
4. Stages binaries atomically to ensure zero downtime.

### 5.3 Verifying the Release
```bash
make verify-release
```

### 5.4 Development Builds (Unsigned)
For local testing when no signing key is available:
```bash
ALLOW_UNSIGNED=1 make build
```

---

## 6. Pre-Commit Hooks & Continuous Integration

### 6.1 Pre-Commit Hooks
Install pre-commit hooks locally to catch issues prior to committing:
```bash
pre-commit install
```
Configured in [`.pre-commit-config.yaml`](file:///home/mike10010100/git/transit-tracker/.pre-commit-config.yaml).

### 6.2 GitHub Actions CI
The CI workflow in [`.github/workflows/ci.yml`](file:///home/mike10010100/git/transit-tracker/.github/workflows/ci.yml):
- Enforces workflow concurrency (`cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}`).
- Uses pinned immutable action SHAs.
- Runs 5 validation jobs:
  1. `version-bump-check`: Enforces SemVer bump in `VERSION` and changelog entry on all PRs.
  2. `go-client`: gofmt, go vet, golangci-lint, race tests, 93% coverage gate, signed build.
  3. `shell`: check-sh, test-sh (10 integration tests + version bump unit tests).
  4. `python-server`: ruff check, ruff format, mypy, pip-audit, unit tests, 92% coverage gate.
  5. `docker`: hadolint, signed build with BuildKit secret, container smoke test.
- Automatically creates GitHub Releases and tags (`vX.Y.Z`) on merge to `main`.

---

## 7. Release Lifecycle & Continuous Deployment

### 7.1 Semantic Versioning & CHANGELOG
- All changes must bump [`VERSION`](file:///home/mike10010100/git/transit-tracker/VERSION) per [SemVer 2.0.0](https://semver.org/).
- Changes must be documented in [`CHANGELOG.md`](file:///home/mike10010100/git/transit-tracker/CHANGELOG.md) under `## [X.Y.Z] - YYYY-MM-DD`.
- Verified in CI on every PR via [`scripts/check_version_bump.sh`](file:///home/mike10010100/git/transit-tracker/scripts/check_version_bump.sh).

### 7.2 Branch Protection on `main`
- Direct pushes to `main` are restricted. All modifications require a pull request.
- Status checks must pass cleanly (`strict: true`).
- Linear history is enforced (squash or rebase; no merge bubbles).

### 7.3 Automated Host Deployment
- Local deployments use [`scripts/deploy.sh`](file:///home/mike10010100/git/transit-tracker/scripts/deploy.sh) (or `make deploy`).
- On server boot, `/home/mike10010100/startup.sh` automatically pulls the latest `main` branch, builds the versioned image, and re-launches the container stack.

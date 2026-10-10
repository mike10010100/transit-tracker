#!/bin/bash
# Integration test for client-go/launcher/TransitTracker.sh
# Tests Requirement R4: Automated First-Run Launcher Discovery, Persistence, and Bootstrap

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$REPO_ROOT/client-go/launcher/TransitTracker.sh"

if [ ! -f "$LAUNCHER" ]; then
    echo "ERROR: Launcher script not found at $LAUNCHER" >&2
    exit 1
fi

TEST_TMP="$(mktemp -d /tmp/test_launcher_XXXXXX)"
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_TMP"
}
trap cleanup EXIT INT TERM

echo "==> Building test dummy ELF binary..."
DUMMY_GO="$TEST_TMP/dummy.go"
DUMMY_BIN="$TEST_TMP/dummy_bin"
cat << 'EOF' > "$DUMMY_GO"
package main

import (
	"fmt"
	"os"
	"strings"
)

func main() {
	if marker := os.Getenv("TEST_LAUNCHER_SENTINEL"); marker != "" {
		_ = os.WriteFile(marker, []byte(strings.Join(os.Args, " ")), 0644)
	}
	fmt.Println("dummy binary executed successfully with args:", strings.Join(os.Args, " "))
	os.Exit(0)
}
EOF

(cd "$TEST_TMP" && go build -o "$DUMMY_BIN" "$DUMMY_GO")

if [ ! -x "$DUMMY_BIN" ]; then
    echo "ERROR: Failed to build dummy binary" >&2
    exit 1
fi

# Verify dummy binary is an ELF binary
MAGIC="$(head -c 4 "$DUMMY_BIN")"
if [ "$MAGIC" != "$(printf '\177ELF')" ]; then
    echo "ERROR: Dummy binary is not an ELF binary" >&2
    exit 1
fi

echo "==> Selecting open port and starting mock HTTP server..."
MOCK_SERVER_PY="$TEST_TMP/mock_server.py"
READY_FILE="$TEST_TMP/server.ready"
cat << 'EOF' > "$MOCK_SERVER_PY"
import http.server
import socket
import socketserver
import sys

import hashlib

binary_path = sys.argv[1]
ready_file = sys.argv[2]
mode = sys.argv[3] if len(sys.argv) > 3 else "normal"

if mode == "--bad-binary":
    bin_data = b"NOT_AN_ELF_FILE_CORRUPT"
elif mode == "--pseudo-elf":
    bin_data = b"\x7fELF"
else:
    with open(binary_path, "rb") as f:
        bin_data = f.read()

real_sha256 = hashlib.sha256(bin_data).hexdigest()
if mode == "--bad-sha":
    manifest_sha = "0000000000000000000000000000000000000000000000000000000000000000"
else:
    manifest_sha = real_sha256

class MockHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if mode == "--foreign-iot":
            if self.path in ("/healthz", "/health"):
                body = b'{"version": "2.4.1", "name": "nginx"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
            return

        if self.path in ("/healthz", "/health"):
            body = b'{"status": "ok", "version": "1.0.0-test", "stopped": false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/identity":
            body = b'{"service": "transit-tracker", "version": "1.0.0-test"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/tracker-arm.manifest":
            if mode in ("--missing-arm", "--missing-manifest"):
                self.send_response(404)
                self.end_headers()
                return
            m_body = f'{{"format":"transit-tracker-ota-v1","version":"1.0.0-test","sha256":"{manifest_sha}","size":{len(bin_data)}}}'.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(m_body)))
            self.end_headers()
            self.wfile.write(m_body)
        elif self.path == "/tracker-arm":
            if mode == "--missing-arm":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(bin_data)))
            self.end_headers()
            self.wfile.write(bin_data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

# Find open port
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('', 0))
port = s.getsockname()[1]
s.close()

server = ReusableTCPServer(("0.0.0.0", port), MockHandler)
with open(ready_file, "w") as f:
    f.write(str(port) + "\n")

server.serve_forever()
EOF

python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE" &
SERVER_PID=$!

# Wait for server ready
for _ in $(seq 1 30); do
    if [ -s "$READY_FILE" ]; then
        break
    fi
    sleep 0.1
done

if [ ! -s "$READY_FILE" ]; then
    echo "ERROR: Mock server failed to start" >&2
    exit 1
fi

MOCK_PORT="$(tr -d '[:space:]' < "$READY_FILE")"
echo "Mock server listening on port $MOCK_PORT (PID $SERVER_PID)"

# Verify mock server healthz
HEALTHZ_RESP="$(curl -fs "http://127.0.0.1:${MOCK_PORT}/healthz")"
if ! echo "$HEALTHZ_RESP" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    echo "ERROR: Mock server healthz invalid: $HEALTHZ_RESP" >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# TEST 1: Tier 1 LAN discovery (mDNS probe)
# -----------------------------------------------------------------------------
echo "==> Test 1: Tier 1 LAN discovery (mDNS hostname probe)..."
TEST1_DIR="$TEST_TMP/test1"
mkdir -p "$TEST1_DIR"
CONFIG_FILE1="$TEST1_DIR/tracker_server.txt"
FALLBACK_FILE1="$TEST1_DIR/fallback_tracker_server.txt"
BIN_TARGET1="$TEST1_DIR/tracker-arm"
BACKUP_TARGET1="$TEST1_DIR/tracker_backup"
LOG_TARGET1="$TEST1_DIR/bootstrap.log"
SENTINEL_FILE1="$TEST1_DIR/sentinel.txt"

(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE1"
    export FALLBACK_CONFIG="$FALLBACK_FILE1"
    export BINARY="$BIN_TARGET1"
    export BACKUP="$BACKUP_TARGET1"
    export LOG_FILE="$LOG_TARGET1"
    export SERVER_PORT="$MOCK_PORT"
    export MDNS_HOST="127.0.0.1"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE1"

    sh "$LAUNCHER"
)

if [ ! -f "$CONFIG_FILE1" ]; then
    echo "ERROR: tracker_server.txt not created in Test 1" >&2
    exit 1
fi
PERSISTED_URL1="$(tr -d '[:space:]' < "$CONFIG_FILE1")"
echo "  [PASS] Tier 1 discovered and persisted: $PERSISTED_URL1"

# -----------------------------------------------------------------------------
# TEST 2: Tier 2 Cascading Discovery (Default Route Gateway Hex IP)
# -----------------------------------------------------------------------------
echo "==> Test 2: Tier 2 LAN discovery (Gateway IP probe from /proc/net/route)..."
TEST2_DIR="$TEST_TMP/test2"
mkdir -p "$TEST2_DIR"

# Create mock /proc/net/route with 127.0.0.1 gateway (hex: 0100007F)
MOCK_ROUTE="$TEST2_DIR/mock_proc_route"
MOCK_ARP="$TEST2_DIR/mock_proc_arp"
cat << 'EOF' > "$MOCK_ROUTE"
Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
lo	00000000	0100007F	0003	0	0	100	00000000	0	0	0
EOF
touch "$MOCK_ARP"

SENTINEL_FILE="$TEST2_DIR/sentinel.txt"
CONFIG_FILE="$TEST2_DIR/tracker_server.txt"
FALLBACK_FILE="$TEST2_DIR/fallback_tracker_server.txt"
BIN_TARGET="$TEST2_DIR/tracker-arm"
BACKUP_TARGET="$TEST2_DIR/tracker_backup"
LOG_TARGET="$TEST2_DIR/bootstrap.log"

# Verify config does NOT exist initially
if [ -f "$CONFIG_FILE" ]; then
    echo "ERROR: Config file unexpectedly exists before test" >&2
    exit 1
fi

# Run launcher under test in clean subshell (disable Tier 1 mDNS so Tier 2 triggers)
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE"
    export FALLBACK_CONFIG="$FALLBACK_FILE"
    export BINARY="$BIN_TARGET"
    export BACKUP="$BACKUP_TARGET"
    export LOG_FILE="$LOG_TARGET"
    export SERVER_PORT="$MOCK_PORT"
    export MDNS_HOST="unreachable.invalid.local"
    export PROC_NET_ROUTE="$MOCK_ROUTE"
    export PROC_NET_ARP="$MOCK_ARP"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE"

    sh "$LAUNCHER"
)

# 1. Verify dynamic discovery persisted URL into tracker_server.txt
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: tracker_server.txt was not created" >&2
    cat "$LOG_TARGET" 2>/dev/null || true
    exit 1
fi

PERSISTED_URL="$(tr -d '[:space:]' < "$CONFIG_FILE")"
EXPECTED_URL="http://127.0.0.1:${MOCK_PORT}"
if [ "$PERSISTED_URL" != "$EXPECTED_URL" ]; then
    echo "ERROR: Expected persisted URL '$EXPECTED_URL', got '$PERSISTED_URL'" >&2
    exit 1
fi
echo "  [PASS] Discovered and persisted server URL: $PERSISTED_URL"

# 2. Verify fallback tracker_server.txt also persisted
if [ ! -f "$FALLBACK_FILE" ]; then
    echo "ERROR: Fallback config was not created" >&2
    exit 1
fi
echo "  [PASS] Fallback config persisted"

# 3. Verify binary was downloaded, has valid ELF header, and is executable
if [ ! -x "$BIN_TARGET" ]; then
    echo "ERROR: Downloaded binary is not executable at $BIN_TARGET" >&2
    exit 1
fi

MAGIC="$(head -c 4 "$BIN_TARGET")"
if [ "$MAGIC" != "$(printf '\177ELF')" ]; then
    echo "ERROR: Downloaded binary failed ELF header check" >&2
    exit 1
fi
echo "  [PASS] Binary downloaded and verified ELF header"

# 4. Verify backup copy was created
if [ ! -f "$BACKUP_TARGET" ]; then
    echo "ERROR: Backup was not created at $BACKUP_TARGET" >&2
    exit 1
fi
echo "  [PASS] Binary backup created"

# 5. Verify binary was executed with proper flags (-server and -launcher)
if [ ! -f "$SENTINEL_FILE" ]; then
    echo "ERROR: Dummy binary was not executed (sentinel missing)" >&2
    exit 1
fi

SENTINEL_CONTENT="$(cat "$SENTINEL_FILE")"
if ! echo "$SENTINEL_CONTENT" | grep -q -- "-server $EXPECTED_URL"; then
    echo "ERROR: Binary not invoked with -server: $SENTINEL_CONTENT" >&2
    exit 1
fi
if ! echo "$SENTINEL_CONTENT" | grep -q -- "-launcher $LAUNCHER"; then
    echo "ERROR: Binary not invoked with -launcher: $SENTINEL_CONTENT" >&2
    exit 1
fi
echo "  [PASS] Binary executed with correct arguments: $SENTINEL_CONTENT"

# -----------------------------------------------------------------------------
# TEST 3: Skip Discovery when tracker_server.txt already exists
# -----------------------------------------------------------------------------
echo "==> Test 3: Skip discovery when tracker_server.txt already contains valid URL..."
TEST3_DIR="$TEST_TMP/test3"
mkdir -p "$TEST3_DIR"
CONFIG_FILE3="$TEST3_DIR/tracker_server.txt"
echo "http://127.0.0.1:${MOCK_PORT}" > "$CONFIG_FILE3"
SENTINEL_FILE3="$TEST3_DIR/sentinel.txt"
BIN_TARGET3="$TEST3_DIR/tracker-arm"
BACKUP_TARGET3="$TEST3_DIR/tracker_backup"
LOG_TARGET3="$TEST3_DIR/bootstrap.log"

# Point PROC_NET_ROUTE to empty file so discovery would fail if attempted
EMPTY_ROUTE="$TEST3_DIR/empty_route"
touch "$EMPTY_ROUTE"

(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE3"
    export FALLBACK_CONFIG="$TEST3_DIR/fallback.txt"
    export BINARY="$BIN_TARGET3"
    export BACKUP="$BACKUP_TARGET3"
    export LOG_FILE="$LOG_TARGET3"
    export SERVER_PORT="$MOCK_PORT"
    export MDNS_HOST="unreachable.invalid.local"
    export PROC_NET_ROUTE="$EMPTY_ROUTE"
    export PROC_NET_ARP="$EMPTY_ROUTE"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE3"

    sh "$LAUNCHER"
)

if [ ! -f "$SENTINEL_FILE3" ]; then
    echo "ERROR: Test 3 failed to execute binary" >&2
    exit 1
fi
echo "  [PASS] Preconfigured URL used without executing discovery"

# -----------------------------------------------------------------------------
# TEST 4: Tier 3 Subnet Sweep Discovery
# -----------------------------------------------------------------------------
echo "==> Test 4: Tier 3 LAN discovery (parallel /24 subnet sweep)..."
TEST4_DIR="$TEST_TMP/test4"
mkdir -p "$TEST4_DIR"
CONFIG_FILE4="$TEST4_DIR/tracker_server.txt"
SENTINEL_FILE4="$TEST4_DIR/sentinel.txt"
BIN_TARGET4="$TEST4_DIR/tracker-arm"
BACKUP_TARGET4="$TEST4_DIR/tracker_backup"
LOG_TARGET4="$TEST4_DIR/bootstrap.log"

# Route with no default gateway
NO_GW_ROUTE="$TEST4_DIR/no_gw_route"
cat << 'EOF' > "$NO_GW_ROUTE"
Iface	Destination	Gateway 	Flags	RefCnt	Use	Metric	Mask		MTU	Window	IRTT
EOF

(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE4"
    export FALLBACK_CONFIG="$TEST4_DIR/fallback.txt"
    export BINARY="$BIN_TARGET4"
    export BACKUP="$BACKUP_TARGET4"
    export LOG_FILE="$LOG_TARGET4"
    export SERVER_PORT="$MOCK_PORT"
    export MDNS_HOST="unreachable.invalid.local"
    export PROC_NET_ROUTE="$NO_GW_ROUTE"
    export PROC_NET_ARP="$NO_GW_ROUTE"
    export DISCOVERY_SUBNET="127.0.0"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE4"

    sh "$LAUNCHER"
)

if [ ! -f "$CONFIG_FILE4" ]; then
    echo "ERROR: Test 4 failed: tracker_server.txt not created via subnet sweep" >&2
    exit 1
fi
echo "  [PASS] Server discovered via /24 subnet sweep and persisted"

# -----------------------------------------------------------------------------
# TEST 5: Corrupt binary rejection (ELF verification failure)
# -----------------------------------------------------------------------------
echo "==> Test 5: Reject corrupt / non-ELF download..."
# Stop current server and start mock server with corrupt binary
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE5="$TEST_TMP/server5.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE5" --bad-binary &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE5" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT5="$(tr -d '[:space:]' < "$READY_FILE5")"

TEST5_DIR="$TEST_TMP/test5"
mkdir -p "$TEST5_DIR"
CONFIG_FILE5="$TEST5_DIR/tracker_server.txt"
echo "http://127.0.0.1:${MOCK_PORT5}" > "$CONFIG_FILE5"
SENTINEL_FILE5="$TEST5_DIR/sentinel.txt"
BIN_TARGET5="$TEST5_DIR/tracker-arm"
BACKUP_TARGET5="$TEST5_DIR/tracker_backup"
LOG_TARGET5="$TEST5_DIR/bootstrap.log"

set +e
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE5"
    export FALLBACK_CONFIG="$TEST5_DIR/fallback.txt"
    export BINARY="$BIN_TARGET5"
    export BACKUP="$BACKUP_TARGET5"
    export LOG_FILE="$LOG_TARGET5"
    export SERVER_PORT="$MOCK_PORT5"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE5"

    sh "$LAUNCHER"
)
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "ERROR: Launcher unexpectedly exited with 0 when binary was non-ELF" >&2
    exit 1
fi
if [ -f "$SENTINEL_FILE5" ]; then
    echo "ERROR: Corrupt binary was unexpectedly executed" >&2
    exit 1
fi
echo "  [PASS] Corrupt non-ELF download rejected"

# -----------------------------------------------------------------------------
# TEST 6: Reject pseudo-ELF (4-byte magic only) and recover on next run
# -----------------------------------------------------------------------------
echo "==> Test 6: Reject 4-byte pseudo-ELF and recover on next run..."
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE6="$TEST_TMP/server6.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE6" --pseudo-elf &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE6" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT6="$(tr -d '[:space:]' < "$READY_FILE6")"

TEST6_DIR="$TEST_TMP/test6"
mkdir -p "$TEST6_DIR"
CONFIG_FILE6="$TEST6_DIR/tracker_server.txt"
SENTINEL_FILE6="$TEST6_DIR/sentinel.txt"
BIN_TARGET6="$TEST6_DIR/tracker-arm"
BACKUP_TARGET6="$TEST6_DIR/tracker_backup"
LOG_TARGET6="$TEST6_DIR/bootstrap.log"

set +e
(
    export SERVER="http://127.0.0.1:${MOCK_PORT6}"
    export SERVER_CONFIG="$CONFIG_FILE6"
    export FALLBACK_CONFIG="$TEST6_DIR/fallback.txt"
    export BINARY="$BIN_TARGET6"
    export BACKUP="$BACKUP_TARGET6"
    export LOG_FILE="$LOG_TARGET6"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE6"

    sh "$LAUNCHER"
)
EXIT_CODE6_RUN1=$?
set -e

if [ "$EXIT_CODE6_RUN1" -eq 0 ]; then
    echo "ERROR: Run 1 unexpectedly succeeded on 4-byte pseudo-ELF" >&2
    exit 1
fi
if [ -f "$BIN_TARGET6" ]; then
    echo "ERROR: 4-byte corrupt pseudo-ELF was placed into $BIN_TARGET6" >&2
    exit 1
fi
if [ -f "$BACKUP_TARGET6" ]; then
    echo "ERROR: 4-byte corrupt pseudo-ELF was backed up to $BACKUP_TARGET6" >&2
    exit 1
fi

# Now replace server with valid binary and verify Run 2 recovers cleanly
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE6_FIX="$TEST_TMP/server6_fix.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE6_FIX" normal &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE6_FIX" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT6_FIX="$(tr -d '[:space:]' < "$READY_FILE6_FIX")"

(
    export SERVER="http://127.0.0.1:${MOCK_PORT6_FIX}"
    export SERVER_CONFIG="$CONFIG_FILE6"
    export FALLBACK_CONFIG="$TEST6_DIR/fallback.txt"
    export BINARY="$BIN_TARGET6"
    export BACKUP="$BACKUP_TARGET6"
    export LOG_FILE="$LOG_TARGET6"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE6"

    sh "$LAUNCHER"
)

if [ ! -f "$SENTINEL_FILE6" ]; then
    echo "ERROR: System failed to recover on Run 2 after pseudo-ELF" >&2
    exit 1
fi
echo "  [PASS] 4-byte pseudo-ELF rejected and system recovered"

# -----------------------------------------------------------------------------
# TEST 7: Prevent premature persistence when binary download fails
# -----------------------------------------------------------------------------
echo "==> Test 7: Prevent premature persistence when binary download fails..."
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE7="$TEST_TMP/server7.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE7" --missing-arm &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE7" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT7="$(tr -d '[:space:]' < "$READY_FILE7")"

TEST7_DIR="$TEST_TMP/test7"
mkdir -p "$TEST7_DIR"
CONFIG_FILE7="$TEST7_DIR/tracker_server.txt"
SENTINEL_FILE7="$TEST7_DIR/sentinel.txt"
BIN_TARGET7="$TEST7_DIR/tracker-arm"
BACKUP_TARGET7="$TEST7_DIR/tracker_backup"
LOG_TARGET7="$TEST7_DIR/bootstrap.log"

set +e
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE7"
    export FALLBACK_CONFIG="$TEST7_DIR/fallback.txt"
    export BINARY="$BIN_TARGET7"
    export BACKUP="$BACKUP_TARGET7"
    export LOG_FILE="$LOG_TARGET7"
    export SERVER_PORT="$MOCK_PORT7"
    export MDNS_HOST="127.0.0.1"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE7"

    sh "$LAUNCHER"
)
EXIT_CODE7_RUN1=$?
set -e

if [ "$EXIT_CODE7_RUN1" -eq 0 ]; then
    echo "ERROR: Run 1 unexpectedly succeeded when tracker-arm was missing" >&2
    exit 1
fi
if [ -f "$CONFIG_FILE7" ]; then
    echo "ERROR: Premature persistence bug: tracker_server.txt created before binary validation!" >&2
    exit 1
fi

# Now replace server with healthy server and verify Run 2 discovers and bootstraps
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE7_FIX="$TEST_TMP/server7_fix.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE7_FIX" normal &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE7_FIX" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT7_FIX="$(tr -d '[:space:]' < "$READY_FILE7_FIX")"

(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE7"
    export FALLBACK_CONFIG="$TEST7_DIR/fallback.txt"
    export BINARY="$BIN_TARGET7"
    export BACKUP="$BACKUP_TARGET7"
    export LOG_FILE="$LOG_TARGET7"
    export SERVER_PORT="$MOCK_PORT7_FIX"
    export MDNS_HOST="127.0.0.1"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE7"

    sh "$LAUNCHER"
)

if [ ! -f "$CONFIG_FILE7" ]; then
    echo "ERROR: Run 2 failed to persist discovered server configuration" >&2
    exit 1
fi
if [ ! -f "$SENTINEL_FILE7" ]; then
    echo "ERROR: Run 2 failed to execute dummy binary" >&2
    exit 1
fi
echo "  [PASS] Premature persistence prevented and discovery recovered"

# -----------------------------------------------------------------------------
# TEST 8: Strict service verification (foreign IoT devices ignored)
# -----------------------------------------------------------------------------
echo "==> Test 8: Strict service verification ignores third-party IoT services..."
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE8="$TEST_TMP/server8.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE8" --foreign-iot &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE8" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT8="$(tr -d '[:space:]' < "$READY_FILE8")"

TEST8_DIR="$TEST_TMP/test8"
mkdir -p "$TEST8_DIR"
CONFIG_FILE8="$TEST8_DIR/tracker_server.txt"
SENTINEL_FILE8="$TEST8_DIR/sentinel.txt"
BIN_TARGET8="$TEST8_DIR/tracker-arm"
BACKUP_TARGET8="$TEST8_DIR/tracker_backup"
LOG_TARGET8="$TEST8_DIR/bootstrap.log"

EMPTY_ROUTE8="$TEST8_DIR/empty_route"
touch "$EMPTY_ROUTE8"

set +e
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE8"
    export FALLBACK_CONFIG="$TEST8_DIR/fallback.txt"
    export BINARY="$BIN_TARGET8"
    export BACKUP="$BACKUP_TARGET8"
    export LOG_FILE="$LOG_TARGET8"
    export SERVER_PORT="$MOCK_PORT8"
    export MDNS_HOST="127.0.0.1"
    export PROC_NET_ROUTE="$EMPTY_ROUTE8"
    export PROC_NET_ARP="$EMPTY_ROUTE8"
    export DISCOVERY_SUBNET="192.0.2"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE8"

    sh "$LAUNCHER"
)
EXIT_CODE8=$?
set -e

if [ "$EXIT_CODE8" -eq 0 ]; then
    echo "ERROR: Launcher unexpectedly succeeded against foreign IoT device" >&2
    exit 1
fi
if [ -f "$CONFIG_FILE8" ]; then
    echo "ERROR: Foreign IoT device was falsely persisted to tracker_server.txt" >&2
    exit 1
fi
echo "  [PASS] Foreign IoT device rejected by strict service verification"

# -----------------------------------------------------------------------------
# TEST 9: Reject binary with SHA-256 checksum mismatch
# -----------------------------------------------------------------------------
echo "==> Test 9: Reject binary with SHA-256 checksum mismatch..."
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE9="$TEST_TMP/server9.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE9" --bad-sha &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE9" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT9="$(tr -d '[:space:]' < "$READY_FILE9")"

TEST9_DIR="$TEST_TMP/test9"
mkdir -p "$TEST9_DIR"
CONFIG_FILE9="$TEST9_DIR/tracker_server.txt"
SENTINEL_FILE9="$TEST9_DIR/sentinel.txt"
BIN_TARGET9="$TEST9_DIR/tracker-arm"
BACKUP_TARGET9="$TEST9_DIR/tracker_backup"
LOG_TARGET9="$TEST9_DIR/bootstrap.log"

set +e
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE9"
    export FALLBACK_CONFIG="$TEST9_DIR/fallback.txt"
    export BINARY="$BIN_TARGET9"
    export BACKUP="$BACKUP_TARGET9"
    export LOG_FILE="$LOG_TARGET9"
    export SERVER_PORT="$MOCK_PORT9"
    export MDNS_HOST="127.0.0.1"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE9"

    sh "$LAUNCHER"
)
EXIT_CODE9=$?
set -e

if [ "$EXIT_CODE9" -eq 0 ]; then
    echo "ERROR: Launcher unexpectedly succeeded when binary had SHA-256 mismatch" >&2
    exit 1
fi
if [ -f "$BIN_TARGET9" ]; then
    echo "ERROR: Binary with invalid SHA-256 was installed to $BIN_TARGET9" >&2
    exit 1
fi
if [ -f "$SENTINEL_FILE9" ]; then
    echo "ERROR: Binary with invalid SHA-256 was executed" >&2
    exit 1
fi
echo "  [PASS] Download with SHA-256 mismatch rejected cleanly"

# -----------------------------------------------------------------------------
# TEST 10: Reject binary when release manifest is missing
# -----------------------------------------------------------------------------
echo "==> Test 10: Reject binary when release manifest is missing..."
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

READY_FILE10="$TEST_TMP/server10.ready"
python3 "$MOCK_SERVER_PY" "$DUMMY_BIN" "$READY_FILE10" --missing-manifest &
SERVER_PID=$!

for _ in $(seq 1 30); do
    if [ -s "$READY_FILE10" ]; then
        break
    fi
    sleep 0.1
done
MOCK_PORT10="$(tr -d '[:space:]' < "$READY_FILE10")"

TEST10_DIR="$TEST_TMP/test10"
mkdir -p "$TEST10_DIR"
CONFIG_FILE10="$TEST10_DIR/tracker_server.txt"
SENTINEL_FILE10="$TEST10_DIR/sentinel.txt"
BIN_TARGET10="$TEST10_DIR/tracker-arm"
BACKUP_TARGET10="$TEST10_DIR/tracker_backup"
LOG_TARGET10="$TEST10_DIR/bootstrap.log"

set +e
(
    export SERVER=""
    export SERVER_CONFIG="$CONFIG_FILE10"
    export FALLBACK_CONFIG="$TEST10_DIR/fallback.txt"
    export BINARY="$BIN_TARGET10"
    export BACKUP="$BACKUP_TARGET10"
    export LOG_FILE="$LOG_TARGET10"
    export SERVER_PORT="$MOCK_PORT10"
    export MDNS_HOST="127.0.0.1"
    export TEST_LAUNCHER_SENTINEL="$SENTINEL_FILE10"

    sh "$LAUNCHER"
)
EXIT_CODE10=$?
set -e

if [ "$EXIT_CODE10" -eq 0 ]; then
    echo "ERROR: Launcher unexpectedly succeeded when manifest was missing" >&2
    exit 1
fi
if [ -f "$BIN_TARGET10" ]; then
    echo "ERROR: Binary was installed despite missing manifest" >&2
    exit 1
fi
echo "  [PASS] Download with missing manifest rejected cleanly"

echo "==> All launcher integration tests passed cleanly!"

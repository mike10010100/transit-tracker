#!/bin/sh
# Name: Transit Tracker
# Author: Antigravity
# Permanent OTA Bootstrap Launcher - Launches native Go binary

set -eu

LOG_FILE="${LOG_FILE:-/tmp/tracker_bootstrap.log}"
exec 2>>"$LOG_FILE" || true

SERVER="${SERVER:-}"
SERVER_CONFIG="${SERVER_CONFIG:-/mnt/us/documents/tracker_server.txt}"
FALLBACK_CONFIG="${FALLBACK_CONFIG:-/tmp/tracker_server.txt}"
BINARY="${BINARY:-/tmp/tracker-arm}"
BACKUP="${BACKUP:-/mnt/us/documents/tracker_backup}"
DL_TMP="${BINARY}.dl.$$"
MANIFEST_TMP="${BINARY}.manifest.$$"
PORT="${SERVER_PORT:-${PORT:-8000}}"
PROC_ROUTE="${PROC_NET_ROUTE:-/proc/net/route}"
PROC_ARP="${PROC_NET_ARP:-/proc/net/arp}"
MDNS_HOST="${MDNS_HOST:-transittracker.local}"

# shellcheck disable=SC2317,SC2329
cleanup() {
    rm -f "$DL_TMP" "$MANIFEST_TMP" /tmp/.sweep_found_$$* 2>/dev/null || true
}
trap cleanup EXIT INT TERM

is_elf() {
    target_file="$1"
    [ -f "$target_file" ] || return 1
    size="$(wc -c < "$target_file" 2>/dev/null || echo 0)"
    size="$(echo "$size" | tr -d '[:space:]')"
    [ -n "$size" ] || size=0
    [ "$size" -ge 1000 ] 2>/dev/null || return 1
    magic="$(head -c 4 "$target_file" 2>/dev/null || dd if="$target_file" bs=4 count=1 2>/dev/null || true)"
    [ "$magic" = "$(printf '\177ELF')" ]
}

verify_checksum() {
    target_file="$1"
    expected_sha="$2"
    [ -f "$target_file" ] || return 1
    [ -n "$expected_sha" ] || return 1

    actual_sha=""
    if command -v sha256sum >/dev/null 2>&1; then
        actual_sha="$(sha256sum "$target_file" 2>/dev/null | awk '{print $1}')"
    elif command -v openssl >/dev/null 2>&1; then
        actual_sha="$(openssl dgst -sha256 "$target_file" 2>/dev/null | awk '{print $NF}')"
    fi
    actual_sha="$(echo "$actual_sha" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    expected_sha="$(echo "$expected_sha" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

    [ -n "$actual_sha" ] && [ "$actual_sha" = "$expected_sha" ]
}

verify_server() {
    cand_url="$1"
    [ -n "$cand_url" ] || return 1

    case "$cand_url" in
        http://*|https://*) ;;
        *) cand_url="http://$cand_url" ;;
    esac
    cand_url="${cand_url%/}"

    # 1. Primary probe: /identity (strict transit-tracker service identifier)
    res_id="$(curl -fs -m 1 "$cand_url/identity" 2>/dev/null || true)"
    if [ -n "$res_id" ]; then
        if echo "$res_id" | grep -q '"service"[[:space:]]*:[[:space:]]*"transit-tracker"'; then
            return 0
        fi
    fi

    # 2. Secondary probe: /healthz
    res_hz="$(curl -fs -m 1 "$cand_url/healthz" 2>/dev/null || true)"
    if [ -n "$res_hz" ]; then
        # If an explicit service field is returned, it must be transit-tracker
        if echo "$res_hz" | grep -q '"service"'; then
            if echo "$res_hz" | grep -q '"service"[[:space:]]*:[[:space:]]*"transit-tracker"'; then
                return 0
            fi
            return 1
        fi
        # Transit tracker health signature: status ok combined with version
        if echo "$res_hz" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' && \
           echo "$res_hz" | grep -q '"version"'; then
            return 0
        fi
    fi

    return 1
}

wait_for_network() {
    attempts=0
    while [ "$attempts" -lt 15 ]; do
        if [ -f "$PROC_ROUTE" ] && grep -qv '^[[:space:]]*Iface' "$PROC_ROUTE" 2>/dev/null; then
            return 0
        fi
        if command -v ifconfig >/dev/null 2>&1; then
            if ifconfig 2>/dev/null | grep -q 'inet '; then
                return 0
            fi
        fi
        sleep 1
        attempts=$((attempts + 1))
    done
    return 0
}

parse_hex_ip() {
    hex="$1"
    [ -n "$hex" ] || return 1
    # Hex string AABBCCDD in little-endian byte order -> DD.CC.BB.AA
    b1="$(printf "%d" "0x$(echo "$hex" | cut -c7-8)" 2>/dev/null || true)"
    b2="$(printf "%d" "0x$(echo "$hex" | cut -c5-6)" 2>/dev/null || true)"
    b3="$(printf "%d" "0x$(echo "$hex" | cut -c3-4)" 2>/dev/null || true)"
    b4="$(printf "%d" "0x$(echo "$hex" | cut -c1-2)" 2>/dev/null || true)"
    if [ -n "$b1" ] && [ -n "$b2" ] && [ -n "$b3" ] && [ -n "$b4" ]; then
        echo "$b1.$b2.$b3.$b4"
        return 0
    fi
    return 1
}

find_active_subnet_prefix() {
    if [ -n "${DISCOVERY_SUBNET:-}" ]; then
        echo "${DISCOVERY_SUBNET%.}"
        return 0
    fi

    # Try non-default route destination network from route table
    if [ -f "$PROC_ROUTE" ]; then
        net_hex="$(awk '$2 != "00000000" && $3 == "00000000" { print $2; exit }' "$PROC_ROUTE" 2>/dev/null || true)"
        if [ -n "$net_hex" ]; then
            net_ip="$(parse_hex_ip "$net_hex" 2>/dev/null || true)"
            if [ -n "$net_ip" ] && [ "$net_ip" != "0.0.0.0" ]; then
                echo "$net_ip" | cut -d. -f1-3
                return 0
            fi
        fi
    fi

    # Try default route interface IP via ifconfig
    if [ -f "$PROC_ROUTE" ] && command -v ifconfig >/dev/null 2>&1; then
        iface="$(awk '$2 == "00000000" { print $1; exit }' "$PROC_ROUTE" 2>/dev/null || true)"
        if [ -n "$iface" ]; then
            iface_ip="$(ifconfig "$iface" 2>/dev/null | awk '/inet / { for (i=1; i<=NF; i++) { if ($i ~ /^addr:/) { sub(/^addr:/, "", $i); print $i; exit; } if ($i == "inet" && $(i+1) !~ /^addr:/) { print $(i+1); exit; } } }')"
            if [ -n "$iface_ip" ] && [ "$iface_ip" != "127.0.0.1" ]; then
                echo "$iface_ip" | cut -d. -f1-3
                return 0
            fi
        fi
    fi

    # Try any non-loopback interface from ifconfig
    if command -v ifconfig >/dev/null 2>&1; then
        any_ip="$(ifconfig 2>/dev/null | awk '/inet / { for (i=1; i<=NF; i++) { if ($i ~ /^addr:/) { sub(/^addr:/, "", $i); if ($i !~ /^127\./) { print $i; exit; } } if ($i == "inet" && $(i+1) !~ /^addr:/) { if ($(i+1) !~ /^127\./) { print $(i+1); exit; } } } }')"
        if [ -n "$any_ip" ]; then
            echo "$any_ip" | cut -d. -f1-3
            return 0
        fi
    fi

    echo "192.168.1"
}

probe_tier1() {
    [ -n "$MDNS_HOST" ] || return 1
    target="http://${MDNS_HOST}:${PORT}"
    if verify_server "$target"; then
        echo "$target"
        return 0
    fi
    return 1
}

probe_tier2() {
    gw=""
    if [ -f "$PROC_ROUTE" ]; then
        gw_hex="$(awk '$2 == "00000000" && $3 != "00000000" { print $3; exit }' "$PROC_ROUTE" 2>/dev/null || true)"
        if [ -n "$gw_hex" ]; then
            gw="$(parse_hex_ip "$gw_hex" 2>/dev/null || true)"
        fi
    fi

    if [ -n "$gw" ] && [ "$gw" != "0.0.0.0" ]; then
        target="http://${gw}:${PORT}"
        if verify_server "$target"; then
            echo "$target"
            return 0
        fi
    fi

    # Prime ARP table with broadcast ping
    ping -b -c 1 -w 1 255.255.255.255 >/dev/null 2>&1 || ping -c 1 -w 1 255.255.255.255 >/dev/null 2>&1 || true

    if [ -f "$PROC_ARP" ]; then
        arp_ips="$(awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $3 != "0x0" && $4 != "00:00:00:00:00:00" { print $1 }' "$PROC_ARP" 2>/dev/null || true)"
        for ip in $arp_ips; do
            if [ "$ip" != "$gw" ] && [ "$ip" != "0.0.0.0" ]; then
                target="http://${ip}:${PORT}"
                if verify_server "$target"; then
                    echo "$target"
                    return 0
                fi
            fi
        done
    fi

    return 1
}

probe_tier3() {
    prefix="$(find_active_subnet_prefix)"
    [ -n "$prefix" ] || return 1

    found_file="/tmp/.sweep_found_$$"
    rm -f "$found_file"

    batch_size=25
    host_start=1

    while [ "$host_start" -le 254 ]; do
        host_end=$((host_start + batch_size - 1))
        if [ "$host_end" -gt 254 ]; then
            host_end=254
        fi

        h="$host_start"
        while [ "$h" -le "$host_end" ]; do
            (
                cand_ip="${prefix}.${h}"
                cand_url="http://${cand_ip}:${PORT}"
                if verify_server "$cand_url"; then
                    printf "%s\n" "$cand_url" > "$found_file"
                fi
            ) &
            h=$((h + 1))
        done

        wait

        if [ -s "$found_file" ]; then
            found="$(head -n 1 "$found_file")"
            rm -f "$found_file"
            echo "$found"
            return 0
        fi

        host_start=$((host_end + 1))
    done

    rm -f "$found_file"
    return 1
}

discover_server() {
    wait_for_network

    # Tier 1: mDNS probe
    if res="$(probe_tier1)"; then
        echo "$res"
        return 0
    fi

    # Tier 2: Gateway & ARP probe
    if res="$(probe_tier2)"; then
        echo "$res"
        return 0
    fi

    # Tier 3: Parallel batched /24 subnet sweep
    if res="$(probe_tier3)"; then
        echo "$res"
        return 0
    fi

    return 1
}

persist_server_url() {
    discovered="$1"
    [ -n "$discovered" ] || return 0

    # 1. Primary config: /mnt/us/documents/tracker_server.txt
    prim_dir="$(dirname "$SERVER_CONFIG")"
    if [ -d "$prim_dir" ] || mkdir -p "$prim_dir" 2>/dev/null; then
        tmp_p="${SERVER_CONFIG}.tmp.$$"
        if printf "%s\n" "$discovered" > "$tmp_p" 2>/dev/null; then
            mv -f "$tmp_p" "$SERVER_CONFIG" 2>/dev/null || true
        fi
    fi

    # 2. Fallback config: /tmp/tracker_server.txt
    fb_dir="$(dirname "$FALLBACK_CONFIG")"
    if [ -d "$fb_dir" ] || mkdir -p "$fb_dir" 2>/dev/null; then
        tmp_f="${FALLBACK_CONFIG}.tmp.$$"
        if printf "%s\n" "$discovered" > "$tmp_f" 2>/dev/null; then
            mv -f "$tmp_f" "$FALLBACK_CONFIG" 2>/dev/null || true
        fi
    fi
}

# 1. Server resolution logic
DISCOVERED_SERVER=""
if [ -f "$SERVER_CONFIG" ]; then
    SERVER_VAL="$(tr -d '\r\n ' < "$SERVER_CONFIG" 2>/dev/null || true)"
    if [ -n "$SERVER_VAL" ]; then
        case "$SERVER_VAL" in
            http://*|https://*) SERVER="$SERVER_VAL" ;;
            *) SERVER="http://$SERVER_VAL" ;;
        esac
    fi
fi

if [ -z "$SERVER" ]; then
    echo "No server config found; initiating cascading LAN discovery..." >&2
    if discovered="$(discover_server)"; then
        SERVER="$discovered"
        DISCOVERED_SERVER="$discovered"
        echo "Discovered server: $SERVER" >&2
    else
        echo "LAN discovery failed to locate server." >&2
    fi
fi

# 2. Binary bootstrap
if [ -n "$SERVER" ]; then
    case "$SERVER" in
        http://*|https://*) ;;
        *) SERVER="http://$SERVER" ;;
    esac

    # Restore from backup if binary missing or invalid
    if [ ! -x "$BINARY" ] || ! is_elf "$BINARY"; then
        rm -f "$BINARY" 2>/dev/null || true
        if is_elf "$BACKUP"; then
            cp "$BACKUP" "$BINARY" 2>/dev/null || true
            chmod +x "$BINARY" 2>/dev/null || true
        else
            rm -f "$BACKUP" 2>/dev/null || true
        fi
    fi

    # First-install bootstrap / download if missing or invalid
    if [ ! -x "$BINARY" ] || ! is_elf "$BINARY"; then
        echo "Downloading tracker-arm from $SERVER..." >&2
        manifest_fetched=0
        exp_sha=""
        if curl -fs -m 10 "$SERVER/tracker-arm.manifest" -o "$MANIFEST_TMP" 2>/dev/null; then
            exp_sha="$(tr -d '\n\r' < "$MANIFEST_TMP" 2>/dev/null | sed -n 's/.*"sha256"[[:space:]]*:[[:space:]]*"\([a-fA-F0-9]\{64\}\)".*/\1/p' | head -n 1 || true)"
            manifest_fetched=1
        fi
        rm -f "$MANIFEST_TMP" 2>/dev/null || true

        if curl -fs -m 30 --max-filesize 33554432 "$SERVER/tracker-arm" -o "$DL_TMP" 2>/dev/null; then
            if ! is_elf "$DL_TMP"; then
                echo "Downloaded binary failed ELF check" >&2
                rm -f "$DL_TMP"
            elif [ "$manifest_fetched" -eq 1 ] && [ -n "$exp_sha" ] && ! verify_checksum "$DL_TMP" "$exp_sha"; then
                echo "Downloaded binary failed SHA-256 checksum verification" >&2
                rm -f "$DL_TMP"
            elif [ "$manifest_fetched" -eq 0 ] || [ -z "$exp_sha" ]; then
                echo "Downloaded binary rejected: missing release manifest or sha256" >&2
                rm -f "$DL_TMP"
            else
                chmod +x "$DL_TMP"
                mv -f "$DL_TMP" "$BINARY"
                backup_dir="$(dirname "$BACKUP")"
                if [ -d "$backup_dir" ] || mkdir -p "$backup_dir" 2>/dev/null; then
                    cp "$BINARY" "$BACKUP" 2>/dev/null || true
                fi
                if [ -n "$DISCOVERED_SERVER" ]; then
                    persist_server_url "$SERVER"
                fi
            fi
        fi
    fi

    # If binary download or validation fails for a discovered server, ensure invalid
    # configuration is not left behind so the next run can re-discover.
    if [ ! -x "$BINARY" ] || ! is_elf "$BINARY"; then
        if [ -n "$DISCOVERED_SERVER" ]; then
            rm -f "$SERVER_CONFIG" "$FALLBACK_CONFIG" 2>/dev/null || true
        fi
    fi

    # Execute binary
    if is_elf "$BINARY" && [ -x "$BINARY" ]; then
        if [ -n "$DISCOVERED_SERVER" ]; then
            persist_server_url "$SERVER"
        fi
        if [ "$BINARY" != "/tmp/tracker" ]; then
            ln -sf "$BINARY" /tmp/tracker 2>/dev/null || cp "$BINARY" /tmp/tracker 2>/dev/null || true
        fi
        exec "$BINARY" -server "$SERVER" -launcher "$0"
    fi
fi

# 3. Fallback: Display cannot connect message
if command -v eips >/dev/null 2>&1; then
    eips -c
    eips 15 18 "Cannot connect to Transit Tracker server at:"
    eips 15 20 "${SERVER:-unknown}"
    eips 15 23 "Please start server and retry."
    sleep 8
    eips -c
else
    echo "Cannot connect to Transit Tracker server at: ${SERVER:-unknown}" >&2
fi

if command -v lipc-set-prop >/dev/null 2>&1; then
    lipc-set-prop -i com.lab126.appmgrd start app://com.lab126.booklet.home 2>/dev/null || true
fi

exit 1

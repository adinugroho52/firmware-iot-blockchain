#!/usr/bin/env bash
# =============================================================================
#!/usr/bin/env bash
# =============================================================================
# simulate_lifecycle.sh
# ---------------------------------------------------------------------------
# Production-grade lifecycle simulation + benchmark for blockchain-IoT
# firmware integrity stack. Designed to run ON the Raspberry Pi gateway.
#
# Major phases:
#   0. Stop systemd service, sample baseline (10s), then start it and sample
#      idle load (10s).
#   1. Loop 10 times:
#      a. Hash original firmware, register on-chain, reset ESP32 (2m delay).
#      b. Flash rogue firmware to ESP32, reset (2m delay).
#      c. Hash original, bump version, re-register on-chain (30s delay).
#      d. Flash original firmware to ESP32, reset (2m delay).
#      e. Revoke device (30s delay).
#      f. Measure /firmware/verify latency and throughput.
#      g. Sample Pi CPU/RAM.
#      h. Sleep 30s before next iteration.
#   2. Write full transcript + TX addresses + metrics to text file.
#
# Output: results/lifecycle_<ts>_fullreport.txt
#
# Usage:
#   ./simulate_lifecycle.sh \
#       [--service firmware-gateway] \
#       [--original /home/adinugroho52/main.py] \
#       [--rogue /home/adinugroho52/main_rogue.py] \
#       [--device-id esp32-bench-01] \
#       [--iterations 10]
# =============================================================================

set -euo pipefail

# ─── Config (env var defaults) ──────────────────────────────────────────────
GATEWAY="${GATEWAY:-http://192.168.1.1:8000}"
ESP32_PORT="${ESP32_PORT:-/dev/ttyUSB0}"
SERVICE_NAME="${SERVICE_NAME:-firmware-gateway}"
DEVICE_ID="${DEVICE_ID:-esp32-bench-v4}"
FIRMWARE_ORIGINAL="${FIRMWARE_ORIGINAL:-/home/adinugroho52/main.py}"
FIRMWARE_ROGUE="${FIRMWARE_ROGUE:-/home/adinugroho52/main_rogue.py}"
INITIAL_VERSION="${INITIAL_VERSION:-1.0.0}"
LOOP_COUNT="${LOOP_COUNT:-10}"
ACTION_DELAY="${ACTION_DELAY:-30}"             # 30 seconds for normal actions
FLASH_DELAY="${FLASH_DELAY:-120}"              # 2 minutes for ESP32 reset/flash
BETWEEN_DELAY="${BETWEEN_DELAY:-30}"           # 30 seconds between iterations
LATENCY_SAMPLES="${LATENCY_SAMPLES:-100}"      # sequential verify calls
THROUGHPUT_PARALLEL="${THROUGHPUT_PARALLEL:-50}"   # concurrent verify calls
TIMEOUT="${TIMEOUT:-180}"                      # tx confirmation timeout

# ─── CLI argument parsing ────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --original)
            FIRMWARE_ORIGINAL="$2"
            shift 2
            ;;
        --rogue)
            FIRMWARE_ROGUE="$2"
            shift 2
            ;;
        --device-id)
            DEVICE_ID="$2"
            shift 2
            ;;
        --gateway)
            GATEWAY="$2"
            shift 2
            ;;
        --iterations)
            LOOP_COUNT="$2"
            shift 2
            ;;
        --initial-version)
            INITIAL_VERSION="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--service NAME] [--original PATH] [--rogue PATH] [--device-id ID] [--gateway URL] [--iterations N] [--initial-version VER]"
            exit 1
            ;;
    esac
done

# Auto-activate venv
VENV="${VENV:-/home/adinugroho52/venv}"
if [[ -z "${VIRTUAL_ENV:-}" && -f "$VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
fi

WORKDIR="$(mktemp -d -t fwsim-XXXXXX)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-./results}"
mkdir -p "$RESULT_DIR"
FULLREPORT="$RESULT_DIR/lifecycle_${TS}_fullreport.txt"
TX_LOG="$RESULT_DIR/lifecycle_${TS}_tx_addresses.txt"

# ─── Output helpers ──────────────────────────────────────────────────────────
BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
YELLOW=$(printf '\033[33m'); BLUE=$(printf '\033[34m'); RESET=$(printf '\033[0m')

# Tee output to both stdout and report file (stripping ANSI)
exec > >(tee >(sed -r 's/\x1B\[[0-9;]*[mK]//g' >> "$FULLREPORT")) 2>&1

step()    { echo; echo "${BOLD}${BLUE}▶ $*${RESET}"; }
info()    { echo "  ${DIM}$*${RESET}"; }
ok()      { echo "  ${GREEN}✓ $*${RESET}"; }
warn()    { echo "  ${YELLOW}⚠ $*${RESET}"; }
fail()    { echo "  ${RED}✗ $*${RESET}" >&2; }

log_tx() {
    local phase="$1" tx="$2" version="${3:-}" hash="${4:-}"
    echo "$phase | $tx | $version | $hash" >> "$TX_LOG"
}

cleanup() {
    [[ -d "$WORKDIR" && "$WORKDIR" == /tmp/fwsim-* ]] && rm -rf "$WORKDIR"
}
trap cleanup EXIT

# ─── Pre-flight ──────────────────────────────────────────────────────────────
echo "================================================================"
echo " Firmware Lifecycle Simulation v2 (ESP32 flashing + systemd)"
echo " Started : $(date -u +%FT%TZ)"
echo " Host    : $(hostname)"
echo " Service : $SERVICE_NAME"
echo "================================================================"

step "Pre-flight checks"
for cmd in curl sha256sum python3 mpremote systemctl; do
    command -v "$cmd" >/dev/null || { fail "$cmd not found in PATH"; exit 1; }
done
[[ -f "$FIRMWARE_ORIGINAL" ]] || { fail "Original firmware not found: $FIRMWARE_ORIGINAL"; exit 1; }
[[ -f "$FIRMWARE_ROGUE" ]]    || { fail "Rogue firmware not found: $FIRMWARE_ROGUE"; exit 1; }
[[ -e "$ESP32_PORT" ]]         || warn "ESP32 port not present: $ESP32_PORT"

info "Gateway             : $GATEWAY"
info "Service             : $SERVICE_NAME"
info "Device ID           : $DEVICE_ID"
info "Original firmware   : $FIRMWARE_ORIGINAL"
info "Rogue firmware      : $FIRMWARE_ROGUE"
info "Initial version     : $INITIAL_VERSION"
info "Iterations          : $LOOP_COUNT"
info "Action delay        : ${ACTION_DELAY}s"
info "Flash/reset delay   : ${FLASH_DELAY}s"
info "Between iterations  : ${BETWEEN_DELAY}s"

curl -fsS --max-time 5 "$GATEWAY/health" >/dev/null \
    && ok "Gateway reachable" \
    || { fail "Gateway not reachable at $GATEWAY"; exit 1; }

# Initialize TX log header
{
    echo "=== Transaction Addresses Log (${TS}) ==="
    echo "phase | tx_hash | version | firmware_hash"
    echo "------|---------|---------|---------------"
} > "$TX_LOG"

# ─── Helpers ─────────────────────────────────────────────────────────────────
sha256()  { sha256sum "$1" | awk '{print $1}'; }
pyget()   { python3 -c "import json,sys;print(json.load(sys.stdin).get('$1',''))"; }
now_ms()  { date +%s%3N; }

bump_patch() {
    local v="$1" major minor patch
    IFS=. read -r major minor patch <<< "$v"
    echo "${major}.${minor}.$((patch+1))"
}

api_register() {
    local fw_hash="$1" version="$2"
    curl -fsS --max-time "$TIMEOUT" -X POST "$GATEWAY/firmware/register" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"device_id":"%s","fw_hash":"%s","version":"%s"}' \
                "$DEVICE_ID" "$fw_hash" "$version")"
}

api_revoke() {
    curl -fsS --max-time "$TIMEOUT" -X POST "$GATEWAY/firmware/revoke" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"device_id":"%s"}' "$DEVICE_ID")"
}

verify_timed() {
    local fw_hash="$1" t0 t1
    t0=$(now_ms)
    curl -fsS --max-time 30 -G "$GATEWAY/firmware/verify" \
        --data-urlencode "device_id=$DEVICE_ID" \
        --data-urlencode "fw_hash=$fw_hash" >/dev/null
    t1=$(now_ms)
    echo $((t1 - t0))
}

# Flash ESP32 main.py and reset (via mpremote)
flash_and_reset() {
    local fw="$1" label="${2:-firmware}"
    step "Flashing $label to ESP32 and resetting…"
    if [[ ! -e "$ESP32_PORT" ]]; then
        warn "ESP32 port not present, skipping flash"
        return 0
    fi
    if mpremote connect "$ESP32_PORT" fs cp "$fw" :/main.py 2>&1 | head -1; then
        ok "Flashed $label"
        mpremote connect "$ESP32_PORT" reset 2>&1 | head -1
        ok "Reset ESP32"
        return 0
    else
        warn "Flash of $label failed"
        return 1
    fi
}

sample_pi() {
    python3 - <<'PY'
import time
def read():
    with open('/proc/stat') as f:
        parts = f.readline().split()[1:]
    nums = [int(x) for x in parts]
    idle = nums[3] + nums[4]
    total = sum(nums)
    return idle, total
i1, t1 = read()
time.sleep(1.0)
i2, t2 = read()
cpu = 100.0 * (1 - (i2 - i1) / max(1, (t2 - t1)))
mem_total = mem_avail = 0
with open('/proc/meminfo') as f:
    for line in f:
        if line.startswith('MemTotal:'):     mem_total = int(line.split()[1])
        elif line.startswith('MemAvailable:'): mem_avail = int(line.split()[1])
mb = lambda kb: kb // 1024
print(f"{cpu:.2f} {mb(mem_total - mem_avail)} {mb(mem_total)}")
PY
}

# ─── Phase 0: Baseline sampling ──────────────────────────────────────────────
step "Phase 0: System baseline (service stopped)"
info "Stopping systemd service: $SERVICE_NAME…"
if sudo systemctl stop "$SERVICE_NAME" 2>&1 | head -1; then
    ok "Service stopped"
else
    warn "Failed to stop service"
fi

sleep 5

info "Sampling baseline (10 seconds)…"
BASELINE_CPU_SAMPLES=()
BASELINE_MEM_SAMPLES=()
for i in $(seq 1 10); do
    PI_SAMPLE=$(sample_pi 2>/dev/null || echo "0 0 0")
    read -r cpu mem_used mem_total <<< "$PI_SAMPLE"
    BASELINE_CPU_SAMPLES+=("$cpu")
    BASELINE_MEM_SAMPLES+=("$mem_used")
    echo -n "."
    sleep 1
done
echo
BASELINE_CPU_AVG=$(printf '%s\n' "${BASELINE_CPU_SAMPLES[@]}" | python3 -c "import statistics,sys; xs=[float(x) for x in sys.stdin if x.strip()]; print(f'{statistics.fmean(xs):.2f}')")
BASELINE_MEM_AVG=$(printf '%s\n' "${BASELINE_MEM_SAMPLES[@]}" | python3 -c "import statistics,sys; xs=[float(x) for x in sys.stdin if x.strip()]; print(f'{statistics.fmean(xs):.0f}')")
ok "Baseline: CPU=${BASELINE_CPU_AVG}%  RAM=${BASELINE_MEM_AVG}MB"

step "Phase 0b: Starting service and sampling idle load"
info "Starting systemd service: $SERVICE_NAME…"
if sudo systemctl start "$SERVICE_NAME" 2>&1 | head -1; then
    ok "Service started"
else
    fail "Failed to start service"; exit 1
fi

sleep 5  # Let service stabilize
info "Sampling idle load (10 seconds)…"
IDLE_CPU_SAMPLES=()
IDLE_MEM_SAMPLES=()
for i in $(seq 1 10); do
    PI_SAMPLE=$(sample_pi 2>/dev/null || echo "0 0 0")
    read -r cpu mem_used mem_total <<< "$PI_SAMPLE"
    IDLE_CPU_SAMPLES+=("$cpu")
    IDLE_MEM_SAMPLES+=("$mem_used")
    echo -n "."
    sleep 1
done
echo
IDLE_CPU_AVG=$(printf '%s\n' "${IDLE_CPU_SAMPLES[@]}" | python3 -c "import statistics,sys; xs=[float(x) for x in sys.stdin if x.strip()]; print(f'{statistics.fmean(xs):.2f}')")
IDLE_MEM_AVG=$(printf '%s\n' "${IDLE_MEM_SAMPLES[@]}" | python3 -c "import statistics,sys; xs=[float(x) for x in sys.stdin if x.strip()]; print(f'{statistics.fmean(xs):.0f}')")
ok "Idle load: CPU=${IDLE_CPU_AVG}%  RAM=${IDLE_MEM_AVG}MB"

# ─── Main loop ───────────────────────────────────────────────────────────────
CURRENT_VERSION="$INITIAL_VERSION"
LOOP_START=$(date +%s)
LATENCIES=()
THROUGHPUTS=()

for ITER in $(seq 1 "$LOOP_COUNT"); do
    ITER_T0=$(date +%s)
    echo
    echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
    echo "${BOLD} ITERATION $ITER / $LOOP_COUNT  —  version $CURRENT_VERSION${RESET}"
    echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"

    # ── a) Hash original, register, reset ESP32 ────────────────────────────────
    step "[$ITER.a] Hash original firmware and register on-chain"
    HASH_ORIGINAL=$(sha256 "$FIRMWARE_ORIGINAL")
    info "SHA-256: $HASH_ORIGINAL"

    REG_RESP=$(api_register "$HASH_ORIGINAL" "$CURRENT_VERSION")
    TX_REG=$(echo "$REG_RESP" | pyget tx_hash)
    ok "Registered (tx: ${TX_REG:0:18}…)"
    log_tx "iter${ITER}_a_register" "$TX_REG" "$CURRENT_VERSION" "$HASH_ORIGINAL"
    
    info "Waiting ${ACTION_DELAY}s before flash…"
    sleep "$ACTION_DELAY"

    flash_and_reset "$FIRMWARE_ORIGINAL" "original v$CURRENT_VERSION"
    info "Waiting ${FLASH_DELAY}s after reset…"
    sleep "$FLASH_DELAY"

    # ── b) Flash rogue, reset ──────────────────────────────────────────────────
    step "[$ITER.b] Flash rogue firmware to ESP32"
    flash_and_reset "$FIRMWARE_ROGUE" "rogue"
    info "Waiting ${FLASH_DELAY}s after reset…"
    sleep "$FLASH_DELAY"

    # ── c) Bump version, hash original, re-register ────────────────────────────
    step "[$ITER.c] Bump version, hash original, and re-register on-chain"
    CURRENT_VERSION="$(bump_patch "$CURRENT_VERSION")"
    info "New version: $CURRENT_VERSION"
    
    REG_RESP2=$(api_register "$HASH_ORIGINAL" "$CURRENT_VERSION")
    TX_REG2=$(echo "$REG_RESP2" | pyget tx_hash)
    ok "Re-registered (tx: ${TX_REG2:0:18}…)"
    log_tx "iter${ITER}_c_register_bumped" "$TX_REG2" "$CURRENT_VERSION" "$HASH_ORIGINAL"

    info "Waiting ${ACTION_DELAY}s before flash…"
    sleep "$ACTION_DELAY"

    # ── d) Flash original, reset ───────────────────────────────────────────────
    step "[$ITER.d] Flash original firmware back to ESP32"
    flash_and_reset "$FIRMWARE_ORIGINAL" "original v$CURRENT_VERSION"
    info "Waiting ${FLASH_DELAY}s after reset…"
    sleep "$FLASH_DELAY"

    # ── e) Revoke device ───────────────────────────────────────────────────────
    step "[$ITER.e] Revoke device"
    REV_RESP=$(api_revoke)
    TX_REV=$(echo "$REV_RESP" | pyget tx_hash)
    ok "Revoked (tx: ${TX_REV:0:18}…)"
    log_tx "iter${ITER}_e_revoke" "$TX_REV" "$CURRENT_VERSION" ""

    info "Waiting ${ACTION_DELAY}s before verification benchmark…"
    sleep "$ACTION_DELAY"

    # ── f1) Sequential latency ─────────────────────────────────────────────────
    step "[$ITER.f1] Sequential verify latency ($LATENCY_SAMPLES samples)"
    LATENCY_FILE="$WORKDIR/iter${ITER}_latency.txt"
    : > "$LATENCY_FILE"
    for i in $(seq 1 "$LATENCY_SAMPLES"); do
        verify_timed "$HASH_ORIGINAL" >> "$LATENCY_FILE"
    done
    read -r MEAN P50 P95 < <(python3 - <<PY
import statistics
xs = sorted(int(x) for x in open("$LATENCY_FILE") if x.strip())
mean = statistics.fmean(xs) if xs else 0
p50  = xs[len(xs)//2] if xs else 0
p95  = xs[max(0, int(len(xs)*0.95) - 1)] if xs else 0
print(f"{mean:.2f} {p50} {p95}")
PY
)
    ok "Sequential: mean=${MEAN}ms  p50=${P50}ms  p95=${P95}ms"
    LATENCIES+=("$MEAN")

    # ── f2) Parallel throughput ────────────────────────────────────────────────
    step "[$ITER.f2] Parallel verify throughput ($THROUGHPUT_PARALLEL concurrent)"
    THRU_T0=$(now_ms)
    seq 1 "$THROUGHPUT_PARALLEL" | xargs -P "$THROUGHPUT_PARALLEL" -I{} \
        curl -fsS --max-time 30 -G "$GATEWAY/firmware/verify" \
            --data-urlencode "device_id=$DEVICE_ID" \
            --data-urlencode "fw_hash=$HASH_ORIGINAL" >/dev/null 2>&1 || true
    THRU_T1=$(now_ms)
    THRU_MS=$((THRU_T1 - THRU_T0))
    THROUGHPUT=$(python3 -c "print(round($THROUGHPUT_PARALLEL / max(0.001, $THRU_MS/1000), 2))")
    ok "Parallel: throughput=${THROUGHPUT} req/s (${THRU_MS}ms for $THROUGHPUT_PARALLEL reqs)"
    THROUGHPUTS+=("$THROUGHPUT")

    # ── g) Sample Pi ───────────────────────────────────────────────────────────
    step "[$ITER.g] Sample Pi CPU/RAM"
    PI_SAMPLE=$(sample_pi 2>/dev/null || echo "0 0 0")
    read -r PI_CPU PI_MEM_USED PI_MEM_TOTAL <<< "$PI_SAMPLE"
    ok "Pi: cpu=${PI_CPU}%  ram=${PI_MEM_USED}MB / ${PI_MEM_TOTAL}MB"

    ITER_T1=$(date +%s)
    WALLCLOCK=$((ITER_T1 - ITER_T0))
    ok "Iteration wallclock: ${WALLCLOCK}s"

    # Before next iteration
    if (( ITER < LOOP_COUNT )); then
        info "Sleeping ${BETWEEN_DELAY}s before next iteration…"
        sleep "$BETWEEN_DELAY"
    fi
done

# ─── Summary & final report ──────────────────────────────────────────────────
LOOP_END=$(date +%s)
TOTAL_WALLCLOCK=$((LOOP_END - LOOP_START))

echo
echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"
echo "${BOLD} BENCHMARK SUMMARY${RESET}"
echo "${BOLD}════════════════════════════════════════════════════════════════${RESET}"

echo
echo "Baseline (service stopped):"
echo "  CPU  : ${BASELINE_CPU_AVG}%"
echo "  RAM  : ${BASELINE_MEM_AVG} MB"
echo
echo "Idle load (service running, no active verification):"
echo "  CPU  : ${IDLE_CPU_AVG}%"
echo "  RAM  : ${IDLE_MEM_AVG} MB"
echo
echo "Verification latency (ms, across all iterations):"
if [[ ${#LATENCIES[@]} -gt 0 ]]; then
    printf '%s\n' "${LATENCIES[@]}" | python3 -c "
import statistics, sys
xs = [float(x) for x in sys.stdin if x.strip()]
print(f'  min  : {min(xs):.2f}')
print(f'  mean : {statistics.fmean(xs):.2f}')
print(f'  max  : {max(xs):.2f}')
"
fi
echo
echo "Verification throughput (req/s, across all iterations):"
if [[ ${#THROUGHPUTS[@]} -gt 0 ]]; then
    printf '%s\n' "${THROUGHPUTS[@]}" | python3 -c "
import statistics, sys
xs = [float(x) for x in sys.stdin if x.strip()]
print(f'  min  : {min(xs):.2f}')
print(f'  mean : {statistics.fmean(xs):.2f}')
print(f'  max  : {max(xs):.2f}')
"
fi
echo
echo "Total wallclock time: ${TOTAL_WALLCLOCK}s (~$((TOTAL_WALLCLOCK / 60)) minutes)"
echo
echo "${BOLD}${GREEN}Simulation complete.${RESET}"
echo "Full report : $FULLREPORT"
echo "TX log      : $TX_LOG"
echo
echo "TX addresses for manual verification:"
cat "$TX_LOG"

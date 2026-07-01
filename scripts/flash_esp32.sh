#!/usr/bin/env bash
# flash_esp32.sh
# ==============
# Flash MicroPython runtime + user firmware to an ESP32 (2 MB flash).
# Uses esptool.py (erase + flash) and mpremote (file transfer).
#
# Prerequisites (install on your workstation or the Pi):
#   pip install esptool mpremote
#
# Download the latest MicroPython ESP32 binary from:
#   https://micropython.org/download/ESP32_GENERIC/
#
# Usage:
#   ./flash_esp32.sh [PORT] [MICROPYTHON_BIN]
#   ./flash_esp32.sh /dev/ttyUSB0 ESP32_GENERIC-D2WD-20220117-v1.18.bin

set -euo pipefail

PORT="${1:-/dev/ttyUSB0}"
MICROPYTHON_BIN="${2:-ESP32_GENERIC-D2WD-20220117-v1.18.bin}"
FIRMWARE_DIR="$(dirname "$0")/../esp32_firmware"

echo "==============================="
echo " ESP32 Firmware Flash Script"
echo " Port        : $PORT"
echo " MicroPython : $MICROPYTHON_BIN"
echo " Firmware dir: $FIRMWARE_DIR"
echo "==============================="

if [[ ! -f "$MICROPYTHON_BIN" ]]; then
  echo "ERROR: MicroPython binary not found: $MICROPYTHON_BIN"
  echo "Download from: https://micropython.org/download/ESP32_GENERIC/"
  exit 1
fi

# Step 1: Erase flash
echo ""
echo "[1/4] Erasing ESP32 flash..."
esptool.py --chip esp32 --port "$PORT" erase_flash

# Step 2: Flash MicroPython
echo ""
echo "[2/4] Flashing MicroPython runtime..."
esptool.py \
  --chip esp32 \
  --port "$PORT" \
  --baud 460800 \
  write_flash \
  --flash_size 2MB \
  --flash_mode dio \
  0x1000 "$MICROPYTHON_BIN"

# Step 3: Wait for device to boot
echo ""
echo "[3/4] Waiting for device to boot..."
sleep 3

# Step 4: Upload user firmware (main.py)
echo ""
echo "[4/4] Uploading main.py to /flash/main.py..."
mpremote connect "$PORT" fs cp "$FIRMWARE_DIR/main.py" :/main.py

echo ""
echo "Done! ESP32 is running MicroPython + firmware."
echo ""
echo "Next steps:"
echo "  1. Edit esp32_firmware/main.py — set WIFI_SSID, WIFI_PASSWORD, MQTT_BROKER, DEVICE_ID"
echo "  2. Re-upload:  mpremote connect $PORT fs cp esp32_firmware/main.py :/main.py"
echo "  3. Hash firmware and register on-chain:"
echo "     python scripts/register_firmware.py \\"
echo "       --firmware esp32_firmware/main.py \\"
echo "       --device-id esp32-lab-01 \\"
echo "       --version 1.0.0"
echo "  4. Verify REPL output:"
echo "     mpremote connect $PORT repl"

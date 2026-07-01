#!/usr/bin/env bash
# setup_gateway.sh
# ================
# One-shot setup for the Raspberry Pi 5 edge gateway.
# Run as root or with sudo on a fresh Raspberry Pi OS Bookworm installation.
#
# Usage:
#   sudo bash setup_gateway.sh

set -euo pipefail
GATEWAY_DIR="/opt/firmware-gateway"
SERVICE_USER="firmware-gw"
DB_DIR="/var/lib/firmware-gateway"

echo "════════════════════════════════════"
echo "  Firmware Blockchain Gateway Setup "
echo "      Raspberry Pi OS (64-bit)      "
echo "════════════════════════════════════"

# 1. System update & packages
echo "[1/7] Updating system and installing packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  mosquitto mosquitto-clients \
  python3 python3-pip python3-venv \
  git curl

# 2. Mosquitto MQTT broker
echo "[2/7] Configuring Mosquitto..."
cat > /etc/mosquitto/conf.d/firmware-gw.conf << 'EOF'
listener 1883
allow_anonymous true
# For production, replace with:
# allow_anonymous false
# password_file /etc/mosquitto/passwd
# and run: mosquitto_passwd -c /etc/mosquitto/passwd <username>
EOF
systemctl enable mosquitto
systemctl restart mosquitto
echo "Mosquitto running on port 1883"

# 3. Create service user
echo "[3/7] Creating service user '$SERVICE_USER'..."
id "$SERVICE_USER" &>/dev/null || useradd -r -s /sbin/nologin "$SERVICE_USER"

# 4. Install gateway application
echo "[4/7] Installing gateway application to $GATEWAY_DIR..."
mkdir -p "$GATEWAY_DIR"
cp -r "$(dirname "$0")/../gateway/"* "$GATEWAY_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$GATEWAY_DIR"

# Python venv + deps
python3 -m venv "$GATEWAY_DIR/venv"
"$GATEWAY_DIR/venv/bin/pip" install --quiet --upgrade pip
"$GATEWAY_DIR/venv/bin/pip" install --quiet -r "$GATEWAY_DIR/requirements.txt"
echo "      Python venv ready"

# 5. Environment file
echo "[5/7] Creating environment template..."
mkdir -p "$DB_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DB_DIR"

ENV_FILE="$GATEWAY_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" << 'ENVEOF'
# Blockchain
INFURA_URL=https://sepolia.infura.io/v3/YOUR_INFURA_PROJECT_ID
CONTRACT_ADDRESS=0xYOUR_DEPLOYED_CONTRACT_ADDRESS
PRIVATE_KEY=YOUR_WALLET_PRIVATE_KEY_WITHOUT_0X_PREFIX
CHAIN_ID=11155111

# MQTT
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_USER=
MQTT_PASSWORD=

# DB & Logging
DB_PATH=/var/lib/firmware-gateway/audit.db
LOG_LEVEL=INFO
ENVEOF
  chmod 600 "$ENV_FILE"
  chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
  echo "Edit $ENV_FILE with your credentials before starting the service!"
else
  echo ".env already exists — skipping"
fi

# 6. Systemd service
echo "[6/7] Installing systemd service..."
cat > /etc/systemd/system/firmware-gateway.service << SVCEOF
[Unit]
Description=Firmware Blockchain Integrity Gateway
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$GATEWAY_DIR
EnvironmentFile=$GATEWAY_DIR/.env
ExecStart=$GATEWAY_DIR/venv/bin/python gateway.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=firmware-gateway

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable firmware-gateway
echo "Service installed (not started — fill in .env first)"

# 7. Deploy contract helper
echo "[7/8] Copying contract helper script to gateway directory"
cp "$(dirname "$0")/deploy_contract.py" "$GATEWAY_DIR/"
cp "$(dirname "$0")/register_firmware.py" "$GATEWAY_DIR/"


# 8. Install Foundry (Solidity compiler)
echo "[8/8] Installing Foundry (forge) for ARM64-native contract compilation"
FOUNDRY_DIR="/usr/local/share/foundry"
export FOUNDRY_DIR

if ! command -v forge &>/dev/null; then
  mkdir -p "$FOUNDRY_DIR"
  curl -fsSL https://foundry.paradigm.xyz | bash -s -- --no-modify-path
  "$FOUNDRY_DIR/bin/foundryup" &>/dev/null

  # Make binaries reachable for every user
  chmod -R a+rX "$FOUNDRY_DIR"
  for bin in forge cast anvil chisel; do
    if [[ -f "$FOUNDRY_DIR/bin/$bin" ]]; then
      ln -sf "$FOUNDRY_DIR/bin/$bin" "/usr/local/bin/$bin"
    fi
  done

  # Final sanity check
  if command -v forge &>/dev/null; then
    echo "forge installed system-wide: $(forge --version)"
  else
    echo "forge install may have failed, check output above"
  fi
else
  echo "forge already installed: $(forge --version)"
fi

echo ""
echo "================================================================="
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit $ENV_FILE"
echo "     Fill in INFURA_URL, PRIVATE_KEY, and CONTRACT_ADDRESS"
echo ""
echo "  2. Deploy the smart contract (first time only):"
echo "     cd $GATEWAY_DIR"
echo "     source venv/bin/activate"
echo "     python deploy_contract.py"
echo "     # Copy the printed CONTRACT_ADDRESS into .env"
echo ""
echo "  3. Start the gateway:"
echo "     sudo systemctl start firmware-gateway"
echo "     sudo journalctl -u firmware-gateway -f"
echo ""
echo "  4. Register initial firmware hash:"
echo "     python register_firmware.py \\"
echo "       --firmware /path/to/main.py \\"
echo "       --device-id esp32-lab-01 \\"
echo "       --version 1.0.0"
echo ""
echo "  5. Flash ESP32 (from workstation):"
echo "     bash scripts/flash_esp32.sh /dev/ttyUSB0 ESP32_GENERIC-*.bin"
echo "================================================================="

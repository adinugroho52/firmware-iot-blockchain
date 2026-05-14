"""
ESP32 MicroPython Firmware — firmware-blockchain-iot
=====================================================
Responsibilities:
  1. Read temperature/humidity from DHT22.
  2. Compute SHA-256 hash of own firmware binary (this file) at boot.
  3. Report sensor data + firmware hash to the Raspberry Pi gateway
     via MQTT over Wi-Fi.
  4. Accept OTA update commands from the gateway; re-hash after update.

Flash layout (2 MB):
  0x000000 – 0x008000  Bootloader / partition table
  0x010000 – 0x1FFFFF  MicroPython + user code (main.py resides in /flash)

Dependencies (pre-installed in MicroPython firmware):
  - uhashlib, ubinascii, umqtt.simple, dht, machine, network, uos, utime
"""

import gc
import machine
import network
import uos
import utime
import uhashlib
import ubinascii
import ujson
import dht
from umqtt.simple import MQTTClient

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

WIFI_SSID     = "your_wifi_ssid"
WIFI_PASSWORD = "your_wifi_password"

MQTT_BROKER   = "192.168.1.1"     # Raspberry Pi gateway IP
MQTT_PORT     = 1883
MQTT_USER     = ""                # Leave empty if no auth
MQTT_PASSWORD = ""

DEVICE_ID     = "esp32-lab-01"  # Must match blockchain registration
FIRMWARE_FILE = "/main.py"        # MicroPython VFS root

DHT_PIN       = machine.Pin(4)    # GPIO4 — DHT22 data pin
SENSOR_TOPIC  = b"iot/sensors"
INTEGRITY_TOPIC = b"iot/integrity"
OTA_TOPIC     = b"iot/ota/" + DEVICE_ID.encode()

REPORT_INTERVAL_S = 30            # Sensor report cadence

# ─── FIRMWARE SELF-HASH ────────────────────────────────────────────────────────

def compute_firmware_hash(filepath: str) -> str:
    """
    SHA-256 of the raw bytes of *this* firmware file.
    Reads in 512-byte chunks to stay within ESP32 heap limits.
    """
    import uos
    # Print the root directory listing so the correct path is always visible
    try:
        print("[HASH] VFS root contents:", uos.listdir("/"))
    except Exception as e:
        print("[HASH] Cannot list /:", e)

    h = uhashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            size = 0
            while True:
                chunk = f.read(512)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        digest = ubinascii.hexlify(h.digest()).decode()
        print("[HASH] Read {} bytes from {}".format(size, filepath))
        return digest
    except OSError as e:
        print("[HASH] FAILED to open '{}': {}".format(filepath, e))
        print("[HASH] Check FIRMWARE_FILE constant matches uos.listdir('/')")
        return ""

# ─── WI-FI ────────────────────────────────────────────────────────────────────

def connect_wifi() -> bool:
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        return True
    print("[WiFi] Connecting to", WIFI_SSID)
    sta.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(20):
        if sta.isconnected():
            print("[WiFi] Connected:", sta.ifconfig())
            return True
        utime.sleep(1)
    print("[WiFi] Connection failed")
    return False

# ─── MQTT ─────────────────────────────────────────────────────────────────────

_ota_pending = {}

def on_message(topic, msg):
    """Handle OTA instructions from the gateway."""
    global _ota_pending
    print("[MQTT] Received on", topic, ":", msg)
    try:
        payload = ujson.loads(msg)
        if topic == OTA_TOPIC and payload.get("action") == "update":
            _ota_pending = payload   # Gateway will push new .mpy via HTTP
    except Exception as e:
        print("[MQTT] Parse error:", e)

def mqtt_connect() -> MQTTClient:
    client_id = DEVICE_ID.encode()
    c = MQTTClient(
        client_id,
        MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER or None,
        password=MQTT_PASSWORD or None,
        keepalive=60,
    )
    c.set_callback(on_message)
    c.connect()
    c.subscribe(OTA_TOPIC)
    print("[MQTT] Connected to broker at", MQTT_BROKER)
    return c

# ─── DHT22 SENSOR ────────────────────────────────────────────────────────────

def read_sensor(sensor: dht.DHT22):
    """Return (temperature_C, humidity_pct) or (None, None) on error."""
    try:
        sensor.measure()
        return sensor.temperature(), sensor.humidity()
    except OSError as e:
        print("[DHT22] Read error:", e)
        return None, None

# ─── OTA UPDATE ──────────────────────────────────────────────────────────────

def apply_ota(client: MQTTClient, payload: dict, sensor: dht.DHT22):
    """
    Minimal OTA: gateway sends new firmware content as base64 in the MQTT
    payload (suitable for small MicroPython scripts ≤ ~50 KB before heap
    pressure). For larger binaries, use urequests to pull from gateway HTTP.
    """
    import ubinascii as b64
    new_fw_b64 = payload.get("firmware_b64", "")
    if not new_fw_b64:
        print("[OTA] No firmware payload")
        return

    new_fw = b64.a2b_base64(new_fw_b64)
    tmp = "/flash/main_new.py"
    with open(tmp, "wb") as f:
        f.write(new_fw)

    # Verify hash before applying
    h = uhashlib.sha256()
    h.update(new_fw)
    new_hash = ubinascii.hexlify(h.digest()).decode()
    expected = payload.get("expected_hash", "")

    if expected and new_hash != expected:
        print("[OTA] Hash mismatch! Aborting. Got:", new_hash)
        uos.remove(tmp)
        return

    print("[OTA] Hash verified:", new_hash)
    uos.rename(tmp, FIRMWARE_FILE)

    # Report new hash for gateway to register on-chain
    msg = ujson.dumps({
        "device_id":     DEVICE_ID,
        "event":         "ota_complete",
        "firmware_hash": new_hash,
        "version":       payload.get("version", "unknown"),
    })
    client.publish(INTEGRITY_TOPIC, msg.encode())
    print("[OTA] Applied. Rebooting in 3 s...")
    utime.sleep(3)
    machine.reset()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main():
    #print("main function started")
    #import sys
    #sys.stdout.flush()

    global _ota_pending

    # 1. Compute self-hash before anything else
    print("[BOOT] Computing firmware integrity hash...")
    fw_hash = compute_firmware_hash(FIRMWARE_FILE)
    print("[BOOT] Firmware SHA-256:", fw_hash)

    # 2. Connect to Wi-Fi
    if not connect_wifi():
        print("[BOOT] No Wi-Fi — halting")
        return

    # 3. Init sensor
    sensor = dht.DHT22(DHT_PIN)

    # 4. MQTT
    client = mqtt_connect()

    # 5. Publish boot integrity report
    boot_msg = ujson.dumps({
        "device_id":     DEVICE_ID,
        "event":         "boot",
        "firmware_hash": fw_hash,
    })
    client.publish(INTEGRITY_TOPIC, boot_msg.encode())

    # 6. Main sense-report loop
    last_report = utime.time()
    while True:
        # Check for incoming MQTT messages (non-blocking)
        client.check_msg()

        # Handle pending OTA
        if _ota_pending:
            apply_ota(client, _ota_pending, sensor)
            _ota_pending = {}

        now = utime.time()
        if now - last_report >= REPORT_INTERVAL_S:
            last_report = now
            temp, hum = read_sensor(sensor)
            if temp is not None:
                payload = ujson.dumps({
                    "device_id":   DEVICE_ID,
                    "temperature": temp,
                    "humidity":    hum,
                    "fw_hash":     fw_hash,
                    "ts":          now,
                })
                client.publish(SENSOR_TOPIC, payload.encode(), retain=True)
                print("[SENSOR]", payload)

            gc.collect()

        utime.sleep_ms(500)

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

main()

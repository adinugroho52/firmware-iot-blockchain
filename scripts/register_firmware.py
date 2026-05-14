#!/usr/bin/env python3
"""
register_firmware.py
====================
Compute the SHA-256 hash of a MicroPython firmware file (.py or .mpy)
and register it on-chain via the gateway REST API.

Usage:
    python register_firmware.py \
        --firmware ../esp32_firmware/main.py \
        --device-id esp32-lab-01 \
        --version 1.0.0 \
        [--gateway http://192.168.1.1:8000]

This is the CLI equivalent of the PoC's Remix IDE "storeFirmwareHash" call.
"""

import argparse
import hashlib
import json
import sys
import urllib.request

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def register(gateway: str, device_id: str, fw_hash: str, version: str):
    url     = f"{gateway}/firmware/register"
    payload = json.dumps({
        "device_id": device_id,
        "fw_hash":   fw_hash,
        "version":   version,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Register firmware hash on-chain")
    parser.add_argument("--firmware",  required=True, help="Path to firmware file")
    parser.add_argument("--device-id", required=True, help="Device identifier string")
    parser.add_argument("--version",   required=True, help="Firmware version string")
    parser.add_argument("--gateway",   default="http://192.168.1.1:8000",
                        help="Gateway base URL (default: http://192.168.1.1:8000)")
    args = parser.parse_args()

    print(f"[1/3] Hashing firmware: {args.firmware}")
    fw_hash = sha256_file(args.firmware)
    print(f"      SHA-256: {fw_hash}")

    print(f"[2/3] Registering on-chain via {args.gateway}...")
    try:
        result = register(args.gateway, args.device_id, fw_hash, args.version)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    tx = result.get("tx_hash", "")
    print(f"      ✅ Registered! tx_hash: {tx}")
    print(f"\n[3/3] Summary:")
    print(f"      device_id : {args.device_id}")
    print(f"      version   : {args.version}")
    print(f"      fw_hash   : {fw_hash}")
    print(f"      tx_hash   : {tx}")
    print(f"\n👉  View on Sepolia Etherscan:")
    print(f"    https://sepolia.etherscan.io/tx/{tx}")


if __name__ == "__main__":
    main()

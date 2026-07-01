#!/usr/bin/env python3
"""
deploy_contract.py
==================
Deploy FirmwareIntegrity.sol to Sepolia — ARM64 / Raspberry Pi compatible.

Requires Foundry (forge) on PATH; installed automatically by
scripts/setup_gateway.sh, or manually with:
    curl -L https://foundry.paradigm.xyz | bash
    foundryup

Usage:
    python deploy_contract.py

Environment variables (set in .env or shell):
    INFURA_URL      https://sepolia.infura.io/v3/<YOUR_KEY>
    PRIVATE_KEY     hex private key of the deployer wallet (no 0x prefix)
    CHAIN_ID        11155111  (Sepolia default)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

INFURA_URL  = os.environ["INFURA_URL"]
PRIVATE_KEY = os.environ["PRIVATE_KEY"]
CHAIN_ID    = int(os.environ.get("CHAIN_ID", "11155111"))

SOL_FILE = Path(__file__).parent.parent / "contracts" / "FirmwareIntegrity.sol"
OUT_FILE = Path(__file__).parent / "deployed.json"

# Embedded ABI matching the smart contract

EMBEDDED_ABI = [
    {"name": "storeFirmwareHash", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "deviceID", "type": "string"}, {"name": "hash", "type": "string"},
                {"name": "version", "type": "string"}], "outputs": []},
    {"name": "revokeFirmware", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "deviceID", "type": "string"}], "outputs": []},
    {"name": "getFirmwareRecord", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "deviceID", "type": "string"}],
     "outputs": [{"name": "hash", "type": "string"}, {"name": "version", "type": "string"},
                 {"name": "timestamp", "type": "uint256"}, {"name": "revoked", "type": "bool"}]},
    {"name": "getFirmwareHash", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "deviceID", "type": "string"}], "outputs": [{"name": "", "type": "string"}]},
    {"name": "verifyFirmware", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "deviceID", "type": "string"}, {"name": "hash", "type": "string"}],
     "outputs": [{"name": "valid", "type": "bool"}]},
    {"name": "owner", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"inputs": [], "stateMutability": "nonpayable", "type": "constructor"},
    {"name": "FirmwareRegistered", "type": "event",
     "inputs": [{"name": "deviceID", "type": "string", "indexed": True},
                {"name": "hash", "type": "string", "indexed": False},
                {"name": "version", "type": "string", "indexed": False},
                {"name": "timestamp", "type": "uint256", "indexed": False}]},
    {"name": "FirmwareRevoked", "type": "event",
     "inputs": [{"name": "deviceID", "type": "string", "indexed": True},
                {"name": "timestamp", "type": "uint256", "indexed": False}]},
]


# Compilation with Foundry

def compile_contract() -> tuple[list, str]:
    """
    Compiles FirmwareIntegrity.sol using the `forge` CLI from Foundry.
        Install Foundry on the Pi (one-time):
        curl -L https://foundry.paradigm.xyz | bash
        source ~/.bashrc
        foundryup
    """
    check = subprocess.run(["forge", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print("\nERROR: `forge` is not installed or not on PATH.")
        print("Install Foundry (native ARM64 binary):")
        print("    curl -L https://foundry.paradigm.xyz | bash")
        print("    foundryup")
        print("\nOr just re-run scripts/setup_gateway.sh, which installs")
        print("Foundry system-wide (see step [7b]).")
        print("\nThen re-run this script.")
        sys.exit(1)

    print(f"      forge version: {check.stdout.strip()}")

    # Build inside a temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src = tmp / "src"
        src.mkdir()
        (src / "FirmwareIntegrity.sol").write_text(SOL_FILE.read_text())
        (tmp / "foundry.toml").write_text(
            "[profile.default]\n"
            'src = "src"\n'
            "optimizer = true\n"
            "optimizer_runs = 200\n"
            'solc_version = "0.8.20"\n'
        )

        result = subprocess.run(
            ["forge", "build", "--root", str(tmp)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("ERROR: forge build failed:\n" + result.stderr)
            sys.exit(1)

        artifact_path = tmp / "out" / "FirmwareIntegrity.sol" / "FirmwareIntegrity.json"
        if not artifact_path.exists():
            print("ERROR: expected forge artifact not found:", artifact_path)
            sys.exit(1)

        artifact = json.loads(artifact_path.read_text())
        abi      = artifact["abi"]
        bytecode = artifact["bytecode"]["object"]   # hex string, may include 0x prefix

    # Normalize: web3.py wants hex without 0x
    if bytecode.startswith("0x"):
        bytecode = bytecode[2:]

    return abi, bytecode


# ================ #
# Deployment logic #
# ================ #

def deploy(abi: list, bytecode: str) -> None:
    print("\n[DEPLOY] Connecting to Sepolia via Infura...")
    w3 = Web3(Web3.HTTPProvider(INFURA_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print("ERROR: Cannot connect to Ethereum node. Check INFURA_URL.")
        sys.exit(1)

    block   = w3.eth.block_number
    account = w3.eth.account.from_key(PRIVATE_KEY)
    balance = w3.eth.get_balance(account.address)

    print(f"         Latest block   : {block:,}")
    print(f"         Deployer       : {account.address}")
    print(f"         Balance        : {w3.from_wei(balance, 'ether'):.6f} ETH")

    if balance == 0:
        print("\nERROR: Deployer wallet has no Sepolia ETH.")
        print("Get free Sepolia ETH: https://faucet.sepolia.dev")
        sys.exit(1)

    print("\n[DEPLOY] Estimating gas and submitting transaction...")
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce    = w3.eth.get_transaction_count(account.address)

    try:
        gas_est = Contract.constructor().estimate_gas({"from": account.address})
        gas     = int(gas_est * 1.2)   # 20% buffer
        print(f"         Gas estimate   : {gas_est:,}  (using {gas:,} with 20% buffer)")
    except Exception as e:
        print(f"         Gas estimation failed ({e}), using fixed 600 000")
        gas = 600_000

    tx = Contract.constructor().build_transaction({
        "chainId":  CHAIN_ID,
        "gas":      gas,
        "gasPrice": w3.eth.gas_price,
        "nonce":    nonce,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"         Tx hash        : {tx_hash.hex()}")
    print("         Waiting for block confirmation (~15 s on Sepolia)...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    addr    = receipt.contractAddress

    print(f"\n{'='*56}")
    print(f"Contract deployed successfully!")
    print(f"{'='*56}")
    print(f"Address  : {addr}")
    print(f"Gas used : {receipt.gasUsed:,}")
    print(f"Block    : {receipt.blockNumber:,}")
    print(f"Tx hash  : {receipt.transactionHash.hex()}")
    print(f"Etherscan: https://sepolia.etherscan.io/address/{addr}")
    print(f"{'='*56}")

    info = {
        "contract_address": addr,
        "tx_hash":          receipt.transactionHash.hex(),
        "block":            receipt.blockNumber,
        "abi":              abi,
        "chain_id":         CHAIN_ID,
    }
    OUT_FILE.write_text(json.dumps(info, indent=2))
    print(f"\nDeployment info saved → {OUT_FILE}")
    print(f"\nAdd to /opt/firmware-gateway/.env:")
    print(f"CONTRACT_ADDRESS={addr}")


# Entry point

def main() -> None:
    print("=" * 56)
    print(f"  FirmwareIntegrity Deployment — Sepolia Testnet")
    print(f"  Compiler: Foundry (forge, native ARM64)")
    print("=" * 56)

    print("\n[1/2] Compiling with Foundry (forge) — native ARM64...")
    abi, bytecode = compile_contract()

    print(f"      ABI entries  : {len(abi)}")
    print(f"      Bytecode size: {len(bytecode)//2} bytes")

    print("\n[2/2] Deploying to Sepolia...")
    deploy(abi, bytecode)


if __name__ == "__main__":
    main()

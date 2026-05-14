#!/usr/bin/env python3
"""
deploy_contract.py
==================
Deploy FirmwareIntegrity.sol to Sepolia — ARM64 / Raspberry Pi compatible.

WHY THE OLD SCRIPT BROKE
  py-solc-x downloads pre-built x86_64 ELF binaries from GitHub releases.
  Those binaries cannot execute on ARM64 (Raspberry Pi 5), producing the
  "downloaded binary would not execute" error regardless of what Python does.

THIS SCRIPT OFFERS THREE STRATEGIES — pick with --strategy A|B|C
  B (default) — Foundry's `forge` CLI, which ships a native ARM64 binary
                and is the cleanest solution on the Pi.
  C           — Compile on a dev machine (x86/Mac), copy the artifact JSON
                to the Pi, then run this script only for the deployment step.
  A           — Bundled ABI; useful only if the contract has not been modified
                and you supply a compiled_artifact.json with the bytecode.

Usage:
    python deploy_contract.py [--strategy B]

Environment variables (set in .env or shell):
    INFURA_URL      https://sepolia.infura.io/v3/<YOUR_KEY>
    PRIVATE_KEY     hex private key of the deployer wallet (no 0x prefix)
    CHAIN_ID        11155111  (Sepolia default)
"""

import argparse
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

# ─── Embedded ABI (matches FirmwareIntegrity.sol as written) ─────────────────
# The ABI describes function signatures only — it does not contain bytecode
# and is therefore architecture-neutral. It is kept here so gateway.py can
# import it without needing the full deployed.json at runtime.

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


# ─── Strategy B: Foundry / forge (ARM64-native, recommended on Pi) ────────────

def compile_with_forge() -> tuple[list, str]:
    """
    Compiles FirmwareIntegrity.sol using the `forge` CLI from Foundry.
    forge ships a pre-built aarch64 binary — no x86 emulation required.

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
        print("    source ~/.bashrc && foundryup")
        print("\nThen re-run this script. Alternatively, use --strategy C to")
        print("compile on a dev machine and copy the artifact here.")
        sys.exit(1)

    print(f"      forge version: {check.stdout.strip()}")

    # Build inside a throwaway directory — avoids needing a full Foundry project
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

    # Normalise: web3.py wants hex without 0x
    if bytecode.startswith("0x"):
        bytecode = bytecode[2:]

    return abi, bytecode


# ─── Strategy C: artifact compiled on a dev machine ──────────────────────────

def load_artifact_file() -> tuple[list, str]:
    """
    Loads a compiled_artifact.json produced on an x86 / Mac dev machine.

    How to produce it (dev machine only — no Pi needed for this step):

      Option 1 — Foundry:
        forge build
        cp out/FirmwareIntegrity.sol/FirmwareIntegrity.json \\
           /path/to/project/scripts/compiled_artifact.json

      Option 2 — Hardhat:
        npx hardhat compile
        cp artifacts/contracts/FirmwareIntegrity.sol/FirmwareIntegrity.json \\
           /path/to/project/scripts/compiled_artifact.json

      Option 3 — solc directly (any platform):
        solc --optimize --optimize-runs 200 --combined-json abi,bin \\
             contracts/FirmwareIntegrity.sol > compiled_artifact.json
        # Then manually reformat to {"abi": [...], "bytecode": "0x..."}

    Copy the file to the Pi and place it alongside this script as
    `scripts/compiled_artifact.json`, then run:
        python deploy_contract.py --strategy C
    """
    artifact_path = Path(__file__).parent / "compiled_artifact.json"
    if not artifact_path.exists():
        print(f"\nERROR: compiled_artifact.json not found at {artifact_path}")
        print("Compile on a dev machine and copy the file here.")
        print("See the docstring in load_artifact_file() for instructions.")
        sys.exit(1)

    print(f"      Loading: {artifact_path}")
    data     = json.loads(artifact_path.read_text())
    abi      = data.get("abi", EMBEDDED_ABI)
    bytecode = data.get("bytecode", "")
    if isinstance(bytecode, dict):
        # Foundry format: {"object": "0x..."}
        bytecode = bytecode.get("object", "")
    if bytecode.startswith("0x"):
        bytecode = bytecode[2:]
    if not bytecode:
        print("ERROR: compiled_artifact.json contains no bytecode.")
        sys.exit(1)

    return abi, bytecode


# ─── Strategy A: ABI-only, requires compiled_artifact.json for bytecode ───────

def load_embedded_abi() -> tuple[list, str]:
    """
    Uses the ABI embedded in this file. Still needs bytecode from somewhere,
    so it falls through to load_artifact_file() for the bytecode portion.
    This is useful when you trust the embedded ABI but have a separately
    compiled bytecode file.
    """
    print("      Using embedded ABI + bytecode from compiled_artifact.json")
    _, bytecode = load_artifact_file()
    return EMBEDDED_ABI, bytecode


# ─── Shared deployment logic ──────────────────────────────────────────────────

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
    print(f"  ✅  Contract deployed successfully!")
    print(f"{'='*56}")
    print(f"  Address  : {addr}")
    print(f"  Gas used : {receipt.gasUsed:,}")
    print(f"  Block    : {receipt.blockNumber:,}")
    print(f"  Tx hash  : {receipt.transactionHash.hex()}")
    print(f"  Etherscan: https://sepolia.etherscan.io/address/{addr}")
    print(f"{'='*56}")

    info = {
        "contract_address": addr,
        "tx_hash":          receipt.transactionHash.hex(),
        "block":            receipt.blockNumber,
        "abi":              abi,
        "chain_id":         CHAIN_ID,
    }
    OUT_FILE.write_text(json.dumps(info, indent=2))
    print(f"\n  Deployment info saved → {OUT_FILE}")
    print(f"\n👉  Add to /opt/firmware-gateway/.env:")
    print(f"    CONTRACT_ADDRESS={addr}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy FirmwareIntegrity.sol to Sepolia (ARM64-compatible)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Strategies:
  B  Compile with Foundry/forge (native ARM64 binary). Recommended.
     Install: curl -L https://foundry.paradigm.xyz | bash && foundryup

  C  Load compiled_artifact.json produced on a dev machine.
     Place the file at scripts/compiled_artifact.json then run with -s C.

  A  Use embedded ABI + bytecode from compiled_artifact.json.
     Useful when the ABI matches but you have a custom bytecode file.
""",
    )
    parser.add_argument(
        "-s", "--strategy", choices=["A", "B", "C"], default="B",
        help="Compilation strategy (default: B — Foundry)",
    )
    args = parser.parse_args()

    print("=" * 56)
    print(f"  FirmwareIntegrity Deployment — Sepolia Testnet")
    print(f"  Strategy: {args.strategy}")
    print("=" * 56)

    if args.strategy == "B":
        print("\n[1/2] Compiling with Foundry (forge) — native ARM64...")
        abi, bytecode = compile_with_forge()
    elif args.strategy == "C":
        print("\n[1/2] Loading compiled_artifact.json...")
        abi, bytecode = load_artifact_file()
    else:
        print("\n[1/2] Using embedded ABI + compiled_artifact.json bytecode...")
        abi, bytecode = load_embedded_abi()

    print(f"      ABI entries  : {len(abi)}")
    print(f"      Bytecode size: {len(bytecode)//2} bytes")

    print("\n[2/2] Deploying to Sepolia...")
    deploy(abi, bytecode)


if __name__ == "__main__":
    main()

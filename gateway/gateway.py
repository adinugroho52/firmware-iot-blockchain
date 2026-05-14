"""
Raspberry Pi 5 Edge Gateway — firmware-blockchain-iot
======================================================
Responsibilities:
  1. Subscribe to MQTT topics published by ESP32 nodes.
  2. On device boot event: query blockchain for registered hash → compare
     against reported hash → publish verification result back.
  3. On OTA-complete event: register new hash on-chain via web3.py.
  4. Expose a minimal REST API (FastAPI) for Home Assistant webhook.
  5. Persist verification history to SQLite for audit trail.

Runtime:  Python 3.11+  (Raspberry Pi OS Bookworm)
Install:  See setup.sh
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ─── CONFIGURATION  ──────────────────────────────────────────────────────────

INFURA_URL        = os.environ["INFURA_URL"]          # https://sepolia.infura.io/v3/<key>
CONTRACT_ADDRESS  = os.environ["CONTRACT_ADDRESS"]    # deployed FirmwareIntegrity address
PRIVATE_KEY       = os.environ["PRIVATE_KEY"]         # owner wallet private key (hex, no 0x)
CHAIN_ID          = int(os.environ.get("CHAIN_ID", "11155111"))  # 11155111 = Sepolia

MQTT_BROKER       = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT         = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER         = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD     = os.environ.get("MQTT_PASSWORD", "")

DB_PATH           = Path(os.environ.get("DB_PATH", "/var/lib/firmware-gateway/audit.db"))
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO")

# ABI — only the functions we call (reduces payload size)
CONTRACT_ABI = [
    {
        "name": "storeFirmwareHash",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "deviceID", "type": "string"},
            {"name": "hash",     "type": "string"},
            {"name": "version",  "type": "string"},
        ],
        "outputs": [],
    },
    {
        "name": "verifyFirmware",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "deviceID", "type": "string"},
            {"name": "hash",     "type": "string"},
        ],
        "outputs": [{"name": "valid", "type": "bool"}],
    },
    {
        "name": "getFirmwareRecord",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "deviceID", "type": "string"}],
        "outputs": [
            {"name": "hash",      "type": "string"},
            {"name": "version",   "type": "string"},
            {"name": "timestamp", "type": "uint256"},
            {"name": "revoked",   "type": "bool"},
        ],
    },
    {
        "name": "revokeFirmware",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "deviceID", "type": "string"}],
        "outputs": [],
    },
]

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("gateway")

# ─── DATABASE ────────────────────────────────────────────────────────────────

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            device_id   TEXT    NOT NULL,
            event       TEXT    NOT NULL,
            fw_hash     TEXT,
            version     TEXT,
            chain_valid INTEGER,   -- NULL=not checked, 0=fail, 1=pass
            tx_hash     TEXT,
            notes       TEXT
        )
    """)
    conn.commit()
    return conn

def log_event(conn: sqlite3.Connection, device_id: str, event: str, **kwargs):
    conn.execute(
        """INSERT INTO audit_log (ts, device_id, event, fw_hash, version,
                                  chain_valid, tx_hash, notes)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            int(time.time()),
            device_id,
            event,
            kwargs.get("fw_hash"),
            kwargs.get("version"),
            kwargs.get("chain_valid"),
            kwargs.get("tx_hash"),
            kwargs.get("notes"),
        ),
    )
    conn.commit()

# ─── BLOCKCHAIN CLIENT ───────────────────────────────────────────────────────

# How many times to retry a transaction that fails with nonce/gas errors
TX_MAX_RETRIES   = 3
# Each retry bumps gas price by this multiplier (must be >1.10 to satisfy
# Ethereum's "replacement transaction underpriced" rule, which requires ≥10%
# increase to replace a pending tx at the same nonce)
TX_GAS_BUMP      = 1.15   # 15% bump per retry
TX_WAIT_TIMEOUT  = 120    # seconds to wait for receipt


class BlockchainClient:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(INFURA_URL))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not self.w3.is_connected():
            raise RuntimeError("Cannot connect to Ethereum node via Infura")
        log.info("Connected to Ethereum. Latest block: %d", self.w3.eth.block_number)

        self.account = self.w3.eth.account.from_key(PRIVATE_KEY)
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI,
        )
        log.info("Contract: %s | Owner: %s", CONTRACT_ADDRESS, self.account.address)

    # ── Read-only calls (no gas) ──────────────────────────────────────────────

    def verify(self, device_id: str, fw_hash: str) -> bool:
        return self.contract.functions.verifyFirmware(device_id, fw_hash).call()

    def get_record(self, device_id: str) -> dict:
        h, v, ts, revoked = self.contract.functions.getFirmwareRecord(device_id).call()
        return {"hash": h, "version": v, "timestamp": ts, "revoked": revoked}

    # ── Nonce helper ──────────────────────────────────────────────────────────

    def _next_nonce(self) -> int:
        """
        Always fetch the *pending* nonce so we account for any transactions
        that were submitted but not yet mined. Using 'pending' rather than
        'latest' prevents reusing a nonce that is already in the mempool,
        which is the root cause of 'replacement transaction underpriced'.
        """
        return self.w3.eth.get_transaction_count(
            self.account.address, block_identifier="pending"
        )

    def _gas_price(self) -> int:
        """Current network gas price with a small safety buffer."""
        return int(self.w3.eth.gas_price * 1.10)

    # ── Generic send-with-retry ───────────────────────────────────────────────

    def _send(self, fn, gas: int) -> str:
        """
        Build, sign, and send a contract call with automatic retry on
        'replacement transaction underpriced' and nonce collision errors.

        On each retry the gas price is multiplied by TX_GAS_BUMP (≥10%),
        satisfying Ethereum's replacement rule. The nonce is re-fetched each
        time so stale-nonce errors also resolve themselves.
        """
        gas_price = self._gas_price()
        last_exc  = None

        for attempt in range(1, TX_MAX_RETRIES + 1):
            nonce = self._next_nonce()
            log.debug("Attempt %d/%d  nonce=%d  gasPrice=%d",
                      attempt, TX_MAX_RETRIES, nonce, gas_price)
            try:
                tx = fn.build_transaction({
                    "chainId":  CHAIN_ID,
                    "gas":      gas,
                    "gasPrice": gas_price,
                    "nonce":    nonce,
                })
                signed  = self.account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = self.w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=TX_WAIT_TIMEOUT
                )
                hex_hash = receipt.transactionHash.hex()
                log.info("tx confirmed: %s (block %d, gas used %d)",
                         hex_hash, receipt.blockNumber, receipt.gasUsed)
                return hex_hash

            except Exception as e:
                last_exc  = e
                err_lower = str(e).lower()
                # Retryable conditions: nonce collision or gas-price too low
                if any(k in err_lower for k in (
                    "replacement transaction underpriced",
                    "nonce too low",
                    "already known",
                    "transaction underpriced",
                )):
                    log.warning("Attempt %d failed (%s) — bumping gas price by %.0f%% and retrying",
                                attempt, e, (TX_GAS_BUMP - 1) * 100)
                    gas_price = int(gas_price * TX_GAS_BUMP)
                else:
                    # Non-retryable (e.g. contract revert, invalid params)
                    raise

        raise RuntimeError(
            f"Transaction failed after {TX_MAX_RETRIES} attempts: {last_exc}"
        )

    # ── Write calls ───────────────────────────────────────────────────────────

    def store_hash(self, device_id: str, fw_hash: str, version: str) -> str:
        log.info("storeFirmwareHash: device=%s version=%s", device_id, version)
        return self._send(
            self.contract.functions.storeFirmwareHash(device_id, fw_hash, version),
            gas=220_000,
        )

    def revoke(self, device_id: str) -> str:
        log.info("revokeFirmware: device=%s", device_id)
        return self._send(
            self.contract.functions.revokeFirmware(device_id),
            gas=90_000,
        )

# ─── MQTT HANDLER ────────────────────────────────────────────────────────────

class GatewayMQTT:
    INTEGRITY_TOPIC = "iot/integrity"
    SENSOR_TOPIC    = "iot/sensors"
    RESULT_TOPIC    = "iot/verify_result"

    def __init__(self, chain: BlockchainClient, db: sqlite3.Connection):
        self.chain  = chain
        self.db     = db
        self.client = mqtt.Client(client_id="rpi-gateway", protocol=mqtt.MQTTv5)
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        self.client.on_connect    = self._on_connect
        self.client.on_message    = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, props):
        log.info("MQTT connected (rc=%s)", reason_code)
        client.subscribe(self.INTEGRITY_TOPIC)
        client.subscribe(self.SENSOR_TOPIC)

    def _on_disconnect(self, client, userdata, flags, reason_code, props):
        log.warning("MQTT disconnected (rc=%s) — will auto-reconnect", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic   = msg.topic
            log.debug("MQTT [%s]: %s", topic, payload)

            if topic == self.INTEGRITY_TOPIC:
                self._handle_integrity(payload)
            elif topic == self.SENSOR_TOPIC:
                self._handle_sensor(payload)
        except Exception as e:
            log.exception("Error processing MQTT message: %s", e)

    def _handle_integrity(self, payload: dict):
        device_id = payload.get("device_id", "")
        event     = payload.get("event", "boot")
        fw_hash   = payload.get("firmware_hash", "")
        version   = payload.get("version", "")

        if not device_id or not fw_hash:
            log.warning("Incomplete integrity payload: %s", payload)
            return

        if event == "boot":
            # Query blockchain to verify reported hash
            valid = self.chain.verify(device_id, fw_hash)
            log.info("[VERIFY] %s hash=%s… valid=%s", device_id, fw_hash[:12], valid)

            result_msg = json.dumps({
                "device_id": device_id,
                "valid":     valid,
                "fw_hash":   fw_hash,
                "ts":        int(time.time()),
            })
            # retain=True so HA immediately gets the last result on subscribe,
            # preventing the sensor from being stuck on 'unknown' after a restart.
            self.client.publish(self.RESULT_TOPIC, result_msg, retain=True)

            log_event(self.db, device_id, "boot_verify",
                      fw_hash=fw_hash, chain_valid=int(valid))

            if not valid:
                log.error("INTEGRITY VIOLATION on %s! Hash not on-chain.", device_id)

        elif event == "ota_complete":
            # Register new hash on-chain after successful OTA
            log.info("[OTA] Registering new hash for %s v%s", device_id, version)
            try:
                tx = self.chain.store_hash(device_id, fw_hash, version)
                log.info("[OTA] Registered. tx=%s", tx)
                log_event(self.db, device_id, "ota_registered",
                          fw_hash=fw_hash, version=version, tx_hash=tx)
            except Exception as e:
                log.exception("[OTA] Failed to register on-chain: %s", e)
                log_event(self.db, device_id, "ota_register_error", notes=str(e))

    def _handle_sensor(self, payload: dict):
        # Lightweight sanity check: hash in sensor frame matches boot-registered hash
        device_id = payload.get("device_id", "")
        fw_hash   = payload.get("fw_hash", "")
        if device_id and fw_hash:
            valid = self.chain.verify(device_id, fw_hash)
            if not valid:
                log.warning("[SENSOR] Unexpected hash from %s — possible tampering!", device_id)

    def start(self):
        self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

# ─── REST API (FastAPI) ──────────────────────────────────────────────────────

# Shared state (set in lifespan)
_chain: BlockchainClient = None
_db:    sqlite3.Connection = None
_mqtt_gw: GatewayMQTT    = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _chain, _db, _mqtt_gw
    _db      = init_db(DB_PATH)
    _chain   = BlockchainClient()
    _mqtt_gw = GatewayMQTT(_chain, _db)
    _mqtt_gw.start()
    log.info("Gateway started")
    yield
    _mqtt_gw.stop()
    _db.close()
    log.info("Gateway stopped")


app = FastAPI(title="Firmware Integrity Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"

@app.get("/", include_in_schema=False)
def serve_dashboard():
    """Serve the web dashboard at the root URL."""
    return FileResponse(DASHBOARD_PATH, media_type="text/html")



class RegisterRequest(BaseModel):
    device_id: str
    fw_hash:   str          # SHA-256 hex of the firmware file
    version:   str


class RevokeRequest(BaseModel):
    device_id: str


@app.get("/health")
def health():
    block = _chain.w3.eth.block_number
    return {"status": "ok", "latest_block": block}


@app.post("/firmware/register")
def register_firmware(req: RegisterRequest):
    """Register (or update) a device's authoritative firmware hash on-chain."""
    if len(req.fw_hash) != 64:
        raise HTTPException(400, "fw_hash must be 64-char hex SHA-256")
    try:
        tx = _chain.store_hash(req.device_id, req.fw_hash, req.version)
        log_event(_db, req.device_id, "api_register",
                  fw_hash=req.fw_hash, version=req.version, tx_hash=tx)
        return {"tx_hash": tx}
    except Exception as e:
        log.exception(e)
        raise HTTPException(500, str(e))


@app.get("/firmware/verify")
def verify_firmware(device_id: str, fw_hash: str):
    """Verify whether a hash matches the on-chain record."""
    valid = _chain.verify(device_id, fw_hash)
    record = _chain.get_record(device_id)
    return {"device_id": device_id, "valid": valid, "record": record}


@app.get("/firmware/record")
def get_record(device_id: str):
    return _chain.get_record(device_id)


@app.post("/firmware/revoke")
def revoke_firmware(req: RevokeRequest):
    try:
        tx = _chain.revoke(req.device_id)
        log_event(_db, req.device_id, "api_revoke", tx_hash=tx)
        return {"tx_hash": tx}
    except Exception as e:
        log.exception(e)
        raise HTTPException(500, str(e))


@app.get("/admin/pending-nonce")
def get_pending_nonce():
    """
    Returns the current pending nonce and network gas price.
    Useful for diagnosing stuck transactions before retrying.
    """
    pending  = _chain.w3.eth.get_transaction_count(_chain.account.address, "pending")
    latest   = _chain.w3.eth.get_transaction_count(_chain.account.address, "latest")
    return {
        "address":       _chain.account.address,
        "nonce_latest":  latest,
        "nonce_pending": pending,
        "stuck_count":   pending - latest,   # txs in mempool not yet mined
        "gas_price_gwei": round(_chain.w3.from_wei(_chain.w3.eth.gas_price, "gwei"), 3),
    }


@app.post("/admin/cancel-pending")
def cancel_pending():
    """
    Cancels all stuck pending transactions by sending zero-value self-transfers
    at each pending nonce with a 20% gas price premium — the standard way to
    flush a stuck mempool without waiting for transactions to expire.
    """
    latest  = _chain.w3.eth.get_transaction_count(_chain.account.address, "latest")
    pending = _chain.w3.eth.get_transaction_count(_chain.account.address, "pending")
    stuck   = pending - latest
    if stuck == 0:
        return {"cancelled": 0, "message": "No stuck transactions"}

    log.warning("Cancelling %d stuck transaction(s) at nonces %d..%d",
                stuck, latest, pending - 1)
    cancelled = []
    gas_price = int(_chain.w3.eth.gas_price * 1.20)   # 20% premium to win replacement

    for nonce in range(latest, pending):
        try:
            tx = {
                "to":       _chain.account.address,   # self-transfer
                "value":    0,
                "gas":      21_000,
                "gasPrice": gas_price,
                "nonce":    nonce,
                "chainId":  CHAIN_ID,
            }
            signed  = _chain.account.sign_transaction(tx)
            tx_hash = _chain.w3.eth.send_raw_transaction(signed.raw_transaction)
            cancelled.append({"nonce": nonce, "tx_hash": tx_hash.hex()})
            log.info("Cancellation tx submitted: nonce=%d tx=%s", nonce, tx_hash.hex())
        except Exception as e:
            log.error("Failed to cancel nonce %d: %s", nonce, e)
            cancelled.append({"nonce": nonce, "error": str(e)})

    return {"cancelled": len(cancelled), "transactions": cancelled}


@app.get("/audit")
def get_audit(device_id: str | None = None, limit: int = 50):
    """Return recent audit log entries."""
    if device_id:
        rows = _db.execute(
            "SELECT * FROM audit_log WHERE device_id=? ORDER BY ts DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    else:
        rows = _db.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    cols = ["id","ts","device_id","event","fw_hash","version","chain_valid","tx_hash","notes"]
    return [dict(zip(cols, r)) for r in rows]


# ─── STANDALONE ENTRY POINT ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway:app", host="0.0.0.0", port=8000, log_level=LOG_LEVEL.lower())

#!/usr/bin/env python3
"""Web-Frontend für den Autopilot: Wallet verbinden, Bot starten - fertig.

  python server.py            ->  http://127.0.0.1:8000

Wallet-Connect-Flow (der Haupt-Wallet-Key verlässt NIE den Browser):
  1. MetaMask connect           -> Adresse fürs Monitoring
  2. /api/agent/create          -> Server erzeugt frisches Agent-Wallet,
                                   liefert EIP-712 Typed Data für approveAgent
  3. MetaMask signTypedData_v4  -> Nutzer signiert die Bot-Vollmacht
  4. /api/agent/approve         -> Server reicht die Signatur bei Hyperliquid
                                   ein; der Agent-Key bleibt nur auf dem Server
  5. Autopilot starten          -> alles Weitere läuft im Hintergrund

Sicherheit: Der Server bindet standardmäßig auf 127.0.0.1. NIEMALS ungeschützt
ins Internet stellen - er hält den Agent-Key und steuert den Bot.
"""

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from bot.autopilot import Autopilot
from bot.config import ROOT, load_config
from bot.exchange import api_url

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler("autopilot.log")])
log = logging.getLogger(__name__)

cfg = load_config()
autopilot = Autopilot(cfg)
app = FastAPI(title="Hyperliquid Trading Bot")
STATIC = Path(__file__).parent / "static"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Optionaler Zugriffsschutz: DASHBOARD_TOKEN in .env setzen, wenn das UI
# über Tailscale/SSH-Tunnel von unterwegs erreichbar sein soll. Ohne Token
# bleibt alles wie gehabt (nur sinnvoll auf 127.0.0.1).
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")


@app.middleware("http")
async def require_token(request: Request, call_next):
    if DASHBOARD_TOKEN and request.url.path.startswith("/api"):
        supplied = request.headers.get("x-auth-token", "")
        if not secrets.compare_digest(supplied, DASHBOARD_TOKEN):
            return JSONResponse({"detail": "Token fehlt oder falsch"}, status_code=401)
    return await call_next(request)

# Schwebende Agent-Approvals: nonce -> (agent_key, action)
_pending: dict[int, tuple[str, dict]] = {}


class WalletBody(BaseModel):
    address: str


class AgentCreateBody(BaseModel):
    address: str
    chain_id: str = "0xa4b1"  # aktive Chain der Wallet (eth_chainId), Default Arbitrum One


class ApproveBody(BaseModel):
    address: str
    nonce: int
    signature: str  # 65-Byte-Hex-Signatur aus eth_signTypedData_v4


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    s = autopilot.status()
    s["autopilot_running"] = autopilot.running
    s["wallet"] = autopilot.account_address
    env_text = (ROOT / ".env").read_text() if (ROOT / ".env").exists() else ""
    # echter 32-Byte-Key zählt, nicht der "0x..."-Platzhalter aus .env.example
    s["agent_ready"] = bool(re.search(r"^HL_PRIVATE_KEY=0x[0-9a-fA-F]{64}\s*$", env_text, re.M))
    s["config"] = {
        "dry_run": cfg.dry_run,
        "network": cfg.network,
        "max_leverage": cfg.risk.max_leverage,
        "copy_ratio": cfg.copytrade.copy_ratio,
        "max_daily_loss": cfg.risk.max_daily_loss,
    }
    return s


@app.get("/api/journal")
def journal(limit: int = 25):
    """Letzte Bot-Entscheidungen (Orders, Vetos, Rotationen) fürs Dashboard."""
    return autopilot.journal.tail(min(limit, 200))


@app.post("/api/paper/reset")
def paper_reset():
    """Setzt das Paper-Konto zurück (nur im Dry-Run, nur bei gestopptem Bot)."""
    if not cfg.dry_run:
        raise HTTPException(400, "Nur im Dry-Run-Modus verfügbar")
    if autopilot.running:
        raise HTTPException(400, "Erst den Autopilot stoppen")
    from bot.paper import PaperBroker

    PaperBroker(cfg.backtest.initial_equity, cfg.backtest.fee_rate).reset()
    for name in ("history.jsonl", "trades.jsonl", "leader_perf.json"):
        (Path(__file__).parent / "runtime" / name).unlink(missing_ok=True)
    log.info("Paper-Konto und Verlaufsdaten zurückgesetzt")
    return {"ok": True}


@app.get("/api/history")
def history(limit: int = 500):
    """Equity-Kurve für das Dashboard (letzte `limit` Minuten-Punkte)."""
    path = Path(__file__).parent / "runtime" / "history.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@app.post("/api/wallet")
def set_wallet(body: WalletBody):
    if not ADDR_RE.match(body.address):
        raise HTTPException(400, "Ungültige Adresse")
    autopilot.account_address = body.address
    log.info("Wallet verbunden: %s", body.address)
    return {"ok": True, "address": body.address}


@app.post("/api/agent/create")
def agent_create(body: AgentCreateBody):
    """Erzeugt ein Agent-Wallet und liefert die zu signierende EIP-712-Struktur.

    Struktur wie hyperliquid.utils.signing.sign_agent - mit einem Unterschied:
    Die signatureChainId ist die AKTIVE Chain der Nutzer-Wallet (MetaMask
    verweigert Signaturen für fremde Chain-IDs); Hyperliquid akzeptiert
    laut SDK jede Signatur-Chain, solange Aktion und Signatur übereinstimmen.
    """
    if not ADDR_RE.match(body.address):
        raise HTTPException(400, "Ungültige Adresse")
    try:
        chain_id_int = int(body.chain_id, 16)
    except ValueError:
        raise HTTPException(400, "Ungültige chain_id")
    from eth_account import Account

    agent_key = "0x" + secrets.token_hex(32)
    agent_address = Account.from_key(agent_key).address
    nonce = int(time.time() * 1000)
    action = {
        "type": "approveAgent",
        "hyperliquidChain": "Mainnet" if not cfg.is_testnet else "Testnet",
        "signatureChainId": body.chain_id,
        "agentAddress": agent_address,
        "agentName": "autopilot",
        "nonce": nonce,
    }
    _pending[nonce] = (agent_key, action)
    typed_data = {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": chain_id_int,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "HyperliquidTransaction:ApproveAgent": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "agentAddress", "type": "address"},
                {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"},
            ],
        },
        "primaryType": "HyperliquidTransaction:ApproveAgent",
        "message": {
            "hyperliquidChain": action["hyperliquidChain"],
            "agentAddress": agent_address,
            "agentName": "autopilot",
            "nonce": nonce,
        },
    }
    return {"typed_data": typed_data, "nonce": nonce, "agent_address": agent_address}


@app.post("/api/agent/approve")
def agent_approve(body: ApproveBody):
    """Reicht die MetaMask-Signatur bei Hyperliquid ein und speichert den Agent-Key."""
    if body.nonce not in _pending:
        raise HTTPException(400, "Unbekannte oder abgelaufene Approval-Anfrage")
    agent_key, action = _pending.pop(body.nonce)

    sig = body.signature.removeprefix("0x")
    if len(sig) != 130:
        raise HTTPException(400, "Ungültige Signatur")
    r, s, v = "0x" + sig[:64], "0x" + sig[64:128], int(sig[128:130], 16)
    if v < 27:
        v += 27

    payload = {
        "action": action,
        "nonce": body.nonce,
        "signature": {"r": r, "s": s, "v": v},
        "vaultAddress": None,
    }
    resp = requests.post(f"{api_url(cfg.is_testnet)}/exchange", json=payload, timeout=20)
    data = resp.json() if resp.ok else {"status": "err", "response": resp.text}
    if data.get("status") != "ok":
        raise HTTPException(502, f"Hyperliquid hat die Approval abgelehnt: {data}")

    _write_env(agent_key, body.address)
    autopilot.account_address = body.address
    log.info("Agent-Wallet für %s freigeschaltet und gespeichert", body.address)
    return {"ok": True}


@app.post("/api/autopilot/start")
def start():
    if autopilot.running:
        return {"ok": True, "already_running": True}
    if not cfg.is_testnet and not cfg.dry_run:
        # Mainnet-Live nur, wenn der Nutzer es in config.yaml UND hier bestätigt hat
        log.warning("Start auf MAINNET LIVE angefordert")
    autopilot.start()
    return {"ok": True}


@app.post("/api/autopilot/stop")
def stop():
    autopilot.stop()
    return {"ok": True}


def _write_env(agent_key: str, address: str) -> None:
    env = ROOT / ".env"
    lines = []
    if env.exists():
        lines = [l for l in env.read_text().splitlines()
                 if not l.startswith(("HL_PRIVATE_KEY=", "HL_ACCOUNT_ADDRESS="))]
    lines += [f"HL_PRIVATE_KEY={agent_key}", f"HL_ACCOUNT_ADDRESS={address}"]
    env.write_text("\n".join(lines) + "\n")
    env.chmod(0o600)


if __name__ == "__main__":
    if cfg.autopilot.autostart:
        # 24/7-Betrieb (VPS/systemd): nach jedem (Neu-)Start sofort weitermachen,
        # ohne dass jemand im Dashboard auf "Start" klicken muss.
        log.info("Autostart aktiv - Autopilot startet sofort")
        autopilot.start()
    print(f"\n  Dashboard: http://{cfg.autopilot.server_host}:{cfg.autopilot.server_port}\n")
    uvicorn.run(app, host=cfg.autopilot.server_host, port=cfg.autopilot.server_port, log_level="warning")

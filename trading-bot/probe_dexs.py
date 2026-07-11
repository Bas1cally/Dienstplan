#!/usr/bin/env python3
"""Capability-Probe: Welche Perp-DEXs lassen FREMDE Trader scannen?

  python3 probe_dexs.py

Read-only, handelt nie, schreibt nie, braucht keine Keys. Beantwortet empirisch
die Frage, die kein Doku-Lesen sicher klärt: Kann man bei Extended/Lighter/
Variational die Top-Trader listen UND deren offene Positionen ohne ihren
API-Key lesen? Nur was hier ✓ ist, taugt als Copy-Quelle (Ausführung bleibt HL).

Hyperliquid läuft als Kontrolle mit (dort ist bekannt: alles ✓).

Endpunkte sind unten als CONFIG editierbar: Stimmt eine geratene Leaderboard-URL
nicht, öffne die Venue-Website, kopiere im Browser-Network-Tab die echte
XHR-URL und trage sie hier ein - dann Probe erneut laufen lassen.
"""

import argparse
import json
import re
import sys

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(s: str) -> str:
    """ANSI-Farbcodes strippen (für Telegram/Logs)."""
    return _ANSI.sub("", s)

# ------------------------------------------------------------------ CONFIG ---
# Kandidaten-Endpunkte je Venue. `sample` = eine Trader-Kennung zum Testen des
# Fremd-Positions-Zugriffs (Adresse/Account-Index/Vault - venue-spezifisch).
# Leere/geratene Werte darf/soll der Nutzer mit echten aus dem Network-Tab ersetzen.
VENUES = {
    "hyperliquid": {  # Kontrolle: bekannt transparent
        "markets": ("POST", "https://api.hyperliquid.xyz/info", {"type": "allMids"}),
        "leaderboard": ("GET", "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", None),
        "positions": ("POST", "https://api.hyperliquid.xyz/info",
                      {"type": "clearinghouseState", "user": "{sample}"}),
        "fills": ("POST", "https://api.hyperliquid.xyz/info",
                  {"type": "userFills", "user": "{sample}"}),
        "sample": "0x0000000000000000000000000000000000000000",
        "pos_keys": ["assetPositions", "marginSummary"],
    },
    "extended": {  # StarkEx - Konto-Endpunkte laut Doku key-pflichtig; Leaderboard prüfen
        "markets": ("GET", "https://api.extended.exchange/api/v1/info/markets", None),
        "leaderboard": ("GET", "https://api.extended.exchange/api/v1/info/leaderboard", None),
        "positions": ("GET", "https://api.extended.exchange/api/v1/info/positions?account={sample}", None),
        "fills": ("GET", "https://api.extended.exchange/api/v1/info/trades?account={sample}", None),
        "sample": "",   # <- echte Account-ID aus dem Leaderboard hier eintragen
        "pos_keys": ["positions", "position", "size"],
    },
    "lighter": {  # zk-rollup - Account per Index?
        "markets": ("GET", "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks", None),
        "leaderboard": ("GET", "https://mainnet.zklighter.elliot.ai/api/v1/leaderboard", None),
        "positions": ("GET", "https://mainnet.zklighter.elliot.ai/api/v1/account?by=index&value={sample}", None),
        "fills": ("GET", "https://mainnet.zklighter.elliot.ai/api/v1/trades?account_index={sample}", None),
        "sample": "1",
        "pos_keys": ["positions", "position", "collateral"],
    },
    "variational": {  # RFQ/P2P, sehr neu - öffentliches Positions-Feed fraglich
        "markets": ("GET", "https://api.variational.io/v1/markets", None),
        "leaderboard": ("GET", "https://api.variational.io/v1/leaderboard", None),
        "positions": ("GET", "https://api.variational.io/v1/positions?address={sample}", None),
        "fills": ("GET", "https://api.variational.io/v1/trades?address={sample}", None),
        "sample": "",
        "pos_keys": ["positions", "position"],
    },
}

OK, WARN, FAIL, SKIP = "\033[92m✓\033[0m", "\033[93m?\033[0m", "\033[91m✗\033[0m", "−"


def _http(method: str, url: str, body):
    import requests

    if method == "POST":
        r = requests.post(url, json=body, timeout=15)
    else:
        r = requests.get(url, timeout=15)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json")
                           else r.text[:400])


def _classify(status: int, payload) -> tuple[str, str]:
    """(-> symbol, kurz-notiz). 401/403 = auth nötig; 2xx mit Inhalt = offen."""
    if status in (401, 403):
        return WARN, f"{status} auth nötig"
    if status == 404:
        return FAIL, "404 nicht vorhanden"
    if 200 <= status < 300:
        if payload in (None, "", [], {}):
            return WARN, f"{status} leer"
        return OK, f"{status} offen"
    return FAIL, str(status)


def _has_positions(payload, keys: list[str]) -> bool:
    blob = json.dumps(payload)[:20000].lower() if not isinstance(payload, str) else payload.lower()
    return any(k.lower() in blob for k in keys)


def probe_venue(name: str, cfg: dict, fetch=_http) -> dict:
    result = {"venue": name}
    sample = cfg.get("sample") or ""

    def run(kind):
        spec = cfg.get(kind)
        if not spec:
            return SKIP, "kein Endpoint konfiguriert", None
        method, url, body = spec
        needs_sample = "{sample}" in (url + json.dumps(body or ""))
        if needs_sample and not sample:
            return SKIP, "keine Sample-ID gesetzt", None
        url = url.replace("{sample}", sample)
        b = json.loads(json.dumps(body).replace("{sample}", sample)) if body else None
        try:
            status, payload = fetch(method, url, b)
        except Exception as e:
            return FAIL, f"Netzfehler: {str(e)[:50]}", None
        sym, note = _classify(status, payload)
        return sym, note, payload

    sym, note, _ = run("markets");        result["reachable"] = (sym, note)
    sym, note, _ = run("leaderboard");    result["leaderboard"] = (sym, note)
    sym, note, pl = run("positions")
    # Fremd-Positionen nur dann ✓, wenn 2xx UND positions-artige Felder enthalten
    if sym == OK and not _has_positions(pl, cfg.get("pos_keys", [])):
        sym, note = WARN, note + " (keine pos-Felder)"
    result["foreign_positions"] = (sym, note)
    sym, note, _ = run("fills");          result["foreign_fills"] = (sym, note)
    return result


def verdict(r: dict) -> str:
    fp = r["foreign_positions"][0]
    if fp == OK:
        return "→ als Copy-Quelle NUTZBAR (fremde Positionen offen)"
    if fp == WARN and "auth" in r["foreign_positions"][1]:
        return "→ nur mit API-Key des Kontos (Watchlist-Variante nötig)"
    if r["leaderboard"][0] == OK:
        return "→ Leaderboard offen, aber Positionen nicht - Endpunkt im Network-Tab prüfen"
    return "→ (noch) nicht nutzbar - echte Endpunkte/Sample-ID eintragen und erneut proben"


def summarize(only: str | None = None, fetch=_http) -> str:
    """Kompakte, Telegram-taugliche Matrix (ohne ANSI). Für den /probe-Befehl."""
    lines = ["🔍 <b>Multi-DEX-Probe</b> (read-only)"]
    usable = []
    for name, cfg in VENUES.items():
        if only and name != only:
            continue
        r = probe_venue(name, cfg, fetch=fetch)
        rc, lb = _plain(r["reachable"][0]), _plain(r["leaderboard"][0])
        fp, fpn = _plain(r["foreign_positions"][0]), r["foreign_positions"][1]
        lines.append(f"\n<b>{name}</b>: API {rc} | Leaderboard {lb} | Positionen {fp} ({fpn})")
        lines.append(f"  {verdict(r)}")
        if r["foreign_positions"][0] == OK:
            usable.append(name)
    lines.append("\n" + ("✓ scanbar: " + ", ".join(usable) if usable
                         else "Keine Venue offen scanbar - echte Endpunkte/Sample-IDs nötig."))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="nur diese Venue proben (z.B. extended)")
    args = p.parse_args()

    print("\n=== Multi-DEX Trader-Scan-Probe (read-only) ===\n")
    cols = ("reachable", "leaderboard", "foreign_positions", "foreign_fills")
    print(f"  {'Venue':13s} {'API':16s} {'Leaderboard':16s} {'Fremd-Positionen':22s} Fills")
    any_usable = False
    for name, cfg in VENUES.items():
        if args.only and name != args.only:
            continue
        r = probe_venue(name, cfg)
        cells = [f"{r[c][0]} {r[c][1]}" for c in cols]
        print(f"  {name:13s} {cells[0]:16s} {cells[1]:16s} {cells[2]:24s} {cells[3]}")
        v = verdict(r)
        print(f"  {'':13s} {v}")
        any_usable = any_usable or r["foreign_positions"][0] == OK
        print()

    print("Legende:  ✓ offen   ? auth/leer/unklar   ✗ nicht vorhanden   − nicht konfiguriert")
    print("Nur '✓ Fremd-Positionen' taugt als Scan-Quelle. Bei '?' die echte URL aus dem")
    print("Browser-Network-Tab der Venue-Website oben in VENUES eintragen und erneut proben.")
    sys.exit(0 if any_usable else 2)


if __name__ == "__main__":
    main()

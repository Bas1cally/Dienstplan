"""Sprint-Buch v2: frisches Leader-Signal -> einsteigen -> HALTEN -> +100$ TP.

Zustandsmaschine statt Dauer-Reconciliation (v1 hat mit plan_rebalance je Tick
139 Trades produziert - bei 10x Hebel frisst das Fees):

  FLACH   - scannt ALLE Leader der Rotation (LARP-gefiltert), nicht nur den
            besten - sonst passiert tagelang nichts, wenn genau der eine
            gerade pausiert. Bestehende Positionen zählen NICHT (nie in
            laufende Trades einsteigen) - jeder Leader bekommt beim ersten
            Blick eine eigene Baseline. Erst ein Übergang 0 -> Position ist
            ein frisches Signal; melden mehrere Leader gleichzeitig, gewinnt
            der mit dem höchsten Score. Einstieg mit leverage-fachem
            Exposure, dieser Leader ist für den Ritt fixiert.
  IM RITT - kein Rebalancing. Exits NUR wenn: der Leader den Coin komplett
            schließt oder flippt (mitgehen - sein Edge ist das Exit-Timing),
            der Leader aus der Rotation fällt (wir wären blind), RISK_OFF,
            oder das Zyklus-Ziel/Bust greift. Größenänderungen: ignorieren.

Zyklus: startet mit 1000$; Ziel-PnL kumuliert über beliebig viele Ritte.
  Equity >= 1100 -> Take-Profit, Gewinn "banked", Konto-Reset, nächster Zyklus
  beobachtet den DANN besten Leader (Rotation je Zyklus).
  Equity <= 5%   -> Zyklus geplatzt (10x-Liquidations-Modell), ebenso Reset.

Nur Paper-Modus; Fees konservativ als Taker.
"""

import json
import logging
import time

from .journal import RUNTIME
from .paper import PaperBroker

log = logging.getLogger(__name__)


class SprintBook:
    def __init__(self, cfg, fee_rate: float, notifier=None, journal=None,
                 runtime_dir=None, clock=time.time):
        runtime = runtime_dir or RUNTIME
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.clock = clock
        self.paper = PaperBroker(cfg.equity, fee_rate, path=runtime / "sprint_book.json")
        self.state_path = runtime / "sprint_cycles.json"
        self.banked = 0.0
        self.won = 0
        self.busted = 0
        self.total_trades = 0            # über alle abgeschlossenen Zyklen
        self.ride_leader = ""            # fixiert, solange Positionen offen sind
        self.strikes: dict[str, int] = {}   # addr -> aktive Strikes (LARP-Enttarnung)
        self.banned: set[str] = set()       # fürs Sprint-Buch gesperrte Leader
        self._ride_start_equity: float | None = None  # Equity bei Ritt-Beginn (PnL-Attribution)
        self._ride_entry_sizes: dict[str, float] = {}  # coin -> |Leader-Größe| beim Einstieg
        # Baseline je Leader (addr -> coin -> signierte Größe): nur Übergänge
        # 0 -> Position NACH der Baseline sind frische Signale. Läuft für ALLE
        # Rotations-Leader mit (auch während eines Ritts), damit nach dem Ritt
        # keine längst laufenden Positionen fälschlich als "frisch" gelten.
        self._baselines: dict[str, dict[str, float]] = {}
        self._migrate_v1_or_load()

    # ---------- Tick (Zustandsmaschine) ----------

    def tick(self, leaders: list[dict], snapshots: list, prices: dict[str, float],
             risk_off: bool = False) -> None:
        if not prices:
            return
        # 1. Zyklus-Ende hat Vorrang (auch flach möglich: kumulierte Ritte >= Ziel)
        eq = self.paper.equity(prices)
        if eq >= self.cfg.equity + self.cfg.target_profit:
            self._end_cycle(prices, won=True)
            return
        if eq <= self.cfg.equity * self.cfg.bust_frac:
            self._end_cycle(prices, won=False)
            return
        # 2. Markt-Schutz: glattstellen, Zyklus läuft weiter (kein Wiederspiegeln)
        if risk_off:
            if self.paper.sizes():
                log.warning("Sprint: RISK_OFF - stelle glatt (Zyklus läuft weiter)")
                for coin in list(self.paper.sizes()):
                    self._close_coin(coin, prices, "risk_off")
                # Markt-Event, nicht Leader-Schuld -> kein Strike, nur zurücksetzen
                self.ride_leader = ""
                self._ride_start_equity = None
                self._ride_entry_sizes.clear()
                self._save_state()
            return
        if not leaders or not snapshots:
            return

        if self.paper.sizes():
            self._tick_riding(snapshots, prices)
        else:
            self._tick_waiting(leaders, snapshots, prices)
        # Baselines ALLER Rotations-Leader aktuell halten (auch während eines
        # Ritts) und Ausgeschiedene vergessen - sonst gelten deren während des
        # Ritts eröffnete Positionen später fälschlich als "frisch".
        self._refresh_baselines(leaders, snapshots)

    # ---------- FLACH: alle Leader auf frische Signale scannen ----------

    def _tick_waiting(self, leaders: list[dict], snapshots: list,
                      prices: dict[str, float]) -> None:
        by_addr = {s.address.lower(): s for s in snapshots}
        # Höchster Score zuerst: melden mehrere Leader gleichzeitig, gewinnt der Beste
        for l in sorted(leaders, key=lambda l: float(l.get("score", 0)), reverse=True):
            addr = str(l.get("address", "")).lower()
            if addr in self.banned:
                continue  # LARP-enttarnt: seine frischen Signale zählen nicht mehr
            snap = by_addr.get(addr)
            if snap is None:
                continue
            prev = self._baselines.get(snap.address.lower())
            if prev is None:
                continue  # erster Blick: _refresh_baselines legt die Baseline an
            book = self._book_of(snap)
            fresh = [c for c, sz in book.items() if sz != 0 and prev.get(c, 0.0) == 0.0]
            if not fresh:
                continue
            for coin in fresh:
                self._enter(coin, snap, prices)
            if self.paper.sizes():
                return  # eingestiegen: dieser Leader ist der Ritt

    # ---------- IM RITT: halten, nur dem Ride-Leader folgen ----------

    def _tick_riding(self, snapshots: list, prices: dict[str, float]) -> None:
        snap = next((s for s in snapshots
                     if s.address.lower() == self.ride_leader.lower()), None)
        if snap is None:
            # Leader aus der Rotation gefallen: wir wären blind -> schließen,
            # zurück auf FLACH (Zyklus-PnL bleibt stehen, nächster Tick wartet neu)
            log.warning("Sprint: Ride-Leader %s aus der Rotation - schließe Positionen",
                        self.ride_leader[:10])
            for coin in list(self.paper.sizes()):
                self._close_coin(coin, prices, "leader_rotated")
            self._settle_ride(prices)
            return
        book = self._book_of(snap)
        prev = self._baselines.get(snap.address.lower())

        # Exit-Folge: Leader komplett raus / geflippt / >= partial_exit_frac abgebaut
        for coin, our_size in list(self.paper.sizes().items()):
            leader_sz = book.get(coin, 0.0)
            entry_sz = self._ride_entry_sizes.get(coin, abs(leader_sz))
            if leader_sz == 0.0:
                self._close_coin(coin, prices, "leader_exit")
            elif (leader_sz > 0) != (our_size > 0):
                # Flip = mitgehen: alte Richtung schließen UND die neue eröffnen
                self._close_coin(coin, prices, "leader_flip")
                self._enter(coin, snap, prices)
            elif abs(leader_sz) <= (1 - self.cfg.partial_exit_frac) * entry_sz:
                # Scale-out: Leader hat den Großteil abgebaut -> wir gehen mit
                self._close_coin(coin, prices, "leader_scaleout")
        # Weitere frische Einstiege desselben Leaders mitnehmen (nicht ohne Baseline,
        # z.B. direkt nach Neustart - dann erst re-baselinen, kein Fehl-Einstieg)
        if prev is not None:
            fresh = [c for c, sz in book.items()
                     if sz != 0 and prev.get(c, 0.0) == 0.0
                     and c not in self.paper.sizes()]
            for coin in fresh:
                self._enter(coin, snap, prices)

        if not self.paper.sizes():
            self._settle_ride(prices)   # alle Ritte beendet -> abrechnen + FLACH

    def _refresh_baselines(self, leaders: list[dict], snapshots: list) -> None:
        keep = {str(l.get("address", "")).lower() for l in leaders}
        keep.add(self.ride_leader.lower())
        for s in snapshots:
            if s.address.lower() in keep:
                self._baselines[s.address.lower()] = self._book_of(s)
        for addr in list(self._baselines):
            if addr not in keep:
                self._baselines.pop(addr, None)

    # ---------- Ein-/Ausstieg ----------

    def _enter(self, coin: str, snap, prices: dict[str, float]) -> None:
        if coin in self.cfg.exclude_coins:
            return  # Beta statt Leader-Alpha (z.B. BTC) - kein Signal
        price = prices.get(coin)
        if not price or price <= 0:
            return
        equity = self.paper.equity(prices)
        target = snap.exposure(coin) * self.cfg.leverage * equity
        # Gross-Cap: Gesamtbuch bleibt unter leverage x Equity
        gross = sum(abs(s) * prices.get(c, 0.0) for c, s in self.paper.sizes().items())
        headroom = max(0.0, self.cfg.leverage * equity - gross)
        notional = max(-headroom, min(headroom, target))
        if abs(notional) < self.cfg.min_notional:
            return
        if self._ride_start_equity is None:
            self._ride_start_equity = equity   # erster Einstieg des Ritts -> PnL-Basis
        self.paper.execute(coin, notional / price, price)
        self.ride_leader = snap.address
        self._ride_entry_sizes[coin] = abs(self._book_of(snap).get(coin, 0.0))
        side = "LONG" if notional > 0 else "SHORT"
        cycle = self.won + self.busted + 1
        log.info("Sprint: %s %s $%.0f (frisches Signal von %s, Zyklus %d)",
                 side, coin, abs(notional), snap.address[:10], cycle)
        if self.journal:
            self.journal.record("sprint_entry", coin=coin, side=side,
                                notional=round(abs(notional), 0),
                                leader=snap.address, cycle=cycle)
        if self.notifier:
            self.notifier.send(f"🟢 <b>Sprint-Einstieg</b>: {side} {coin} "
                               f"${abs(notional):,.0f}\nLeader <code>{snap.address[:10]}…</code> "
                               f"| Zyklus {cycle}")
        self._save_state()

    def _settle_ride(self, prices: dict[str, float]) -> None:
        """Ritt beendet (flach): PnL dem Ride-Leader zuschreiben - Verlust = Strike,
        Gewinn heilt einen Strike. 2 Strikes -> LARP-enttarnt, gesperrt."""
        leader = self.ride_leader
        start = self._ride_start_equity
        self.ride_leader = ""
        self._ride_entry_sizes.clear()
        self._ride_start_equity = None
        if leader and start is not None:
            ride_pnl = self.paper.equity(prices) - start
            key = leader.lower()
            if ride_pnl < 0:
                self.strikes[key] = self.strikes.get(key, 0) + 1
                n = self.strikes[key]
                log.warning("Sprint: Verlust-Ritt %+.2f -> Strike %d für %s",
                            ride_pnl, n, leader[:10])
                if self.journal:
                    self.journal.record("sprint_strike", leader=leader, strikes=n,
                                        ride_pnl=round(ride_pnl, 2))
                if n >= self.cfg.strike_ban and key not in self.banned:
                    self.banned.add(key)
                    if self.journal:
                        self.journal.record("sprint_ban", leader=leader)
                    if self.notifier:
                        self.notifier.send(
                            f"🚫 <b>Leader enttarnt</b> <code>{leader[:10]}…</code>\n"
                            f"{n} Verlust-Ritte in Folge - fürs Sprint-Buch gesperrt.")
                elif self.notifier:
                    self.notifier.send(f"⚠️ Strike {n}/{self.cfg.strike_ban} für "
                                       f"<code>{leader[:10]}…</code> (Verlust-Ritt {ride_pnl:+,.2f}$)")
            elif ride_pnl > 0 and self.strikes.get(key):
                self.strikes[key] = max(0, self.strikes[key] - 1)  # profitabel heilt
        self._save_state()

    def _close_coin(self, coin: str, prices: dict[str, float], reason: str) -> None:
        size = self.paper.sizes().get(coin, 0.0)
        price = prices.get(coin)
        if size == 0.0 or not price:
            return
        self.paper.execute(coin, -size, price)
        cycle_pnl = self.paper.equity(prices) - self.cfg.equity
        reason_txt = {"leader_exit": "Leader raus", "leader_flip": "Leader gedreht",
                      "leader_rotated": "Leader rotiert", "risk_off": "RISK_OFF"}.get(reason, reason)
        log.info("Sprint: Exit %s (%s), Zyklus-PnL %+.2f", coin, reason_txt, cycle_pnl)
        if self.journal:
            self.journal.record("sprint_exit", coin=coin, reason=reason,
                                cycle_pnl=round(cycle_pnl, 2))
        if self.notifier:
            self.notifier.send(f"🔴 <b>Sprint-Exit</b> {coin}: {reason_txt}\n"
                               f"Zyklus-PnL {cycle_pnl:+,.2f} $")

    # ---------- Zyklus-Ende ----------

    def _end_cycle(self, prices: dict[str, float], won: bool) -> None:
        self.paper.flatten(prices)
        final = self.paper.equity(prices)
        pnl = final - self.cfg.equity
        cycle = self.won + self.busted + 1
        self.banked += pnl
        self.total_trades += self.paper.trades
        if won:
            self.won += 1
        else:
            self.busted += 1
        kind = "sprint_tp" if won else "sprint_bust"
        log.warning("Sprint-Zyklus %d %s: PnL %+.2f (banked gesamt %+.2f)",
                    cycle, "ZIEL ERREICHT" if won else "GEPLATZT", pnl, self.banked)
        if self.journal:
            self.journal.record(kind, cycle=cycle, pnl=round(pnl, 2),
                                banked=round(self.banked, 2),
                                leader=self.ride_leader)
        if self.notifier:
            head = "🏁 <b>Sprint-Zyklus {c}: Ziel erreicht</b>" if won else \
                   "💥 <b>Sprint-Zyklus {c}: geplatzt</b> (10x-Liquidations-Modell)"
            self.notifier.send(
                head.format(c=cycle) + f"\nPnL {pnl:+,.2f} $ | Bilanz: {self.won}✅ "
                f"{self.busted}💥 | banked {self.banked:+,.2f} $\nNeuer Zyklus wartet "
                f"auf frisches Signal des besten Leaders."
            )
        self.paper.reset()
        self.ride_leader = ""
        self._ride_start_equity = None
        self._ride_entry_sizes.clear()
        self._baselines.clear()
        self._save_state()

    def close(self, prices: dict[str, float]) -> int:
        """Manueller Not-Ausstieg (/sprint close): Ritt zu, Zyklus läuft weiter,
        KEIN Strike (war deine Entscheidung, nicht der Leader). Gibt Anzahl zu."""
        n = len(self.paper.sizes())
        for coin in list(self.paper.sizes()):
            self._close_coin(coin, prices, "manual")
        self.ride_leader = ""
        self._ride_start_equity = None
        self._ride_entry_sizes.clear()
        self._save_state()
        return n

    # ---------- Status & Persistenz ----------

    def stats(self, prices: dict[str, float]) -> dict:
        # KEIN Sonderfall für leere prices: equity()/position_rows() degradieren
        # selbst schon sauber (Preis fehlt -> Entry-Preis bzw. 0 als Fallback,
        # nie ein Crash). Ein "if prices else leer"-Sonderfall hier würde eine
        # ECHTE offene Position kurz nach einem Neustart (bevor frische Preise
        # da sind) fälschlich als 'wartet auf frisches Signal' zeigen - genau
        # der Bug, der hier gefunden wurde.
        eq = self.paper.equity(prices)
        # EINE Abfrage des Positionsbestands, held/state/positions leiten sich alle
        # daraus ab - der Hintergrund-Loop (eigener Thread) kann jederzeit einen
        # Ritt schließen; zwei getrennte self.paper-Aufrufe könnten sonst
        # auseinanderlaufen (state="hält" während positions bereits leer ist).
        positions = self.paper.position_rows(prices)
        held = [f"{p['coin']} {'LONG' if p['size'] > 0 else 'SHORT'}" for p in positions]
        cycles_done = self.won + self.busted
        # Ritt-PnL: nur der AKTUELLE Ritt seit seinem eigenen Start - getrennt von
        # cycle_pnl (Summe über ALLE Ritte des Zyklus, inkl. bereits geschlossener).
        ride_pnl = (round(eq - self._ride_start_equity, 2)
                   if self._ride_start_equity is not None else None)
        return {
            "equity": round(eq, 2),
            "cycle": cycles_done + 1,
            "cycle_pnl": round(eq - self.cfg.equity, 2),
            "ride_pnl": ride_pnl,
            "positions": positions,
            "target": round(self.cfg.equity + self.cfg.target_profit, 2),
            "progress_pct": round((eq - self.cfg.equity) / self.cfg.target_profit * 100, 1),
            "state": "hält" if positions else "wartet auf frisches Signal",
            "held": held,
            "banked": round(self.banked, 2),
            "won": self.won,
            "busted": self.busted,
            "trades": self.paper.trades,
            "avg_trades_per_cycle": round((self.total_trades + self.paper.trades)
                                          / max(1, cycles_done + 1), 1),
            "leader": self.ride_leader,
            "strikes": {a[:10]: n for a, n in self.strikes.items() if n > 0},
            "banned": [a[:10] for a in self.banned],
        }

    def _migrate_v1_or_load(self) -> None:
        raw = None
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            pass
        if raw is not None:
            self.banked = float(raw.get("banked", 0.0))
            self.won = int(raw.get("won", 0))
            self.busted = int(raw.get("busted", 0))
            self.total_trades = int(raw.get("total_trades", 0))
            self.ride_leader = str(raw.get("ride_leader", ""))
            self.strikes = {str(k): int(v) for k, v in (raw.get("strikes") or {}).items()}
            self.banned = {str(a).lower() for a in (raw.get("banned") or [])}
        # v1-Migration: altes Buch hat mit Dauer-Reconciliation gechurnt (139 Trades)
        # -> Buch einmalig sauber neu starten, Bilanz (banked/won/busted) behalten.
        v1_state = raw is not None and "ride_leader" not in raw
        v1_dirty_book = raw is None and self.paper.trades > 0
        if v1_state or v1_dirty_book:
            log.warning("Sprint v1 -> v2: setze verchurntes Buch zurück (%d Trades), "
                        "Bilanz bleibt", self.paper.trades)
            self.paper.reset()
            self.ride_leader = ""
            self._save_state()

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(exist_ok=True)
            self.state_path.write_text(json.dumps({
                "updated": int(self.clock()), "banked": round(self.banked, 2),
                "won": self.won, "busted": self.busted,
                "total_trades": self.total_trades, "ride_leader": self.ride_leader,
                "strikes": self.strikes, "banned": sorted(self.banned),
            }))
        except OSError:
            log.exception("sprint_cycles.json nicht schreibbar")

    @staticmethod
    def _book_of(snap) -> dict[str, float]:
        return {c: float(p.size) for c, p in snap.positions.items() if float(p.size) != 0.0}

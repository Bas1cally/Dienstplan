"""Copy-Quellen: quellen-agnostisches Interface für die Trader-Discovery.

Bisher kam jeder kopierte Trader von Hyperliquid (dort ist jede Wallet nativ
transparent). Um die Trader-Basis auf weitere Perp-DEXs zu erweitern, ohne
Scanner/Sprint/Copier anzufassen, kapseln wir die Discovery hinter EINEM
Interface, das immer `LeaderSnapshot` liefert (die bestehende Dataclass).

WICHTIG - Ausführung bleibt IMMER Hyperliquid. Eine CopySource liest nur
fremde Positionen; kopiert/gehandelt wird auf HL, wo der Bot sitzt.

Realität (siehe probe_dexs.py): Nicht jeder öffentliche Perp-DEX zeigt fremde
Wallets. Extended/X10 (StarkEx) braucht laut Doku den API-Key DES Kontos für
Positionsdaten; Lighter (zk) verbirgt Einzelpositionen tendenziell. Deshalb
existieren Fremd-Venue-Adapter zunächst nur als Stub und werden erst mit Code
gefüllt, wenn `probe_dexs.py` beweist, dass die Venue fremde Positionen hergibt.
"""

from typing import Protocol, runtime_checkable

from ..copytrade.tracker import LeaderSnapshot


@runtime_checkable
class CopySource(Protocol):
    """Eine Quelle kopierbarer Trader. Muss `LeaderSnapshot` liefern."""

    name: str

    def discover(self, min_score: float) -> list[str]:
        """Liefert kopierwürdige Trader-Referenzen (Adressen/IDs) der Quelle."""
        ...

    def snapshot(self, ref: str) -> LeaderSnapshot:
        """Aktuelle offene Positionen eines Traders als LeaderSnapshot.

        Coins müssen bereits auf Hyperliquid-Symbole gemappt sein (map_coin);
        nicht auf HL handelbare Märkte werden weggelassen.
        """
        ...

    def map_coin(self, venue_symbol: str) -> str | None:
        """Venue-Symbol -> Hyperliquid-Coin, oder None wenn auf HL nicht handelbar."""
        ...


class HyperliquidSource:
    """Dünner Adapter um den bestehenden LeaderTracker.

    Damit erfüllt der bestehende (bewährte) HL-Pfad dasselbe Interface wie
    künftige Fremd-Quellen - die Snapshots aller Quellen werden im Autopilot
    einfach gemergt. Auf HL sind Symbole bereits HL-Symbole (map_coin = Identität).
    """

    name = "hyperliquid"

    def __init__(self, tracker, hl_coins: set[str] | None = None):
        self.tracker = tracker              # bot.copytrade.tracker.LeaderTracker
        self.hl_coins = hl_coins            # optionale Whitelist handelbarer Coins

    def discover(self, min_score: float) -> list[str]:
        # Discovery/Ranking läuft weiterhin über den Autopilot (analyze_traders);
        # die Quelle liefert Snapshots zu den bereits gewählten Adressen.
        return list(self.tracker.addresses)

    def snapshot(self, ref: str) -> LeaderSnapshot:
        return self.tracker.snapshot(ref)

    def map_coin(self, venue_symbol: str) -> str | None:
        if self.hl_coins is not None and venue_symbol not in self.hl_coins:
            return None
        return venue_symbol


class _UnavailableSource:
    """Basis für noch nicht bestätigte Fremd-Venues - schlägt bewusst laut fehl,
    statt still Blinddaten zu liefern. Wird ersetzt, sobald probe_dexs.py die
    Venue als 'foreign_positions ✓' bestätigt."""

    name = "unavailable"
    probe_hint = "python3 probe_dexs.py"

    def discover(self, min_score: float) -> list[str]:
        raise NotImplementedError(
            f"{self.name}: Fremd-Positionen noch nicht als scanbar bestätigt. "
            f"Erst {self.probe_hint} laufen lassen.")

    def snapshot(self, ref: str) -> LeaderSnapshot:
        raise NotImplementedError(f"{self.name}: kein Adapter (siehe {self.probe_hint})")

    def map_coin(self, venue_symbol: str) -> str | None:
        return None


class ExtendedSource(_UnavailableSource):
    """Extended/X10 (StarkEx). Öffentliche API = nur Marktdaten; Konto-Endpunkte
    brauchen laut Doku den X-Api-Key des Kontos. Erst bauen, wenn die Probe einen
    offenen Leaderboard-/Positions-Endpunkt findet."""
    name = "extended"


class LighterSource(_UnavailableSource):
    """Lighter (zk-rollup). ZK verbirgt Einzelpositionen tendenziell - Probe klärt,
    ob der Explorer/die API sie per Account-Index hergibt."""
    name = "lighter"


class VariationalSource(_UnavailableSource):
    """Variational (RFQ/P2P, sehr neu). Öffentliches Positions-Feed fraglich - Probe klärt."""
    name = "variational"

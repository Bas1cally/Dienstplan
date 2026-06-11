"""Unit-Tests: Signal-Bridge (Prop-Modus) und Max-Drawdown-Halt."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import RiskConfig
from bot.risk import RiskManager
from bot.signals import SignalBridge

RISK = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2.0, take_profit_r=2.0,
                  max_leverage=4, max_daily_loss=0.05, slippage=0.005,
                  max_total_drawdown=0.10)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def bridge(tmp, notifier=None):
    return SignalBridge(notifier=notifier, path=Path(tmp) / "signals.jsonl")


# ---------- Signal-Bridge ----------

def test_emit_writes_complete_ticket():
    with tempfile.TemporaryDirectory() as tmp:
        b = bridge(tmp)
        t = b.emit("scalp", "BTC", "BUY", 0.05, 96_500, stop=95_800,
                   take_profit=98_000, reason="Vol-Schock Mean-Reversion")
        assert t["size"] == 0.05 and t["stop"] == 95_800
        lines = (Path(tmp) / "signals.jsonl").read_text().splitlines()
        saved = json.loads(lines[0])
        assert saved["coin"] == "BTC" and saved["side"] == "BUY"
        assert saved["take_profit"] == 98_000 and "t" in saved


def test_emit_negative_size_becomes_absolute():
    with tempfile.TemporaryDirectory() as tmp:
        t = bridge(tmp).emit("copy", "ETH", "SELL", -1.5, 3_400)
        assert t["size"] == 1.5, "Ticket trägt die absolute Größe, Richtung steckt in side"


def test_telegram_ticket_format():
    with tempfile.TemporaryDirectory() as tmp:
        n = FakeNotifier()
        bridge(tmp, notifier=n).emit("copy", "km:TSLA", "BUY", 10, 412.5, reason="Ziel 4.000 USD")
        assert len(n.sent) == 1
        msg = n.sent[0]
        assert "SIGNAL" in msg and "km:TSLA" in msg and "BUY" in msg and "Ziel" in msg


def test_no_optional_channels_no_crash():
    with tempfile.TemporaryDirectory() as tmp:
        b = bridge(tmp)            # kein Notifier, kein Webhook
        b.emit("flatten", "BTC", "SELL", 0.1, 100_000)
        assert (Path(tmp) / "signals.jsonl").exists()


# ---------- Max-Drawdown-Halt (Prop-Regel) ----------

def test_total_drawdown_thresholds():
    rm = RiskManager(RISK)
    assert not rm.total_drawdown_exceeded(10_000, 9_500)    # -5%: ok
    assert not rm.total_drawdown_exceeded(10_000, 9_001)    # -9.99%: ok
    assert rm.total_drawdown_exceeded(10_000, 9_000)        # -10%: Halt
    assert rm.total_drawdown_exceeded(10_000, 8_000)        # -20%: Halt
    assert not rm.total_drawdown_exceeded(0, 5_000)         # ungültiger Start: kein Halt


def test_daily_and_total_are_independent():
    rm = RiskManager(RISK)
    # Tag startete bei 9400 (nach Verlusten), heute -1%: Tageslimit ok,
    # aber Gesamt-Drawdown vom Start (10k) längst gerissen
    assert not rm.daily_loss_exceeded(9_400, 9_306)
    assert rm.total_drawdown_exceeded(10_000, 8_900)


# ---------- Config-Validierung ----------

def test_signals_mode_requires_dry_run():
    import yaml

    from bot.config import ROOT, load_config

    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["execution"] = {"mode": "signals"}
    raw["dry_run"] = False
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = Path(f.name)
    try:
        load_config(path)
        raise AssertionError("signals-Modus ohne dry_run muss abgelehnt werden")
    except ValueError as e:
        assert "signals" in str(e)
    finally:
        path.unlink()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")

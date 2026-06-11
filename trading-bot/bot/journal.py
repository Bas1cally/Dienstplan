"""Trade-Journal: lückenloses Protokoll aller Entscheidungen nach runtime/trades.jsonl.

Aufgezeichnet wird nicht nur, was der Bot TUT (Orders), sondern auch, was er
NICHT tut und warum (Validator-Vetos, Risk-Off-Glattstellungen, Circuit
Breaker). Genau diese Negativ-Liste braucht man nach dem Paper-Test, um zu
beurteilen, ob die Filter PnL retten oder nur Trades kosten.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"


class Journal:
    MAX_BYTES = 5_000_000

    def __init__(self, path: Path | None = None):
        self.path = path or RUNTIME / "trades.jsonl"

    def record(self, kind: str, **data) -> None:
        """kind: order | veto | flatten | circuit_breaker | rotation"""
        try:
            self.path.parent.mkdir(exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps({"t": int(time.time()), "kind": kind, **data},
                                   ensure_ascii=False) + "\n")
            if self.path.stat().st_size > self.MAX_BYTES:
                lines = self.path.read_text().splitlines()[-20_000:]
                self.path.write_text("\n".join(lines) + "\n")
        except OSError:
            log.exception("Journal nicht schreibbar")

    def tail(self, n: int = 50) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):  # neueste zuerst
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

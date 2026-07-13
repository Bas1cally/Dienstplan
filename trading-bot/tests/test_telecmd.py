"""Unit-Tests für die Telegram-Fernsteuerung (reine Dispatch-Logik, kein Netz)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.telecmd import TelegramCommander


def commander(**handlers):
    base = {
        "/status": lambda: "STATUS-OK",
        "/help": lambda: "HILFE",
    }
    base.update(handlers)
    return TelegramCommander(base, token="t", chat_id="42")


def test_known_command_dispatches():
    assert commander().dispatch("/status") == "STATUS-OK"


def test_command_with_bot_suffix():
    # Gruppen-Syntax /status@meinbot muss auch greifen
    assert commander().dispatch("/status@autopilot_bot") == "STATUS-OK"


def test_command_with_args_uses_first_token():
    assert commander().dispatch("/status now please") == "STATUS-OK"


def test_unknown_command_offers_help():
    out = commander().dispatch("/foobar")
    assert "Unbekannt" in out and "HILFE" in out


def test_non_command_ignored():
    assert commander().dispatch("einfach nur text") is None
    assert commander().dispatch("") is None


def test_handler_exception_is_caught():
    def boom():
        raise RuntimeError("kaputt")

    out = commander(**{"/report": boom}).dispatch("/report")
    assert "fehlgeschlagen" in out and "kaputt" in out


def test_disabled_without_credentials():
    c = TelegramCommander({}, token="", chat_id="")
    assert not c.enabled
    c.start()  # darf nicht crashen, tut aber nichts
    assert c._thread is None


def test_enabled_with_credentials():
    assert commander().enabled


class _Resp:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code}")


def test_send_falls_back_to_plaintext_on_html_400():
    """Live-Vorfall 13.07.: rohes '<' in der /status-Antwort -> Telegram lehnt
    HTML mit 400 ab -> KOMPLETTE Funkstille. Der Fallback muss die Nachricht
    als Klartext (ohne parse_mode) nachsenden."""
    import bot.telecmd as tc

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(400 if "parse_mode" in json else 200)

    orig = tc.requests.post
    tc.requests.post = fake_post
    try:
        commander()._send("Richtung<25: kaputtes HTML")
    finally:
        tc.requests.post = orig
    assert len(calls) == 2, "400 -> zweiter Versuch"
    assert "parse_mode" in calls[0] and "parse_mode" not in calls[1]
    assert calls[1]["text"] == "Richtung<25: kaputtes HTML", "Inhalt kommt trotzdem an"


def test_notifier_falls_back_to_plaintext_on_html_400():
    import bot.notify as nf

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(400 if "parse_mode" in json else 200)

    n = nf.Notifier()
    n.token, n.chat_id = "t", "42"
    orig = nf.requests.post
    nf.requests.post = fake_post
    try:
        n.send("Score<10 push")
    finally:
        nf.requests.post = orig
    assert len(calls) == 2 and "parse_mode" not in calls[1]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")

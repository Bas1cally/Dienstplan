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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  OK  {fn.__name__}")
    print(f"\n{len(fns)} Tests bestanden.")

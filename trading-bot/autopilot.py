#!/usr/bin/env python3
"""Headless-Start des Autopiloten (ohne Web-UI). Mit UI: python server.py"""

import logging
import os
import time

from bot.autopilot import Autopilot
from bot.config import load_config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler("autopilot.log")])

if __name__ == "__main__":
    ap = Autopilot(load_config())
    ap.start()
    try:
        while ap.running:
            time.sleep(5)
    except KeyboardInterrupt:
        ap.stop()

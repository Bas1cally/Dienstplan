# VPS-Setup: Schritt für Schritt zum 24/7-Bot (~20 Minuten)

## 0. Anbieter & Konditionen

Der Bot ist genügsam: 2 vCPU, 2-4 GB RAM, 40 GB Disk reichen locker.

| Anbieter | Modell | Preis (prüfen!) | Einordnung |
|---|---|---|---|
| **Hetzner Cloud** (Empfehlung) | CX22 (2 vCPU, 4 GB) | ~4-5 €/Monat | Bestes Preis/Leistung, deutscher Anbieter, stündliche Abrechnung, jederzeit kündbar |
| Netcup | VPS 200/500 | ~3-5 €/Monat | günstig, solide, träger Support |
| DigitalOcean / Vultr | Basic 2 GB | ~6-7 $/Monat | gut, wenn du eine Region nahe Tokio willst (s.u.) |
| Contabo | VPS S | ~5 €/Monat | billig, aber durchwachsener Ruf - eher nicht |

**Region:** Für unseren Bot (Reconciliation, kein HFT) ist EU (Falkenstein/
Nürnberg) völlig ok. Wer das letzte Quäntchen Copy-Lag will: Hyperliquids
Infrastruktur läuft in Tokio (AWS ap-northeast-1) - Vultr/DO Tokio oder
Hetzner Singapur senken die Latenz von ~250 ms auf ~10-70 ms. Für den
Paper-Test: egal, nimm EU.

**Image:** Ubuntu 24.04 LTS. Beim Anlegen direkt deinen **SSH-Key** hinterlegen
(kein Passwort-Login).

---

## 1. Grundabsicherung (einmalig, 5 Min)

```bash
ssh root@<VPS-IP>

# eigenen Nutzer anlegen, root-Login später aus
adduser trader
usermod -aG sudo trader
rsync -a ~/.ssh /home/trader/ && chown -R trader:trader /home/trader/.ssh

# Firewall: NUR SSH offen - das Dashboard wird NIE öffentlich exponiert
apt update && apt -y install ufw
ufw allow OpenSSH
ufw enable

# automatische Sicherheitsupdates
apt -y install unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades

# SSH härten: Passwort-Login aus
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

## 2. Tailscale (privater Zugang zum Dashboard)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up        # Link öffnen, mit deinem Konto anmelden
```

Tailscale-App auch auf Handy/PC installieren (gleiches Konto). Danach ist der
VPS unter seinem Tailscale-Namen privat erreichbar - ohne offene Ports.

## 3. Bot installieren

```bash
su - trader
sudo apt -y install python3-pip python3-venv git

git clone https://github.com/Bas1cally/Dienstplan.git   # bei privatem Repo: PAT/Deploy-Key
cd Dienstplan/trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env
#   DASHBOARD_TOKEN=$(openssl rand -hex 24)   <- PFLICHT (Fernzugriff!)
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID     <- dringend empfohlen
#   ANTHROPIC_API_KEY                         <- optional (KI-News/Validator)

nano config.yaml
#   autopilot.autostart: true                 <- startet ohne Dashboard-Klick
#   (dry_run: true bleibt - Paper-Test braucht keine Wallet-Keys!)
```

## 4. Preflight

```bash
python doctor.py        # alles Pflicht-Grüne? (WS, Leaderboard, Multi-DEX)
python simulate.py      # Generalprobe 8/8
python doctor.py --notify   # Telegram-Testnachricht aufs Handy
```

## 5. Als Dienst einrichten ("einmal starten und gut is")

```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot.service
#   User=trader
#   WorkingDirectory=/home/trader/Dienstplan/trading-bot
#   ExecStart=/home/trader/Dienstplan/trading-bot/.venv/bin/python3 server.py
#   ReadWritePaths=/home/trader/Dienstplan/trading-bot

sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
systemctl status trading-bot          # active (running)?
journalctl -u trading-bot -f          # Live-Logs
```

## 6. Verifizieren

- Handy (Tailscale an): `http://<vps-name>:8000` → Token eingeben →
  Dashboard zeigt „running" + „⚡ WS live"
- Telegram: „🚀 Autopilot gestartet" muss angekommen sein
- Reboot-Test: `sudo reboot` → nach 1 Min läuft alles wieder von selbst

## Betrieb

| Was | Wie |
|---|---|
| Logs | `journalctl -u trading-bot -f` |
| Auswertung | `cd ~/Dienstplan/trading-bot && .venv/bin/python report.py` |
| Update einspielen | `git pull && sudo systemctl restart trading-bot` |
| Stoppen | `sudo systemctl stop trading-bot` |
| Tagesgeschehen | kommt von allein per Telegram (Digest, Watchdog, Alerts) |

## Sicherheit auf dem VPS - die drei Regeln

1. **Dashboard nie öffentlich:** `server_host: 127.0.0.1` bleibt, Zugriff nur
   via Tailscale, `DASHBOARD_TOKEN` gesetzt. Port 8000 ist in ufw NICHT offen.
2. **Agent-Key-Prinzip:** Wenn später live gehandelt wird, liegt auf dem VPS
   nur der Agent-Key (darf traden, NIE auszahlen). Der Haupt-Wallet-Key
   berührt den Server niemals.
3. **Wallet-Connect für Live:** MetaMask läuft im Browser deines Handys/PCs -
   über Tailscale aufs Dashboard, Vollmacht signieren, fertig. Für den
   Paper-Test ist gar keine Wallet nötig.

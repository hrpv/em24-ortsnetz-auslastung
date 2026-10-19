#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ortsnetz_sender.py

Sendet alle 5 Minuten Netzspannung (L1-L3), Netzfrequenz, PV-Leistung (kWp) und eine
einfache PV-Tagesprognose an https://www.ortsnetz-auslastung.de (API v1, siehe API.md).

Messwerte : MQTT (mbmd / Carlo Gavazzi EM24) auf localhost, gelesen per mosquitto_sub
Prognose  : Open-Meteo (kostenlos, ohne API-Key), grob aus der Globalstrahlung gerechnet
Abhaengig : nur Python-Standardbibliothek (3.9+) und mosquitto_sub
            (sudo apt install mosquitto-clients)

Aufruf:
    ./ortsnetz_sender.py --dry-run --once   # Werte lesen, Payload anzeigen, nichts senden
    ./ortsnetz_sender.py --once             # genau eine Messung senden
    ./ortsnetz_sender.py                    # Dauerbetrieb, alle 5 Minuten
"""

import argparse
import json
import logging
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------------
# Standort: Bitte innerhalb des eigenen Ortsnetzes anpassen bzw. leicht verschieben.
LATITUDE = 48.0
LONGITUDE = 9.0
PLANT_KWP = 10.0
SMARTMETER_MODEL = "Carlo Gavazzi EM24 (mbmd)"
INTEGRATION_VERSION = "python-mbmd-0.1.0"  # max. 32 Zeichen

# MQTT (mbmd veroeffentlicht Zahlen als Text, z. B. "232.500")
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
TOPIC_PREFIX = "mbmd/cgex3x02-1"
TOPIC_L1 = f"{TOPIC_PREFIX}/Voltage/L1"
TOPIC_L2 = f"{TOPIC_PREFIX}/Voltage/L2"
TOPIC_L3 = f"{TOPIC_PREFIX}/Voltage/L3"
TOPIC_FREQUENCY = f"{TOPIC_PREFIX}/Frequency"  # "" wenn nicht gesendet werden soll
SAMPLE_SECONDS = 15  # so lange mithoeren; der letzte Wert je Topic zaehlt

# API
API_URL = "https://www.ortsnetz-auslastung.de/v1/measurements"
UPLOAD_INTERVAL_S = 5 * 60
HTTP_TIMEOUT_S = 10

# Prognose (Open-Meteo). Nur ein grober Tageswert, deshalb bewusst einfach gehalten:
# kWh = Globalstrahlung [kWh/m2] * kWp * PERFORMANCE_FACTOR
FORECAST_ENABLED = True
FORECAST_REFRESH_S = 3 * 3600
PERFORMANCE_FACTOR = 0.85
TIMEZONE = "Europe/Berlin"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Plausibilitaetsgrenzen (wie beim offiziellen MQTT-Script des Projekts)
VOLTAGE_MIN, VOLTAGE_MAX = 150.0, 300.0
FREQUENCY_MIN, FREQUENCY_MAX = 45.0, 55.0
# ----------------------------------------------------------------------------

log = logging.getLogger("ortsnetz")
stop_event = threading.Event()
_forecast_cache = {"day": None, "kwh": None, "fetched": 0.0}


def parse_mosquitto_output(text):
    """Liest Zeilen 'topic payload' (mosquitto_sub -v); der letzte Wert je Topic gewinnt."""
    values = {}
    for line in text.splitlines():
        topic, _, payload = line.strip().partition(" ")
        if not topic or not payload:
            continue
        try:
            values[topic] = float(payload.strip())
        except ValueError:
            log.debug("Nicht numerisch, ignoriert: %s", line)
    return values


def read_mqtt():
    """Hoert SAMPLE_SECONDS lang auf den Topics und liefert den letzten Wert je Topic."""
    topics = [TOPIC_L1, TOPIC_L2, TOPIC_L3]
    if TOPIC_FREQUENCY:
        topics.append(TOPIC_FREQUENCY)
    cmd = ["mosquitto_sub", "-h", MQTT_HOST, "-p", str(MQTT_PORT), "-v",
           "-W", str(SAMPLE_SECONDS)]
    if MQTT_USER:
        cmd += ["-u", MQTT_USER, "-P", MQTT_PASSWORD]
    for topic in topics:
        cmd += ["-t", topic]
    try:
        # mosquitto_sub endet nach -W mit einem Nicht-Null-Code, das ist hier normal.
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=SAMPLE_SECONDS + 10)
    except subprocess.TimeoutExpired:
        log.warning("mosquitto_sub hat nicht rechtzeitig beendet")
        return {}
    if not result.stdout.strip() and result.stderr.strip():
        log.warning("mosquitto_sub: %s", result.stderr.strip())
    return parse_mosquitto_output(result.stdout)


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": INTEGRATION_VERSION})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.load(resp)


def get_forecast_kwh():
    """Heutige PV-Gesamtprognose in kWh (grobe Schaetzung) oder None."""
    if not FORECAST_ENABLED:
        return None
    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    cache = _forecast_cache
    if cache["day"] == today and time.monotonic() - cache["fetched"] < FORECAST_REFRESH_S:
        return cache["kwh"]
    query = urllib.parse.urlencode({
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "shortwave_radiation_sum",  # MJ/m2 pro Tag
        "timezone": TIMEZONE,
        "forecast_days": 1,
    })
    try:
        data = http_get_json(f"{FORECAST_URL}?{query}")
        if data["daily"]["time"][0] != today:
            raise ValueError("Prognose gehoert nicht zum heutigen Tag")
        megajoule = data["daily"]["shortwave_radiation_sum"][0]
        if megajoule is None:
            raise ValueError("kein Strahlungswert geliefert")
        kwh = round(megajoule / 3.6 * PLANT_KWP * PERFORMANCE_FACTOR, 1)
        cache.update(day=today, kwh=kwh, fetched=time.monotonic())
        log.info("Prognose heute: %.1f MJ/m2 -> %.1f kWh", megajoule, kwh)
        return kwh
    except Exception as exc:  # Netzwerk, JSON, fehlende Felder
        log.warning("Prognose nicht verfuegbar: %s", exc)
        return cache["kwh"] if cache["day"] == today else None


def build_payload(values):
    """Baut den API-Payload; None, wenn Pflichtwerte fehlen oder unplausibel sind."""
    volts = [values.get(t) for t in (TOPIC_L1, TOPIC_L2, TOPIC_L3)]
    if any(v is None for v in volts):
        log.warning("Spannungswerte fehlen (L1/L2/L3 = %s), Upload uebersprungen", volts)
        return None
    if not all(VOLTAGE_MIN <= v <= VOLTAGE_MAX for v in volts):
        log.warning("Spannung ausserhalb %.0f-%.0f V: %s, Upload uebersprungen",
                    VOLTAGE_MIN, VOLTAGE_MAX, volts)
        return None
    payload = {
        "observed_at": datetime.now(timezone.utc).replace(microsecond=0)
                       .isoformat().replace("+00:00", "Z"),
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "l1_v": round(volts[0], 1),
        "l2_v": round(volts[1], 1),
        "l3_v": round(volts[2], 1),
        "plant_capacity_kwp": PLANT_KWP,
        "smartmeter_model": SMARTMETER_MODEL,
        "integration_version": INTEGRATION_VERSION,
    }
    freq = values.get(TOPIC_FREQUENCY) if TOPIC_FREQUENCY else None
    if freq is not None and FREQUENCY_MIN <= freq <= FREQUENCY_MAX:
        payload["grid_frequency_hz"] = round(freq, 3)
    forecast = get_forecast_kwh()
    if forecast is not None:
        payload["pv_forecast_kwh"] = forecast
    return payload


def post_measurement(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": INTEGRATION_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            answer = json.load(resp)
            status = answer.get("status", {})
            log.info("HTTP %s angenommen, Ampel: %s", resp.status, status.get("overall", "?"))
            if status.get("overall") not in (None, "green"):
                log.warning("Ampelstatus: %s", status)
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403:
            log.error("HTTP 403: Standort fuer die Annahme gesperrt. %s", detail)
        elif exc.code == 422:
            log.error("HTTP 422: Wert ungueltig. %s | Payload: %s", detail, payload)
        else:
            log.error("HTTP %s: %s", exc.code, detail)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("Senden fehlgeschlagen: %s", exc)
    return False


def run_cycle(dry_run):
    payload = build_payload(read_mqtt())
    if payload is None:
        return False
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    return post_measurement(payload)


def main():
    parser = argparse.ArgumentParser(description="Sendet Netzspannung an ortsnetz-auslastung.de")
    parser.add_argument("--once", action="store_true", help="nur eine Messung, dann beenden")
    parser.add_argument("--dry-run", action="store_true", help="nichts senden, Payload ausgeben")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Ausgaben")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if shutil.which("mosquitto_sub") is None:
        raise SystemExit("mosquitto_sub fehlt: sudo apt install mosquitto-clients")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())

    if args.once:
        raise SystemExit(0 if run_cycle(args.dry_run) else 1)

    next_run = time.monotonic()
    while not stop_event.is_set():
        try:
            run_cycle(args.dry_run)
        except Exception:  # der Dienst soll bei unerwarteten Fehlern weiterlaufen
            log.exception("Unerwarteter Fehler im Zyklus")
        next_run += UPLOAD_INTERVAL_S
        stop_event.wait(max(0.0, next_run - time.monotonic()))


if __name__ == "__main__":
    main()

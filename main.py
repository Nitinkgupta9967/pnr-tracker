
import os
import re
import json
import time
import logging
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

PNR = os.getenv("PNR", "").strip()
API_KEY = os.getenv("RAILKIT_API_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL", "900")))
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))

API_URL = f"https://api.railkit.in/api/v1/pnr/{PNR}"
session = requests.Session()


def validate_config():
    if not re.fullmatch(r"\d{10}", PNR):
        raise ValueError("PNR must be exactly 10 digits")
    for name, value in (
        ("RAILKIT_API_KEY", API_KEY),
        ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", CHAT_ID),
    ):
        if not value:
            raise ValueError(f"Missing environment variable: {name}")


def send_telegram(message):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram sendMessage failed")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state))
    temp.replace(STATE_FILE)


def fetch_status():
    response = session.get(
        API_URL,
        headers={
            "x-api-key": API_KEY,
            "accept": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("success") is not True:
        raise RuntimeError("PNR API returned unsuccessful response")

    data = payload.get("data") or {}
    passengers = data.get("passengers") or []
    if not passengers:
        raise RuntimeError("API returned no passenger records")

    result = []

    for index, passenger in enumerate(passengers, start=1):
        current = passenger.get("current") or {}
        booking = passenger.get("booking") or {}

        # Current status is authoritative when provided.
        status = str(
            current.get("status") or booking.get("status") or ""
        ).upper().strip()

        details = str(
            current.get("details") or booking.get("details") or status
        ).upper().strip()

        combined = f"{status} {details}"

        if re.search(r"\bCNF\b|CONFIRMED", combined):
            category = "CNF"
        elif re.search(r"\bRAC\b", combined):
            category = "RAC"
        elif re.search(r"\bWL\b|WAITLIST", combined):
            category = "WL"
        else:
            category = status or "UNKNOWN"

        # Handles GNWL/12, RLWL/4, PQWL/7, WL 12, etc.
        match = re.search(
            r"\b(?:GNWL|RLWL|PQWL|TQWL|RQWL|DPWL|WL)\s*/?\s*(\d+)",
            combined,
        )
        wl_number = int(match.group(1)) if match and category == "WL" else None

        result.append({
            "passenger": str(
                passenger.get("serialNumber") or f"Passenger {index}"
            ),
            "category": category,
            "wl_number": wl_number,
            "display": details or status or "Unknown",
        })

    return result


def compare_status(previous, current):
    alerts = []

    for index, new in enumerate(current):
        name = new["passenger"]
        old = previous[index] if index < len(previous) else None

        if old is None:
            alerts.append(f"{name}: new status {new['display']}")
            continue

        old_cat = old.get("category", "UNKNOWN")
        new_cat = new["category"]
        old_wl = old.get("wl_number")
        new_wl = new["wl_number"]

        if new_cat == "CNF" and old_cat != "CNF":
            alerts.append(
                f"🎉 {name} CONFIRMED!\n"
                f"{old.get('display')} → {new['display']}"
            )
        elif new_cat == "RAC" and old_cat not in ("RAC", "CNF"):
            alerts.append(
                f"✅ {name} reached RAC!\n"
                f"{old.get('display')} → {new['display']}"
            )
        elif (
            new_cat == "WL"
            and old_cat == "WL"
            and old_wl is not None
            and new_wl is not None
            and new_wl < old_wl
        ):
            alerts.append(
                f"📉 {name}: waitlist decreased!\n"
                f"{old.get('display')} → {new['display']}"
            )
        elif new_cat not in ("UNKNOWN", "") and new_cat != old_cat:
            alerts.append(
                f"🔔 {name}: {old.get('display')} → {new['display']}"
            )
        elif (
            new_cat == "WL"
            and old_cat == "WL"
            and old_wl is not None
            and new_wl is not None
            and new_wl > old_wl
        ):
            alerts.append(
                f"⚠️ {name}: waitlist increased!\n"
                f"{old.get('display')} → {new['display']}"
            )

    return alerts


def check_once():
    current = fetch_status()
    previous = load_state()

    if previous is None:
        # Establish baseline on first run.
        # Do not announce a change that occurred before monitoring began.
        send_telegram(
            f"🚆 PNR tracker started\nPNR: {PNR}\n\n"
            + "\n".join(
                f"{p['passenger']}: {p['display']}"
                for p in current
            )
        )
        save_state(current)
        logging.info("Initial status saved")
        return

    alerts = compare_status(previous, current)

    # Deliver alerts before advancing the saved state.
    # If Telegram fails, the next check can retry the notification.
    if alerts:
        send_telegram(
            f"🚆 PNR status update\nPNR: {PNR}\n\n"
            + "\n\n".join(alerts)
        )

    save_state(current)
    logging.info("Check complete; changes=%d", len(alerts))


def main():
    validate_config()
    logging.info("PNR tracker started; interval=%d seconds", INTERVAL)

    while True:
        try:
            check_once()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            logging.error("API/Telegram HTTP error: %s", code)
            if code == 429:
                time.sleep(min(INTERVAL * 2, 3600))
        except Exception as exc:
            logging.error("Check failed: %s", type(exc).__name__)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

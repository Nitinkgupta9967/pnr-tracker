
import json
import os
from pathlib import Path

import requests

PNR = os.environ["PNR"]
API_KEY = os.environ["RAILKIT_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path("state.json")

session = requests.Session()
session.headers.update({"x-api-key": API_KEY})


def telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def main():
    response = session.get(
        f"https://api.railkit.in/api/v1/pnr/{PNR}",
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("success") is not True:
        raise RuntimeError("RailKit did not return a successful PNR result")

    data = payload["data"]
    passengers = data.get("passengers", [])
    if not passengers:
        raise RuntimeError("No passenger status data returned")

    current = []
    for passenger in passengers:
        booking = passenger.get("booking", {})
        now = passenger.get("current", {})
        current.append({
            "booking": booking.get("status", ""),
            "current": now.get("status", ""),
            "details": now.get("details", ""),
        })

    previous = None
    if STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            previous = None

    STATE_FILE.write_text(json.dumps(current, indent=2))

    if previous is None:
        telegram(f"PNR {PNR} tracker started.\nCurrent status:\n" +
                 "\n".join(
                     f"Passenger {i}: {p['current']} {p['details']}"
                     for i, p in enumerate(current, 1)
                 ))
        return

    if current != previous:
        telegram(f"PNR {PNR} status changed:\n" +
                 "\n".join(
                     f"Passenger {i}: {p['current']} {p['details']}"
                     for i, p in enumerate(current, 1)
                 ))


if __name__ == "__main__":
    main()

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://havnsoe.com/arrangementer/"
TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path("gus_state.json")

MONTHS = {
    "januar": 1, "februar": 2, "marts": 3,
    "april": 4, "maj": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "december": 12
}


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def get_date(text):
    text = clean(text).lower()

    match = re.search(
        r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](20\d{2})(?!\d)",
        text
    )
    if match:
        day, month, year = map(int, match.groups())
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    match = re.search(
        r"(?<!\d)(\d{1,2})\s+("
        + "|".join(MONTHS)
        + r")\s+(20\d{2})(?!\d)",
        text
    )
    if match:
        day = int(match.group(1))
        month = MONTHS[match.group(2)]
        year = int(match.group(3))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def get_events():
    response = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    events = {}

    for heading in soup.select(
        ".mec-event-title, .mec-event-article h4"
    ):
        title = clean(heading.get_text(" ", strip=True))
        if not title:
            continue

        container = heading.find_parent(
            class_=re.compile(r"mec-event-article")
        )
        if container is None:
            continue

        text = clean(container.get_text(" ", strip=True))
        link = heading.find("a", href=True)
        event_url = urljoin(URL, link["href"]) if link else URL

        sold_out = bool(re.search(r"udsolgt|sold out", text, re.I))
        available = bool(
            re.search(r"køb billet|sidste par billetter", text, re.I)
        )

        event_date = get_date(title)

        if event_date is None:
            date_element = container.select_one(
                ".mec-start-date-label, .mec-event-date"
            )
            if date_element:
                event_date = get_date(
                    clean(date_element.get_text(" ", strip=True))
                )

        key = event_url if event_url != URL else title
        events[key] = {
            "title": title,
            "url": event_url,
            "available": available and not sold_out,
            "date": event_date
        }

    if not events:
        raise RuntimeError(
            "Ingen arrangementer fundet. "
            "Kontroller hjemmesidens struktur."
        )

    return events


def notify(title, message, url):
    if not TOPIC:
        raise RuntimeError("NTFY_TOPIC mangler")

    response = requests.post(
        "https://ntfy.sh/" + TOPIC,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Click": url,
            "Tags": "bell"
        },
        timeout=20
    )
    response.raise_for_status()


def main():
    current = get_events()

    if not STATE_FILE.exists():
        STATE_FILE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print("Foerste kontrol: arrangementer gemt.")
        return

    previous = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )

    for key, event in current.items():
        old = previous.get(key)

        if old is None:
            notify(
                "Nyt saunagus",
                event["title"],
                event["url"]
            )
            continue

        old_date = old.get("date")
        if old_date is None:
            old_date = get_date(old.get("title", ""))

        new_date = event.get("date")

        if old_date and new_date and old_date != new_date:
            notify(
                "Ny dato for saunagus",
                event["title"],
                event["url"]
            )
        elif event["available"] and not old.get(
            "available", False
        ):
            notify(
                "Billetter tilgaengelige",
                event["title"],
                event["url"]
            )

    STATE_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print("Kontrol gennemfoert.")


if __name__ == "__main__":
    main()

import json
import os
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://havnsoe.com/arrangementer/"
TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path("gus_state.json")

def clean(text):
    return re.sub(r"\s+", " ", text).strip()

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
        event_url = link["href"] if link else URL

        sold_out = bool(
            re.search(r"udsolgt|sold out", text, re.I)
        )
        available = bool(
            re.search(r"køb billet|sidste par billetter", text, re.I)
        )

        key = event_url if event_url != URL else title
        events[key] = {
            "title": title,
            "url": event_url,
            "available": available and not sold_out
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
        print("Første kontrol: arrangementer gemt.")
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
        elif event["available"] and not old.get(
            "available", False
        ):
            notify(
                "Billetter tilgængelige",
                event["title"],
                event["url"]
            )

    STATE_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print("Kontrol gennemført.")

if __name__ == "__main__":
    main()

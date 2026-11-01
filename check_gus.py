import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://havnsoe.com/arrangementer/"
TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path("gus_state.json")
VERSION = 3

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


def get_time(text):
    match = re.search(
        r"(?<!\d)(?:kl\.?\s*)?([01]?\d|2[0-3])"
        r"[:.]([0-5]\d)(?!\d)",
        text, re.I
    )
    return (
        f"{int(match.group(1)):02d}:{match.group(2)}"
        if match else None
    )


def ticket_status(container):
    # Læs særskilte statusmærker først.
    badges = [
        clean(node.get_text(" ", strip=True))
        for node in container.select(
            ".mec-event-title-soldout, "
            ".mec-event-title .soldout, "
            ".mec-event-title [class*='soldout']"
        )
    ]
    badges = [label for label in badges if label]

    if badges:
        return " | ".join(dict.fromkeys(badges))

    # Læs den faktiske tekst på billetknappen.
    buttons = [
        clean(node.get_text(" ", strip=True))
        for node in container.select(".mec-booking-button")
    ]
    buttons = [label for label in buttons if label]

    if buttons:
        return " | ".join(dict.fromkeys(buttons))

    return "Ikke angivet"


def parse_events(html):
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    for container in soup.select(".mec-event-article"):
        heading = container.select_one(".mec-event-title, h4")

        if heading is None:
            raise RuntimeError("Et arrangement mangler titel.")

        title_node = heading.select_one("a") or heading
        title = clean(title_node.get_text(" ", strip=True))

        if not title:
            raise RuntimeError("Et arrangement har en tom titel.")

        link = heading.find("a", href=True)
        event_url = (
            urljoin(URL, link["href"])
            if link else URL
        )

        date_element = container.select_one(
            ".mec-start-date-label, .mec-event-date"
        )
        event_date = get_date(title)

        if event_date is None and date_element:
            event_date = get_date(
                date_element.get_text(" ", strip=True)
            )

        event_time = get_time(title)
        key = event_url + "|" + title

        if key in events:
            raise RuntimeError(
                "To arrangementer har samme nøgle: " + key
            )

        status = ticket_status(container)

        events[key] = {
            "title": title,
            "url": event_url,
            "date": event_date,
            "time": event_time,
            "status": status,
            "available": status.casefold() not in (
                "udsolgt",
                "sold out",
                "se detaljer",
                "ikke angivet"
            )
        }

    return events


def get_events():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            page = browser.new_page(
                viewport={"width": 390, "height": 844},
                locale="da-DK"
            )

            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            page.wait_for_function(
                "() => typeof window.jQuery === 'function'",
                timeout=30000
            )
            page.wait_for_timeout(10000)

            def count():
                return page.locator(
                    ".mec-event-article"
                ).count()

            def button_visible():
                button = page.locator(
                    ".mec-load-more-button"
                ).first
                return (
                    button.count() > 0
                    and button.is_visible()
                )

            print(
                "Arrangementer ved start:", count(),
                flush=True
            )

            # Fortsæt, indtil alle arrangementer er hentet.
            for attempt in range(1, 101):
                if not button_visible():
                    print(
                        "Ingen Se flere-knap. Listen er færdig.",
                        flush=True
                    )
                    break

                before = count()
                responses = []

                def record(response):
                    request = response.request
                    if (
                        "admin-ajax.php" in response.url
                        and "mec_grid_load_more"
                        in (request.post_data or "")
                    ):
                        responses.append(response)

                page.on("response", record)

                try:
                    page.locator(
                        ".mec-load-more-button"
                    ).first.click(timeout=15000)

                    deadline = time.monotonic() + 25

                    while time.monotonic() < deadline:
                        if count() > before or not button_visible():
                            break
                        page.wait_for_timeout(500)

                    page.wait_for_timeout(1000)

                finally:
                    page.remove_listener("response", record)

                after = count()

                print(
                    f"Klik {attempt}: {before} -> {after}",
                    flush=True
                )

                if not responses:
                    raise RuntimeError(
                        "Ingen MEC-svar efter Se flere."
                    )

                if any(r.status != 200 for r in responses):
                    raise RuntimeError(
                        "MEC-serveren returnerede en fejl."
                    )

                if after > before:
                    continue

                if not button_visible():
                    print(
                        "Se flere-knappen forsvandt. "
                        "Listen er færdig.",
                        flush=True
                    )
                    break

                raise RuntimeError(
                    "Se flere gav ingen nye arrangementer, "
                    "men knappen er stadig synlig."
                )
            else:
                raise RuntimeError(
                    "Sikkerhedsgrænse for Se flere nået."
                )

            has_calendar = page.locator(
                ".mec-event-article, .mec-wrap, "
                ".mec-skin-grid-container, "
                ".mec-event-grid-classic"
            ).count() > 0

            if not has_calendar:
                raise RuntimeError(
                    "Arrangementskalenderen blev ikke fundet."
                )

            events = parse_events(page.content())

            print(
                "Arrangementer hentet i alt:",
                len(events),
                flush=True
            )

            return events

        finally:
            browser.close()


def notify(title, message, url):
    if not TOPIC:
        raise RuntimeError("NTFY_TOPIC mangler")

    # JSON understøtter danske tegn i titler og beskeder.
    response = requests.post(
        "https://ntfy.sh/",
        json={
            "topic": TOPIC,
            "title": title,
            "message": message,
            "click": url,
            "tags": ["bell"]
        },
        timeout=20
    )
    response.raise_for_status()


def save_state(events):
    data = {
        "version": VERSION,
        "events": events
    }

    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


def compare_events(old, new):
    messages = []

    old_date = old.get("date")
    new_date = new.get("date")

    if old_date and new_date and old_date != new_date:
        messages.append(
            f"Dato: {old_date} → {new_date}"
        )

    old_time = old.get("time")
    new_time = new.get("time")

    if old_time and new_time and old_time != new_time:
        messages.append(
            f"Tidspunkt: {old_time} → {new_time}"
        )

    old_status = old.get(
        "status", "Ikke angivet"
    )
    new_status = new.get(
        "status", "Ikke angivet"
    )

    if old_status != new_status:
        messages.append(
            f"Billetstatus: {old_status} → {new_status}"
        )

    return messages


def main():
    current = get_events()

    if not STATE_FILE.exists():
        save_state(current)
        print(
            "Første kontrol: arrangementer gemt "
            "uden notifikationer.",
            flush=True
        )
        return

    previous_data = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )

    # Registrer status ved opgradering uden
    # at sende notifikationer for eksisterende events.
    if (
        not isinstance(previous_data, dict)
        or previous_data.get("version") != VERSION
    ):
        save_state(current)
        print(
            "Opgradering til version 3: billetstatus "
            "registreret uden notifikationer.",
            flush=True
        )
        return

    previous = previous_data.get("events")

    if not isinstance(previous, dict):
        raise RuntimeError(
            "Ugyldigt format i gus_state.json."
        )

    old_by_url = {}
    for old in previous.values():
        old_by_url.setdefault(
            old.get("url", ""), []
        ).append(old)

    new_by_url = {}
    for event in current.values():
        new_by_url.setdefault(
            event["url"], []
        ).append(event)

    notifications = []

    for key, event in current.items():
        old = previous.get(key)

        if old is None:
            url = event["url"]
            old_matches = old_by_url.get(url, [])

            if (
                url != URL
                and len(old_matches) == 1
                and len(new_by_url[url]) == 1
            ):
                old = old_matches[0]

        if old is None:
            notifications.append((
                "Nyt saunagus",
                event["title"]
                + "\nBilletstatus: "
                + event["status"],
                event["url"]
            ))
            continue

        changes = compare_events(old, event)

        if changes:
            notifications.append((
                "Saunagus ændret",
                event["title"]
                + "\n"
                + "\n".join(changes),
                event["url"]
            ))

    for title, message, url in notifications:
        notify(title, message, url)
        print(
            "Notifikation sendt:",
            title,
            message.replace("\n", " | "),
            flush=True
        )

    save_state(current)

    print(
        "Kontrol gennemført. "
        f"{len(current)} arrangementer, "
        f"{len(notifications)} notifikationer.",
        flush=True
    )


if __name__ == "__main__":
    main()

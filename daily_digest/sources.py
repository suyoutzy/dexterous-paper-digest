"""Official RSS fallback, with announcement dates labeled separately."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

import requests

from .state import StateError


def collect_rss(db: dict, today: date) -> tuple[dict, int]:
    try:
        response = requests.get("https://rss.arxiv.org/rss/cs.RO", timeout=(10, 30), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError()
        feed = ET.fromstring(response.content)
        channel = feed.find("channel")
        if feed.tag != "rss" or channel is None:
            raise ValueError()
        built = parsedate_to_datetime(channel.findtext("lastBuildDate", "")).date()
        if not today - timedelta(days=7) <= built <= today + timedelta(days=1):
            raise ValueError()
    except (requests.RequestException, ValueError, TypeError, ET.ParseError):
        raise StateError("Official arXiv RSS could not be refreshed safely.") from None
    merged, count = dict(db), 0
    for item in channel.findall("item"):
        link = item.findtext("link", "")
        match = re.fullmatch(r"https?://arxiv\.org/abs/(\d{4}\.\d{4,5})(?:v\d+)?", link, flags=re.ASCII)
        if not match:
            continue
        identifier = match.group(1)
        key = f"arxiv:{identifier}"
        old = merged.get(key, {})
        announce = item.findtext("{http://arxiv.org/schemas/atom}announce_type", "")
        # A replacement/cross-list announcement must not turn an old paper into
        # a new one. Only refresh such entries when its original date is known.
        if announce != "new":
            try:
                date.fromisoformat(old.get("publish_date", ""))
            except (TypeError, ValueError):
                continue
        description = item.findtext("description", "")
        abstract = description.split("Abstract:", 1)[-1].strip()
        title = item.findtext("title", "").strip()
        if not title or not abstract:
            continue
        try:
            announced = parsedate_to_datetime(item.findtext("pubDate", channel.findtext("pubDate", ""))).date().isoformat()
        except (ValueError, TypeError):
            continue
        merged[key] = dict(old, paper_id=key, arxiv_id=identifier, title=title, abstract=abstract,
            authors=item.findtext("{http://purl.org/dc/elements/1.1/}creator", "") or old.get("authors", ""),
            publish_date=old.get("publish_date", announced),
            date_basis=old.get("date_basis", "initial_publication") if old else "rss_announcement",
            arxiv_url=f"https://arxiv.org/abs/{identifier}", source="arxiv",
            matched_categories=old.get("matched_categories", ["Dexterous-Hand"]),
            matched_keywords=old.get("matched_keywords", []))
        count += 1
    return merged, count

#!/usr/bin/env python3
"""Mise à jour des épisodes publics de Premier Visionnement et Autopsie.

Bibliothèque standard Python uniquement, destinée à GitHub Actions.
Le catalogue conserve les épisodes déjà importés si le flux est limité
aux derniers épisodes. Ne pas ajouter de flux RSS Patreon privé.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent.parent
DEST = BASE / "data" / "episodes.json"
FEEDS = (
    (
        "premier-visionnement",
        "Premier Visionnement",
        "https://feed.podbean.com/premiervisionnement/feed.xml",
    ),
    (
        "autopsie-du-jeu-video",
        "Autopsie du Jeu Vidéo",
        "https://feed.podbean.com/autopsiedujeuvideo/feed.xml",
    ),
)


class Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if not self.skip and tag in {"br", "p", "div", "li"}:
            self.output.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if not self.skip and tag in {"p", "div", "li"}:
            self.output.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.output.append(data)


def clean(value: str | None) -> str:
    parser = Cleaner()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", "".join(parser.output)).strip()


def child(parent: ET.Element | None, local_name: str) -> ET.Element | None:
    if parent is None:
        return None
    for element in parent:
        if element.tag.split("}")[-1] == local_name:
            return element
    return None


def children(parent: ET.Element | None, local_name: str):
    if parent is None:
        return []
    return [e for e in parent if e.tag.split("}")[-1] == local_name]


def value(parent: ET.Element | None, local_name: str) -> str:
    element = child(parent, local_name)
    return clean(element.text) if element is not None else ""


def valid_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else ""


def image_url(parent: ET.Element | None) -> str:
    if parent is None:
        return ""
    for element in parent:
        local = element.tag.split("}")[-1]
        if local in {"thumbnail", "image"}:
            href = element.attrib.get("href") or element.attrib.get("url")
            if href:
                return valid_url(href)
    image = child(parent, "image")
    return valid_url(value(image, "url"))


def iso_date(value_raw: str) -> str:
    if not value_raw:
        return ""
    try:
        dt = parsedate_to_datetime(value_raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value_raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_feed(content: bytes, podcast_id: str, podcast_label: str) -> list[dict]:
    root = ET.fromstring(content)
    channel = child(root, "channel")
    if channel is None:
        raise ValueError("Flux RSS sans balise <channel>")
    default_image = image_url(channel)
    episodes = []
    for item in children(channel, "item"):
        title = value(item, "title")
        if not title:
            continue
        url = valid_url(value(item, "link"))
        guid = value(item, "guid") or url or title + value(item, "pubDate")
        stable_id = hashlib.sha256(f"{podcast_id}\0{guid}".encode("utf-8")).hexdigest()[:24]
        description = value(item, "description") or value(item, "summary")
        enclosure = child(item, "enclosure")
        audio = ""
        if enclosure is not None and "audio" in enclosure.attrib.get("type", "audio/"):
            audio = valid_url(enclosure.attrib.get("url", ""))
        date = iso_date(value(item, "pubDate") or value(item, "published"))
        categories = [clean(c.text) for c in children(item, "category") if clean(c.text)]
        episodes.append({
            "id": stable_id,
            "podcast": podcast_id,
            "podcast_nom": podcast_label,
            "titre": title,
            "date": date,
            "description": description[:1800],
            "lien": url,
            "audio": audio,
            "image": image_url(item) or default_image,
            "categories_rss": categories[:15],
        })
    return episodes


def load_old() -> list[dict]:
    if not DEST.exists():
        return []
    try:
        data = json.loads(DEST.read_text(encoding="utf-8"))
        return data.get("episodes", []) if isinstance(data.get("episodes", []), list) else []
    except (ValueError, OSError):
        return []


def update() -> None:
    saved = {item["id"]: item for item in load_old() if isinstance(item, dict) and item.get("id")}
    print(f"Déjà en catalogue : {len(saved)} épisode(s)")
    for podcast_id, podcast_label, url in FEEDS:
        try:
            req = Request(url, headers={
                "User-Agent": "PremierVisionnementSite/1.0 (public podcast catalogue)",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            })
            with urlopen(req, timeout=35) as response:
                content = response.read(25 * 1024 * 1024 + 1)
            if len(content) > 25 * 1024 * 1024:
                raise ValueError("Flux RSS trop volumineux")
            items = parse_feed(content, podcast_id, podcast_label)
            if not items:
                raise ValueError("Aucun épisode trouvé")
            for episode in items:
                saved[episode["id"]] = episode
            print(f"{podcast_label}: {len(items)} épisode(s) lus dans le flux")
        except Exception as exc:
            cached = sum(e.get("podcast") == podcast_id for e in saved.values())
            if not cached:
                raise RuntimeError(f"Impossible d'importer {podcast_label}: {exc}") from exc
            print(f"ATTENTION : {podcast_label}, flux inaccessible ({exc}); {cached} épisodes conservés")

    episodes = sorted(saved.values(), key=lambda e: (e.get("date", ""), e.get("id", "")), reverse=True)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "episodes": episodes}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not DEST.exists() or DEST.read_text(encoding="utf-8") != rendered:
        DEST.write_text(rendered, encoding="utf-8")
        print(f"Catalogue enregistré : {len(episodes)} épisode(s)")
    else:
        print(f"Catalogue inchangé : {len(episodes)} épisode(s)")


if __name__ == "__main__":
    update()

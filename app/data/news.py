"""Free RSS/Atom news ingestion with explicit provenance.

News is optional context, never a hard dependency: a failed or malformed feed
returns no items and the technical strategy keeps running. Items always carry
their source and timestamps so a later research pass can audit what was known
at decision time.
"""

from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

logger = logging.getLogger(__name__)

DEFAULT_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True)
class NewsItem:
    headline: str
    source: str
    url: str = ""
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider: str = "rss"
    symbols: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        """Stable identity for idempotent research persistence."""
        identity = "|".join(
            (
                self.source.strip().lower(),
                " ".join(self.headline.split()).lower(),
                self.url.strip(),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "headline": self.headline,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fetched_at": self.fetched_at.isoformat(),
            "provider": self.provider,
            "symbols": list(self.symbols),
            "content_hash": self.content_hash,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _published(text: str | None) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _source_name(rss_url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(rss_url).netloc or rss_url
    except ValueError:
        return rss_url


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def fetch_news_items(rss_url: str = DEFAULT_RSS, limit: int = 10, *, timeout: float = 15.0) -> list[NewsItem]:
    try:
        response = requests.get(rss_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("news fetch failed: %s", exc)
        return []

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        logger.warning("news parse failed: %s", exc)
        return []

    source = _source_name(rss_url)
    fetched_at = datetime.now(timezone.utc)
    items: list[NewsItem] = []

    for node in root.findall("./channel/item"):
        headline = _text(node.find("title"))
        if not headline:
            continue
        items.append(
            NewsItem(
                headline=headline,
                source=source,
                url=_text(node.find("link")),
                published_at=_published(_text(node.find("pubDate"))),
                fetched_at=fetched_at,
            )
        )

    entries = root.findall("atom:entry", _ATOM_NS) or root.findall("entry")
    for node in entries:
        headline = _text(node.find("atom:title", _ATOM_NS)) or _text(node.find("title"))
        if not headline:
            continue
        link_node = node.find("atom:link", _ATOM_NS)
        if link_node is None:
            link_node = node.find("link")
        url = link_node.attrib.get("href", "") if link_node is not None else ""
        items.append(
            NewsItem(
                headline=headline,
                source=source,
                url=url,
                published_at=_published(_text(node.find("atom:updated", _ATOM_NS)) or _text(node.find("updated"))),
                fetched_at=fetched_at,
            )
        )

    items.sort(key=lambda item: item.published_at or fetched_at, reverse=True)
    return items[: max(0, int(limit))]


def fetch_headlines(rss_url: str = DEFAULT_RSS, limit: int = 10) -> list[str]:
    """Backwards-compatible headline-only view."""
    return [item.headline for item in fetch_news_items(rss_url, limit)]


def headlines_from(items: list[NewsItem | str] | None) -> list[str]:
    """Normalise injected news getters to plain strings for sentiment agents."""
    out: list[str] = []
    for item in items or []:
        if isinstance(item, NewsItem):
            out.append(item.headline)
        elif isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            headline = str(item.get("headline") or "").strip()
            if headline:
                out.append(headline)
    return out


__all__ = ["DEFAULT_RSS", "NewsItem", "fetch_headlines", "fetch_news_items", "headlines_from"]

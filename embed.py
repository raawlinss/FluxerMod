"""
Embed yardımcı sınıfı — Fluxer mesaj embed'leri için.
"""
from typing import List, Optional


class Embed:
    def __init__(
        self,
        title: str = None,
        description: str = None,
        color: int = 0x5865F2,
        url: str = None,
    ):
        self.title = title
        self.description = description
        self.color = color
        self.url = url
        self.fields: List[dict] = []
        self.footer_text: Optional[str] = None
        self.timestamp: Optional[str] = None

    def add_field(self, name: str, value: str, inline: bool = False) -> "Embed":
        self.fields.append({"name": name, "value": value, "inline": inline})
        return self

    def set_footer(self, text: str) -> "Embed":
        self.footer_text = text
        return self

    def set_timestamp(self) -> "Embed":
        from datetime import datetime, timezone
        self.timestamp = datetime.now(timezone.utc).isoformat()
        return self

    def to_dict(self) -> dict:
        d: dict = {"color": self.color}
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.url:
            d["url"] = self.url
        if self.fields:
            d["fields"] = self.fields
        if self.footer_text:
            d["footer"] = {"text": self.footer_text}
        if self.timestamp:
            d["timestamp"] = self.timestamp
        return d


# Renkler
class Color:
    RED = 0xED4245
    GREEN = 0x57F287
    YELLOW = 0xFEE75C
    BLUE = 0x5865F2
    PURPLE = 0x9B59B6
    TWITTER = 0x1DA1F2

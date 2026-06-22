# -*- coding: utf-8 -*-
"""Google Merchant XML feed parser.

Reads data/googleMerchant_sk_export.xml and returns a list of real product
dicts. No invented fields -- every key maps to an actual tag in the feed.
"""
import html
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FEED_PATH = DATA_DIR / "googleMerchant_sk_export.xml"

DIET_BREADCRUMBS = {
    "Vegánske potraviny": "Vegán",
    "Vegetariánske potraviny": "Vegetariánske",
    "Bezlepkové potraviny": "Bezlepkové",
    "Zdravé potraviny": "Zdravé",
    "BIO potraviny": "BIO",
}


def _unesc(s):
    return html.unescape(s)


def parse_items(feed_path=FEED_PATH):
    """Parse all <item> blocks from the Google Merchant feed."""
    with open(feed_path, encoding="utf-8") as f:
        data = f.read()
    blocks = re.findall(r"<item>(.*?)</item>", data, re.S)
    items = []
    for b in blocks:
        def g(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", b, re.S)
            return _unesc(m.group(1).strip()) if m else ""

        product_type = g("g:product_type")
        breadcrumbs = [s.strip() for s in product_type.split(">")]
        diet_tags = [label for seg, label in DIET_BREADCRUMBS.items() if seg in breadcrumbs]

        items.append({
            "id": g("g:id"),
            "title": g("title"),
            "desc": g("description"),
            "product_type": product_type,
            "link": g("link"),
            "price": g("g:price"),
            "sale_price": g("g:sale_price"),
            "brand": g("g:brand"),
            "availability": g("g:availability"),
            "gtin": g("g:gtin"),
            "image_link": g("g:image_link"),
            "unit_pricing_measure": g("g:unit_pricing_measure"),
            "shipping_weight": g("g:shipping_weight"),
            "diet_tags": diet_tags,
        })
    return items


if __name__ == "__main__":
    items = parse_items()
    print("total items:", len(items))
    print(items[0])

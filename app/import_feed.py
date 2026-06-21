# -*- coding: utf-8 -*-
"""Run feed.parse_items() and write data/products.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feed import parse_items, DATA_DIR

OUT_PATH = DATA_DIR / "products.json"


def main():
    items = parse_items()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(items)} products to {OUT_PATH}")


if __name__ == "__main__":
    main()

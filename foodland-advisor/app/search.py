# -*- coding: utf-8 -*-
"""Local, dependency-free search over products.json + foodland_knowledge.json.

Simple case-insensitive / diacritic-insensitive token + substring matching.
No vector DB. Every result is tagged with a "source" field so callers
(main.py) can tell products apart from FAQ/recipe/blog/etc. entries.
"""
import json
import re
import unicodedata
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR.parent / "data"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize(s):
    if not s:
        return ""
    s = str(s).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s


def tokenize(s):
    return _TOKEN_RE.findall(normalize(s))


def load_products(path=None):
    path = path or DATA_DIR / "products.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_knowledge(path=None):
    path = path or DATA_DIR / "foodland_knowledge.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _score(query_tokens, query_norm, fields, boost_field=""):
    """fields: list of raw text strings to match tokens against.
    boost_field: the single most important field (title/question/intent) --
    an exact substring hit there is a strong signal."""
    combined_tokens = set()
    for f in fields:
        combined_tokens.update(tokenize(f))
    score = len(query_tokens & combined_tokens)
    if boost_field and query_norm and query_norm in normalize(boost_field):
        score += 5
    return score


def search_products(query, products, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for p in products:
        score = _score(
            qtoks, qnorm,
            [p.get("title", ""), p.get("brand", ""), p.get("product_type", ""), p.get("desc", "")],
            boost_field=p.get("title", ""),
        )
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "product", "score": s, **p} for s, p in scored[:limit]]


def _lang_ok(urls, lang):
    if not lang:
        return True
    return bool(urls.get(lang))


def search_faq(query, knowledge, lang=None, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for f in knowledge.get("faq", []):
        if not _lang_ok(f.get("urls", {}), lang):
            continue
        score = _score(qtoks, qnorm, [f.get("question", ""), f.get("answer", ""), f.get("category", "")],
                        boost_field=f.get("question", ""))
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "faq", "score": s, **f} for s, f in scored[:limit]]


def search_recipes(query, knowledge, lang=None, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for r in knowledge.get("recipes", []):
        if not _lang_ok(r.get("urls", {}), lang):
            continue
        ing_text = " ".join(i.get("text", "") for i in r.get("ingredients", []))
        score = _score(qtoks, qnorm, [r.get("name", ""), r.get("cuisine", ""), ing_text],
                        boost_field=r.get("name", ""))
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    # Full recipe dict already includes ingredients + curated_shop_links
    # (merged in import_knowledge.py), so they ride along automatically.
    return [{"source": "recipe", "score": s, **r} for s, r in scored[:limit]]


def search_blog(query, knowledge, lang=None, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for b in knowledge.get("blog", []):
        if not _lang_ok(b.get("urls", {}), lang):
            continue
        score = _score(qtoks, qnorm, [b.get("title", ""), b.get("theme", "")], boost_field=b.get("title", ""))
        if score > 0:
            scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "blog", "score": s, **b} for s, b in scored[:limit]]


def _search_suggestion_table(query, items, source_name, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for it in items:
        score = _score(qtoks, qnorm, [it.get("product_title", ""), it.get("category", "")],
                        boost_field=it.get("product_title", ""))
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [{"source": source_name, "score": s, **it} for s, it in scored[:limit]]


def search_cross_sell(query, knowledge, limit=10):
    return _search_suggestion_table(query, knowledge.get("cross_sell", []), "cross_sell", limit)


def search_alternatives(query, knowledge, limit=10):
    return _search_suggestion_table(query, knowledge.get("alternatives", []), "alternative", limit)


def search_products_ai(query, knowledge, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for p in knowledge.get("products_ai", []):
        ai_desc_text = " ".join(p.get("ai_description", {}).values())
        score = _score(qtoks, qnorm, [p.get("title", ""), p.get("category", ""), p.get("cuisine", ""), ai_desc_text],
                        boost_field=p.get("title", ""))
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "products_ai", "score": s, **p} for s, p in scored[:limit]]


def search_intent_mapping(query, knowledge, lang=None, limit=10):
    qtoks = set(tokenize(query))
    qnorm = normalize(query)
    scored = []
    for im in knowledge.get("intent_mapping", []):
        if not _lang_ok(im.get("urls", {}), lang):
            continue
        score = _score(qtoks, qnorm, [im.get("intent", ""), im.get("type", ""), im.get("content_name", "")],
                        boost_field=im.get("intent", ""))
        if score > 0:
            scored.append((score, im))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "intent_mapping", "score": s, **im} for s, im in scored[:limit]]


def search_all(query, products, knowledge, lang=None, limit_per_source=5):
    """Run every section's search and return a merged, score-sorted list."""
    results = []
    results += search_products(query, products, limit_per_source)
    results += search_faq(query, knowledge, lang, limit_per_source)
    results += search_recipes(query, knowledge, lang, limit_per_source)
    results += search_blog(query, knowledge, lang, limit_per_source)
    results += search_cross_sell(query, knowledge, limit_per_source)
    results += search_alternatives(query, knowledge, limit_per_source)
    results += search_products_ai(query, knowledge, limit_per_source)
    results += search_intent_mapping(query, knowledge, lang, limit_per_source)
    results.sort(key=lambda r: -r["score"])
    return results


if __name__ == "__main__":
    products = load_products()
    knowledge = load_knowledge()
    import sys
    q = " ".join(sys.argv[1:]) or "kimchi"
    res = search_all(q, products, knowledge, lang="SK", limit_per_source=3)
    for r in res[:15]:
        label = r.get("title") or r.get("question") or r.get("name") or r.get("product_title") or r.get("intent")
        print(r["source"], r["score"], label)

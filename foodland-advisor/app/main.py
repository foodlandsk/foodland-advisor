# -*- coding: utf-8 -*-
"""Foodland AI Seller Advisor -- backend (v1, search_only mode).

Pure Python standard library, no external dependencies (no FastAPI/uvicorn
required) so it runs anywhere with just `python3 app/main.py`. Serves both
the JSON API and the static chat.html UI from the same port (avoids CORS).

Endpoints:
  GET  /                      -> chat.html UI
  GET  /health                -> status + loaded record counts
  GET  /products/search?q=    -> product search over the real feed
  GET  /knowledge/search?q=&lang= -> search over foodland_knowledge.json
  POST /ask {question, lang}  -> seller-advisor endpoint (search_only mode
                                  unless OPENAI_API_KEY is set in the env)

Anti-hallucination contract: every product/price/URL/ingredient returned by
this server is read verbatim from data/products.json or
data/foodland_knowledge.json. Nothing here is invented at request time.
"""
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
CHAT_HTML_PATH = PROJECT_DIR / "chat.html"
ANALYTICS_PATH = DATA_DIR / "analytics.jsonl"

sys.path.insert(0, str(APP_DIR))
import search as search_mod  # noqa: E402

PORT = int(os.environ.get("PORT", "8000"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW_SECONDS = 600  # 10 min

CONFIDENCE_LABEL = {
    "high": "Na Foodlande nájdete",
    "medium": "Najbližšie, čo sme na Foodlande našli (over si vhodnosť)",
}

# ---------------------------------------------------------------------------
# Data (loaded once at startup)
# ---------------------------------------------------------------------------
PRODUCTS = search_mod.load_products()
KNOWLEDGE = search_mod.load_knowledge()

# Lookup tables for cross-referencing real data (never fabricated -- every
# entry here comes straight from products.json / foodland_knowledge.json).
PRODUCTS_BY_ID = {p["id"]: p for p in PRODUCTS if p.get("id")}
PRODUCTS_BY_LINK = {p["link"]: p for p in PRODUCTS if p.get("link")}
CROSS_SELL_BY_ID = {c["product_id"]: c for c in KNOWLEDGE.get("cross_sell", []) if c.get("product_id")}
ALTERNATIVES_BY_ID = {a["product_id"]: a for a in KNOWLEDGE.get("alternatives", []) if a.get("product_id")}


def image_for_link(link):
    """Real product image for a given real product link, or '' if unknown.
    Never guessed -- straight lookup against products.json by exact link."""
    p = PRODUCTS_BY_LINK.get(link)
    return p.get("image_link", "") if p else ""


def enrich_results_with_images(results):
    """products_ai / cross_sell / alternative rows don't carry image_link
    themselves (they're built from xlsx tables, not the feed), so attach
    the real product image by joining back to products.json. Plain
    "product" results already have image_link natively."""
    enriched = []
    for r in results:
        r = dict(r)
        if r.get("source") == "products_ai" and not r.get("image_link"):
            r["image_link"] = PRODUCTS_BY_ID.get(r.get("id", ""), {}).get("image_link", "")
        elif r.get("source") in ("cross_sell", "alternative") and not r.get("image_link"):
            r["image_link"] = image_for_link(r.get("product_link", ""))
        enriched.append(r)
    return enriched


_rate_lock = threading.Lock()
_rate_store = {}  # ip -> list[float] timestamps

_analytics_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Lightweight per-session memory (in-memory, resets on restart -- same
# tradeoff as rate limiting). This ONLY remembers which recipe(s) were last
# discussed in a session, so a short follow-up like "a čo soľ?" can be
# resolved against that recipe's real ingredient list. It does not attempt
# general multi-turn understanding -- if the follow-up doesn't match any
# ingredient text from the remembered recipe, it is treated as a normal,
# independent question.
# ---------------------------------------------------------------------------
_session_lock = threading.Lock()
_session_store = {}  # session_id -> {"recipes": [...], "ts": float}
SESSION_TTL_SECONDS = 1800  # 30 min


def remember_session_recipes(session_id, recipes):
    if not session_id or not recipes:
        return
    with _session_lock:
        _session_store[session_id] = {"recipes": recipes, "ts": time.time()}


def get_session_recipes(session_id):
    if not session_id:
        return []
    with _session_lock:
        entry = _session_store.get(session_id)
        if not entry:
            return []
        if time.time() - entry["ts"] > SESSION_TTL_SECONDS:
            del _session_store[session_id]
            return []
        return entry["recipes"]


# ---------------------------------------------------------------------------
# Rate limiting + analytics
# ---------------------------------------------------------------------------
def check_rate_limit(ip):
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with _rate_lock:
        bucket = _rate_store.setdefault(ip, [])
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def log_analytics(ip, question, mode, result_count):
    entry = {
        "ip_hash": hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "mode": mode,
        "result_count": result_count,
    }
    line = json.dumps(entry, ensure_ascii=False)
    with _analytics_lock:
        with open(ANALYTICS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Ingredient -> shopping suggestion logic (the priority order from the spec)
# ---------------------------------------------------------------------------
def _curated_match(ingredient_text, curated_links):
    """Does this ingredient line correspond to a curated_shop_links entry?
    Token-overlap check (diacritic/case-insensitive), real data only."""
    text_tokens = search_mod.meaningful_tokens(ingredient_text)
    best = None
    for c in curated_links:
        c_tokens = search_mod.meaningful_tokens(c.get("ingredient", ""))
        if c_tokens and c_tokens.issubset(text_tokens):
            return c
        if c_tokens and (c_tokens & text_tokens) and best is None:
            best = c
    return best


def build_ingredient_line(ing, curated_links):
    """One ingredient's shopping suggestion, in strict priority order:
    inline_link > curated_shop_links > product_matches (by confidence) >
    generic_staple wording > no_reliable_match wording."""
    line = {
        "text": ing.get("text", ""),
        "match_status": ing.get("match_status", ""),
    }

    inline_link = ing.get("inline_link")
    if inline_link:
        line["suggestion"] = {"type": "inline_link", "link": inline_link}
        return line

    curated = _curated_match(ing.get("text", ""), curated_links)
    if curated:
        line["suggestion"] = {
            "type": "curated_shop_link",
            "ingredient": curated.get("ingredient", ""),
            "link": curated.get("link", ""),
        }
        return line

    status = ing.get("match_status", "")
    if status == "matched" and ing.get("product_matches"):
        matches = ing["product_matches"]
        best = matches[0]
        # Enrich with the real product image (products.json), looked up by
        # product_id -- the ingredient matcher itself never stored images.
        best_with_image = dict(best)
        best_with_image["image_link"] = PRODUCTS_BY_ID.get(best.get("product_id", ""), {}).get("image_link", "")
        line["suggestion"] = {
            "type": "product_match",
            "confidence": best.get("match_confidence", ""),
            "confidence_label": CONFIDENCE_LABEL.get(best.get("match_confidence", ""), ""),
            "product": best_with_image,
            "other_matches": matches[1:],
        }
        return line

    if status == "generic_staple_not_typically_stocked":
        line["suggestion"] = {
            "type": "generic_staple",
            "message": "Bežná surovina, ktorú Foodland typicky nemá v špecializovanej ponuke.",
        }
        return line

    if status == "no_reliable_match":
        line["suggestion"] = {
            "type": "no_match",
            "message": "Pre túto ingredienciu sa na Foodlande nenašiel spoľahlivý produkt -- neuvádzame náhradu, aby sme si nevymýšľali.",
        }
        return line

    line["suggestion"] = {"type": "none", "message": ""}
    return line


def build_shopping_list_summary(lines):
    """Collapse the per-ingredient lines into one 'shopping list' view --
    every item that has a real, clickable link, plus a count of how many
    ingredients fall into each honesty bucket. Nothing here is a new
    judgement call -- it just re-groups the same suggestion data already
    computed in build_ingredient_line."""
    items = []
    counts = {"ready": 0, "check": 0, "not_on_foodland": 0, "no_match": 0}
    for line in lines:
        sug = line.get("suggestion", {})
        t = sug.get("type")
        if t in ("inline_link", "curated_shop_link"):
            items.append({"text": line["text"], "link": sug.get("link", ""), "note": ""})
            counts["ready"] += 1
        elif t == "product_match":
            product = sug.get("product", {})
            high = sug.get("confidence") == "high"
            items.append({
                "text": line["text"],
                "link": product.get("link", ""),
                "note": "" if high else "over si vhodnosť",
            })
            counts["ready" if high else "check"] += 1
        elif t == "generic_staple":
            counts["not_on_foodland"] += 1
        elif t == "no_match":
            counts["no_match"] += 1
    return {"items": items, "counts": counts}


def gather_related_suggestions(lines, limit=3):
    """Real cross-sell / alternative tips for the products actually matched
    to this recipe's ingredients -- looked up from the same cross_sell /
    alternatives tables the rest of the backend uses, keyed by product_id.
    Never invented; skipped entirely if no matched product has any row."""
    seen_links = set()
    for line in lines:
        sug = line.get("suggestion", {})
        if sug.get("type") == "product_match":
            seen_links.add(sug.get("product", {}).get("link", ""))
        elif sug.get("type") in ("inline_link", "curated_shop_link"):
            seen_links.add(sug.get("link", ""))

    extra = []
    for line in lines:
        sug = line.get("suggestion", {})
        if sug.get("type") != "product_match":
            continue
        product_id = sug.get("product", {}).get("product_id", "")
        for table in (CROSS_SELL_BY_ID, ALTERNATIVES_BY_ID):
            row = table.get(product_id)
            if not row:
                continue
            for s in row.get("suggestions", [])[:2]:
                link = s.get("link", "")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                extra.append({
                    "title": s.get("title", ""),
                    "link": link,
                    "reason": s.get("reason", ""),
                    "image_link": image_for_link(link),
                    "based_on": sug.get("product", {}).get("title", ""),
                })
                if len(extra) >= limit:
                    return extra
    return extra


def find_followup_ingredient_match(recipe, question):
    """Does this question's words overlap with any ingredient line text of
    a previously-discussed recipe? Stopword-filtered token overlap (same
    meaningful_tokens() helper used by search.py) -- common words like
    "a"/"je"/"vás" never count as a match on their own, only real keywords."""
    qtoks = search_mod.meaningful_tokens(question)
    if not qtoks:
        return []
    matches = []
    for ing in recipe.get("ingredients", []):
        itoks = search_mod.meaningful_tokens(ing.get("text", ""))
        if qtoks & itoks:
            matches.append(ing)
    return matches


def build_followup_answer(session_id, question, lang):
    """If this question doesn't match any recipe on its own, but the
    session recently discussed one, check whether it's asking about one of
    that recipe's real ingredients (e.g. 'a čo soľ?' right after a Kimchi
    question). Returns None if nothing real matches -- never fabricates a
    follow-up just to seem conversational."""
    for recipe in get_session_recipes(session_id):
        if not recipe.get("ingredients_available"):
            continue
        matched_ings = find_followup_ingredient_match(recipe, question)
        if not matched_ings:
            continue
        curated_links = recipe.get("curated_shop_links", [])
        lines = [build_ingredient_line(ing, curated_links) for ing in matched_ings]
        sk_url = recipe.get("urls", {}).get("SK", "")
        fallback_url = next((u for u in recipe.get("urls", {}).values() if u), "")
        return {
            "recipe_name": recipe.get("name", ""),
            "cuisine": recipe.get("cuisine", ""),
            "ingredients_available": True,
            "is_followup": True,
            "followup_note": f"Pokračujem v predošlej otázke k receptu „{recipe.get('name', '')}“.",
            "recipe_link": sk_url or fallback_url,
            "ingredients": lines,
            "shopping_list": build_shopping_list_summary(lines),
            "related_suggestions": gather_related_suggestions(lines),
        }
    return None


def build_recipe_answer(recipe):
    """Full structured answer for 'what do I need to cook X' for one recipe."""
    sk_url = recipe.get("urls", {}).get("SK", "")
    fallback_url = next((u for u in recipe.get("urls", {}).values() if u), "")

    if not recipe.get("ingredients_available"):
        return {
            "recipe_name": recipe.get("name", ""),
            "cuisine": recipe.get("cuisine", ""),
            "ingredients_available": False,
            "message": (
                "Foodland pre tento recept nemá zverejnený štruktúrovaný zoznam "
                "ingrediencií (stránka obsahuje len text/video bez zoznamu)."
            ),
            "recipe_link": sk_url or fallback_url,
        }

    curated_links = recipe.get("curated_shop_links", [])
    lines = [build_ingredient_line(ing, curated_links) for ing in recipe.get("ingredients", [])]

    source_lang = recipe.get("ingredient_source_lang", "SK")
    source_note = ""
    if source_lang and source_lang != "SK":
        source_note = (
            f"Ingrediencie sú prevzaté z {source_lang} jazykovej verzie receptu -- "
            "SK stránka pre tento recept momentálne nebola dostupná."
        )

    return {
        "recipe_name": recipe.get("name", ""),
        "cuisine": recipe.get("cuisine", ""),
        "ingredients_available": True,
        "source_lang": source_lang,
        "source_lang_note": source_note,
        "source_url": recipe.get("ingredient_source_url", ""),
        "recipe_link": sk_url or fallback_url,
        "ingredients": lines,
        "shopping_list": build_shopping_list_summary(lines),
        "related_suggestions": gather_related_suggestions(lines),
    }


def find_top_recipes(question, lang):
    """The best-matching recipe(s) for this question, as raw recipe dicts.
    Ties at the top score are all included (capped at 3) rather than
    silently guessing one -- e.g. a bare 'Kimchi' query matches several
    real Kimchi recipes equally well."""
    results = search_mod.search_recipes(question, KNOWLEDGE, lang=lang, limit=10)
    if not results:
        return []
    top_score = results[0]["score"]
    return [r for r in results if r["score"] == top_score][:3]


def build_recipe_answers(recipes):
    return [build_recipe_answer(r) for r in recipes]


# ---------------------------------------------------------------------------
# Optional LLM synthesis (only runs if OPENAI_API_KEY is set; never required)
# ---------------------------------------------------------------------------
def call_openai(question, lang, context_results):
    """Send ONLY the already-retrieved real context to OpenAI and ask it to
    answer strictly from it. Any failure falls back to search_only --
    this must never turn into a 500."""
    context_lines = []
    for r in context_results[:15]:
        label = r.get("title") or r.get("question") or r.get("name") or r.get("product_title") or r.get("intent") or ""
        link = r.get("link") or r.get("product_link") or (r.get("urls") or {}).get(lang) or ""
        context_lines.append(f"[{r.get('source')}] {label} | {link}")
    system_prompt = (
        "Si predajný poradca e-shopu Foodland.sk. Odpovedaj VÝHRADNE na základe "
        "nižšie uvedeného kontextu (reálne dáta z feedu/knowledge databázy). "
        "Nikdy nevymýšľaj produkty, ceny, recepty ani URL, ktoré nie sú v kontexte. "
        "Ak kontext otázku nepokrýva, jasne povedz, že to nevieš. Na konci uveď "
        "zoznam reálnych zdrojových URL, ktoré si použil."
    )
    user_prompt = f"Otázka ({lang}): {question}\n\nKontext:\n" + "\n".join(context_lines)

    payload = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "FoodlandAdvisor/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _client_ip(self):
        return self.client_address[0]

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html_text):
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/chat.html"):
            if CHAT_HTML_PATH.exists():
                self._send_html(200, CHAT_HTML_PATH.read_text(encoding="utf-8"))
            else:
                self._send_html(404, "<h1>chat.html not found</h1>")
            return

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "products_loaded": len(PRODUCTS),
                "knowledge_loaded": {k: len(v) for k, v in KNOWLEDGE.items()},
                "mode": "llm" if OPENAI_API_KEY else "search_only",
            })
            return

        if path == "/products/search":
            q = (qs.get("q", [""])[0]).strip()
            if not q:
                self._send_json(400, {"error": "missing required query param 'q'"})
                return
            results = search_mod.search_products(q, PRODUCTS, limit=20)
            self._send_json(200, {"query": q, "count": len(results), "results": results})
            return

        if path == "/knowledge/search":
            q = (qs.get("q", [""])[0]).strip()
            lang = (qs.get("lang", [""])[0]).strip() or None
            if not q:
                self._send_json(400, {"error": "missing required query param 'q'"})
                return
            results = []
            results += search_mod.search_faq(q, KNOWLEDGE, lang, limit=10)
            results += search_mod.search_recipes(q, KNOWLEDGE, lang, limit=10)
            results += search_mod.search_blog(q, KNOWLEDGE, lang, limit=10)
            results += search_mod.search_cross_sell(q, KNOWLEDGE, limit=10)
            results += search_mod.search_alternatives(q, KNOWLEDGE, limit=10)
            results += search_mod.search_products_ai(q, KNOWLEDGE, limit=10)
            results += search_mod.search_intent_mapping(q, KNOWLEDGE, lang, limit=10)
            results.sort(key=lambda r: -r["score"])
            results = enrich_results_with_images(results)
            self._send_json(200, {"query": q, "lang": lang, "count": len(results), "results": results})
            return

        self._send_json(404, {"error": f"unknown path {path}"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        parsed = urlsplit(self.path)
        if parsed.path != "/ask":
            self._send_json(404, {"error": f"unknown path {parsed.path}"})
            return

        ip = self._client_ip()
        if not check_rate_limit(ip):
            self._send_json(429, {"error": "rate limit exceeded (30 questions / 10 min per IP)"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        question = (body.get("question") or "").strip()
        lang = (body.get("lang") or "SK").strip().upper()
        session_id = (body.get("session_id") or "").strip()
        if not question:
            self._send_json(400, {"error": "missing required field 'question'"})
            return

        search_results = search_mod.search_all(question, PRODUCTS, KNOWLEDGE, lang=lang, limit_per_source=5)
        search_results = enrich_results_with_images(search_results)
        top_recipes = find_top_recipes(question, lang)
        recipe_answers = build_recipe_answers(top_recipes)

        if recipe_answers:
            remember_session_recipes(session_id, top_recipes)
        elif session_id:
            # No recipe matched this question directly -- check whether it's
            # a real follow-up about a recipe discussed earlier in this
            # session (e.g. "a čo soľ?" right after a Kimchi question).
            followup = build_followup_answer(session_id, question, lang)
            if followup:
                recipe_answers = [followup]

        if OPENAI_API_KEY:
            try:
                answer_text = call_openai(question, lang, search_results)
                response = {
                    "mode": "llm",
                    "question": question,
                    "lang": lang,
                    "answer": answer_text,
                    "recipe_answers": recipe_answers,
                    "results": search_results,
                }
                log_analytics(ip, question, "llm", len(search_results))
                self._send_json(200, response)
                return
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError) as exc:
                # Never 500 -- fall back to search_only and say why.
                response = {
                    "mode": "search_only",
                    "question": question,
                    "lang": lang,
                    "llm_error": f"OpenAI volanie zlyhalo, padáme na search_only: {exc}",
                    "recipe_answers": recipe_answers,
                    "results": search_results,
                }
                log_analytics(ip, question, "search_only_fallback", len(search_results))
                self._send_json(200, response)
                return

        response = {
            "mode": "search_only",
            "question": question,
            "lang": lang,
            "recipe_answers": recipe_answers,
            "results": search_results,
        }
        log_analytics(ip, question, "search_only", len(search_results))
        self._send_json(200, response)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ANALYTICS_PATH.exists():
        ANALYTICS_PATH.touch()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Foodland Advisor backend running at http://localhost:{PORT}")
    print(f"  mode: {'llm' if OPENAI_API_KEY else 'search_only'}")
    print(f"  products loaded: {len(PRODUCTS)}")
    print(f"  knowledge loaded: {{ {', '.join(f'{k}: {len(v)}' for k, v in KNOWLEDGE.items())} }}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()

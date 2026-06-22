# Foodland AI Poradca -- backend v1

Predajný poradca pre Foodland.sk (ázijské potraviny), ktorý odpovedá na otázky
o produktoch, receptoch, FAQ a ingredienciách výhradne na základe reálnych dát
z Google Merchant feedu a ručne overených znalostných tabuliek. Nič v
odpovediach nie je vymyslené modelom -- v tejto v1 (bez OpenAI kľúča) sa
vôbec nič nepretĺka cez žiadny model, len sa vyhľadáva a štruktúrovane
vracajú reálne záznamy.

## Spustenie (žiadny pip install nie je potrebný)

Backend je napísaný čisto v Python štandardnej knižnici, takže stačí mať
nainštalovaný Python 3.8+.

```bash
cd foodland-advisor
python3 app/main.py
```

Potom otvor **http://localhost:8000** v prehliadači -- načíta sa chat okno
(`chat.html`), ktoré je servované priamo z toho istého backendu (žiadne
CORS problémy, žiadny ďalší server).

Dáta (`data/products.json`, `data/foodland_knowledge.json`) sú už
predgenerované a priložené, takže spustenie vyžaduje len ten jeden príkaz.

## Štruktúra projektu

```
app/
  feed.py             parser Google Merchant XML feedu
  import_feed.py      import XML -> data/products.json
  import_knowledge.py import 7× xlsx + foodland_recipe_ingredients.json -> data/foodland_knowledge.json
  search.py           lokálne vyhľadávanie (produkty + knowledge)
  main.py             HTTP server + /health, /products/search, /knowledge/search, /ask
data/
  googleMerchant_sk_export.xml       zdrojový feed (2140 produktov)
  *.xlsx                             7 znalostných tabuliek (FAQ, recepty, magazín, cross-sell...)
  foodland_recipe_ingredients.json   53 receptov / 661 riadkov ingrediencií + mapovanie na produkty
  products.json                      vygenerované z feedu
  foodland_knowledge.json            vygenerované z xlsx + ingrediencií
  analytics.jsonl                    runtime log otázok (vytvára sa pri behu)
chat.html             jednoduché chat okno (servované z "/")
requirements.txt      (žiadne závislosti -- len Python stdlib)
.env.example          voliteľné premenné prostredia (PORT, OPENAI_API_KEY)
railpack.json          start command pre nasadenie na Railway
```

## Ako znova vygenerovať dáta

Ak sa zmení feed alebo niektorá z xlsx tabuliek, dáta sa obnovia takto:

```bash
cd app
python3 import_feed.py        # prepíše ../data/products.json
python3 import_knowledge.py   # prepíše ../data/foodland_knowledge.json
```

Oba skripty sú idempotentné -- opakovaný spustenie nad rovnakými vstupmi
vždy vyprodukuje identický výstup.

## Endpoints

- `GET /health` -- počet načítaných produktov a knowledge záznamov, aktuálny režim.
- `GET /products/search?q=...` -- čisté produktové vyhľadávanie nad feedom.
- `GET /knowledge/search?q=...&lang=SK` -- vyhľadávanie nad FAQ/receptami/magazínom/cross-sell/alternatívami/Products_AI/IntentMapping, voliteľný jazykový filter.
- `POST /ask` -- `{"question": "...", "lang": "SK", "session_id": "..."}` (`session_id` je voliteľný, pre follow-up otázky -- pozri nižšie) -- hlavný poradenský endpoint. Vždy vráti `mode` (`search_only` alebo `llm`), zoznam nájdených výsledkov (`results`) a -- ak otázka zasiahla nejaký recept (alebo ide o follow-up na predošlý recept v session) -- aj `recipe_answers` s rozpisom ingrediencií, nákupných tipov, súhrnným nákupným zoznamom a súvisiacimi tipmi.

## search_only vs. llm režim

Bez `OPENAI_API_KEY` v prostredí backend bežia v `search_only` režime --
`/ask` vráti priamo štruktúrované výsledky vyhľadávania, žiadnu generovanú
vetu. Toto je aktuálne nastavený a odskúšaný režim (podľa tvojej voľby
"zatiaľ bez kľúča").

Ak neskôr doplníš `OPENAI_API_KEY` (do prostredia, alebo skopíruj
`.env.example` na `.env` a nastav premenné cez `export $(cat .env | xargs)`
pred spustením), backend automaticky prejde do `llm` režimu -- pošle
OpenAI len nájdený kontext (žiadne vymýšľanie) a vráti odpoveď + reálne
zdrojové URL. Pri zlyhaní volania (chýbajúci internet, zlý kľúč...) sa
backend bezpečne vráti k `search_only` odpovedi -- `/ask` nikdy nevráti 500.

## Nové funkcie (inšpirované Amazon/Walmart/Instacart AI nákupnými asistentmi)

- **Obrázky produktov v chate** -- `/ask` aj `/knowledge/search` teraz k
  produktovým zhodám (`product_match`, `cross_sell`, `alternative`,
  `products_ai`) dopĺňajú `image_link`, dohľadaný spätným joinom na
  `products.json` podľa reálneho `product_id`/`link` -- nikdy nevymyslený,
  ak obrázok neexistuje, pole je prázdne.
- **Súhrnný nákupný zoznam** -- `recipe_answers[].shopping_list` zoskupuje
  ingrediencie receptu do jedného zoznamu (`items`) s reálnymi odkazmi a
  počtami (`counts`: `ready`/`check`/`not_on_foodland`/`no_match`), nad
  presne tými istými dátami, ktoré sa už zobrazujú pri jednotlivých
  ingredienciách.
- **Automatický cross-sell/alternatívy pri recepte** --
  `recipe_answers[].related_suggestions` k zhodnutým produktom v recepte
  doplní reálne cross-sell/alternatívne tipy z `foodland_knowledge.json`
  (tabuľky `cross_sell`/`alternatives`, join cez `product_id`) -- ak pre
  produkt žiadny tip v dátach nie je, pole ostáva prázdne.
- **Pamäť konverzácie (follow-up otázky)** -- `/ask` teraz prijíma voliteľné
  `session_id` (chat.html ho generuje pri načítaní stránky). Keď otázka
  sama osebe nezasiahne žiadny recept, ale v session bol nedávno (30 min)
  riešený recept, backend skontroluje, či sa otázka prekrýva s reálnym
  textom niektorej jeho ingrediencie (napr. "a čo soľ?" po otázke na
  Kimchi) -- ak áno, vráti `is_followup: true` a `followup_note` s názvom
  receptu. **Dôležité obmedzenie:** keďže recept-vyhľadávanie (`search_recipes`)
  nemá filter bežných slov ("je", "a", "vás"...), krátke follow-up otázky
  často omylom zasiahnu nejaký recept priamo (cez zhodu na takéto bežné
  slovo) -- v tom prípade sa vráti táto (síce reálna, ale nie vždy
  najrelevantnejšia) priama zhoda namiesto `is_followup` odpovede. Funkcia
  teda funguje spoľahlivo len pre otázky, ktoré cez bežné vyhľadávanie
  nezasiahnu žiadny recept.

## Ingrediencie receptov -- ako sa vyberá produktový tip

Pre otázky typu "čo potrebujem na uvarenie X" sa pre každú ingredienciu
v recepte vyberá návrh v tomto poradí:

1. `inline_link` -- priamy odkaz z receptu (najvyššia istota).
2. `curated_shop_links` -- ručne vybraná kategória pre danú ingredienciu.
3. `product_matches` -- automatizovaná zhoda kľúčových slov nad feedom,
   oznámená s úrovňou istoty (`high`/`medium`) a vždy s hedge formuláciou
   ("over si vhodnosť" pri `medium`) -- nikdy nie ako 100% záruka.
4. Bežná surovina (soľ, cukor, voda, cesnak...) -- backend otvorene povie,
   že ide o surovinu, ktorú Foodland typicky nemá v špecializovanej ponuke.
5. Žiadna spoľahlivá zhoda -- backend to povie priamo, nikdy nedomyslí náhradu.

Ak recept nemá zverejnený zoznam ingrediencií (2 z 53 receptov), alebo má
ingrediencie prevzaté z CZ jazykovej verzie (ďalšie 2 z 53), `/ask` to
explicitne uvedie v odpovedi.

## Rate limiting a analytika

- Per-IP limit: 30 otázok / 10 minút na `/ask`. Po prekročení vráti `429`.
  (Limit je in-memory -- reštart backendu ho vynuluje, čo je v poriadku pre v1.)
- Každá otázka na `/ask` sa zaloguje ako jeden riadok JSON do
  `data/analytics.jsonl`: hash IP adresy (nie raw IP), čas, otázka, režim,
  počet nájdených výsledkov.

## Nasadenie na Railway

Backend je na Railway pripravený bez úprav -- `main.py` už číta port z
premennej `$PORT` a počúva na `0.0.0.0` (presne to, čo Railway vyžaduje).
V projekte je priložený aj `railpack.json`, ktorý Railway povie presný
príkaz na spustenie (`python3 app/main.py`) -- bez neho by ho Railway
nenašlo, pretože `main.py` je v podpriečinku `app/`, nie v koreni projektu.

**Spôsob A -- cez Railway CLI, bez GitHubu (najrýchlejšie):**

```bash
npm i -g @railway/cli
railway login
cd foodland-advisor
railway init
railway up
```

**Spôsob B -- cez GitHub:**

1. Pushni priečinok `foodland-advisor/` (s `railpack.json`) do GitHub repa.
2. V Railway dashboarde: New Project → Deploy from GitHub repo.
3. Railway zbuilduje (žiadne pip závislosti, takže build je rýchly) a spustí
   podľa `railpack.json`.

V oboch prípadoch voliteľne doplň `OPENAI_API_KEY` v Settings → Variables,
ak chceš `llm` režim -- `PORT` nastavuje Railway sám, nič k tomu netreba.

**Dôležité obmedzenie:** `data/analytics.jsonl` sa píše na lokálny disk
behu kontajnera. Railway disk bez priloženého Volume nie je trvalý medzi
redeployami -- log otázok sa teda pri každom novom nasadení vynuluje. Pre
v1 to nie je problém (rate limit je aj tak in-memory a resetuje sa rovnako),
ale ak by si chcel/a log uchovávať dlhodobo, treba v Railway pripojiť Volume
na `data/`.

Podmienky bezplatného plánu Railway (kredit, limity) sa menia -- over si
aktuálny stav na railway.com pred nasadením.

## Známe obmedzenia

- Automatický keyword/stem matcher pre ingrediencie je jednoduchý
  (bez embeddingov), takže pri opisných slovách (napr. "tenké", "nakrájané")
  občas vznikne slabšia `medium` zhoda, ktorá nemusí byť presná -- presne
  preto je vždy oznámená s hedge formuláciou, nikdy ako istota. Počas
  testovania tohto backendu som našiel a opravil chybu, kde sa pri
  ingredienciách s opakovaným slovom (napr. "kóšer soli alebo ... soli")
  rátalo opakovanie ako dva nezávislé signály a zhoda sa nesprávne
  vykazovala ako `high` -- opravené, teraz korektne `medium`.
- Backend je čisto Python stdlib (žiadny FastAPI/uvicorn), aby šlo spustiť
  bez `pip install`. Ak by si neskôr chcel/a prejsť na FastAPI (napr. pre
  async, OpenAPI docs, Pydantic validáciu), architektúra (`feed.py`,
  `search.py`, oddelená logika v `main.py`) sa dá na FastAPI prepísať bez
  zmeny dátovej vrstvy.
- `/ask` v `llm` režime nebol odskúšaný naživo (nemám OpenAI kľúč) --
  kód je napísaný a zapojený tak, aby pri zlyhaní volania bezpečne spadol
  na `search_only`, ale samotné úspešné LLM volanie si over až s reálnym
  kľúčom.
- `search.py` nemá zoznam bežných slov (stopwords) -- krátke slová ako
  "je", "a", "vás" sa počítajú ako plnohodnotné zhody. V praxi to znamená,
  že aj otázky mimo tému (napr. o vrátení tovaru) môžu omylom "trafiť"
  nejaký recept len cez takéto bežné slovo. Priamo to ovplyvňuje aj nový
  follow-up mechanizmus (pozri vyššie) -- testované a overené priamym
  jednotkovým testom správania `build_followup_answer`, integračne sa to
  ale cez `/ask` prejaví len pri otázkach, ktoré takto omylom nezasiahnu
  žiadny recept.

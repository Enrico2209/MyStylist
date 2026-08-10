# Regole di keyword-tagging per attributi stilistici

## 1. Campi da scrapare per ogni prodotto (oltre alle foto)

Tutti estraibili dalla stessa pagina prodotto già aperta per le foto — nessun costo aggiuntivo.

| Campo | Dove si trova | Affidabilità | Uso |
|---|---|---|---|
| `category_path` | già disponibile dal crawling categorie | Alta | garment_type/subcategory, prior di formalità |
| `gender` | derivato da category_path (`/abbigliamento-donna/` vs `/abbigliamento/`) | Alta | filtro outfit (stesso genere) |
| `brand` | breadcrumb o prima parola del `<h1>` | Alta | affinità di stile (tabella seed sotto) |
| `product_title` | `<h1>` pagina prodotto | Alta | fit (slim/oversize/regular), garment_type di dettaglio |
| `description_text` | blocco descrizione prodotto | Alta per stile/formalità | keyword matching (vedi §2) |
| `composition` | regex `Composizione:\s*(.+)` | Alta | materiale → stagione/texture |
| `fit` | regex `Vestibilità\s+(\w+)` | Alta | fit → formalità/streetwear |
| `color_attribute` | filtro layered-nav o slug URL (ultimo segmento prima del codice) | Media | nome colore commerciale (poi raffinato con k-means sulla foto) |
| `price` | prezzo pagina | Bassa, non usarlo per lo stile | solo eventuale tier qualità, non tag stilistico |
| `sku` | `Codice Prodotto:` | — | solo identificativo, non stile |

## 2. Tassonomia di style_tags

Categorie non mutuamente esclusive — un capo può prendere più tag con un punteggio di confidenza ciascuno.

`elegante` · `casual` · `streetwear` · `sportivo` · `workwear` · `outdoor_tecnico` · `vintage_prep` · `minimal` · `military` · `boho_fantasia`

## 3. Dizionari keyword (italiano, da `description_text` + `product_title`)

```python
STYLE_KEYWORDS = {
    "elegante": [
        "elegante", "raffinato", "cerimonia", "ufficio", "classe", "impeccabile",
        "sartoriale", "blazer", "completo", "smoking", "abito", "chic",
    ],
    "casual": [
        "casual", "quotidiano", "tempo libero", "comodo", "versatile",
        "tutti i giorni", "informale",
    ],
    "streetwear": [
        "streetwear", "urban", "oversize", "graffiti", "stampa grafica",
        "skate", "hip hop", "baggy",
    ],
    "sportivo": [
        "sportivo", "tecnico performante", "training", "running", "palestra",
        "performance", "traspirante",
    ],
    "workwear": [
        "workwear", "cargo", "utility", "operaio", "resistente", "hi-vis",
    ],
    "outdoor_tecnico": [
        "impermeabile", "membrana", "trekking", "montagna", "antivento",
        "idrorepellente", "tecnico outdoor",
    ],
    "vintage_prep": [
        "vintage", "retro", "college", "preppy", "old school", "anni '70",
        "anni '80", "anni '90",
    ],
    "minimal": [
        "minimal", "essenziale", "pulito", "lineare", "basico", "senza tempo",
    ],
    "military": [
        "militare", "camouflage", "mimetico", "cargo militare",
    ],
    "boho_fantasia": [
        "boho", "etnico", "fantasia", "paisley", "hippie",
    ],
}

# Materiale -> segnali di stile e stagione
MATERIAL_SIGNALS = {
    "lana":            {"style": ["elegante", "vintage_prep"], "season": "inverno"},
    "cashmere":        {"style": ["elegante"], "season": "inverno"},
    "piumino":         {"style": ["outdoor_tecnico"], "season": "inverno"},
    "nylon tecnico":   {"style": ["outdoor_tecnico", "streetwear"], "season": "tutte"},
    "pelle":           {"style": ["streetwear", "elegante"], "season": "mezza_stagione"},  # dipende dal taglio
    "denim":           {"style": ["casual", "vintage_prep"], "season": "tutte"},
    "lino":            {"style": ["elegante", "casual"], "season": "estate"},
    "cotone":          {"style": ["casual"], "season": "tutte"},
    "poliestere":      {"style": ["sportivo", "outdoor_tecnico"], "season": "tutte"},
}

# Fit -> segnali di stile (da campo Vestibilità o dal titolo)
FIT_SIGNALS = {
    "slim":     {"style": ["elegante", "minimal"], "formality_delta": +1},
    "skinny":   {"style": ["streetwear"], "formality_delta": 0},
    "regular":  {"style": ["casual"], "formality_delta": 0},
    "oversize": {"style": ["streetwear"], "formality_delta": -1},
    "baggy":    {"style": ["streetwear"], "formality_delta": -1},
    "relaxed":  {"style": ["casual"], "formality_delta": 0},
}

# Pattern -> da description_text, fallback "tinta unita" se nessun match
PATTERN_KEYWORDS = {
    "righe":      ["a righe", "rigato", "righine"],
    "quadri":     ["a quadri", "check", "tartan"],
    "camouflage": ["camouflage", "mimetico"],
    "stampato":   ["stampa", "stampato", "grafica"],
    "animalier":  ["animalier", "leopardato", "zebrato"],
    # se nessuna corrisponde -> "tinta_unita" di default
}
```

## 4. Formalità: dalla categoria + dai tag

Prior di base dalla `category_path` (più affidabile del testo libero):

```python
CATEGORY_FORMALITY_PRIOR = {
    "completi": 5, "giacche": 4, "camicie": 3, "polo": 3,
    "maglieria": 3, "pantaloni": 3, "jeans": 2, "t-shirt": 2,
    "felpe": 2, "tute": 1, "costumi": 1, "sneakers": 2,
}

STYLE_TAG_FORMALITY = {
    "elegante": 5, "vintage_prep": 3, "minimal": 3, "casual": 2,
    "workwear": 2, "boho_fantasia": 2, "streetwear": 1,
    "sportivo": 1, "outdoor_tecnico": 1, "military": 2,
}
```

`formality_score` finale = media pesata tra prior di categoria (peso 0.5), tag di stile matchati (peso 0.35), e delta del fit (peso 0.15) — poi arrotondata 1-5.

## 5. Confidenza e fallback alla vision API

Ogni tag ottenuto da keyword matching porta un punteggio di confidenza:

- Match su `description_text` esplicito → confidenza alta (0.8)
- Match solo su `brand` (tabella affinità sotto) → confidenza media (0.4), usata come prior/tie-break, mai da sola
- Nessun match in nessuna fonte → prodotto **flaggato per revisione vision API** (campione, non tutto il catalogo)

Regola pratica: se `style_tags` risulta vuoto dopo lo scan testuale, quel prodotto entra nella coda "da chiamare con vision API" — così il costo resta proporzionale solo ai casi realmente ambigui.

## 6. Tabella di affinità brand (seed iniziale, da raffinare nel tempo)

Basata sui brand osservati nei log di scraping. Peso basso (0.4), solo come prior aggiuntivo.

```python
BRAND_AFFINITY = {
    "nuvolari": ["casual", "elegante"],          # linea propria, molto trasversale
    "k-way": ["outdoor_tecnico", "casual"],
    "the north face": ["outdoor_tecnico", "sportivo"],
    "napapijri": ["outdoor_tecnico"],
    "carhartt wip": ["workwear", "streetwear"],
    "dickies": ["workwear", "streetwear"],
    "deus ex machina": ["streetwear", "vintage_prep"],
    "vans": ["streetwear"],
    "obey": ["streetwear"],
    "stussy": ["streetwear"],
    "stone island": ["streetwear", "outdoor_tecnico"],
    "fred perry": ["vintage_prep", "casual"],
    "lyle & scott": ["vintage_prep", "casual"],
    "lee": ["casual", "vintage_prep"],
    "levis": ["casual", "vintage_prep"],
    "calvin klein": ["minimal", "casual"],
    "tommy hilfiger": ["casual", "vintage_prep"],
    "tommy jeans": ["streetwear", "casual"],
    "guess jeans": ["casual"],
    "nike": ["sportivo", "streetwear"],
    "adidas": ["sportivo", "streetwear"],
    "new balance": ["sportivo", "streetwear"],
    "asics": ["sportivo"],
    "saucony": ["sportivo", "vintage_prep"],
    "hoka": ["sportivo"],
    "dr. martens": ["streetwear", "vintage_prep"],
    "colmar": ["outdoor_tecnico", "elegante"],
    "blauer": ["outdoor_tecnico", "casual"],
    "woolrich": ["outdoor_tecnico", "elegante"],
    "c.p. company": ["outdoor_tecnico", "streetwear"],
    "ralph lauren": ["elegante", "vintage_prep"],
    "goorin bros": ["vintage_prep", "streetwear"],
    "gas": ["casual"],
    "diesel": ["streetwear", "casual"],
    "psycho bunny": ["casual", "elegante"],
    "weekend offender": ["streetwear", "vintage_prep"],
    "edwin": ["vintage_prep", "casual"],
}
```

## 7. Cosa resta SOLO alla vision API (non recuperabile da testo)

- Pattern/texture quando la descrizione non li menziona (succede spesso)
- Verifica di coerenza cromatica reale tra capo e foto (il nome commerciale del colore può essere fuorviante — es. "verde bottiglia" venduto sotto un colore che nella swatch appare più simile a "verde militare")
- Validazione a campione della qualità del tagging automatico (es. 5% random del catalogo, per calcolare un tasso di errore stimato)

## 8. Prossimo passo naturale

Scrivere la funzione `derive_attributes(product_metadata: dict) -> dict` che applica queste regole e produce l'oggetto attributi finale, più la funzione di scraping che raccoglie `product_metadata` durante il download foto (stesso giro di rete, zero richieste aggiuntive).

#!/usr/bin/env python3
"""
Nuvolari.biz — Scraper con foto + metadata + tagging stilistico (v3)
========================================================================
 
Un unico script che, per ogni prodotto:
1. Apre la pagina UNA SOLA VOLTA (nessuna richiesta doppia).
2. Estrae le foto reali della galleria (mage/gallery/gallery, no banner).
3. Estrae i metadata testuali: brand, titolo, descrizione, prezzo,
   composizione, vestibilità, colore (dallo slug).
4. Applica le regole di keyword-tagging (style_tags, formality_score,
   season, pattern) — vedi style_tagging_rules.md per la logica.
5. Salva foto + metadata.json per prodotto, e un catalogo consolidato
   (catalogo.jsonl) con tutti i prodotti, pronto per la fase di
   clustering/scoring successiva.
 
Uso in notebook
-----------------
    %run scrape_with_attributes.py
 
    run_scrape_with_attributes(
        output='/Users/enricociaralli/Desktop/nuvolari/nuvolari_full_organizzato',
        cache_file='/Users/enricociaralli/Desktop/nuvolari/category_cache.json',
        progress_file='/Users/enricociaralli/Desktop/nuvolari/progress.txt',
        max_products=20,  # test veloce, togli per lo scraping completo
    )
"""
 
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
 
import requests
from bs4 import BeautifulSoup
 
BASE_URL = "https://www.nuvolari.biz"
USER_AGENT = "NuvolariPartnerBot/1.0 (+contatto: enricociaralli@gmail.com)"

BRAND_LIST_FILE = Path(__file__).resolve().parent / "brand_list.json"
 
CATEGORY_SEEDS_FALLBACK = [
    "/abbigliamento.html",
    "/abbigliamento-donna.html",
    "/scarpe.html",
    "/accessori.html",
]
 
EXCLUDED_NAV_HINTS = [
    "/customer/", "/checkout/", "/wishlist", "/stores", "#",
    "/privacy-policy", "/cookie-policy", "/pagamenti", "/condizioni-generali",
    "/taglie", "/spedizioni", "/resi_rimborsi", "/gift-card", "/faq",
    "/chi-siamo", "/sostenibilita", "/work-with-us", "/fidelity-card",
    ".svg", ".png", ".jpg", "javascript:",
]
 
EXCLUDED_PATH_HINTS = [
    "/media/wysiwyg/", "/media/logo", "/media/PopUp", "/static/", "placeholder",
]
 
# =====================================================================
# REGOLE DI TAGGING (da style_tagging_rules.md)
# =====================================================================
 
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
        "vintage", "retro", "college", "preppy", "old school",
        "anni 70", "anni 80", "anni 90",
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
 
MATERIAL_SIGNALS = {
    "lana":          {"style": ["elegante", "vintage_prep"], "season": "inverno"},
    "cashmere":      {"style": ["elegante"], "season": "inverno"},
    "piumino":       {"style": ["outdoor_tecnico"], "season": "inverno"},
    "nylon":         {"style": ["outdoor_tecnico", "streetwear"], "season": "tutte"},
    "pelle":         {"style": ["streetwear", "elegante"], "season": "mezza_stagione"},
    "denim":         {"style": ["casual", "vintage_prep"], "season": "tutte"},
    "lino":          {"style": ["elegante", "casual"], "season": "estate"},
    "cotone":        {"style": ["casual"], "season": "tutte"},
    "poliestere":    {"style": ["sportivo", "outdoor_tecnico"], "season": "tutte"},
    "viscosa":       {"style": ["elegante", "casual"], "season": "mezza_stagione"},
    "elastan":       {"style": ["sportivo"], "season": "tutte"},
}
 
FIT_SIGNALS = {
    "slim":     {"style": ["elegante", "minimal"], "formality_delta": 1},
    "skinny":   {"style": ["streetwear"], "formality_delta": 0},
    "regular":  {"style": ["casual"], "formality_delta": 0},
    "oversize": {"style": ["streetwear"], "formality_delta": -1},
    "baggy":    {"style": ["streetwear"], "formality_delta": -1},
    "relaxed":  {"style": ["casual"], "formality_delta": 0},
}
 
PATTERN_KEYWORDS = {
    "righe":      ["a righe", "rigato", "righine"],
    "quadri":     ["a quadri", "check", "tartan"],
    "camouflage": ["camouflage", "mimetico"],
    "stampato":   ["stampa", "stampato", "grafica"],
    "animalier":  ["animalier", "leopardato", "zebrato"],
}
 
CATEGORY_FORMALITY_PRIOR = {
    "completi": 5, "giacche": 4, "camicie": 3, "polo": 3,
    "maglieria": 3, "maglie": 3, "pantaloni": 3, "jeans": 2, "t-shirt": 2,
    "felpe": 2, "tute": 1, "costumi": 1, "sneakers": 2, "giubbotti": 2,
    "cappotti": 4, "bomber": 2, "gilet": 3,
}
 
STYLE_TAG_FORMALITY = {
    "elegante": 5, "vintage_prep": 3, "minimal": 3, "casual": 2,
    "workwear": 2, "boho_fantasia": 2, "streetwear": 1,
    "sportivo": 1, "outdoor_tecnico": 1, "military": 2,
}
 
BRAND_AFFINITY = {
    "nuvolari": ["casual", "elegante"],
    "k-way": ["outdoor_tecnico", "casual"],
    "the-north-face": ["outdoor_tecnico", "sportivo"],
    "napapijri": ["outdoor_tecnico"],
    "carhartt-wip": ["workwear", "streetwear"],
    "dickies": ["workwear", "streetwear"],
    "deus-ex-machina": ["streetwear", "vintage_prep"],
    "vans": ["streetwear"],
    "obey": ["streetwear"],
    "stussy": ["streetwear"],
    "stone-island": ["streetwear", "outdoor_tecnico"],
    "fred-perry": ["vintage_prep", "casual"],
    "lyle-scott": ["vintage_prep", "casual"],
    "lee": ["casual", "vintage_prep"],
    "levi-s": ["casual", "vintage_prep"],
    "calvin-klein-jeans": ["minimal", "casual"],
    "tommy-hilfiger": ["casual", "vintage_prep"],
    "tommy-jeans": ["streetwear", "casual"],
    "guess-jeans": ["casual"],
    "nike": ["sportivo", "streetwear"],
    "adidas": ["sportivo", "streetwear"],
    "adidas-originals": ["streetwear", "vintage_prep"],
    "new-balance": ["sportivo", "streetwear"],
    "asics": ["sportivo"],
    "saucony": ["sportivo", "vintage_prep"],
    "hoka": ["sportivo"],
    "dr-martens": ["streetwear", "vintage_prep"],
    "colmar-originals": ["outdoor_tecnico", "elegante"],
    "blauer": ["outdoor_tecnico", "casual"],
    "woolrich": ["outdoor_tecnico", "elegante"],
    "c-p-company": ["outdoor_tecnico", "streetwear"],
    "ralph-lauren": ["elegante", "vintage_prep"],
    "goorin-bros": ["vintage_prep", "streetwear"],
    "gas": ["casual"],
    "diesel": ["streetwear", "casual"],
    "psycho-bunny": ["casual", "elegante"],
    "weekend-offender": ["streetwear", "vintage_prep"],
    "edwin": ["vintage_prep", "casual"],
    "lacoste": ["vintage_prep", "elegante"],
    "propaganda": ["streetwear"],
}
 
COLOR_WORDS = {
    "nero", "black", "bianco", "white", "blu", "blue", "navy", "verde", "green",
    "rosso", "red", "giallo", "yellow", "grigio", "grey", "gray", "marrone",
    "brown", "beige", "rosa", "pink", "viola", "purple", "arancione", "orange",
    "militare", "military", "panna", "cream", "bordeaux", "azzurro", "celeste",
    "oro", "gold", "argento", "silver", "multicolor", "multi", "fango", "tortora",
    "salvia", "khaki", "denim", "olive", "oliva", "avio", "petrolio", "senape",
    "cammello", "camel", "ecru", "sabbia", "sand", "mustard", "burgundy", "teal",
}
 
 
def slugify(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-")[:120] or "prodotto"
 
 
def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9"})
    return s


# =====================================================================
# Lista brand ufficiale (per riconoscere il brand reale dal titolo)
# =====================================================================
#
# Il link "brand" dentro la pagina prodotto NON è affidabile: è il primo
# <a href="/brands/..."> trovato nell'HTML, che nella grande maggioranza
# dei casi è un link del menu di navigazione (sempre lo stesso, es.
# "NUVOLARI BRAND") e non il brand del prodotto specifico. Il titolo del
# prodotto invece segue quasi sempre il pattern
# "<CATEGORIA> <GENERE> <BRAND> <NOME PRODOTTO>", quindi cerchiamo quale
# brand della lista ufficiale (scaricata una volta da /brands) compare nel
# titolo.

def discover_brand_list(session: requests.Session) -> dict:
    """Scarica la lista ufficiale dei brand dal menu /brands del sito
    (nome visualizzato -> slug)."""
    try:
        r = session.get(f"{BASE_URL}/brands", timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] impossibile scaricare la lista brand: {e}", file=sys.stderr, flush=True)
        return {}
    soup = BeautifulSoup(r.text, "lxml")
    brand_list = {}
    for a in soup.find_all("a", href=re.compile(r"/brands/[^/?#]+/?$")):
        text = a.get_text(strip=True)
        href = a.get("href")
        if text and href:
            brand_list[text] = href.rstrip("/").split("/")[-1]
    return brand_list


def load_brand_list() -> dict:
    if BRAND_LIST_FILE.exists():
        return json.loads(BRAND_LIST_FILE.read_text(encoding="utf-8"))
    return {}


def save_brand_list(brand_list: dict) -> None:
    BRAND_LIST_FILE.write_text(json.dumps(brand_list, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [i] lista brand salvata in {BRAND_LIST_FILE} ({len(brand_list)} brand)", flush=True)


BRAND_LIST = load_brand_list()


def match_brand_from_title(title: str) -> tuple:
    """Trova quale brand ufficiale compare nel titolo del prodotto.

    Prova prima i nomi più lunghi/specifici (es. "FRED PERRY x RAF SIMONS"
    prima di "FRED PERRY") per evitare match parziali sbagliati, e richiede
    un confine di parola per non scambiare parole generiche di senso
    compiuto per brand (es. alcuni brand nel catalogo si chiamano proprio
    "ICON"/"SUIT"/"ONLY" — il confine di parola limita, ma non azzera, il
    rischio di falsi positivi su questi nomi ambigui).
    """
    if not title or not BRAND_LIST:
        return None, None
    for display_name, slug in sorted(BRAND_LIST.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(display_name) + r"\b", title, re.IGNORECASE):
            return display_name, slug
    return None, None


# =====================================================================
# FASE 1: scoperta categorie (invariata rispetto alla v2)
# =====================================================================
 
def discover_category_seeds(session: requests.Session) -> list:
    print("  [*] scarico la homepage per leggere il menu...", flush=True)
    seeds = set()
    try:
        r = session.get(BASE_URL, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [!] impossibile leggere la homepage: {e}", file=sys.stderr, flush=True)
        return CATEGORY_SEEDS_FALLBACK
 
    soup = BeautifulSoup(r.text, "lxml")
    for nav in soup.find_all("nav"):
        for a in nav.find_all("a", href=True):
            href = a["href"]
            full = urljoin(BASE_URL, href).split("?")[0].split("#")[0]
            if not full.startswith(BASE_URL):
                continue
            path = urlparse(full).path
            if not path or path == "/":
                continue
            if any(x in full.lower() for x in EXCLUDED_NAV_HINTS):
                continue
            seeds.add(full)
 
    return sorted(seeds) if seeds else CATEGORY_SEEDS_FALLBACK
 
 
def crawl_categories(session: requests.Session, max_pages_per_category: int = 60) -> dict:
    seeds = discover_category_seeds(session)
    print(f"  [*] {len(seeds)} categorie/brand scoperti dal menu del sito", flush=True)
 
    product_to_relpath = {}
 
    for seed in seeds:
        seed_path = urlparse(seed).path
        seed_path = seed_path[:-5] if seed_path.endswith(".html") else seed_path
        seed_parts = [slugify(p) for p in seed_path.split("/") if p]
 
        page = 1
        empty_streak = 0
        while page <= max_pages_per_category and empty_streak < 2:
            sep = "&" if "?" in seed else "?"
            url = f"{seed}{sep}p={page}"
            try:
                r = session.get(url, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"  [!] errore crawling {url}: {e}", file=sys.stderr, flush=True)
                break
 
            soup = BeautifulSoup(r.text, "lxml")
            links = soup.select("a.product-item-link[href]") or soup.select(".product-item-info a[href]")
 
            found = 0
            for a in links:
                href = a.get("href")
                if not (href and href.startswith("http") and "/media/" not in href):
                    continue
                href = href.split("?")[0]
                product_slug = slugify(Path(urlparse(href).path).stem)
                candidate_relpath = Path(*seed_parts, product_slug) if seed_parts else Path(product_slug)
 
                existing = product_to_relpath.get(href)
                if existing is None:
                    found += 1
                    product_to_relpath[href] = candidate_relpath
                elif len(candidate_relpath.parts) > len(existing.parts):
                    product_to_relpath[href] = candidate_relpath
 
            label = seed_path or seed
            print(f"  [*] {label} pagina {page}: {found} nuovi prodotti (totale finora: {len(product_to_relpath)})", flush=True)
            empty_streak = 0 if found else empty_streak + 1
            page += 1
            time.sleep(0.4)
 
    return product_to_relpath
 
 
def save_cache(product_to_relpath: dict, cache_file: str) -> None:
    data = {url: str(relpath) for url, relpath in product_to_relpath.items()}
    Path(cache_file).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [i] cache salvata in {cache_file} ({len(data)} prodotti)", flush=True)
 
 
def load_cache(cache_file: str) -> dict:
    data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    return {url: Path(relpath) for url, relpath in data.items()}
 
 
def load_progress(progress_file: str) -> set:
    p = Path(progress_file)
    if not p.exists():
        return set()
    return set(line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
 
 
def append_progress(progress_file: str, product_url: str) -> None:
    with open(progress_file, "a", encoding="utf-8") as f:
        f.write(product_url + "\n")
        f.flush()
 
 
# =====================================================================
# FASE 2: apertura pagina prodotto UNA VOLTA -> foto + metadata grezzi
# =====================================================================
 
def fetch_product_page(session: requests.Session, product_url: str):
    """Scarica la pagina prodotto una sola volta. Ritorna (soup, None) o (None, errore)."""
    try:
        r = session.get(product_url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        return None, str(e)
    return BeautifulSoup(r.text, "lxml"), None
 
 
def extract_gallery_images_from_soup(soup: BeautifulSoup, product_url: str) -> list:
    image_urls = set()
 
    for script in soup.find_all("script", {"type": "text/x-magento-init"}):
        if not script.string or "mage/gallery/gallery" not in script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for _selector, config in data.items():
            gallery_cfg = config.get("mage/gallery/gallery") if isinstance(config, dict) else None
            if not gallery_cfg:
                continue
            for item in gallery_cfg.get("data", []):
                candidate = item.get("full") or item.get("img") or item.get("thumb")
                if candidate:
                    image_urls.add(urljoin(product_url, candidate))
 
    if not image_urls:
        gallery_div = soup.find(attrs={"data-gallery-role": "gallery-placeholder"})
        if gallery_div and gallery_div.get("data-gallery"):
            try:
                items = json.loads(gallery_div["data-gallery"])
                for item in items:
                    candidate = item.get("full") or item.get("img")
                    if candidate:
                        image_urls.add(urljoin(product_url, candidate))
            except json.JSONDecodeError:
                pass
 
    return [
        u for u in image_urls
        if not any(bad in urlparse(u).path.lower() for bad in EXCLUDED_PATH_HINTS)
    ]
 
 
def extract_metadata_from_soup(soup: BeautifulSoup, product_url: str) -> dict:
    def get_meta(prop):
        tag = soup.find("meta", {"property": prop})
        return tag["content"].strip() if tag and tag.get("content") else None
 
    meta = {"url": product_url}
    meta["title"] = get_meta("og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    meta["description_text"] = get_meta("og:description") or ""
 
    price_raw = get_meta("product:price:amount")
    try:
        meta["price"] = float(price_raw) if price_raw else None
    except ValueError:
        meta["price"] = None
    meta["currency"] = get_meta("product:price:currency")
 
    meta["brand"], meta["brand_slug"] = match_brand_from_title(meta["title"])
 
    full_text = soup.get_text("\n", strip=True)
 
    # "Vestibilità" compare spesso anche nel testo discorsivo della
    # descrizione (es. "vestibilità rilassata") PRIMA della vera scheda
    # tecnica più in basso in pagina (es. "Vestibilità regular"): prendiamo
    # l'ULTIMO match, non il primo, per centrare quello tecnico.
    fit_matches = list(re.finditer(r"Vestibilit[àa]\s*:?\s*(\S+)", full_text, re.IGNORECASE))
    meta["fit"] = fit_matches[-1].group(1).lower().strip(":.,") if fit_matches else None

    # stesso problema del fit: "composizione" compare a volte anche nel
    # testo di marketing (es. "la sua ricercata composizione materica...")
    # prima della vera scheda tecnica ("COMPOSIZIONE\n50% poliestere, ...",
    # senza ":") più in basso in pagina — prendiamo l'ultimo match.
    comp_matches = list(re.finditer(r"Composizione\s*:?\s*([^\n]+)", full_text, re.IGNORECASE))
    meta["composition"] = comp_matches[-1].group(1).strip() if comp_matches else None
 
    m = re.search(r"Codice Prodotto:\s*([^\n]+)", full_text, re.IGNORECASE)
    meta["sku"] = m.group(1).strip() if m else None
 
    # colore euristico dallo slug URL
    slug = Path(urlparse(product_url).path).stem
    tokens = re.split(r"[-_]", slug.lower())
    meta["color_hints"] = [t for t in tokens if t in COLOR_WORDS] or None
 
    return meta
 
 
# =====================================================================
# FASE 3: derive_attributes — applica le regole di tagging
# =====================================================================
 
def compute_style_scores(metadata: dict) -> dict:
    """Punteggio grezzo (non sogliato) per ognuno dei 10 style_tags.

    Usato sia da derive_attributes (che applica poi la soglia 0.5 per il
    badge style_tags/needs_vision_review) sia dalla Fase 3, che ha bisogno
    del segnale continuo per costruire un vettore stile senza perdere
    l'informazione sotto soglia (vedi feature_engineering.py).
    """
    desc = (metadata.get("description_text") or "").lower()
    title = (metadata.get("title") or "").lower()
    composition = (metadata.get("composition") or "").lower()
    fit = metadata.get("fit")
    brand_slug = metadata.get("brand_slug")
    combined_text = f"{desc} {title}"

    scores = {tag: 0.0 for tag in STYLE_KEYWORDS}
    text_matched_tags = set()  # tag con supporto esplicito da description_text/title

    for tag, keywords in STYLE_KEYWORDS.items():
        if any(kw in combined_text for kw in keywords):
            scores[tag] += 0.8
            text_matched_tags.add(tag)

    for material, info in MATERIAL_SIGNALS.items():
        if material in composition:
            for tag in info["style"]:
                scores[tag] += 0.5

    if fit and fit in FIT_SIGNALS:
        for tag in FIT_SIGNALS[fit]["style"]:
            scores[tag] += 0.3

    if brand_slug and brand_slug in BRAND_AFFINITY:
        for tag in BRAND_AFFINITY[brand_slug]:
            scores[tag] += 0.4

    return scores, text_matched_tags


def derive_attributes(metadata: dict, category_path: Path) -> dict:
    fit = metadata.get("fit")
    composition = (metadata.get("composition") or "").lower()
    combined_text = f"{(metadata.get('description_text') or '').lower()} {(metadata.get('title') or '').lower()}"
    scores, text_matched_tags = compute_style_scores(metadata)

    THRESHOLD = 0.5
    style_tags = {tag: round(s, 2) for tag, s in scores.items() if s >= THRESHOLD}
    # non basta che UN tag superi la soglia: se nessuno dei tag "vincenti" ha
    # supporto esplicito nel testo (description/title) — cioè il punteggio
    # viene solo da materiale/fit/brand, segnali generici che da soli non
    # bastano a caratterizzare lo stile del capo specifico — il prodotto
    # resta comunque da rivedere. Altrimenti capi con description vuota ma
    # materiale comune (es. "98% cotone") e brand generico (es. la linea
    # propria Nuvolari, molto trasversale) risultano "taggati con sicurezza"
    # senza che il testo dica davvero nulla di distintivo su quel capo.
    needs_vision_review = len(style_tags) == 0 or not (set(style_tags) & text_matched_tags)

    # --- formalità ---
    category_parts = [p.lower() for p in category_path.parts]
    category_prior = 3  # default neutro
    for part in category_parts:
        if part in CATEGORY_FORMALITY_PRIOR:
            category_prior = CATEGORY_FORMALITY_PRIOR[part]
            break
 
    if style_tags:
        weighted_sum = sum(STYLE_TAG_FORMALITY.get(t, 3) * w for t, w in style_tags.items())
        style_formality_avg = weighted_sum / sum(style_tags.values())
    else:
        style_formality_avg = category_prior
 
    fit_delta = FIT_SIGNALS.get(fit, {}).get("formality_delta", 0) if fit else 0
    fit_term = max(1, min(5, category_prior + fit_delta))
 
    formality_raw = 0.5 * category_prior + 0.35 * style_formality_avg + 0.15 * fit_term
    formality_score = max(1, min(5, round(formality_raw)))
 
    # --- stagione ---
    season = None
    if any(w in combined_text for w in ["estate", "estivo", "estiva"]):
        season = "estate"
    elif any(w in combined_text for w in ["inverno", "invernale"]):
        season = "inverno"
    elif "mezza stagione" in combined_text:
        season = "mezza_stagione"
    else:
        for material, info in MATERIAL_SIGNALS.items():
            if material in composition:
                season = info["season"]
                break
    if season is None:
        season = "tutte"
 
    # --- pattern ---
    pattern = "tinta_unita"
    for p, keywords in PATTERN_KEYWORDS.items():
        if any(kw in combined_text for kw in keywords):
            pattern = p
            break
 
    return {
        "style_tags": style_tags,
        "formality_score": formality_score,
        "season": season,
        "pattern": pattern,
        "needs_vision_review": needs_vision_review,
    }
 
 
def download_image(session: requests.Session, img_url: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(img_url).path).name or "image.jpg"
    dest_path = dest_dir / filename
    if dest_path.exists():
        return
    try:
        r = session.get(img_url, timeout=30, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
    except requests.RequestException as e:
        print(f"    [!] errore scaricando {img_url}: {e}", file=sys.stderr, flush=True)
 
 
# =====================================================================
# FUNZIONE PRINCIPALE
# =====================================================================
 
def run_scrape_with_attributes(
    output: str = "output",
    delay: float = 1.0,
    max_products: int = None,
    max_pages_per_category: int = 60,
    cache_file: str = None,
    force_recrawl: bool = False,
    progress_file: str = None,
):
    """
    Scarica foto + metadata + attributi stilistici per ogni prodotto,
    salvando nella struttura organizzata per categoria.
 
    Genera, dentro `output`:
      <categoria>/<sottocategoria>/<slug>/foto*.jpg
      <categoria>/<sottocategoria>/<slug>/metadata.json
      catalogo.jsonl   <- un JSON per riga, un prodotto per riga, per tutto il catalogo
 
    Parametri: vedi versioni precedenti (output, delay, max_products,
    max_pages_per_category, cache_file, force_recrawl, progress_file).
    """
    print(f"[*] Avvio scraping con attributi in {output} ...", flush=True)
    session = get_session()
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "catalogo.jsonl"
 
    if cache_file and Path(cache_file).exists() and not force_recrawl:
        print(f"[*] Trovata cache in {cache_file}, la uso invece di riscansionare...", flush=True)
        product_to_relpath = load_cache(cache_file)
        print(f"    {len(product_to_relpath)} prodotti caricati dalla cache", flush=True)
    else:
        print("[*] Scopro le categorie dal menu del sito e le scansiono...", flush=True)
        product_to_relpath = crawl_categories(session, max_pages_per_category=max_pages_per_category)
        print(f"\n[*] {len(product_to_relpath)} prodotti totali trovati nel sito", flush=True)
        if cache_file:
            save_cache(product_to_relpath, cache_file)
 
    items = list(product_to_relpath.items())
 
    already_done = set()
    if progress_file:
        already_done = load_progress(progress_file)
        if already_done:
            print(f"[*] Trovato progress_file: {len(already_done)} prodotti gia' completati, li salto.", flush=True)
        Path(progress_file).touch(exist_ok=True)
        items = [(url, relpath) for url, relpath in items if url not in already_done]
 
    if max_products:
        items = items[:max_products]
 
    print(f"\n[*] Elaboro {len(items)} prodotti (restanti da fare)...\n", flush=True)
 
    total_images = 0
    total_products_ok = 0
    total_needs_review = 0
 
    with open(catalog_path, "a", encoding="utf-8") as catalog_f:
        for i, (url, relpath) in enumerate(items, 1):
            print(f"[{i}/{len(items)}] {relpath}", flush=True)
 
            soup, err = fetch_product_page(session, url)
            if soup is None:
                print(f"    [!] errore pagina: {err}", flush=True)
                if progress_file:
                    append_progress(progress_file, url)
                time.sleep(delay)
                continue
 
            images = extract_gallery_images_from_soup(soup, url)
            raw_metadata = extract_metadata_from_soup(soup, url)
            attributes = derive_attributes(raw_metadata, relpath)
 
            if not images:
                print("    (nessuna galleria trovata, salto le foto ma salvo comunque i metadata)", flush=True)
            else:
                dest_dir = output_dir / relpath
                for img_url in images:
                    download_image(session, img_url, dest_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / "metadata.json").write_text(
                    json.dumps({**raw_metadata, **attributes, "relpath": str(relpath), "image_count": len(images)},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                total_images += len(images)
 
            record = {**raw_metadata, **attributes, "relpath": str(relpath)}
            catalog_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            catalog_f.flush()
 
            total_products_ok += 1
            if attributes["needs_vision_review"]:
                total_needs_review += 1
 
            tags_str = ", ".join(attributes["style_tags"].keys()) or "(nessuno — needs_vision_review)"
            print(f"    -> {len(images)} foto | stile: {tags_str} | formalita': {attributes['formality_score']}", flush=True)
 
            if progress_file:
                append_progress(progress_file, url)
 
            time.sleep(delay)
 
    stats = {
        "prodotti_processati": total_products_ok,
        "foto_totali": total_images,
        "prodotti_da_rivedere_vision_api": total_needs_review,
        "catalogo": str(catalog_path.resolve()),
        "output_dir": str(output_dir.resolve()),
    }
 
    print("\n[OK] Fatto (per questa sessione).", flush=True)
    print(f"     Prodotti processati:              {stats['prodotti_processati']}", flush=True)
    print(f"     Foto totali scaricate:             {stats['foto_totali']}", flush=True)
    print(f"     Da rivedere con vision API:         {stats['prodotti_da_rivedere_vision_api']}", flush=True)
    print(f"     Catalogo consolidato:               {stats['catalogo']}", flush=True)
    if progress_file:
        print(f"     Per riprendere in futuro: stesso comando, stesso progress_file='{progress_file}'", flush=True)

    return stats


# =====================================================================
# REFRESH: ri-estrae solo i metadata testuali (no foto) per correggere
# i regex di composizione/vestibilità senza rifare tutto lo scraping
# =====================================================================

def refresh_metadata(output: str, delay: float = 1.0, progress_file: str = None, max_products: int = None) -> dict:
    """Rivisita ogni pagina prodotto già scaricata (stesso URL salvato nel
    metadata.json esistente), ri-estrae SOLO i metadata testuali (niente
    foto, già presenti su disco) e riscrive metadata.json con i valori
    corretti di composition/fit/style_tags/formality_score/season/pattern.

    Usare dopo una correzione ai regex/alla logica di extract_metadata_from_soup
    o derive_attributes, per propagare la correzione senza dover riscaricare
    le foto (già buone).
    """
    print(f"[*] Avvio refresh metadata (solo pagina, no foto) in {output} ...", flush=True)
    session = get_session()
    output_dir = Path(output)

    meta_paths = sorted(output_dir.rglob("metadata.json"))
    print(f"[*] {len(meta_paths)} metadata.json trovati", flush=True)

    already_done = set()
    if progress_file:
        already_done = load_progress(progress_file)
        if already_done:
            print(f"[*] Trovato progress_file: {len(already_done)} gia' aggiornati, li salto.", flush=True)
        Path(progress_file).touch(exist_ok=True)

    if max_products:
        meta_paths = meta_paths[:max_products]

    total_ok = 0
    total_errors = 0
    total_needs_review = 0

    for i, meta_path in enumerate(meta_paths, 1):
        old_metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        url = old_metadata.get("url")
        relpath = Path(old_metadata.get("relpath", str(meta_path.parent.relative_to(output_dir))))

        if not url or url in already_done:
            continue

        soup, err = fetch_product_page(session, url)
        if soup is None:
            print(f"[{i}/{len(meta_paths)}] {relpath} -> errore pagina: {err}", flush=True)
            total_errors += 1
            if progress_file:
                append_progress(progress_file, url)
            time.sleep(delay)
            continue

        raw_metadata = extract_metadata_from_soup(soup, url)
        attributes = derive_attributes(raw_metadata, relpath)

        new_metadata = {
            **raw_metadata,
            **attributes,
            "relpath": str(relpath),
            "image_count": old_metadata.get("image_count"),
        }
        meta_path.write_text(json.dumps(new_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        total_ok += 1
        if attributes["needs_vision_review"]:
            total_needs_review += 1

        tags_str = ", ".join(attributes["style_tags"].keys()) or "(nessuno)"
        comp = raw_metadata.get("composition") or "-"
        fit = raw_metadata.get("fit") or "-"
        print(f"[{i}/{len(meta_paths)}] {relpath} -> fit={fit} | comp={comp} | stile: {tags_str}", flush=True)

        if progress_file:
            append_progress(progress_file, url)

        time.sleep(delay)

    print("\n[OK] Refresh completato.", flush=True)
    print(f"     Aggiornati:          {total_ok}", flush=True)
    print(f"     Errori pagina:       {total_errors}", flush=True)
    print(f"     Needs vision review: {total_needs_review}", flush=True)

    return {"aggiornati": total_ok, "errori": total_errors, "needs_vision_review": total_needs_review}


def refix_brand_and_attributes(output: str) -> dict:
    """Ricorregge SOLO brand/brand_slug (dal titolo già salvato, tramite
    match_brand_from_title) e ricalcola di conseguenza style_tags/
    formality_score/needs_vision_review — tutto in locale, senza rete,
    perché title/description/composition/fit sono già in metadata.json.

    Serve dopo aver corretto match_brand_from_title (in precedenza il link
    brand estratto era quasi sempre sbagliato — vedi extract_metadata_from_soup).
    """
    output_dir = Path(output)
    meta_paths = sorted(output_dir.rglob("metadata.json"))
    print(f"[*] Ricorreggo brand + attributi per {len(meta_paths)} prodotti (nessuna rete)...", flush=True)

    changed_brand = 0
    for i, meta_path in enumerate(meta_paths, 1):
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        relpath = Path(metadata.get("relpath", str(meta_path.parent.relative_to(output_dir))))

        old_brand_slug = metadata.get("brand_slug")
        brand, brand_slug = match_brand_from_title(metadata.get("title"))
        metadata["brand"] = brand
        metadata["brand_slug"] = brand_slug
        if brand_slug != old_brand_slug:
            changed_brand += 1

        attributes = derive_attributes(metadata, relpath)
        metadata.update(attributes)

        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        if i % 200 == 0 or i == len(meta_paths):
            print(f"    [{i}/{len(meta_paths)}] elaborati...", flush=True)

    print(f"\n[OK] Brand corretto per {changed_brand}/{len(meta_paths)} prodotti.", flush=True)
    return {"totale": len(meta_paths), "brand_corretti": changed_brand}


def rebuild_catalog(output: str) -> str:
    """Ricostruisce catalogo.jsonl da tutti i metadata.json presenti su disco
    (dopo un refresh_metadata, per tenerlo allineato)."""
    output_dir = Path(output)
    catalog_path = output_dir / "catalogo.jsonl"
    meta_paths = sorted(output_dir.rglob("metadata.json"))
    with open(catalog_path, "w", encoding="utf-8") as f:
        for meta_path in meta_paths:
            record = json.loads(meta_path.read_text(encoding="utf-8"))
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[OK] catalogo.jsonl ricostruito con {len(meta_paths)} prodotti in {catalog_path}", flush=True)
    return str(catalog_path)


print("Modulo caricato (v3 — foto + metadata + tagging stilistico). Esempio:\n"
      "run_scrape_with_attributes(\n"
      "    output='/Users/enricociaralli/Desktop/nuvolari/nuvolari_full_organizzato',\n"
      "    cache_file='/Users/enricociaralli/Desktop/nuvolari/category_cache.json',\n"
      "    progress_file='/Users/enricociaralli/Desktop/nuvolari/progress.txt',\n"
      "    max_products=20  # test veloce, togli per lo scraping completo\n"
      ")")
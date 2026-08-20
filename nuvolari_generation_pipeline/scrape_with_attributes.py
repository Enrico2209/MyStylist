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
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse
 
import requests
from bs4 import BeautifulSoup
from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
 
BASE_URL = "https://www.nuvolari.biz"
USER_AGENT = "NuvolariPartnerBot/1.0 (+contatto: enricociaralli@gmail.com)"

BRAND_LIST_FILE = BASE / "brand_list.json"
 
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
    "sportivo": [
        "sportivo", "sport", "running", "training", "palestra", "fitness",
        "tuta", "jogging", "tecnico da gara",
    ],
    "elegante": [
        "elegante", "formale", "cerimonia", "sartoriale", "raffinato",
        "classico", "business",
    ],
    "streetwear": [
        "streetwear", "street", "urban", "skate", "hip hop", "oversize",
        "graphic", "logo all over",
    ],
    "da_mare": [
        "mare", "spiaggia", "beach", "costume", "boxer da bagno", "pareo",
        "copricostume", "telo", "infradito", "ciabatte",
    ],
    "casual": [
        "casual", "quotidiano", "informale", "everyday", "relax",
    ],
}

MATERIAL_SIGNALS = {
    "lana":          {"style": ["elegante"], "season": "inverno"},
    "cashmere":      {"style": ["elegante"], "season": "inverno"},
    "piumino":       {"style": ["casual"], "season": "inverno"},
    "nylon":         {"style": ["streetwear"], "season": "tutte"},
    "pelle":         {"style": ["streetwear", "elegante"], "season": "mezza_stagione"},
    "denim":         {"style": ["casual"], "season": "tutte"},
    "lino":          {"style": ["elegante", "casual"], "season": "estate"},
    "cotone":        {"style": ["casual"], "season": "tutte"},
    "poliestere":    {"style": ["sportivo"], "season": "tutte"},
    "viscosa":       {"style": ["elegante", "casual"], "season": "mezza_stagione"},
    "elastan":       {"style": ["sportivo"], "season": "tutte"},
}
 
# Stagionalità intrinseca del TIPO di capo, indipendente da materiale e testo.
#
# È il segnale che mancava. La stagione veniva dedotta solo dalle parole della
# scheda ("estivo", "invernale") e dalla composizione, ma alcuni capi sono
# stagionali per natura: una sciarpa di cotone resta un capo invernale, un
# costume da bagno resta estivo. Senza questa tabella finivano in "tutte", che
# nell'abbinamento è un jolly compatibile con qualsiasi stagione — ed è così che
# una sciarpa si ritrovava sopra una t-shirt estiva.
#
# Ha la precedenza su materiale e testo proprio perché è il segnale più
# affidabile: il tipo di capo non è ambiguo, la composizione e la descrizione sì.
# Le chiavi si cercano nel titolo con confine di parola.
GARMENT_SEASON = {
    "inverno": [
        "sciarpa", "sciarpe", "scaldacollo", "guanti", "muffole",
        "cappotto", "piumino", "montone", "parka", "pile",
        "maglione", "dolcevita", "lupetto",
        "colbacco", "passamontagna", "berretto di lana",
        # volutamente fuori: cardigan, felpa, giacca. Sono capi trasversali
        # (un cardigan leggero sta su una t-shirt in primavera) e marcarli
        # "inverno" escluderebbe abbinamenti corretti invece di correggerne.
    ],
    "estate": [
        "costume", "bikini", "boxer mare", "pareo",
        "canotta", "canottiera", "top a fascia",
        "infradito", "ciabatte", "sandalo", "sandali",
        "bermuda", "pantaloncino", "pantaloncini", "shorts",
        "cappello di paglia",
    ],
}


# La stagione dal TESTO vale solo se l'aggettivo stagionale descrive il capo
# o il suo tessuto, non l'occasione d'uso. Misurato sulle schede: 405 capi su
# 671 diventavano "estate" per frasi come "un aperitivo estivo", "la tua
# estate", "un look estivo" — marketing sull'occasione, non sul capo — e
# camicie trasversali venivano cosi' escluse da ogni outfit invernale. I
# segnali sostanziali osservati ("fibra estiva", "filato estivo", "classica
# maglietta estiva") hanno tutti la stessa forma: sostantivo di capo/tessuto
# seguito dall'aggettivo.
_NOME_DI_CAPO_O_TESSUTO = (
    r"(?:capo|capi|tessut\w+|fibr\w+|filat\w+|magli\w+|camici\w+|"
    r"pantalon\w+|giacc\w+|giubbott\w+|felp\w+|modell\w+|version\w+|variant\w+)"
)
STAGIONE_ESTIVA_DEL_CAPO = re.compile(_NOME_DI_CAPO_O_TESSUTO + r"\s+estiv")
STAGIONE_INVERNALE_DEL_CAPO = re.compile(_NOME_DI_CAPO_O_TESSUTO + r"\s+invernal")


def season_da_tipo(title: str):
    """Stagione dedotta dal tipo di capo, o None se il capo non è stagionale.

    Cerca con confine di parola per non far scattare "sandalo" dentro altre
    parole. Il primo riscontro vince: le liste non si sovrappongono.
    """
    t = senza_accenti(title or "").lower()
    for stagione, parole in GARMENT_SEASON.items():
        for parola in parole:
            if re.search(r"\b" + re.escape(parola) + r"\b", t):
                return stagione
    return None


FIT_SIGNALS = {
    "slim":     {"style": ["elegante"], "formality_delta": 1},
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
 
# Formalità intrinseca del TIPO di capo, riconosciuta dal TITOLO.
#
# CATEGORY_FORMALITY_PRIOR sopra guarda solo il percorso di categoria, che nel
# catalogo è spesso generico ("abbigliamento", "accessori"): 1018 prodotti su
# 2901 non trovavano nessuna corrispondenza e ricadevano sul neutro 3, e con
# essi TUTTI gli accessori, perché "accessori" non è una chiave di quella
# tabella. Risultato: il 67% del catalogo finiva a formalità 0,50 normalizzata,
# cioè la formalità non distingueva più una camicia da uno zaino — ed è così
# che una borsa street si ritrovava abbinata a camicia e mocassini.
#
# Il titolo è il segnale più ricco: dice sempre di che capo si tratta.
GARMENT_FORMALITY = {
    1: ["zaino", "zainetto", "marsupio", "tuta", "costume", "bikini",
        "infradito", "ciabatte", "canotta", "canottiera", "pareo",
        # zaini, marsupi e pochette a tracolla sono capi street: portati sopra
        # un maglione e un chino stonano, e il vincolo di dispersione li tiene
        # fuori dagli outfit smart solo se partono davvero dal minimo
        "pochette"],
    2: ["t-shirt", "tshirt", "felpa", "jeans", "sneakers", "bermuda",
        "pantaloncino", "pantaloncini", "shorts", "bomber", "giubbotto",
        "cappellino", "cappello", "tracolla", "sandalo", "sandali"],
    3: ["camicia", "polo", "maglia", "maglione", "cardigan", "pantalone",
        "chino", "gilet", "borsa", "sciarpa", "dolcevita", "gonna", "vestito"],
    4: ["giacca", "blazer", "cappotto", "mocassini", "mocassino", "parka",
        "trench", "stringate", "derby", "oxford"],
    5: ["completo", "smoking", "frac", "cravatta", "papillon"],
}


# Tetto di formalità per brand.
#
# Le parole di GARMENT_FORMALITY descrivono la FORMA del capo, non il registro:
# "borsa" vale sia per una borsa in pelle da donna sia per la REDBOX CARRY BAG
# di The North Face, che è una sacca da montagna verde lime; "giacca" vale sia
# per un blazer sia per il K-Way LE VRAI CLAUDE, che è un antivento. La forma
# non basta a distinguerli, il marchio sì: ci sono brand la cui produzione sta
# tutta in un registro, e per loro il tipo di capo non deve poter alzare il
# punteggio oltre un certo livello.
#
# Il tetto conta perché la formalità entra nel vincolo di dispersione (0,3):
# tetto 1 (= 0,00 normalizzato) ammette accanto solo capi di livello 1-2 —
# t-shirt, felpe, tute, sneakers, mai camicie o mocassini. Tetto 2 (= 0,25)
# arriva fino al livello 3, quindi un piumino The North Face può ancora stare
# con un maglione, ma non con un completo.
#
# È un tetto e non un valore fisso: un costume K-Way è già 1 e ci resta.
BRAND_FORMALITY_TETTO = {
    # street puro: qualunque capo è un capo trap
    "sprayground": 1, "bape": 1, "a-bathing-ape": 1, "off-white": 1,
    "stussy": 1, "supreme": 1,
    # K-Way fa antivento: il titolo li chiama "giacca" e 28 capi su 51
    # finivano a formalità 3, cioè alla pari di una camicia. Tetto 2 e non 1:
    # l'antivento è casual sportivo, non street. A 1 (= 0,00) la dispersione
    # massima di 0,3 lo avrebbe ammesso solo accanto a livelli 1-2, cioè
    # tute, costumi e marsupi — e avrebbe escluso l'abbinamento con un
    # pantalone cargo, che è livello 3 ed è invece un outfit giusto.
    "k-way": 2,
    # sportivi e outdoor: il capospalla tecnico e la sacca da montagna non
    # sono formali, ma restano abbinabili a un maglione — da qui il tetto 2
    "the-north-face": 2, "napapijri": 2, "nike": 2, "adidas": 2,
    "adidas-originals": 2, "new-balance": 2, "asics": 2, "saucony": 2,
    "hoka": 2, "puma": 2, "under-armour": 2,
}


# Termini che nominano un modello preciso e battono la parola generica che li
# accompagna. Servono perché la scansione sotto va dal più formale al meno
# formale, e quindi la parola generica — che è quasi sempre la più formale —
# vince sempre: in "PANTALONE CARGO" si fermava su "pantalone" (3, come una
# camicia) senza mai guardare "cargo", che è workwear; in "GIACCA K-WAY LE
# VRAI" si fermava su "giacca" (4, come un blazer) invece di riconoscere un
# antivento. La regola "vince il più formale" nasceva per un caso solo —
# "GIACCA CAMICIA" è un capospalla — e lì continua a valere, perché queste
# eccezioni si controllano prima e riguardano altri termini.
GARMENT_FORMALITY_SPECIFICO = {
    "cargo": 2,       # 68 capi su 73 stavano a 3 dietro la parola "pantalone"
    "kway": 2,
    "k-way": 2,
    "antivento": 2,
    "trackpant": 2,
    "tracktop": 2,
}


# Accessori-contenitore. Di un brand sportivo o street sono capi street a
# tutti gli effetti, qualunque parola usi il titolo: la tabella mette già
# zaino e marsupio a 1, ma "borsa" sta a 3 perché la stessa parola vale anche
# per una borsa in pelle da donna. Da lì l'incoerenza misurata nel catalogo —
# ZAINO THE NORTH FACE BASE CAMP a livello 1 e BORSA THE NORTH FACE REDBOX
# CARRY BAG a livello 2, stessa marca e stessa funzione.
ACCESSORI_CONTENITORE = re.compile(
    r"\b(borsa|borse|sacca|sacche|zaino|zainetto|marsupio|marsupi|tracolla|"
    r"tracolle|pochette|backpack|bkpack|daypack|duffel|carry bag)\b")


# Cappotti travestiti da giubbotto. Stessa famiglia di difetti del cargo e del
# K-Way — la parola generica del titolo nasconde il capo — ma qui il rimedio non
# può stare in GARMENT_FORMALITY_SPECIFICO, perché nel titolo la forma non
# compare affatto: "GIUBBOTTO UOMO NUVOLARI OTIS NAVY" è un caban di lana
# doppiopetto, e lo si scopre solo leggendo la scheda.
CAPPOTTO_FORMA = re.compile(
    r"(caban|montgomery|doppiopetto|sei bottoni|peacoat|cappotto|trench)")
# Il titolo ha l'ultima parola: un gilet imbottito e una giacca a vento citano
# il cappotto nella scheda per confronto, ma cappotti non sono.
CAPPOTTO_ESCLUSI = re.compile(r"\b(gilet|giacca a vento|piumino|smanicato)\b")
# Frase di repertorio che elenca i reparti del negozio invece di descrivere il
# prodotto ("disponibili in vari stili come sneakers, stivali o mocassini").
DESCRIZIONE_DI_REPERTORIO = re.compile(r"variet(?:a|à) di stili come")
CAPPOTTO_FORMALITA_MINIMA = 3


def e_un_cappotto(metadata: dict) -> bool:
    titolo = senza_accenti(metadata.get("title") or "").lower()
    if CAPPOTTO_ESCLUSI.search(titolo):
        return False
    if CAPPOTTO_FORMA.search(titolo):
        return True
    descrizione = senza_accenti(metadata.get("description_text") or "").lower()
    if DESCRIZIONE_DI_REPERTORIO.search(descrizione):
        return False
    return bool(CAPPOTTO_FORMA.search(descrizione))


def formalita_da_tipo(title: str):
    """Formalità 1–5 dedotta dal tipo di capo nel titolo, o None.

    Prima i termini specifici (vedi GARMENT_FORMALITY_SPECIFICO), che battono
    la parola generica che li accompagna. Poi la scansione dal più formale al
    più casual, dove "giacca" batte "camicia" in "GIACCA CAMICIA", che è un
    capospalla. Confine di parola per evitare riscontri dentro altre parole.
    """
    t = senza_accenti(title or "").lower()
    for parola, livello in GARMENT_FORMALITY_SPECIFICO.items():
        if re.search(r"\b" + re.escape(parola) + r"\b", t):
            return livello
    for livello in sorted(GARMENT_FORMALITY, reverse=True):
        for parola in GARMENT_FORMALITY[livello]:
            if re.search(r"\b" + re.escape(parola) + r"\b", t):
                return livello
    return None


STYLE_TAG_FORMALITY = {
    "elegante": 4, "casual": 2, "streetwear": 1, "sportivo": 1, "da_mare": 1,
}
 
BRAND_AFFINITY = {
    "nuvolari": ["casual", "elegante"],
    "k-way": ["casual", "sportivo"],
    "the-north-face": ["sportivo", "casual"],
    "napapijri": ["casual"],
    "carhartt-wip": ["casual", "streetwear"],
    "dickies": ["casual", "streetwear"],
    "deus-ex-machina": ["streetwear", "casual"],
    "vans": ["streetwear"],
    "obey": ["streetwear"],
    "stussy": ["streetwear"],
    "stone-island": ["streetwear"],
    "fred-perry": ["casual"],
    "lyle-scott": ["casual"],
    "lee": ["casual"],
    "levi-s": ["casual"],
    "calvin-klein-jeans": ["casual"],
    "tommy-hilfiger": ["casual"],
    "tommy-jeans": ["streetwear", "casual"],
    "guess-jeans": ["casual"],
    "nike": ["sportivo", "streetwear"],
    "adidas": ["sportivo", "streetwear"],
    "adidas-originals": ["streetwear", "casual"],
    "new-balance": ["sportivo", "streetwear"],
    "asics": ["sportivo"],
    "saucony": ["sportivo", "casual"],
    "hoka": ["sportivo"],
    "dr-martens": ["streetwear", "casual"],
    "colmar-originals": ["sportivo", "elegante"],
    "blauer": ["casual"],
    "woolrich": ["casual", "elegante"],
    "c-p-company": ["streetwear"],
    "ralph-lauren": ["elegante", "casual"],
    "goorin-bros": ["casual", "streetwear"],
    "gas": ["casual"],
    "diesel": ["streetwear", "casual"],
    "psycho-bunny": ["casual", "elegante"],
    "weekend-offender": ["streetwear", "casual"],
    "edwin": ["casual"],
    "lacoste": ["casual", "elegante"],
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


def senza_accenti(s: str) -> str:
    """Toglie i segni diacritici, lasciando la lettera di base.

    Serve perché la lista ufficiale e i titoli dei prodotti non concordano
    sugli accenti: la lista dice "GAËLLE PARIS", i titoli scrivono "GAELLE
    PARIS". Confrontandoli così com'erano si perdevano 33 prodotti.
    """
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn")


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
    title_piatto = senza_accenti(title)
    for display_name, slug in sorted(BRAND_LIST.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(senza_accenti(display_name)) + r"\b",
                     title_piatto, re.IGNORECASE):
            return display_name, slug
    return None, None


def match_brand_from_url(url: str) -> tuple:
    """Ripiego sull'URL quando il titolo non nomina il brand.

    Il titolo del catalogo spesso salta il brand e tiene solo modello e colore
    ("PANTALONE TUTA 38233 NAVY/BLACK"), mentre lo slug resta nel path
    (".../pj-tuta-sergio-tacchini-sc-38233-navy-black-231.html"). Cercando lì
    si recuperano ~180 prodotti che altrimenti restano senza brand.

    Il confine richiesto è `[/-]slug[-/.]`: il dominio nuvolari.biz è preceduto
    da un punto e quindi non produce falsi positivi sul brand proprio del
    negozio, che invece viene riconosciuto quando compare davvero nel path
    (".../pantalone-nuvolari-sc-fury-tortora.html").
    """
    if not url or not BRAND_LIST:
        return None, None
    url = url.lower()
    for display_name, slug in sorted(BRAND_LIST.items(), key=lambda kv: -len(kv[1])):
        if re.search(r"[/-]" + re.escape(slug) + r"[-/.]", url):
            return display_name, slug
    return None, None


def match_brand(title: str, url: str) -> tuple:
    """Brand dal titolo se c'è, altrimenti dallo slug nell'URL."""
    nome, slug = match_brand_from_title(title)
    if slug:
        return nome, slug
    return match_brand_from_url(url)


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
 
    meta["brand"], meta["brand_slug"] = match_brand(meta["title"], meta.get("url"))
 
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
 
# Parole di stile che restano affidabili anche dentro la prosa della scheda.
#
# Tutte le altre non lo sono, ed è misurato: la descrizione attiva in media 2,9
# tag su 10 per capo, e 308 capi ne attivano 6 o più — nessun capo è insieme
# elegante, streetwear, militare, minimal e workwear. Il motivo è che il copy
# non descrive il capo ma l'occasione e gli abbinamenti: "abbinalo a un blazer
# per un look impeccabile" faceva risultare eleganti 1382 capi (48% del
# catalogo), fra cui tre pantaloni della tuta su sei esempi. Lo stesso bermuda
# G-STAR compariva come esempio per sei tag diversi.
#
# Queste invece nominano una tecnica costruttiva o un trattamento del tessuto:
# sono fatti verificabili, non aggettivi, e per giunta dicono cose che una foto
# NON può vedere — che è esattamente ciò che al testo resta da fare ora che la
# revisione visiva copre il registro stilistico di tutto il catalogo.
# Ora vuoto, e il meccanismo resta: era la deroga che lasciava passare dalla
# DESCRIZIONE (non solo dal titolo) le parole tecniche inequivocabili. Tutte
# quelle che c'erano alimentavano tag che non esistono piu': impermeabile,
# membrana, antivento, trekking -> outdoor_tecnico; camouflage, mimetico ->
# military. Nessuno degli otto registri rimasti ha parole altrettanto sicure
# nella prosa, quindi la deroga non ha piu' clienti (vedi
# guida_classificazione.md).
PAROLE_TECNICHE_AFFIDABILI = set()


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
        # dal titolo qualunque parola vale: lì la parola NOMINA il capo.
        # dalla prosa solo i fatti tecnici (vedi PAROLE_TECNICHE_AFFIDABILI).
        if any(kw in title for kw in keywords) or any(
                kw in desc for kw in keywords if kw in PAROLE_TECNICHE_AFFIDABILI):
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
    # Il tipo di capo letto dal TITOLO viene prima del percorso di categoria:
    # il percorso è generico per un terzo del catalogo e per tutti gli
    # accessori, e li lasciava tutti sul neutro 3.
    category_prior = formalita_da_tipo(metadata.get("title"))
    if category_prior is None:
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

    # Il tetto per brand si applica per ultimo: è una proprietà del marchio,
    # non del singolo capo, e vince su quello che dicono tipo, stile e fit.
    tetto = BRAND_FORMALITY_TETTO.get(metadata.get("brand_slug"))
    if tetto is not None:
        # Borse e zaini di questi marchi scendono a 1 anche quando il brand ha
        # tetto 2: un piumino tecnico sta ancora con un maglione, una sacca da
        # montagna no — è della stessa famiglia del marsupio, che è già 1.
        if ACCESSORI_CONTENITORE.search(senza_accenti(metadata.get("title") or "").lower()):
            tetto = 1
        formality_score = min(formality_score, tetto)

    # Pavimento per i cappotti. "Giubbotto" non dice niente sul registro, e un
    # caban di lana doppiopetto a sei bottoni finiva a 2 come una windbreaker —
    # da lì il caban blu sopra la tuta. La forma la nomina la descrizione, non
    # il titolo, quindi si guardano tutt'e due (vedi CAPPOTTO_FORMA).
    if e_un_cappotto(metadata):
        formality_score = max(formality_score, CAPPOTTO_FORMALITA_MINIMA)
 
    # --- stagione ---
    # Il tipo di capo viene per primo: è l'unico segnale non ambiguo. Una
    # sciarpa è invernale anche se è di cotone e la scheda non lo dice, e
    # lasciarla in "tutte" la rendeva abbinabile a una t-shirt estiva.
    season = season_da_tipo(metadata.get("title"))
    if season is None:
        if STAGIONE_ESTIVA_DEL_CAPO.search(combined_text):
            season = "estate"
        elif STAGIONE_INVERNALE_DEL_CAPO.search(combined_text):
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
    """Ricorregge SOLO brand/brand_slug (da titolo e URL già salvati, tramite
    match_brand) e ricalcola di conseguenza style_tags/formality_score/
    needs_vision_review — tutto in locale, senza rete, perché
    title/url/description/composition/fit sono già in metadata.json.

    Serve dopo aver corretto match_brand (in precedenza il link brand estratto
    era quasi sempre sbagliato — vedi extract_metadata_from_soup — e poi il
    match guardava solo il titolo, che spesso non nomina il brand).

    Il ricalcolo degli attributi non è un extra: brand_slug entra nei punteggi
    di stile tramite BRAND_AFFINITY, quindi correggere il brand senza rifare
    derive_attributes lascerebbe style_tags coerenti col brand vecchio.
    """
    output_dir = Path(output)
    meta_paths = sorted(output_dir.rglob("metadata.json"))
    print(f"[*] Ricorreggo brand + attributi per {len(meta_paths)} prodotti (nessuna rete)...", flush=True)

    changed_brand = 0
    for i, meta_path in enumerate(meta_paths, 1):
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        relpath = Path(metadata.get("relpath", str(meta_path.parent.relative_to(output_dir))))

        old_brand_slug = metadata.get("brand_slug")
        brand, brand_slug = match_brand(metadata.get("title"), metadata.get("url"))
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


# =====================================================================
# Descrizione completa dalla pagina prodotto
# =====================================================================
#
# og:description — l'unica cosa che salvavamo — e' una versione accorciata e
# incoerente: su 2901 schede solo il 21% conservava la sezione "Come abbinarla?"
# e il 32% il blocco tecnico. La pagina invece ha entrambi, in due contenitori
# distinti, e nella parte discorsiva ci sono LINK alle categorie dei capi da
# abbinare — che non sono prosa da interpretare ma puntatori mappabili sugli
# slot dell'outfit.

SELETTORI_DESCRIZIONE = ("#product-info-short-description", ".wrapper-short-description")
SELETTORI_SCHEDA = ("#description", ".product.attribute.description")


def _testo_da(soup, selettori) -> str:
    for sel in selettori:
        el = soup.select_one(sel)
        if el:
            testo = el.get_text("\n", strip=True)
            if testo:
                return testo
    return ""


def estrai_descrizione_completa(soup, product_url: str) -> dict:
    """Descrizione discorsiva, scheda tecnica e link di abbinamento."""
    contenitore = None
    for sel in SELETTORI_DESCRIZIONE:
        contenitore = soup.select_one(sel)
        if contenitore:
            break

    abbinamenti = []
    if contenitore:
        for a in contenitore.find_all("a", href=True):
            href = urljoin(product_url, a["href"])
            if "/coordinati" in href:
                continue  # rimando generico agli outfit del sito, non un capo
            abbinamenti.append({"testo": a.get_text(strip=True), "categoria_url": href})

    return {
        "descrizione_completa": _testo_da(soup, SELETTORI_DESCRIZIONE),
        "scheda_tecnica": _testo_da(soup, SELETTORI_SCHEDA),
        "abbinamenti_suggeriti": abbinamenti,
    }


def scarica_descrizioni(output: str, delay: float = 0.7, progress_file: str = None,
                        max_products: int = None, riparti: bool = False) -> dict:
    """Rivisita ogni pagina prodotto e allega la descrizione completa al suo
    metadata.json, senza toccare foto ne' attributi.

    Riprendibile: i capi che hanno gia' 'descrizione_completa' si saltano, a
    meno di riparti=True. I campi vecchi (description_text) restano dove sono:
    servono ancora a chi legge la scheda accorciata, e sovrascriverli
    renderebbe impossibile confrontare le due versioni.
    """
    output_dir = Path(output)
    meta_paths = sorted(output_dir.rglob("metadata.json"))
    da_fare = []
    for mp in meta_paths:
        m = json.loads(mp.read_text(encoding="utf-8"))
        if not m.get("url"):
            continue
        if riparti or "descrizione_completa" not in m:
            da_fare.append(mp)
    if max_products:
        da_fare = da_fare[:max_products]
    print(f"[*] {len(da_fare)} schede da scaricare (su {len(meta_paths)} prodotti)", flush=True)

    session = get_session()
    fatti = errori = con_abbinamenti = 0
    for i, mp in enumerate(da_fare, 1):
        m = json.loads(mp.read_text(encoding="utf-8"))
        soup, err = fetch_product_page(session, m["url"])
        if soup is None:
            errori += 1
            print(f"  [{i}/{len(da_fare)}] ERRORE {m.get('relpath','?')[:46]}: {err}", flush=True)
            time.sleep(delay)
            continue
        m.update(estrai_descrizione_completa(soup, m["url"]))
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        fatti += 1
        if m["abbinamenti_suggeriti"]:
            con_abbinamenti += 1
        if i % 100 == 0 or i == len(da_fare):
            print(f"  [{i}/{len(da_fare)}] {fatti} scaricate, {con_abbinamenti} con abbinamenti, "
                  f"{errori} errori", flush=True)
        time.sleep(delay)

    print(f"\n[OK] {fatti} descrizioni allegate, {con_abbinamenti} con link di abbinamento, "
          f"{errori} errori", flush=True)
    return {"scaricate": fatti, "con_abbinamenti": con_abbinamenti, "errori": errori}


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
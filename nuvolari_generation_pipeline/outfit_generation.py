#!/usr/bin/env python3
"""
Nuvolari — Fase 6: generazione outfit
========================================

A partire da un capo "ancora", costruisce outfit completi (top/bottom/
scarpe/outerwear opz./accessorio opz.) usando beam search: ad ogni slot
si tengono aperte le `beam_width` combinazioni parziali migliori, non solo
la scelta migliore al passo precedente — così un primo abbinamento
localmente ottimo ma che poi si rivela un vicolo cieco (es. bottom che sta
bene con l'ancora ma stona con le scarpe migliori) non blocca la ricerca.

Candidati per ogni slot filtrati per:
  - needs_vision_review=False (i capi con segnale di stile inaffidabile —
    tag "confermati" solo da materiale/brand generico, senza riscontro nel
    testo — sono esclusi da subito: vedi derive_attributes in
    scrape_with_attributes.py per la logica)
  - slot (tipo di capo, dedotto dal titolo — vedi classify_slot)
  - genere (dedotto dal titolo "UOMO"/"DONNA", fallback su category_path)
  - stagione compatibile (stessa stagione o "tutte")
  - cluster stilistico compatibile (stesso style_cluster; se uno dei due è
    outlier -1, il filtro cluster non si applica e si lascia decidere solo
    allo score — escludere a priori il 44% di outlier del catalogo
    sarebbe uno spreco, vedi Fase 4). Per gli slot OPZIONALI (outerwear/
    accessorio) il filtro è più severo: i candidati outlier non sono
    ammessi, un pezzo bonus sbagliato è peggio di nessun pezzo bonus.
  - dispersione di formalità nell'outfit: un capo non può entrare se la
    differenza tra il suo formality_norm e quello già presente nell'outfit
    (min/max) supera FORMALITY_SPREAD_MAX — non basta che lo score
    pairwise sia buono, la formalità dell'outfit intero deve restare
    coerente (es. un panciotto sartoriale non può convivere con sneakers
    casual e cappello, anche se il coseno di stile tra i due sembra alto)
  - Regola 1 (coerenza stagionale manica/gamba): se il bottom è a gamba
    corta (bermuda/short), il top DEVE avere maniche corte — niente
    maglioni/felpe/camicie a manica lunga sui pantaloncini. Regola
    unidirezionale: un top a manica corta con pantaloni lunghi resta
    ammesso (es. capo di mezza stagione). Vedi classify_sleeve/
    classify_leg_length e _seasonal_coherence_ok.
  - Regola 2 (niente giacca sui pantaloncini): lo slot outerwear è escluso
    del tutto se il bottom scelto è a gamba corta — vedi _outerwear_allowed.
    (La parte "niente felpe sui pantaloncini" della stessa richiesta è già
    coperta dalla Regola 1, perché una felpa ha sempre manica lunga per
    tipo di capo.)

score(A,B) dalla Fase 5. Outfit score finale = MINIMO dei pairwise score fra
tutti i capi scelti, non media: una media può nascondere un singolo
abbinamento pessimo dietro tanti abbinamenti buoni (visto succedere in
pratica — vedi conversazione), quindi il punteggio outfit riflette
l'anello più debole della catena, non la sua forza media.

Uso in notebook
-----------------
    %run outfit_generation.py

    df = load_and_prepare('/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet')
    outfits = generate_outfits(df, n_outfits=10)
    for o in outfits:
        print(o['outfit_score'], {slot: item['title'] for slot, item in o['slots'].items() if item})
"""

import hashlib
import collections
import itertools
import json
import os
import re
from pathlib import Path

import pandas as pd

from clustering import STYLE_TAG_COLUMNS
from scoring import COLONNA_PALETTE, COLONNA_VETTORE, score_pair

GARMENT_TYPE_KEYWORDS = {
    "top": ["camicia", "camicie", "t-shirt", "tshirt", "maglia", "maglie", "maglietta",
            "polo", "canotta", "canotte", "felpa", "felpe", "top", "blusa", "camicetta",
            "body", "overshirt", "maglione", "pullover", "pull", "cardigan"],
    "bottom": ["pantalone", "pantaloni", "jeans", "bermuda", "short", "shorts", "gonna",
               "leggings", "pantaloncini"],
    "outerwear": ["giacca", "giacche", "giubbotto", "giubbotti", "cappotto", "piumino",
                  "bomber", "parka", "trench", "blazer", "gilet", "smanicato", "tracktop",
                  "anorak", "jkt", "windbreaker", "giaccone"],
    "shoes": ["scarpe", "scarpa", "sneakers", "sneaker", "stivaletto", "stivaletti",
              "mocassino", "mocassini", "sandali", "sandalo", "ciabatte", "stringate",
              "ballerine"],
    "accessory": ["borsa", "tracolla", "zaino", "cintura", "cappello", "sciarpa", "guanti",
                  "occhiali", "portafoglio", "calzini", "cravatta", "berretto"],
    # abiti/completi/costumi sono "capi unici" (coprono da soli top+bottom):
    # fuori scope per la generazione a slot in questa v1, vedi nota sotto.
    "dress_or_suit": ["abito", "vestito", "tuta", "completo", "costume"],
}

# Quanti capi devono cambiare perche' due outfit contino come proposte diverse
# e non come lo stesso outfit ritoccato. La distanza e' quanti capi di uno
# mancano all'altro, presa nel verso peggiore: sostituire un pantalone vale 1,
# e cosi' vale 1 anche aggiungere soltanto un cappello a un outfit esistente.
# A 2 servono due capi cambiati davvero.
#
# A 1 (cioe' senza regola) il pool si riempiva di quasi-doppioni: il 76% delle
# coppie generate dalla stessa ancora differiva di un capo solo su cinque, e
# triplicare il pool aggiungeva 21 capi distinti su 2514.
DISTANZA_MINIMA_VARIANTI = 2

# Quanto due punteggi possono distare e contare ancora come pari merito nello
# slot opzionale. A 0 si ruota solo fra punteggi identici al millesimo, quindi
# la rotazione non costa NIENTE in qualita': i capi scambiati sono equivalenti
# per il punteggio, non semplicemente vicini.
TOLLERANZA_PAREGGIO = 0.0


class IndiceNovita:
    """Dice se un outfit e' abbastanza diverso da TUTTI quelli gia' accettati.

    Il confronto ingenuo e' quadratico — a 7000 outfit sono 49 milioni di
    confronti fra insiemi. Ma due outfit possono essere troppo simili solo se
    condividono quasi tutti i capi, quindi basta guardare quelli che hanno
    almeno un capo in comune e contare le sovrapposizioni: l'indice inverso
    capo -> outfit riduce il confronto a una manciata di candidati.
    """

    def __init__(self, distanza_minima: int = DISTANZA_MINIMA_VARIANTI):
        self.distanza_minima = distanza_minima
        self._per_capo = collections.defaultdict(list)
        self._firme = []

    def __len__(self):
        return len(self._firme)

    def e_nuovo(self, firma: frozenset) -> bool:
        conteggio = collections.Counter()
        for capo in firma:
            for i in self._per_capo.get(capo, ()):
                conteggio[i] += 1
        for i, comuni in conteggio.items():
            altra = self._firme[i]
            # |A - B| e |B - A|: si prende il verso piu' generoso verso di noi,
            # cioe' il massimo, altrimenti un outfit contenuto in un altro
            # (stessi capi piu' un cappello) passerebbe per nuovo.
            if max(len(firma) - comuni, len(altra) - comuni) < self.distanza_minima:
                return False
        return True

    def aggiungi(self, firma: frozenset) -> None:
        i = len(self._firme)
        self._firme.append(firma)
        for capo in firma:
            self._per_capo[capo].append(i)

MANDATORY_SLOTS = ["top", "bottom", "shoes"]
OPTIONAL_SLOTS = ["outerwear", "accessory"]
# Sotto questa soglia, meglio niente accessorio/capospalla che uno stonato.
# Allineata alla soglia di accettazione del pool (0.46 — riancorata per
# percentile dopo la revisione visiva: vedi COSENO_NEUTRO in scoring.py, la
# scala dei punteggi si è compressa verso il basso e 0.46 occupa oggi lo
# stesso posto che 0.6 occupava prima): quando stava a 0.5 un
# capo opzionale poteva trascinare l'outfit nella fascia 0.5-0.6, dove il pool
# scarta l'outfit INTERO — il capo facoltativo, per definizione rinunciabile,
# affondava anche i tre obbligatori che da soli sarebbero passati.
OPTIONAL_SLOT_MIN_SCORE = 0.46
# I candidati outlier (style_cluster=-1) per il capospalla non sono più
# esclusi in blocco: il 70% dei capispalla eleggibili è outlier — non perché
# stonino, ma perché la scheda di un giubbotto dice poco e HDBSCAN non li
# aggrega — e l'esclusione concentrava 956 presenze su 34 giacche (la più
# usata compariva 170 volte). Al posto del divieto, un'asticella più alta:
# l'outlier entra solo se l'outfit resta comunque BUONO, non appena passabile.
OPTIONAL_OUTLIER_MIN_SCORE = 0.60


_KEYWORD_TO_SLOT = {kw: slot for slot, kws in GARMENT_TYPE_KEYWORDS.items() for kw in kws}


def _find_slot_and_keyword(title: str):
    """Il tipo di capo è quasi sempre la prima parola del titolo (es.
    "SCARPE HEYDUDE..."), ma altre parole del titolo (es. il nome del
    colore "N.BLAZER/B TAN" di una sneaker) possono corrispondere per
    caso a una parola chiave di UN'ALTRA categoria. Per questo si scansiona
    il titolo parola per parola, da sinistra, e si prende il primo match —
    non "la prima categoria del dizionario con un match ovunque nel testo"."""
    if not title:
        return None, None
    # il trattino va mantenuto nel token: "t-shirt" tokenizzato senza trattino
    # diventerebbe "t" + "shirt", nessuno dei due presente nel dizionario, e la
    # scansione proseguirebbe fino a un'altra parola nel titolo che potrebbe
    # corrispondere per sbaglio a un'ALTRA categoria (successo con capi di
    # marchi tipo "Guess Jeans": "jeans" nel nome del brand veniva letto come
    # tipo di capo, non "t-shirt" come tipo di capo reale)
    words = re.findall(r"[a-zà-ü'-]+", title.lower())
    for w in words:
        if w in _KEYWORD_TO_SLOT:
            return _KEYWORD_TO_SLOT[w], w
    return None, None


def classify_slot(title: str):
    slot, _keyword = _find_slot_and_keyword(title)
    return slot


# Ripiego sul percorso di categoria quando il titolo non nomina il tipo di
# capo ("DREW PEAK CREW NF0A4SVR..."). Erano 165 capi invisibili alla
# generazione; per 57 la categoria stava scritta nel percorso. Le chiavi sono
# i segmenti reali del catalogo, confrontati per segmento intero — non come
# sottostringhe, per non leggere "lacoste" dentro un percorso brand.
_SEGMENTO_SLOT = {
    "felpe": "top", "t-shirt": "top", "camicie": "top", "polo": "top",
    "maglieria": "top", "canotte": "top",
    "pantaloni": "bottom", "jeans": "bottom", "bermuda": "bottom",
    "giubbotti": "outerwear", "giubbotti-donna": "outerwear", "gilet": "outerwear",
    "scarpe": "shoes", "scarpe-donna": "shoes", "sneakers": "shoes",
    "stivaletti": "shoes", "sandali": "shoes",
    "calzini-uomo": "accessory", "calzini": "accessory", "borse": "accessory",
    "sciarpe": "accessory", "occhiali": "accessory", "cappelli": "accessory",
    "cinture": "accessory",
}


def slot_da_percorso(relpath: str):
    for segmento in (relpath or "").lower().split("/"):
        if segmento in _SEGMENTO_SLOT:
            return _SEGMENTO_SLOT[segmento]
    return None


SHORT_SLEEVE_KEYWORDS = {"t-shirt", "tshirt", "polo", "canotta", "canotte", "maglietta", "top"}
LONG_SLEEVE_KEYWORDS = {"maglia", "maglie", "felpa", "felpe", "maglione", "pullover", "pull",
                         "cardigan", "overshirt"}
# camicia/camicie/blusa/camicetta/body: manica ambigua dal solo tipo di capo,
# serve la sottocategoria del sito o il testo (vedi classify_sleeve)

SHORT_LEG_KEYWORDS = {"bermuda", "short", "shorts", "pantaloncini"}


def classify_sleeve(title: str, relpath: str):
    """Lunghezza manica del top: 'corta' o 'lunga' — necessaria per la
    Regola 1 (bermuda nell'outfit -> il top deve avere maniche corte,
    altrimenti stona per coerenza stagionale). Alcuni tipi di capo hanno la
    manica implicita nel nome (t-shirt/polo/canotta = sempre corta; maglia/
    felpa/maglione = sempre lunga); per camicia/blusa (ambigue) si usa prima
    la sottocategoria del sito (.../manica-lunga/... o .../mezza-manica/...,
    copre il 97% delle camicie), poi il testo del titolo. Se non
    determinabile, default 'lunga' — scelta conservativa: meglio escludere
    un top per sbaglio che abbinarlo a un bermuda se in realtà è invernale.
    """
    _slot, keyword = _find_slot_and_keyword(title)
    if keyword in SHORT_SLEEVE_KEYWORDS:
        return "corta"
    if keyword in LONG_SLEEVE_KEYWORDS:
        return "lunga"
    rp = (relpath or "").lower()
    if "manica-lunga" in rp:
        return "lunga"
    if "mezza-manica" in rp:
        return "corta"
    t = (title or "").lower()
    if "manica corta" in t or "mezza manica" in t:
        return "corta"
    if "manica lunga" in t:
        return "lunga"
    return "lunga"


def classify_leg_length(title: str):
    """Lunghezza gamba del bottom: 'corta' (bermuda/short/pantaloncini) o
    'lunga' (pantaloni/jeans/leggings). Serve sia alla Regola 1 (coerenza
    col top) sia alla Regola 2 (niente giacche sui pantaloncini)."""
    _slot, keyword = _find_slot_and_keyword(title)
    return "corta" if keyword in SHORT_LEG_KEYWORDS else "lunga"


# --- Regola 3 (utente): il mocassino vuole un top con un collo -------------
#
# "Un mocassino è troppo elegante per stare con maglie che non sono polo o
# camicie". La formalità da sola non bastava a tenerli separati: un mocassino
# sta a 3 e una t-shirt a 2, cioè un solo gradino, e FORMALITY_SPREAD_MAX ne
# tollera esattamente uno.
#
# Solo parole che nominano LA FORMA. Restano fuori di proposito:
#   slip-on   -> lo sono anche le Vans Authentic
#   stringato -> vuol dire allacciato, lo sono anche le running
#   ballerina -> è una scarpa da donna piatta, non un mocassino
CALZATURA_ELEGANTE = re.compile(
    r"(mocassin[oi]|loafer|penny loafer|scarp[ae] da barca|boat shoe)", re.I)

# Frase di repertorio del catalogo: elenca i reparti del negozio, non descrive
# il prodotto. Senza questa esclusione un anfibio e una New Balance risultavano
# mocassini perché la scheda diceva "disponibili in vari stili come sneakers,
# scarpe eleganti, stivali o mocassini".
DESCRIZIONE_DI_REPERTORIO = re.compile(r"variet[àa] di stili come", re.I)

# Quando è la sola descrizione a parlare, si accettano solo le forme che un
# copywriter non usa per traslato. "Mocassino" da solo non basta: la scheda
# della Vans Authentic — una skate shoe in tela, verificata sulla foto — la
# chiama "questo mocassino sportivo". "Scarpa da barca" e "loafer" no: quelle
# nominano un oggetto preciso.
FORMA_INEQUIVOCABILE = re.compile(r"(scarp[ae] da barca|boat shoe|loafer)", re.I)

# Top che con un mocassino non ci stanno. La maglieria (maglia, maglione,
# cardigan) NON è qui: il maglione col mocassino è un abbinamento classico,
# e in italiano "maglia" vuol dire quasi sempre quello.
TOP_SENZA_COLLO = {"t-shirt", "tshirt", "maglietta", "canotta", "canotte", "top", "felpa", "felpe"}


def classify_calzatura(title: str, descrizione: str = "") -> bool:
    """Vero se la scarpa ha la forma del mocassino o della scarpa da barca.

    Guarda anche la descrizione, non solo il titolo, perché il titolo mente: la
    scarpa in camoscio beige della segnalazione si chiama "SNEAKERS UOMO FRED
    PERRY B2346" ed è archiviata fra le sneakers, ma la sua stessa scheda dice
    "rilegge la silhouette classica della scarpa da barca". Sono 7 casi su 28:
    un quarto dei mocassini del catalogo non si dichiara tale nel titolo.
    """
    if CALZATURA_ELEGANTE.search(title or ""):
        return True
    if DESCRIZIONE_DI_REPERTORIO.search(descrizione or ""):
        return False
    return bool(FORMA_INEQUIVOCABILE.search(descrizione or ""))


def classify_top_senza_collo(title: str) -> bool:
    """Vero se il top è una t-shirt, una canotta, un top o una felpa."""
    _slot, keyword = _find_slot_and_keyword(title)
    return keyword in TOP_SENZA_COLLO


# --- Regola 4 (utente): il cappotto non va sopra una tuta -----------------
#
# Stessa storia del mocassino, sul capospalla: "giubbotto" non dice il registro.
# Il caban di lana blu doppiopetto a sei bottoni — verificato sulla foto del
# prodotto — si chiama GIUBBOTTO UOMO NUVOLARI OTIS NAVY e sta a formalità 2,
# quindi finiva sopra una tuta con le sneakers.
#
# Il vincolo non passa dalla formalità di proposito: alzare quel capo da 2 a 3
# non avrebbe tolto l'outfit (la dispersione ammessa è esattamente un gradino,
# 0.25 <= 0.30), e alzarlo a 4 lo avrebbe tolto anche dagli abbinamenti giusti
# col jeans. La formalità è un asse solo: dice quanto, non dice con cosa.
FORMA_CAPPOTTO = re.compile(r"(caban|montgomery|doppiopetto|sei bottoni|peacoat|cappotto|trench)", re.I)

# Un gilet imbottito e una giacca a vento citano il cappotto nella scheda ma
# cappotti non sono: il titolo qui è affidabile e ha l'ultima parola.
NON_E_UN_CAPPOTTO = re.compile(r"\b(gilet|giacca a vento|piumino|smanicato)\b", re.I)

BOTTOM_DA_TUTA = re.compile(r"\b(tuta|jogger\w*|sweatpant\w*)\b", re.I)


def classify_cappotto(title: str, descrizione: str = "") -> bool:
    """Vero se il capospalla ha la forma di un cappotto (caban compreso)."""
    if NON_E_UN_CAPPOTTO.search(title or ""):
        return False
    if FORMA_CAPPOTTO.search(title or ""):
        return True
    if DESCRIZIONE_DI_REPERTORIO.search(descrizione or ""):
        return False
    return bool(FORMA_CAPPOTTO.search(descrizione or ""))


def classify_bottom_da_tuta(title: str) -> bool:
    return bool(BOTTOM_DA_TUTA.search(title or ""))


from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
CATALOGO = _CATALOGO


def _descrizioni(relpath_voluti: set) -> dict:
    """relpath -> description_text, solo per i capi richiesti.

    Il parquet porta il titolo ma non la descrizione, e la descrizione è
    l'unico posto dove sta la forma del capo: la scarpa da barca che si chiama
    "sneakers", il caban che si chiama "giubbotto". Si legge dal catalogo, che è
    la stessa fonte da cui il parquet è nato. Se il catalogo non c'è (una copia
    del solo codice) si continua senza: le regole perdono i casi che si
    dichiarano solo nella scheda, non l'intero funzionamento.
    """
    if not CATALOGO.exists():
        return {}
    out = {}
    for p in CATALOGO.rglob("metadata.json"):
        rel = str(p.parent.relative_to(CATALOGO))
        if rel not in relpath_voluti:
            continue
        try:
            out[rel] = json.loads(p.read_text(encoding="utf-8")).get("description_text") or ""
        except (OSError, ValueError):
            continue
    return out


# Accessori che, quando il titolo non dichiara il genere, non vanno trattati
# come unisex. Sono capi connotati: una tracolla street senza indicazione
# risultava "neutra" e finiva su outfit donna. Cappelli, occhiali, zaini e
# sciarpe restano invece genuinamente trasversali e non compaiono qui.
ACCESSORI_MASCHILI = r"\b(borsa|borse|tracolla|tracolle|marsupio|marsupi)\b"


def classify_gender(title: str, relpath: str):
    t = (title or "").lower()
    # Confine di parola solo in coda: il catalogo scrive il brand attaccato al
    # genere ("CAMICIA ICHIDONNA IHESTAMA"), e con \bdonna\b quel capo restava
    # senza genere — cioè jolly — e finiva negli outfit uomo. Otto capi ICHI
    # erano in questo stato; uno, un chino donna, era in un outfit uomo con
    # polo e mocassini. Nessun titolo del catalogo contiene parole che
    # finiscono in -donna o -uomo con un altro significato (misurato).
    if re.search(r"donna\b", t):
        return "donna"
    if re.search(r"uomo\b", t):
        return "uomo"
    # fallback sul percorso di categoria, meno affidabile del titolo
    if "abbigliamento-donna" in (relpath or ""):
        return "donna"
    # borse e tracolle senza genere dichiarato: assegnate a uomo invece che
    # lasciate neutre, altrimenti il jolly del genere le fa entrare ovunque
    if re.search(ACCESSORI_MASCHILI, t):
        return "uomo"
    return None  # genere non determinato -> trattato come compatibile con entrambi


SEASONS = ["estate", "inverno", "mezza_stagione", "tutte"]


def _season_label(row: pd.Series) -> str:
    for s in SEASONS:
        if row.get(f"season_{s}") == 1.0:
            return s
    return "tutte"


def load_and_prepare(features_parquet: str) -> pd.DataFrame:
    """Carica features_clustered.parquet e aggiunge le colonne slot/gender/
    season/sleeve/leg_length necessarie per la generazione outfit (season è
    ricostruita dalle colonne one-hot season_* della Fase 3; le altre sono
    dedotte dal titolo/percorso, vedi sopra)."""
    df = pd.read_parquet(features_parquet)
    df["slot"] = df["title"].apply(classify_slot)
    # titolo muto -> si guarda il percorso di categoria (vedi slot_da_percorso)
    manca = df["slot"].isna()
    df.loc[manca, "slot"] = df.loc[manca, "relpath"].apply(slot_da_percorso)
    df["gender"] = df.apply(lambda r: classify_gender(r["title"], r["relpath"]), axis=1)
    df["season"] = df.apply(_season_label, axis=1)
    df["sleeve"] = df.apply(lambda r: classify_sleeve(r["title"], r["relpath"]), axis=1)
    df["leg_length"] = df["title"].apply(classify_leg_length)

    # La forma del capo è l'unico attributo che il titolo non basta a dare (vedi
    # classify_calzatura e classify_cappotto): serve la descrizione, che sta nel
    # catalogo e non nel parquet. Si legge solo per scarpe e capispalla — 453
    # schede su 2901.
    interessati = set(df.loc[df["slot"].isin(["shoes", "outerwear"]), "relpath"])
    descrizioni = _descrizioni(interessati)
    df["mocassino"] = df.apply(
        lambda r: r["slot"] == "shoes"
        and classify_calzatura(r["title"], descrizioni.get(r["relpath"], "")), axis=1)
    df["top_senza_collo"] = df.apply(
        lambda r: r["slot"] == "top" and classify_top_senza_collo(r["title"]), axis=1)
    df["cappotto"] = df.apply(
        lambda r: r["slot"] == "outerwear"
        and classify_cappotto(r["title"], descrizioni.get(r["relpath"], "")), axis=1)
    df["bottom_da_tuta"] = df.apply(
        lambda r: r["slot"] == "bottom" and classify_bottom_da_tuta(r["title"]), axis=1)

    # Vettore di stile e tavolozza pronti da usare. Sono gli stessi valori di
    # sempre, solo già estratti: score_pair viene chiamata milioni di volte, e
    # rifare a ogni chiamata la selezione pandas per etichetta e il json.loads
    # della tavolozza costava il 70% del tempo di generazione del pool.
    df[COLONNA_VETTORE] = list(df[STYLE_TAG_COLUMNS].to_numpy(dtype=float))
    df[COLONNA_PALETTE] = df["color_palette"].apply(
        lambda p: json.loads(p) if isinstance(p, str) else p)
    return df


def _gender_compatible(a: str, b: str) -> bool:
    return a is None or b is None or a == b


def _season_compatible(a: str, b: str) -> bool:
    return a == "tutte" or b == "tutte" or a == b


def _cluster_compatible(a: int, b: int) -> bool:
    if a == -1 or b == -1:
        return True  # outlier: nessun filtro di cluster, decide solo score()
    return a == b


FORMALITY_SPREAD_MAX = 0.3  # formality_norm ha 5 livelli discreti (0, .25, .5, .75, 1) -> tollera un solo "gradino"


def _formality_range(items) -> tuple:
    values = [row["formality_norm"] for _, row in items]
    return min(values), max(values)


def _formality_ok(items, candidate_formality: float) -> bool:
    """Verifica che aggiungere candidate_formality non allarghi la dispersione
    di formalità dell'outfit oltre FORMALITY_SPREAD_MAX. Non basta che il
    candidato stia bene in coppia con ogni singolo capo già scelto (vedi
    style_match) — l'outfit nel suo insieme deve restare coerente: un pezzo
    molto più formale/informale degli altri stona anche se il suo score
    pairwise con ciascuno preso singolarmente sembrava accettabile."""
    lo, hi = _formality_range(items)
    new_lo, new_hi = min(lo, candidate_formality), max(hi, candidate_formality)
    return (new_hi - new_lo) <= FORMALITY_SPREAD_MAX


def _existing_value(current_items, slot: str, column: str):
    return next((row[column] for s, row in current_items if s == slot), None)


def _seasonal_coherence_ok(current_items, slot: str, candidate_row: pd.Series) -> bool:
    """Regola 1 (utente): se l'outfit ha un bottom a gamba corta (bermuda/
    short), il top deve avere maniche corte — un top invernale a maniche
    lunghe coi pantaloncini stona per coerenza stagionale. La regola è
    unidirezionale (un top a maniche corte con pantaloni lunghi è normale,
    es. mezza stagione), quindi si applica in entrambi gli ordini di
    riempimento slot senza vietare quel caso."""
    if slot == "top":
        existing_bottom_leg = _existing_value(current_items, "bottom", "leg_length")
        if existing_bottom_leg == "corta" and candidate_row["sleeve"] != "corta":
            return False
    elif slot == "bottom":
        existing_top_sleeve = _existing_value(current_items, "top", "sleeve")
        if candidate_row["leg_length"] == "corta" and existing_top_sleeve == "lunga":
            return False

    # Un capo dichiaratamente invernale non convive con la pelle scoperta,
    # qualunque slot occupi: la sciarpa sopra la t-shirt a maniche corte
    # nasceva proprio qui, perché il controllo esisteva solo fra top e bottom
    # e gli accessori non lo attraversavano mai.
    gambe_scoperte = _existing_value(current_items, "bottom", "leg_length") == "corta"
    braccia_scoperte = _existing_value(current_items, "top", "sleeve") == "corta"
    if candidate_row["season"] == "inverno" and (gambe_scoperte or braccia_scoperte):
        return False
    if candidate_row["leg_length"] == "corta" or candidate_row["sleeve"] == "corta":
        if any(r["season"] == "inverno" for _, r in current_items):
            return False
    return True


def _calzatura_ok(current_items, slot: str, candidate_row: pd.Series) -> bool:
    """Regola 3 (utente): niente t-shirt, canotte, top o felpe sotto un
    mocassino. Vale nei due ordini di riempimento, perché le scarpe possono
    entrare prima o dopo il top."""
    if slot == "shoes" and candidate_row["mocassino"]:
        if _existing_value(current_items, "top", "top_senza_collo"):
            return False
    elif slot == "top" and candidate_row["top_senza_collo"]:
        if _existing_value(current_items, "shoes", "mocassino"):
            return False
    return True


def _cappotto_ok(current_items, slot: str, candidate_row: pd.Series) -> bool:
    """Regola 4 (utente): niente cappotto o caban sopra una tuta."""
    if slot == "outerwear" and candidate_row["cappotto"]:
        if _existing_value(current_items, "bottom", "bottom_da_tuta"):
            return False
    elif slot == "bottom" and candidate_row["bottom_da_tuta"]:
        if _existing_value(current_items, "outerwear", "cappotto"):
            return False
    return True


def _outerwear_allowed(current_items) -> bool:
    """Regola 2 (utente): giacche/outerwear solo se i pantaloni sono lunghi
    — niente giacca sui pantaloncini. (La parte "niente felpe sui
    pantaloncini" della stessa regola è già coperta dalla Regola 1, perché
    una felpa ha sempre manica lunga per tipo di capo.)"""
    existing_bottom_leg = _existing_value(current_items, "bottom", "leg_length")
    return existing_bottom_leg != "corta"


def _filtra(candidates: pd.DataFrame, predicato, colonna: str = None) -> pd.DataFrame:
    """Filtra riga per riga aggirando un caso limite di pandas.

    Su un DataFrame vuoto .apply() restituisce una Series di dtype object,
    non bool: candidates[serie_non_booleana] non viene letto come maschera di
    righe ma come selezione di COLONNE, quindi il risultato perde tutte le
    colonne e il filtro successivo muore con KeyError sul nome della colonna.
    Con i filtri a catena qui sotto capita spesso che i candidati si esauriscano
    a metà strada, quindi il caso vuoto va intercettato prima.
    """
    if candidates.empty:
        return candidates
    if colonna is None:
        maschera = candidates.apply(predicato, axis=1)
    else:
        maschera = candidates[colonna].apply(predicato)
    return candidates[maschera.astype(bool)]


def candidates_for_slot(df: pd.DataFrame, slot: str, anchor_row: pd.Series, used_relpaths: set,
                         require_real_cluster: bool = False, current_items=None) -> pd.DataFrame:
    """require_real_cluster=True esclude i capi outlier (style_cluster=-1)
    dai candidati, anche se l'ancora stessa è un outlier. Usato per gli slot
    OPZIONALI (outerwear/accessorio): un capo "jolly" per debolezza di
    segnale (vedi style_match) può comunque superare la soglia di score
    puro — meglio quindi richiedere lì un'appartenenza di cluster reale e
    confermata, piuttosto che rischiare di aggiungere un pezzo stonato.
    Per gli slot obbligatori (top/bottom/scarpe) resta permissivo, perché
    il 44% del catalogo è outlier e negarli del tutto renderebbe molti
    outfit non completabili.

    current_items, se passato, applica anche il filtro di dispersione di
    formalità (vedi _formality_ok) e la coerenza stagionale manica/gamba
    (vedi _seasonal_coherence_ok) rispetto ai capi già scelti nell'outfit.
    """
    candidates = df[
        (df["slot"] == slot)
        & (~df["needs_vision_review"].astype(bool))
        & (~df["relpath"].isin(used_relpaths))
    ]
    if require_real_cluster:
        candidates = candidates[candidates["style_cluster"] != -1]
    candidates = _filtra(candidates, lambda g: _gender_compatible(g, anchor_row["gender"]), "gender")
    candidates = _filtra(candidates, lambda s: _season_compatible(s, anchor_row["season"]), "season")
    candidates = _filtra(candidates, lambda c: _cluster_compatible(c, anchor_row["style_cluster"]), "style_cluster")
    if current_items:
        # Genere e stagione vanno verificati contro TUTTI i capi già scelti, non
        # solo contro l'ancora: se l'ancora ha il valore indeterminato (None per
        # il genere, "tutte" per la stagione) è compatibile con chiunque, e due
        # capi incompatibili fra loro possono entrare entrambi passando ciascuno
        # il confronto con lei. Succedeva: 31 outfit mescolavano uomo e donna,
        # 138 mescolavano estate e inverno.
        candidates = _filtra(
            candidates,
            lambda g: all(_gender_compatible(g, r["gender"]) for _, r in current_items), "gender")
        candidates = _filtra(
            candidates,
            lambda s: all(_season_compatible(s, r["season"]) for _, r in current_items), "season")
        candidates = _filtra(candidates, lambda f: _formality_ok(current_items, f), "formality_norm")
        # su TUTTI gli slot, non più solo top/bottom: accessori e capospalla
        # erano l'unica via per cui un capo invernale entrava in un outfit estivo
        candidates = _filtra(candidates, lambda r: _seasonal_coherence_ok(current_items, slot, r))
        candidates = _filtra(candidates, lambda r: _calzatura_ok(current_items, slot, r))
        candidates = _filtra(candidates, lambda r: _cappotto_ok(current_items, slot, r))
    return candidates


def valida_composizione(o: dict, df) -> tuple:
    """(score, None) se la composizione passa le regole attuali, altrimenti
    (score, motivo). Applica i vincoli di candidates_for_slot a un outfit
    GIA' composto, che la beam search non puo' piu' rivedere.

    Sta qui e non in ripesca_orfane perche' servono a due chiamanti: il
    ripescaggio delle immagini pagate e l'accumulo del pool, che deve poter
    dire se un outfit generato ieri regge ancora le regole di oggi."""
    items = []
    for s, v in o["slots"].items():
        if not v:
            continue
        if v["relpath"] not in df.index:
            return None, "capo sparito dal catalogo"
        items.append((s, df.loc[v["relpath"]]))
    rows = dict(items)

    if any(r["needs_vision_review"] for _, r in items):
        return None, "needs_vision_review"

    coppie = {}
    for (sa, a), (sb, b) in itertools.combinations(items, 2):
        coppie[f"{sa}-{sb}"] = score_pair(a, b)["score"]
    m = min(coppie.values())
    if m < OPTIONAL_SLOT_MIN_SCORE:
        return m, f"score sotto {OPTIONAL_SLOT_MIN_SCORE}"

    generi = {r["gender"] for _, r in items} - {None}
    if len(generi) > 1:
        return m, "genere misto"
    for (_, a), (_, b) in itertools.combinations(items, 2):
        if not (a["season"] == "tutte" or b["season"] == "tutte" or a["season"] == b["season"]):
            return m, f"stagioni {a['season']}/{b['season']}"
    fs = [r["formality_norm"] for _, r in items]
    if max(fs) - min(fs) > FORMALITY_SPREAD_MAX:
        return m, "dispersione formalita'"

    top, bot, ow, sh = (rows.get(k) for k in ("top", "bottom", "outerwear", "shoes"))
    if bot is not None and bot["leg_length"] == "corta":
        if top is not None and top["sleeve"] != "corta":
            return m, "R1 maniche lunghe su shorts"
        if ow is not None:
            return m, "R2 giacca su shorts"
    if any(r["season"] == "inverno" for _, r in items) and (
            (bot is not None and bot["leg_length"] == "corta")
            or (top is not None and top["sleeve"] == "corta")):
        return m, "R1 capo invernale su pelle scoperta"
    if sh is not None and sh["mocassino"] and top is not None and top["top_senza_collo"]:
        return m, "R3 mocassino su top senza collo"
    if ow is not None and ow["cappotto"] and bot is not None and bot["bottom_da_tuta"]:
        return m, "R4 cappotto su tuta"
    return m, None


def build_outfits(df: pd.DataFrame, anchor_relpath: str, w_colore: float = 0.5, w_stile: float = 0.5,
                   beam_width: int = 5, candidates_per_step: int = 30,
                   varianti: int = 1, contatore_uso=None) -> list:
    """Costruisce fino a `varianti` outfit distinti a partire da un capo
    ancora, via beam search slot per slot, ordinati dal migliore. Ritorna
    lista vuota se l'ancora non ha uno slot riconosciuto o se non si riesce
    a riempire tutti gli slot obbligatori.

    Il tetto vero e' beam_width: oltre quello non ci sono rami da consegnare."""
    # Senza contatore in ingresso la rotazione vale solo dentro questa ancora:
    # e' il comportamento giusto per un chiamante singolo (audit_pipeline), non
    # per il pool, che ne passa uno condiviso da tutte le ancore.
    if contatore_uso is None:
        contatore_uso = collections.Counter()

    anchor_rows = df[df["relpath"] == anchor_relpath]
    if anchor_rows.empty:
        return []
    anchor = anchor_rows.iloc[0]
    anchor_slot = anchor["slot"]
    if anchor_slot not in MANDATORY_SLOTS:
        return []

    remaining_mandatory = [s for s in MANDATORY_SLOTS if s != anchor_slot]

    # ogni elemento del beam: (lista di (slot, row), set di relpath già usati)
    beam = [([(anchor_slot, anchor)], {anchor_relpath})]

    def _min_pairwise(items):
        # stesso criterio del punteggio outfit finale (minimo, non media — vedi
        # docstring del modulo): la beam search deve ottimizzare l'anello più
        # debole, non farsi ingannare da una media alta durante la costruzione
        if len(items) < 2:
            return 1.0
        scores = [
            score_pair(items[i][1], items[j][1], w_colore, w_stile)["score"]
            for i in range(len(items)) for j in range(i + 1, len(items))
        ]
        return min(scores)

    for slot in remaining_mandatory:
        new_beam = []
        for items, used in beam:
            candidates = candidates_for_slot(df, slot, anchor, used, current_items=items)
            if candidates.empty:
                continue
            # per velocità, si pre-ordinano i candidati per score contro l'ancora
            # e se ne valutano approfonditamente (contro tutto il parziale) solo i migliori
            candidates = candidates.copy()
            candidates["_anchor_score"] = candidates.apply(
                lambda r: score_pair(anchor, r, w_colore, w_stile)["score"], axis=1
            )
            candidates = candidates.sort_values("_anchor_score", ascending=False).head(candidates_per_step)

            for _, cand in candidates.iterrows():
                new_items = items + [(slot, cand)]
                new_score = _min_pairwise(new_items)
                new_used = used | {cand["relpath"]}
                new_beam.append((new_items, new_used, new_score))

        if not new_beam:
            return []  # nessun candidato disponibile per questo slot -> outfit non completabile
        new_beam.sort(key=lambda t: t[2], reverse=True)
        beam = [(items, used) for items, used, _ in new_beam[:beam_width]]

    # Slot obbligatori riempiti. Gli opzionali si provano su TUTTE le
    # combinazioni rimaste nel beam, non solo sulla prima.
    #
    # Prima si prendeva beam[0] e si buttava il resto: la ricerca era larga 5
    # fino al terzetto e diventava larga 1 esattamente dove restavano ancora
    # due decisioni da prendere. Ma quale terzetto regga meglio un capospalla
    # non si sa finché non lo si prova, e il terzetto in testa spesso non è
    # quello che vince alla fine. Misurato sulla felpa NASA: il terzetto in
    # testa (0,781) finiva a 0,728, il secondo (0,770) sarebbe finito a 0,754
    # — e quel secondo veniva scartato senza mai essere provato.
    def _completa(items, used):
        for slot in OPTIONAL_SLOTS:
            if slot == "outerwear" and not _outerwear_allowed(items):
                continue  # Regola 2: niente giacca/outerwear sui pantaloncini
            # Gli outlier di cluster sono ammessi anche qui (vedi
            # OPTIONAL_OUTLIER_MIN_SCORE): il vincolo di formalità e
            # l'asticella più alta fanno il lavoro che prima faceva il
            # divieto, senza congelare il 70% dei capispalla fuori da ogni
            # outfit.
            candidates = candidates_for_slot(df, slot, anchor, used,
                                              current_items=items)
            if candidates.empty:
                continue
            candidates = candidates.copy()
            candidates["_score"] = candidates.apply(
                lambda r: _min_pairwise(items + [(slot, r)]), axis=1
            )
            # Ognuno con la propria asticella, PRIMA di scegliere il migliore:
            # altrimenti un outlier a 0.65 (bocciato) nasconderebbe un capo di
            # cluster a 0.62 (promosso) solo per avere il punteggio più alto.
            soglie = candidates["style_cluster"].apply(
                lambda c: OPTIONAL_OUTLIER_MIN_SCORE if c == -1 else OPTIONAL_SLOT_MIN_SCORE)
            ammessi = candidates[candidates["_score"] >= soglie]
            if not ammessi.empty:
                # A parita' di punteggio vince il capo MENO usato finora.
                #
                # Qui i pareggi non sono l'eccezione, sono la regola: misurato
                # su 60 outfit, i candidati capospalla ammessi erano 99 su 99 e
                # il margine fra il primo e il secondo aveva mediana 0,0000, con
                # in media sei capi appaiati al massimo (fino a 102). Il nero
                # armonizza col massimo su tutto, quindi mezzo guardaroba di
                # bomber neri casual finisce con lo stesso identico punteggio.
                #
                # `idxmax` rompeva quei pareggi prendendo la prima riga, sempre
                # la stessa: un solo bomber compariva in 4132 outfit e 143
                # capispalla su 274 non entravano in NESSUNO. Non era una scelta
                # di gusto, era l'ordine del dataframe. Ruotando fra i pari
                # merito il punteggio non cambia di un centesimo -- sono
                # identici per definizione -- e il guardaroba entra tutto.
                massimo = ammessi["_score"].max()
                pari = ammessi[ammessi["_score"] >= massimo - TOLLERANZA_PAREGGIO]
                best_candidate = pari.loc[
                    pari["relpath"].map(lambda r: contatore_uso[r]).idxmin()]
                contatore_uso[best_candidate["relpath"]] += 1
                items = items + [(slot, best_candidate)]
                used = used | {best_candidate["relpath"]}
        return items

    def _confeziona(items):
        # punteggi pairwise finali, per trasparenza/debug
        pairwise_scores = {}
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                slot_i, row_i = items[i]
                slot_j, row_j = items[j]
                pairwise_scores[f"{slot_i}-{slot_j}"] = score_pair(row_i, row_j, w_colore, w_stile)["score"]

        outfit_score = min(pairwise_scores.values()) if pairwise_scores else 1.0

        slots_out = {slot: None for slot in MANDATORY_SLOTS + OPTIONAL_SLOTS}
        for slot, row in items:
            slots_out[slot] = {
                "relpath": row["relpath"],
                "title": row["title"],
                "url": row["url"],
                "display_image": row.get("display_image"),
                "all_images": row.get("all_images"),
            }
        return {"slots": slots_out,
                "pairwise_scores": {k: round(v, 3) for k, v in pairwise_scores.items()},
                "outfit_score": round(outfit_score, 3)}

    # Tutto il menu che questa ancora sa produrre, dal migliore in giu'. La
    # beam search completa beam_width rami e prima ne consegnava uno solo: gli
    # altri erano gia' costruiti, valutati e buttati.
    #
    # Qui NON si decide piu' quali tenere. Se due rami sono quasi uguali lo
    # sono rispetto a questa ancora, ma la somiglianza che conta e' quella
    # verso l'INTERO pool: un'altra ancora puo' aver gia' prodotto lo stesso
    # outfit, e da qui non si vede. La scelta sta in run_outfit_pipeline.
    completi = [_completa(items, used) for items, used in beam]
    completi.sort(key=_min_pairwise, reverse=True)

    fuori, visti = [], set()
    for items in completi:
        firma = frozenset(r["relpath"] for _, r in items)
        if firma in visti:
            continue  # due rami possono chiudersi sullo stesso identico insieme
        visti.add(firma)
        fuori.append(_confeziona(items))
        if len(fuori) >= max(1, varianti):
            break
    return fuori


def build_outfit(df: pd.DataFrame, anchor_relpath: str, w_colore: float = 0.5, w_stile: float = 0.5,
                  beam_width: int = 5, candidates_per_step: int = 30) -> dict:
    """Il solo outfit migliore per questa ancora, o None. Involucro su
    build_outfits per i chiamanti che ne vogliono uno (audit_pipeline,
    generate_outfits)."""
    fuori = build_outfits(df, anchor_relpath, w_colore, w_stile,
                          beam_width, candidates_per_step, varianti=1)
    return fuori[0] if fuori else None


def generate_outfits(df: pd.DataFrame, n_outfits: int = 10, anchor_slot: str = "top",
                      min_score: float = 0.46, w_colore: float = 0.5, w_stile: float = 0.5,
                      beam_width: int = 5, candidates_per_step: int = 30) -> list:
    """Genera outfit iterando i capi ancora dello slot indicato in ordine
    deterministico (per relpath — categoria/sottocategoria/slug), non via
    campionamento casuale: così nessun capo del catalogo viene escluso per
    sfortuna nel campionamento, e il risultato è riproducibile a parità di
    dati. Si ferma quando si raggiungono n_outfits outfit sopra min_score,
    o quando si esauriscono le ancore disponibili. Deduplica outfit
    identici (stesso insieme esatto di capi) che possono emergere da
    ancore diverse (es. l'outfit {A,B,C} può essere il migliore risultato
    sia partendo da A come ancora sia da B)."""
    anchor_pool = df[(df["slot"] == anchor_slot) & (~df["needs_vision_review"].astype(bool))]
    anchor_pool = anchor_pool.sort_values("relpath")

    outfits = []
    seen_signatures = set()
    for _, anchor in anchor_pool.iterrows():
        outfit = build_outfit(df, anchor["relpath"], w_colore, w_stile, beam_width, candidates_per_step)
        if not outfit or outfit["outfit_score"] < min_score:
            continue
        signature = frozenset(v["relpath"] for v in outfit["slots"].values() if v)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        outfits.append(outfit)
        if len(outfits) >= n_outfits:
            break

    outfits.sort(key=lambda o: o["outfit_score"], reverse=True)
    return outfits


# =====================================================================
# Pipeline completa: pool di outfit per API/UI a valle
# =====================================================================

def build_garment_id(relpath: str) -> str:
    """Identificativo stabile e "pulito" per un capo, derivato da relpath
    via hash (non il path stesso) — pensato per un'API che non deve far
    trapelare la struttura di cartelle interna (es. /products/{id})."""
    return hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:12]


def build_outfit_id(relpaths) -> str:
    """Identificativo stabile di un outfit, derivato dall'insieme (ordinato,
    per essere indipendente dall'ordine) dei relpath dei capi che lo
    compongono. Poiché la generazione è ora deterministica (vedi
    generate_outfits/run_outfit_pipeline), lo stesso outfit rigenerato in
    run successive ottiene sempre lo stesso id — utile per un'API/UI che
    debba referenziarlo nel tempo (es. outfit salvati/preferiti)."""
    signature = "|".join(sorted(relpaths))
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]


def _serialize_slots(slots: dict) -> dict:
    out = {}
    for slot, item in slots.items():
        if item is None:
            out[slot] = None
            continue
        out[slot] = {
            "product_id": build_garment_id(item["relpath"]),
            "relpath": item["relpath"],
            "title": item["title"],
            "url": item["url"],
            "display_image": item.get("display_image"),
            # all_images arriva dal parquet come ndarray: "or []" su ndarray
            # solleva ValueError (verità ambigua), quindi il None si gestisce
            # esplicitamente prima di convertire in lista
            "all_images": [] if item.get("all_images") is None else list(item["all_images"]),
        }
    return out


_GENDER_LABEL = {"uomo": "uomo", "donna": "donna"}


def _build_outfit_label(gender, slots: dict) -> str:
    """Riga leggibile a colpo d'occhio, es. 'Outfit uomo: Camicia Nuvolari
    Petapa Sottobosco + Bermuda Nuvolari Zoe Verde Salvia + Sneakers
    Lacoste Elite Active' — pensata per scorrere il file JSONL senza dover
    aprire ogni oggetto slots per capire di che outfit si tratta."""
    gender_label = _GENDER_LABEL.get(gender, "unisex")
    parts = [item["title"] for item in slots.values() if item]
    return f"Outfit {gender_label}: " + " + ".join(parts)


def run_outfit_pipeline(df: pd.DataFrame, out_jsonl: str, min_score: float = 0.46,
                         w_colore: float = 0.5, w_stile: float = 0.5,
                         beam_width: int = 15, candidates_per_step: int = 30,
                         varianti_per_ancora: int = 3, accumula: bool = True) -> dict:
    """Genera l'intero pool di outfit scansionando OGNI slot obbligatorio
    (top, bottom, scarpe) come ancora a turno — non solo "top" come fa
    generate_outfits di default — in ordine deterministico per relpath.
    Così ogni capo di ogni slot obbligatorio ha la sua possibilità di
    comparire in almeno un outfit, non solo i top (vedi discussione:
    ancorare solo dai top garantisce copertura ai top, non a bottom/scarpe,
    che potrebbero perdere sistematicamente come candidati senza mai
    essere loro stessi ancora).

    Deduplica GLOBALMENTE (stesso insieme esatto di capi trovato da ancore
    diverse — anche di slot diversi — conta una volta sola).

    Scrive incrementalmente su out_jsonl (un outfit per riga, JSONL) così
    il run lungo è resumibile/ispezionabile a metà. Formato di ogni riga
    pensato per essere consumato direttamente da un'API a valle: ogni capo
    ha un product_id stabile (hash di relpath, non il path stesso) e ogni
    outfit un outfit_id stabile (vedi build_garment_id/build_outfit_id) —
    lo stesso outfit rigenerato in run successive mantiene lo stesso id.
    """
    total_anchors_tried = 0
    total_outfits_written = 0

    # Accumulo: un outfit gia' nel pool che regge ancora le regole non viene
    # buttato solo perche' la beam search di oggi ha preferito altro. Il pool
    # e' un catalogo di proposte valide, non la classifica di un singolo run:
    # ogni rigenerazione da zero costava outfit legittimi (e le loro immagini
    # gia' pagate, che finivano orfane). I superstiti si riscrivono con i
    # punteggi RICALCOLATI, non con quelli vecchi, altrimenti il pool
    # mescolerebbe due tarature diverse.
    ereditati = []
    per_passata = []
    scartati = 0
    if accumula and os.path.exists(out_jsonl):
        df_idx = df.set_index("relpath", drop=False)
        for riga in open(out_jsonl, encoding="utf-8"):
            riga = riga.strip()
            if not riga:
                continue
            o = json.loads(riga)
            punteggio, motivo = valida_composizione(o, df_idx)
            if motivo or punteggio is None or punteggio < min_score:
                scartati += 1
                continue
            items = [(s, df_idx.loc[v["relpath"]]) for s, v in o["slots"].items() if v]
            coppie = {f"{sa}-{sb}": round(score_pair(a, b, w_colore, w_stile)["score"], 3)
                      for (sa, a), (sb, b) in itertools.combinations(items, 2)}
            o["pairwise_scores"] = coppie
            o["outfit_score"] = round(min(coppie.values()), 3)
            ereditati.append(o)
        print(f"[*] Pool esistente: {len(ereditati)} outfit confermati, "
              f"{scartati} non passano piu' le regole", flush=True)

    # Gli ereditati fanno gia' parte del pool: i nuovi devono essere diversi
    # anche da loro, non solo fra di se'.
    indice = IndiceNovita()
    for o in ereditati:
        indice.aggiungi(frozenset(v["relpath"] for v in o["slots"].values() if v))

    with open(out_jsonl, "w", encoding="utf-8") as f:
        for o in ereditati:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
            total_outfits_written += 1
        f.flush()

        # Un solo contatore per tutta la generazione: e' cosi' che la rotazione
        # fra pari merito distribuisce capispalla e accessori sull'intero
        # guardaroba invece che dentro una singola ancora. Parte da quello che
        # il pool ereditato usa gia', altrimenti i capi consumati dai run
        # precedenti ripartirebbero da zero e vincerebbero di nuovo.
        contatore_uso = collections.Counter(
            v["relpath"] for o in ereditati for s, v in o["slots"].items()
            if v and s in OPTIONAL_SLOTS)

        # Menu completo di ogni ancora, calcolato una volta sola: la beam
        # search e' la parte cara, l'ammissione e' aritmetica su insiemi.
        menu = []
        for anchor_slot in MANDATORY_SLOTS:
            anchor_pool = df[(df["slot"] == anchor_slot) & (~df["needs_vision_review"].astype(bool))]
            anchor_pool = anchor_pool.sort_values("relpath")
            n = len(anchor_pool)
            print(f"[*] Slot ancora '{anchor_slot}': {n} candidati...", flush=True)

            for i, (_, anchor) in enumerate(anchor_pool.iterrows(), 1):
                total_anchors_tried += 1
                proposte = build_outfits(df, anchor["relpath"], w_colore, w_stile,
                                          beam_width, candidates_per_step,
                                          varianti=beam_width,
                                          contatore_uso=contatore_uso)
                proposte = [o for o in proposte if o["outfit_score"] >= min_score]
                if proposte:
                    menu.append((anchor_slot, anchor, proposte))
                if i % 200 == 0 or i == n:
                    print(f"    [{anchor_slot} {i}/{n}] ancore con proposte: {len(menu)}", flush=True)

        # ORDINE, non selezione. Nel pool finisce tutto quello che e' valido,
        # quasi-doppioni compresi: quello che l'indice decide e' soltanto QUANDO.
        #
        # Le passate mettono per primo l'outfit migliore di ogni ancora, poi il
        # secondo, e ogni volta solo se e' lontano da tutto il resto: cosi' i
        # primi migliaia di outfit coprono il catalogo il piu' possibile. Quello
        # che le passate scartano non e' perso -- resta nel menu e viene scritto
        # in coda, quando lo spazio distinto e' finito.
        firme_esatte = {frozenset(v["relpath"] for v in o["slots"].values() if v)
                        for o in ereditati}

        def _scrivi(anchor_slot, anchor, outfit):
            relpaths = [v["relpath"] for v in outfit["slots"].values() if v]
            record = {
                "outfit_id": build_outfit_id(relpaths),
                "label": _build_outfit_label(anchor.get("gender"), outfit["slots"]),
                "gender": anchor.get("gender"),
                "outfit_score": outfit["outfit_score"],
                "anchor_slot": anchor_slot,
                "anchor_relpath": anchor["relpath"],
                "slots": _serialize_slots(outfit["slots"]),
                "pairwise_scores": outfit["pairwise_scores"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        presi = [set() for _ in menu]
        for passata in range(1, max(1, varianti_per_ancora) + 1):
            ammessi_passata = 0
            for m, (anchor_slot, anchor, proposte) in enumerate(menu):
                for n_prop, outfit in enumerate(proposte):
                    if n_prop in presi[m]:
                        continue
                    firma = frozenset(v["relpath"] for v in outfit["slots"].values() if v)
                    if firma in firme_esatte:
                        presi[m].add(n_prop)  # gia' nel pool: mai piu' da guardare
                        continue
                    if not indice.e_nuovo(firma):
                        continue  # troppo simile: lo riprende la coda
                    indice.aggiungi(firma)
                    firme_esatte.add(firma)
                    presi[m].add(n_prop)
                    _scrivi(anchor_slot, anchor, outfit)
                    total_outfits_written += 1
                    ammessi_passata += 1
                    break
            f.flush()
            per_passata.append(ammessi_passata)
            print(f"[*] Passata {passata}: {ammessi_passata} outfit distinti "
                  f"(pool a {total_outfits_written})", flush=True)
            if not ammessi_passata:
                break  # nessuno spazio distinto rimasto: le passate dopo sono vuote

        # Coda: tutto il resto del menu. Qui l'unico filtro e' l'identita' --
        # due volte lo stesso identico insieme di capi sarebbe lo stesso
        # outfit_id, non una proposta in piu'.
        in_coda = 0
        for m, (anchor_slot, anchor, proposte) in enumerate(menu):
            for n_prop, outfit in enumerate(proposte):
                if n_prop in presi[m]:
                    continue
                firma = frozenset(v["relpath"] for v in outfit["slots"].values() if v)
                if firma in firme_esatte:
                    continue
                firme_esatte.add(firma)
                _scrivi(anchor_slot, anchor, outfit)
                total_outfits_written += 1
                in_coda += 1
        f.flush()
        print(f"[*] Coda (quasi-doppioni validi): {in_coda} outfit "
              f"(pool a {total_outfits_written})", flush=True)


    stats = {
        "ancore_processate": total_anchors_tried,
        "outfit_ereditati": len(ereditati),
        "outfit_scartati_dal_pool_vecchio": scartati,
        "outfit_nuovi": total_outfits_written - len(ereditati),
        "nuovi_per_passata": per_passata,
        "nuovi_in_coda": in_coda,
        "outfit_unici_scritti": total_outfits_written,
        "output": out_jsonl,
    }
    print(f"\n[OK] Pipeline completata: {stats}", flush=True)
    return stats


print(
    "Modulo caricato (Fase 6 — generazione outfit). Esempio:\n"
    "df = load_and_prepare('/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet')\n"
    "outfits = generate_outfits(df, n_outfits=10)\n"
    "# oppure, per il pool completo (top/bottom/scarpe come ancora, JSONL):\n"
    "run_outfit_pipeline(df, out_jsonl='outfits_pool.jsonl')"
)

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

import re

import pandas as pd

from scoring import score_pair

GARMENT_TYPE_KEYWORDS = {
    "top": ["camicia", "camicie", "t-shirt", "tshirt", "maglia", "maglie", "maglietta",
            "polo", "canotta", "canotte", "felpa", "felpe", "top", "blusa", "camicetta",
            "body", "overshirt", "maglione", "pullover", "pull", "cardigan"],
    "bottom": ["pantalone", "pantaloni", "jeans", "bermuda", "short", "shorts", "gonna",
               "leggings", "pantaloncini"],
    "outerwear": ["giacca", "giacche", "giubbotto", "giubbotti", "cappotto", "piumino",
                  "bomber", "parka", "trench", "blazer", "gilet", "tracktop", "anorak",
                  "jkt", "windbreaker", "giaccone"],
    "shoes": ["scarpe", "scarpa", "sneakers", "sneaker", "stivaletto", "stivaletti",
              "mocassino", "mocassini", "sandali", "sandalo", "ciabatte", "stringate",
              "ballerine"],
    "accessory": ["borsa", "tracolla", "zaino", "cintura", "cappello", "sciarpa", "guanti",
                  "occhiali", "portafoglio", "calzini", "cravatta", "berretto"],
    # abiti/completi/costumi sono "capi unici" (coprono da soli top+bottom):
    # fuori scope per la generazione a slot in questa v1, vedi nota sotto.
    "dress_or_suit": ["abito", "vestito", "tuta", "completo", "costume"],
}

MANDATORY_SLOTS = ["top", "bottom", "shoes"]
OPTIONAL_SLOTS = ["outerwear", "accessory"]
OPTIONAL_SLOT_MIN_SCORE = 0.5  # sotto questa soglia, meglio niente accessorio/outerwear che uno stonato


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


def classify_gender(title: str, relpath: str):
    t = (title or "").lower()
    if re.search(r"\bdonna\b", t):
        return "donna"
    if re.search(r"\buomo\b", t):
        return "uomo"
    # fallback sul percorso di categoria, meno affidabile del titolo
    if "abbigliamento-donna" in (relpath or ""):
        return "donna"
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
    df["gender"] = df.apply(lambda r: classify_gender(r["title"], r["relpath"]), axis=1)
    df["season"] = df.apply(_season_label, axis=1)
    df["sleeve"] = df.apply(lambda r: classify_sleeve(r["title"], r["relpath"]), axis=1)
    df["leg_length"] = df["title"].apply(classify_leg_length)
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
    return True


def _outerwear_allowed(current_items) -> bool:
    """Regola 2 (utente): giacche/outerwear solo se i pantaloni sono lunghi
    — niente giacca sui pantaloncini. (La parte "niente felpe sui
    pantaloncini" della stessa regola è già coperta dalla Regola 1, perché
    una felpa ha sempre manica lunga per tipo di capo.)"""
    existing_bottom_leg = _existing_value(current_items, "bottom", "leg_length")
    return existing_bottom_leg != "corta"


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
    candidates = candidates[candidates["gender"].apply(lambda g: _gender_compatible(g, anchor_row["gender"]))]
    candidates = candidates[candidates["season"].apply(lambda s: _season_compatible(s, anchor_row["season"]))]
    candidates = candidates[candidates["style_cluster"].apply(lambda c: _cluster_compatible(c, anchor_row["style_cluster"]))]
    if current_items:
        candidates = candidates[candidates["formality_norm"].apply(lambda f: _formality_ok(current_items, f))]
        if slot in ("top", "bottom"):
            candidates = candidates[
                candidates.apply(lambda r: _seasonal_coherence_ok(current_items, slot, r), axis=1)
            ]
    return candidates


def build_outfit(df: pd.DataFrame, anchor_relpath: str, w_colore: float = 0.5, w_stile: float = 0.5,
                  beam_width: int = 5, candidates_per_step: int = 30) -> dict:
    """Costruisce un outfit a partire da un capo ancora, via beam search
    slot per slot. Ritorna None se l'ancora non ha uno slot riconosciuto o
    se non si riesce a riempire tutti gli slot obbligatori."""
    anchor_rows = df[df["relpath"] == anchor_relpath]
    if anchor_rows.empty:
        return None
    anchor = anchor_rows.iloc[0]
    anchor_slot = anchor["slot"]
    if anchor_slot not in MANDATORY_SLOTS:
        return None

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
            return None  # nessun candidato disponibile per questo slot -> outfit non completabile
        new_beam.sort(key=lambda t: t[2], reverse=True)
        beam = [(items, used) for items, used, _ in new_beam[:beam_width]]

    # slot obbligatori riempiti: proviamo ad aggiungere quelli opzionali al miglior candidato del beam
    best_items, best_used = beam[0]

    for slot in OPTIONAL_SLOTS:
        if slot == "outerwear" and not _outerwear_allowed(best_items):
            continue  # Regola 2: niente giacca/outerwear sui pantaloncini
        candidates = candidates_for_slot(df, slot, anchor, best_used, require_real_cluster=True,
                                          current_items=best_items)
        if candidates.empty:
            continue
        candidates = candidates.copy()
        candidates["_score"] = candidates.apply(
            lambda r: _min_pairwise(best_items + [(slot, r)]), axis=1
        )
        best_candidate = candidates.loc[candidates["_score"].idxmax()]
        if best_candidate["_score"] >= OPTIONAL_SLOT_MIN_SCORE:
            best_items = best_items + [(slot, best_candidate)]
            best_used = best_used | {best_candidate["relpath"]}

    # punteggi pairwise finali, per trasparenza/debug
    pairwise_scores = {}
    for i in range(len(best_items)):
        for j in range(i + 1, len(best_items)):
            slot_i, row_i = best_items[i]
            slot_j, row_j = best_items[j]
            pairwise_scores[f"{slot_i}-{slot_j}"] = score_pair(row_i, row_j, w_colore, w_stile)["score"]

    outfit_score = min(pairwise_scores.values()) if pairwise_scores else 1.0

    slots_out = {slot: None for slot in MANDATORY_SLOTS + OPTIONAL_SLOTS}
    for slot, row in best_items:
        slots_out[slot] = {
            "relpath": row["relpath"],
            "title": row["title"],
            "url": row["url"],
            "display_image": row.get("display_image"),
            "all_images": row.get("all_images"),
        }

    return {"slots": slots_out, "pairwise_scores": {k: round(v, 3) for k, v in pairwise_scores.items()},
            "outfit_score": round(outfit_score, 3)}


def generate_outfits(df: pd.DataFrame, n_outfits: int = 10, anchor_slot: str = "top",
                      min_score: float = 0.6, w_colore: float = 0.5, w_stile: float = 0.5,
                      beam_width: int = 5, candidates_per_step: int = 30, random_state: int = 0) -> list:
    """Genera outfit a partire da capi ancora campionati dallo slot indicato
    (default 'top', lo slot più naturale da cui partire). Ritorna una lista
    di outfit con outfit_score >= min_score, ordinata dal migliore."""
    anchor_pool = df[(df["slot"] == anchor_slot) & (~df["needs_vision_review"].astype(bool))]
    anchors = anchor_pool.sample(
        n=min(n_outfits * 4, len(anchor_pool)), random_state=random_state
    )

    outfits = []
    for _, anchor in anchors.iterrows():
        outfit = build_outfit(df, anchor["relpath"], w_colore, w_stile, beam_width, candidates_per_step)
        if outfit and outfit["outfit_score"] >= min_score:
            outfits.append(outfit)
        if len(outfits) >= n_outfits:
            break

    outfits.sort(key=lambda o: o["outfit_score"], reverse=True)
    return outfits


print(
    "Modulo caricato (Fase 6 — generazione outfit). Esempio:\n"
    "df = load_and_prepare('/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet')\n"
    "outfits = generate_outfits(df, n_outfits=10)"
)

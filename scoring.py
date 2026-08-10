#!/usr/bin/env python3
"""
Nuvolari — Fase 5: scoring di compatibilità
==============================================

Funzione pairwise score(A,B) = w_colore · color_harmony(A,B) + w_stile · style_match(A,B)
tra due prodotti, usata in Fase 6 per decidere quali capi abbinare.

color_harmony: regole di teoria del colore in spazio Lab/LCh (L=luminosità,
C=croma/saturazione, h=tonalità sul cerchio 0-360°):
  - un colore "neutro" (croma basso: nero/bianco/grigio/beige) va bene quasi
    con tutto — bonus alto indipendentemente dall'altro colore
  - due colori quasi identici ma non uguali (stessa tonalità, ΔE piccolo ma
    non zero) vengono penalizzati: sembra un abbinamento sbagliato per
    sbaglio, non una scelta di stile (es. due neri leggermente diversi)
  - due colori davvero identici (ΔE ~ 0) sono invece un monocromatico
    intenzionale — bonus
  - tonalità vicine (analoghi) o opposte sul cerchio (complementari) —
    bonus, sono le combinazioni classiche della teoria del colore
  - tutto il resto (zona intermedia, senza una regola forte) — punteggio
    neutro/medio

style_match: coseno tra i vettori stile (Fase 3) meno una penalità se la
differenza di formality_score tra i due capi è troppo grande (es. non ha
senso abbinare un blazer sartoriale con pantaloncini da running anche se
condividono qualche tag di stile).

Uso in notebook
-----------------
    %run scoring.py

    df = pd.read_parquet('features_clustered.parquet')
    a, b = df.iloc[10], df.iloc[57]
    result = score_pair(a, b)
    print(result)  # {'color_harmony':..., 'style_match':..., 'score':...}
"""

import json
import math

import numpy as np
import pandas as pd

from clustering import STYLE_TAG_COLUMNS

# --- color_harmony: soglie tarate sulla distribuzione di croma del catalogo
# (mediana ~5, 75° percentile ~12 — un catalogo di abbigliamento generalista
# è dominato da colori tenui/neutri, non da tinte sature) ---
NEUTRAL_CHROMA = 10.0
IDENTICAL_DELTA_E = 3.0
CLASH_DELTA_E_HIGH = 15.0
HUE_MONOCHROME = 20.0
HUE_ANALOGOUS = 60.0
HUE_COMPLEMENTARY = 150.0


def hue_circular_distance(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def color_harmony_pair(lab_a, lab_b) -> float:
    """Punteggio di armonia [0-1] tra due colori Lab, via regole di teoria
    del colore sul cerchio delle tonalità (vedi docstring del modulo)."""
    La, aa, ba = lab_a
    Lb, ab_, bb = lab_b

    delta_e = math.sqrt((La - Lb) ** 2 + (aa - ab_) ** 2 + (ba - bb) ** 2)
    Ca = math.hypot(aa, ba)
    Cb = math.hypot(ab_, bb)

    ha = math.degrees(math.atan2(ba, aa)) % 360
    hb = math.degrees(math.atan2(bb, ab_)) % 360
    dh = hue_circular_distance(ha, hb)

    if delta_e < IDENTICAL_DELTA_E:
        hue_score = 0.85  # colori praticamente identici -> monocromatico intenzionale
    elif dh < HUE_MONOCHROME:
        hue_score = 0.25 if delta_e < CLASH_DELTA_E_HIGH else 0.80  # "quasi uguale ma non uguale" vs monocromatico vero
    elif dh < HUE_ANALOGOUS:
        hue_score = 0.75  # tonalità vicine -> analogo
    elif dh > HUE_COMPLEMENTARY:
        hue_score = 0.70  # tonalità opposte -> complementare
    else:
        hue_score = 0.45  # zona intermedia, nessuna regola forte della teoria del colore

    # un colore neutro (poco saturo: nero/bianco/grigio/beige) va bene con
    # quasi tutto — ma "poco saturo" non è un interruttore netto: un grigio
    # puro (croma ~0) è neutro davvero, mentre un grigio-blu tenue (croma
    # appena sotto soglia) ha comunque un sottotono freddo riconoscibile che
    # può stonare con toni caldi (es. sabbia) anche restando desaturato.
    # Il bonus "neutro" viene quindi sfumato in base a quanto il colore meno
    # saturo dei due si avvicina al grigio puro, non applicato in blocco.
    min_chroma = min(Ca, Cb)
    if min_chroma >= NEUTRAL_CHROMA:
        return hue_score
    neutral_weight = 1 - (min_chroma / NEUTRAL_CHROMA)
    return neutral_weight * 0.90 + (1 - neutral_weight) * hue_score


def color_harmony(palette_a, palette_b, top_k: int = 2) -> float:
    """Media pesata di color_harmony_pair sui top_k colori di ogni palette,
    pesata dal prodotto delle proporzioni — così un colore minoritario in
    una foto (es. un dettaglio) conta meno di quello dominante."""
    pa = palette_a[:top_k]
    pb = palette_b[:top_k]
    total_score, total_weight = 0.0, 0.0
    for ca in pa:
        for cb in pb:
            w = ca["proportion"] * cb["proportion"]
            total_score += w * color_harmony_pair(ca["lab"], cb["lab"])
            total_weight += w
    return total_score / total_weight if total_weight > 0 else 0.5


REFERENCE_STYLE_NORM = 0.55  # ~mediana della norma dei vettori stile nel catalogo (vedi analisi)


def style_match(style_vec_a, style_vec_b, formality_a: float, formality_b: float,
                 formality_penalty_weight: float = 0.5) -> float:
    """Coseno tra vettori stile, penalizzato dalla distanza di formalità E
    smorzato se uno dei due capi ha un segnale di stile debole.

    Il coseno da solo guarda solo la DIREZIONE del vettore, non la sua
    "forza": un capo il cui testo non dice quasi nulla di distintivo (es.
    un panciotto con description_text scarna, tutti i tag vicini a zero)
    può comunque risultare puntare "nella stessa direzione" di un capo dal
    segnale forte e ottenere un coseno altissimo — sembrando compatibile
    con qualunque cosa, anche quando in realtà semplicemente non sappiamo
    cosa sia quel capo stilisticamente. Smorzando per la norma minima dei
    due vettori (rispetto alla mediana del catalogo), un capo dal segnale
    debole non viene più trattato come "jolly" automatico.
    """
    a = np.asarray(style_vec_a, dtype=float)
    b = np.asarray(style_vec_b, dtype=float)
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    cos = float(np.dot(a, b) / (norm_a * norm_b)) if norm_a > 0 and norm_b > 0 else 0.0

    confidence = min(1.0, min(norm_a, norm_b) / REFERENCE_STYLE_NORM)
    cos *= confidence

    formality_diff = abs(formality_a - formality_b)  # formality_norm è già 0-1
    penalty = formality_penalty_weight * formality_diff
    return max(0.0, cos - penalty)


def score_pair(row_a: pd.Series, row_b: pd.Series, w_colore: float = 0.5, w_stile: float = 0.5,
               use_palette: bool = True) -> dict:
    """score(A,B) completo tra due righe di features_clustered.parquet."""
    if use_palette:
        palette_a = json.loads(row_a["color_palette"]) if isinstance(row_a["color_palette"], str) else row_a["color_palette"]
        palette_b = json.loads(row_b["color_palette"]) if isinstance(row_b["color_palette"], str) else row_b["color_palette"]
        color_h = color_harmony(palette_a, palette_b)
    else:
        color_h = color_harmony_pair((row_a["L"], row_a["a"], row_a["b"]), (row_b["L"], row_b["a"], row_b["b"]))

    style_vec_a = row_a[STYLE_TAG_COLUMNS].to_numpy(dtype=float)
    style_vec_b = row_b[STYLE_TAG_COLUMNS].to_numpy(dtype=float)
    style_m = style_match(style_vec_a, style_vec_b, row_a["formality_norm"], row_b["formality_norm"])

    total = w_colore * color_h + w_stile * style_m
    return {"color_harmony": round(color_h, 3), "style_match": round(style_m, 3), "score": round(total, 3)}


print(
    "Modulo caricato (Fase 5 — scoring di compatibilità). Esempio:\n"
    "df = pd.read_parquet('/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet')\n"
    "a, b = df.iloc[10], df.iloc[57]\n"
    "score_pair(a, b)"
)

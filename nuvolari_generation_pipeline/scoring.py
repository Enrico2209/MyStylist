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
# In spazio Lab le coppie complementari classiche non arrivano a 180°: blu e
# arancio, l'esempio da manuale, distano 135°. Con la soglia a 150 la regola
# non scattava quasi mai e il 9,9% delle coppie sature del catalogo — i
# complementari veri — finiva nella zona intermedia a 0,45, cioè valutato come
# un abbinamento senza qualità. Misurato su 312 coppie campionate.
HUE_COMPLEMENTARY = 130.0
# Fra l'analogo e il complementare c'è la relazione split-complementare, che in
# teoria del colore è un abbinamento riconosciuto (verde oliva e bordeaux):
# merita più della zona senza regole, meno del complementare pieno.
HUE_SPLIT_COMPLEMENTARY = 100.0

# Due colori entrambi scuri che differiscono solo per sottotono — il caso
# nero + blu notte — stonano come due blu leggermente diversi: la differenza
# si legge come errore di illuminazione, non come scelta.
#
# Perché sia uno stono servono due cose insieme, ed entrambe sono questioni di
# grado, non interruttori: i due devono essere abbastanza vicini di luminosità
# da sembrare voler essere lo stesso colore, e il sottotono che li separa deve
# vedersi. La versione precedente le trattava come sì/no e chiedeva la cosa
# sbagliata sulla seconda — la croma di UNO dei due invece della distanza FRA i
# due — con l'effetto che due giubbotti blu notte identici all'occhio, croma
# 4,57 e 5,27, prendevano 0,744 e 0,300 contro la stessa felpa nera.
DARK_L_MAX = 40.0             # oltre questa luminosità il colore non è più "scuro"
DARK_L_SIMILE = 12.0          # sotto questo stacco i due sembrano voler essere lo stesso colore
DARK_L_DELTA = 25.0           # oltre questo stacco la coppia si legge come scelta, non errore
UNDERTONE_DELTA_MIN = 4.0     # distanza cromatica sotto cui il sottotono non si vede
UNDERTONE_DELTA_PIENO = 12.0  # ...e sopra cui si vede senza dubbi
STONO_SCURI = 0.30            # il verdetto pieno, quando entrambe le condizioni sono al massimo


def hue_circular_distance(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 360
    return min(d, 360 - d)


def stono_da_sottotono(La, Lb, aa, ba, ab, bb) -> float:
    """Quanto la coppia di scuri è il caso nero+blu notte, da 0 (per niente) a
    1 (in pieno). Continua e non a gradini: la differenza fra due capi che si
    somigliano non deve dipendere da quale lato di una soglia cadono.

    Due fattori, moltiplicati perché servono entrambi:
      - quanto si vede il sottotono: la distanza cromatica FRA i due colori,
        cioè la distanza nel piano a-b. Non la croma di uno solo: due blu
        notte con la stessa identica tinta hanno croma alta ed è comunque un
        monocromatico, non uno sbaglio.
      - quanto sembra voluto: se i due hanno quasi la stessa luminosità
        l'occhio li legge come un tentativo di abbinare fallito; man mano che
        lo stacco cresce la coppia si legge come due profondità scelte apposta.
    """
    if max(La, Lb) >= DARK_L_MAX:
        return 0.0
    stacco_luce = abs(La - Lb)
    if stacco_luce >= DARK_L_DELTA:
        return 0.0

    distanza_cromatica = math.hypot(aa - ab, ba - bb)
    si_vede = (distanza_cromatica - UNDERTONE_DELTA_MIN) / (UNDERTONE_DELTA_PIENO - UNDERTONE_DELTA_MIN)
    si_vede = min(1.0, max(0.0, si_vede))

    if stacco_luce <= DARK_L_SIMILE:
        sembra_voluto = 1.0
    else:
        sembra_voluto = (DARK_L_DELTA - stacco_luce) / (DARK_L_DELTA - DARK_L_SIMILE)
    return si_vede * sembra_voluto


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
    elif dh > HUE_SPLIT_COMPLEMENTARY:
        hue_score = 0.60  # split-complementare: relazione riconosciuta, più debole
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
    bonus_neutro = neutral_weight * 0.90 + (1 - neutral_weight) * hue_score

    # Eccezione al bonus neutro: due scuri che si distinguono solo per il
    # sottotono. È il nero con il blu notte — il colore neutro c'è (croma ~0)
    # ma non "assorbe" nulla, perché senza stacco di luminosità l'occhio legge
    # solo la differenza di sottotono, e la legge come sbaglio. Lo stesso caso
    # fra due colori saturi è già penalizzato sopra a 0,25; senza questa
    # eccezione il bonus neutro lo premiava invece a 0,90.
    stono = stono_da_sottotono(La, Lb, aa, ba, ab_, bb)
    return (1 - stono) * bonus_neutro + stono * STONO_SCURI


# Sotto questo valore, color_harmony_pair sta segnalando uno stono vero e
# proprio: il "quasi uguale ma non uguale" (0,25) e i due scuri distinti solo
# dal sottotono (0,30). Non sono punteggi bassi qualsiasi, sono verdetti.
SOGLIA_STONO = 0.30


def color_harmony(palette_a, palette_b, top_k: int = 2) -> float:
    """Media pesata di color_harmony_pair sui top_k colori di ogni palette,
    pesata dal prodotto delle proporzioni — così un colore minoritario in
    una foto (es. un dettaglio) conta meno di quello dominante.

    Con un'eccezione: se a stonare sono i due colori DOMINANTI, la media non
    si applica e vale il loro punteggio. I dominanti sono quelli che l'occhio
    legge a distanza, e non vengono riscattati dal fatto che i secondari
    vadano d'accordo. È lo stesso motivo per cui il punteggio dell'outfit è
    il minimo delle coppie e non la media: la media nasconde l'anello debole.

    Senza questa eccezione il nero con il blu notte prendeva 0,30 sulla coppia
    dominante e risaliva a 0,61 di media, perché i secondari chiari fra loro
    prendevano 0,80 — e l'abbinamento restava sopra soglia.
    """
    dominante = color_harmony_pair(palette_a[0]["lab"], palette_b[0]["lab"])
    if dominante <= SOGLIA_STONO:
        return dominante

    pa = palette_a[:top_k]
    pb = palette_b[:top_k]
    total_score, total_weight = 0.0, 0.0
    for ca in pa:
        for cb in pb:
            w = ca["proportion"] * cb["proportion"]
            total_score += w * color_harmony_pair(ca["lab"], cb["lab"])
            total_weight += w
    return total_score / total_weight if total_weight > 0 else 0.5


# Soglia di norma sotto cui un capo è considerato "poco caratterizzato":
# per disegno è la mediana della norma dei vettori stile nel catalogo, così
# circa metà dei capi riceve un po' di smorzamento (vedi style_match).
#
# Rimisurata dopo la revisione visiva su tutti i 2901 capi e la ripesatura
# radice-IDF: 0.687 (era 0.574 sui soli vettori testuali). Il significato è
# cambiato in meglio: prima norma bassa voleva dire "scheda muta, non
# sappiamo cosa sia"; ora vuol dire "ha solo tratti comuni a tutti", perché
# i tag rari pesano di più. Resta giusto smorzare: di un capo che è solo
# genericamente casual sappiamo davvero poco di distintivo.
REFERENCE_STYLE_NORM = 0.69

# Coseno mediano fra due capi presi a caso nel catalogo: è, alla lettera, il
# valore di "non ne so niente". Verso questo si smorza un capo dal segnale
# debole, invece che verso zero (vedi style_match).
#
# Rimisurato su 20.000 coppie dopo revisione visiva e radice-IDF: 0.401
# (era 0.60 sui soli vettori testuali). Senza la ripesatura sarebbe stato
# 0.71, perché il tono di fondo "casual" della visione — presente sul 96% dei
# capi — rendeva tutto simile a tutto.
COSENO_NEUTRO = 0.40


# --------------------------------------------------------------------------
# Affinita' fra registri stilistici
# --------------------------------------------------------------------------
# Il coseno secco misura quanto due capi occupano GLI STESSI registri, non
# quanto i loro registri stanno bene insieme. Con cinque assi ortogonali per
# costruzione, ogni abbinamento misto veniva punito: sneaker (sportivo 0.7) e
# jeans (streetwear 0.6) davano coseno 0.219, cioe' l'abbinamento piu' comune
# del campionario risultava fra i peggiori. Misurato su coppie casuali, tutta
# la diagonale stava fra 0.80 e 0.94 e tutto il resto fra 0.03 e 0.41.
#
# Con questa matrice il prodotto diventa x'Ay: le caselle dicono quanto due
# registri convivono. I valori sono una scelta di gusto, non una misura --
# 'casual' e' il ponte verso tutto perche' e' cosi' che e' fatto il
# campionario (2397 capi su 2901 lo hanno come dominante), ed 'elegante' e'
# l'unico registro che si chiude davvero.
AFFINITA_REGISTRI = {
    ("sportivo",   "casual"):     0.85,
    ("sportivo",   "elegante"):   0.15,
    ("sportivo",   "streetwear"): 0.80,
    ("sportivo",   "da_mare"):    0.60,
    ("casual",     "elegante"):   0.65,
    ("casual",     "streetwear"): 0.85,
    ("casual",     "da_mare"):    0.75,
    ("elegante",   "streetwear"): 0.25,
    ("elegante",   "da_mare"):    0.20,
    ("streetwear", "da_mare"):    0.55,
}


# Coseno pesato piu' basso osservato fra due capi del catalogo: sotto questo
# valore la scala e' vuota. Rimisurare se la matrice cambia.
PAVIMENTO_AFFINITA = 0.40


def _matrice_affinita():
    """Costruita dall'ordine di STYLE_TAG_COLUMNS, non da un ordine scritto a
    mano: se un tag cambia posto la matrice segue, invece di scambiare due
    registri in silenzio."""
    tag = [c[len("style_"):] for c in STYLE_TAG_COLUMNS]
    n = len(tag)
    A = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            v = AFFINITA_REGISTRI.get((tag[i], tag[j]))
            if v is None:
                v = AFFINITA_REGISTRI.get((tag[j], tag[i]))
            if v is None:
                raise KeyError(f"affinita' mancante fra {tag[i]} e {tag[j]}")
            A[i, j] = A[j, i] = float(v)
    return A


MATRICE_AFFINITA = _matrice_affinita()


def coseno_pesato(a, b, A=None) -> float:
    """Coseno generalizzato x'Ay / sqrt(x'Ax * y'By). Vale 1 su vettori
    identici come il coseno normale, ma due registri diversi e affini non
    danno piu' zero."""
    A = MATRICE_AFFINITA if A is None else A
    na = float(a @ A @ a)
    nb = float(b @ A @ b)
    if na <= 0 or nb <= 0:
        return 0.0
    grezzo = float(a @ A @ b) / math.sqrt(na * nb)

    # Il coseno pesato comprime tutto in alto: misurato su 20.000 coppie
    # casuali dava mediana 0,955 e sd 0,086, cioe' l'ordine giusto ma nessuna
    # capacita' di distinguere. Le caselle piene della matrice alzano il
    # pavimento, non il soffitto, e sotto PAVIMENTO_AFFINITA non ci arriva
    # nessuno: quella parte di scala non porta informazione.
    #
    # Il quadrato non cambia l'ordine -- lo decide la matrice -- ma riporta la
    # dispersione a quella del coseno secco, cosi' la soglia degli slot
    # opzionali e il peso della penalita' di formalita' restano tarati.
    steso = (grezzo - PAVIMENTO_AFFINITA) / (1.0 - PAVIMENTO_AFFINITA)
    return max(0.0, min(1.0, steso)) ** 2


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

    Lo smorzamento però tira verso COSENO_NEUTRO, non verso zero. Moltiplicare
    e basta affermava una cosa che non sappiamo: che i due capi sono lontani.
    Una felpa e un jeans con vettori IDENTICI (coseno 1,000) e la stessa
    formalità uscivano a 0,485, perché la felpa ha una scheda scarna — 46% del
    catalogo sta sotto la soglia e prendeva lo stesso trattamento. Ora un capo
    poco descritto finisce al valore che avrebbe con un capo qualsiasi: non
    diventa un jolly che batte tutti, e non viene nemmeno dichiarato
    incompatibile con qualcosa con cui va d'accordo.
    """
    a = np.asarray(style_vec_a, dtype=float)
    b = np.asarray(style_vec_b, dtype=float)
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    cos = coseno_pesato(a, b) if norm_a > 0 and norm_b > 0 else 0.0

    # Smorzamento disattivato. Presupponeva che una norma bassa volesse dire
    # "non sappiamo cosa sia questo capo": vero finche' i tag erano bandierine
    # indipendenti ricavate dal testo, e una scheda muta lasciava il vettore
    # quasi a zero. Ora i punteggi sono una ripartizione che somma sempre a 1,
    # assegnata dalla revisione visiva su tutti e 2901 i capi: ogni vettore
    # porta la stessa massa e la norma misura solo QUANTO E' SPARSA la mistura,
    # non quanto ne sappiamo. Smorzare avrebbe colpito 368 capi, cioe' proprio
    # quelli a mistura voluta -- le camicie 0,6 casual / 0,4 elegante sono il
    # 25esimo percentile della norma -- appiattendo la distinzione piu' curata
    # che abbiamo. Il codice resta perche' tornerebbe valido se un giorno
    # entrassero in catalogo capi non revisionati.
    if False:  # pragma: no cover
        confidence = min(1.0, min(norm_a, norm_b) / REFERENCE_STYLE_NORM)
        cos = confidence * cos + (1 - confidence) * COSENO_NEUTRO

    formality_diff = abs(formality_a - formality_b)  # formality_norm è già 0-1
    penalty = formality_penalty_weight * formality_diff
    return max(0.0, cos - penalty)


# Colonne precalcolate da load_and_prepare (vedi outfit_generation). Non sono
# dati nuovi: sono gli stessi valori già pronti nella forma in cui servono.
# Il motivo è misurato — profilando la generazione del pool, il 70% del tempo
# se ne andava dentro pandas a rileggere per etichetta gli stessi dieci numeri
# a ogni confronto, e i confronti sono milioni.
COLONNA_VETTORE = "_vettore_stile"
COLONNA_PALETTE = "_palette"


def _vettore_stile(row: pd.Series):
    pronto = row.get(COLONNA_VETTORE)
    if pronto is not None:
        return pronto
    return row[STYLE_TAG_COLUMNS].to_numpy(dtype=float)


def _palette(row: pd.Series):
    pronta = row.get(COLONNA_PALETTE)
    if pronta is not None:
        return pronta
    grezza = row["color_palette"]
    return json.loads(grezza) if isinstance(grezza, str) else grezza


# Due fantasie forti addosso alla stessa persona si contendono lo sguardo: e'
# la regola di stile piu' vecchia che ci sia, e finora il campo "pattern" —
# calcolato per tutti i capi da Fase 2 e dalla revisione visiva — non lo leggeva
# nessuno.
#
# La forza dice quanto la fantasia si impone: le righe si portano sotto una
# giacca a quadri e non succede niente, un animalier e un camouflage insieme
# sono un'altra cosa. Una tinta unita vale 0, quindi basta che UNO dei due capi
# sia tinta unita perche' la penalita' sparisca del tutto: e' il caso normale,
# e non deve pagare nulla.
FANTASIA_FORZA = {
    "tinta_unita": 0.0,
    "righe": 0.45,
    "quadri": 0.55,
    "stampato": 0.85,
    "camouflage": 0.90,
    "animalier": 1.00,
}
PENALITA_FANTASIE = 0.22   # sottratta al punteggio quando entrambe sono al massimo


def stono_da_fantasie(pattern_a: str, pattern_b: str) -> float:
    """Quanto le due fantasie si contendono lo sguardo, da 0 a 1.

    E' il PRODOTTO delle due forze, non la somma: serve che siano forti
    entrambe. Un capo a righe con una tinta unita non e' un errore, e con la
    somma lo sarebbe stato a meta'.
    """
    fa = FANTASIA_FORZA.get(pattern_a, 0.0)
    fb = FANTASIA_FORZA.get(pattern_b, 0.0)
    return fa * fb


def score_pair(row_a: pd.Series, row_b: pd.Series, w_colore: float = 0.5, w_stile: float = 0.5,
               use_palette: bool = True) -> dict:
    """score(A,B) completo tra due righe di features_clustered.parquet."""
    if use_palette:
        color_h = color_harmony(_palette(row_a), _palette(row_b))
    else:
        color_h = color_harmony_pair((row_a["L"], row_a["a"], row_a["b"]), (row_b["L"], row_b["a"], row_b["b"]))

    style_m = style_match(_vettore_stile(row_a), _vettore_stile(row_b),
                          row_a["formality_norm"], row_b["formality_norm"])

    stono = stono_da_fantasie(row_a.get("pattern"), row_b.get("pattern"))
    total = w_colore * color_h + w_stile * style_m - PENALITA_FANTASIE * stono
    total = max(0.0, min(1.0, total))
    return {"color_harmony": round(color_h, 3), "style_match": round(style_m, 3),
            "stono_fantasie": round(stono, 3), "score": round(total, 3)}


print(
    "Modulo caricato (Fase 5 — scoring di compatibilità). Esempio:\n"
    "df = pd.read_parquet('/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet')\n"
    "a, b = df.iloc[10], df.iloc[57]\n"
    "score_pair(a, b)"
)

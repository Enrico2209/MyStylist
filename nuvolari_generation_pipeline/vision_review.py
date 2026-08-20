#!/usr/bin/env python3
"""
Nuvolari — revisione visiva dei capi in quarantena
====================================================

Chiude la promessa del flag needs_vision_review: 422 capi (14,5% del
catalogo) stavano fuori da ogni outfit perché il loro testo non diceva nulla
di distintivo sullo stile — non perché fossero capi sbagliati. La foto però
c'è sempre stata: questo modulo la mostra a Gemini e si fa dire lo stile.

Per ogni capo in quarantena manda la foto di presentazione + titolo a
gemini-3.5-flash e riceve un JSON con i punteggi sui 10 tag di stile, più
formalità/stagione/pattern come dati ausiliari. Il risultato finisce in un
blocco "vision_review" dentro metadata.json:

  - separato dagli attributi testuali, così refix_brand_and_attributes può
    rigirare quante volte vuole senza cancellarlo;
  - letto da build_style_vector (feature_engineering), che somma il segnale
    visivo a quello testuale;
  - un capo revisionato esce dalla quarantena: il flag efficace nel parquet
    diventa False (vedi needs_review_effettivo in feature_engineering).

Riprendibile: i capi con blocco vision_review già presente vengono saltati.

Uso:
    python3 vision_review.py --prova     # 3 capi, per vedere le risposte
    python3 vision_review.py             # tutti i capi in quarantena
"""

import argparse
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from google import genai
from google.genai import types

from generate_outfit_images import load_api_key

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
CATALOGO = BASE / "nuvolari_full_organizzato"
MODEL = "gemini-3.5-flash"
FONTE = "foto+descrizione-5tag"   # marca la guida attuale; vedi --rifai
LATO_MASSIMO = 640          # la foto serve a leggere lo stile, non i dettagli
PAUSA = 0.3                 # fra le chiamate
TENTATIVI = 3

STYLE_TAGS = [
    "sportivo", "casual", "elegante", "streetwear", "da_mare",
]

GUIDA = CODICE / "guida_classificazione.md"

# La guida sta in un file a parte, non qui dentro: e' un documento di criteri
# che si legge e si corregge come un testo (quando "casual" finisce di nuovo
# ovunque, si aggiusta li'), e va al modello come system_instruction — quindi
# vale per ogni chiamata senza essere ripetuta in ogni richiesta.
ISTRUZIONI = GUIDA.read_text(encoding="utf-8")

SCHEDA = """TITOLO: {titolo}

DESCRIZIONE DEL NEGOZIO:
{descrizione}

SCHEDA TECNICA:
{tecnica}

ABBINAMENTI CONSIGLIATI DALLA SCHEDA (sono ALTRI capi — servono solo a capire
il registro d'uso, vedi la guida): {abbinamenti}"""


def testo_scheda(metadata: dict) -> str:
    """Il materiale che descrive il capo, nella versione migliore disponibile.

    descrizione_completa viene dalla pagina prodotto e contiene la sezione
    "Come abbinarla?"; description_text e' l'og:description, piu' corta e
    incoerente — resta come ripiego per i capi non ancora ripassati.
    """
    descrizione = (metadata.get("descrizione_completa")
                   or metadata.get("description_text") or "(nessuna descrizione)")
    abbinamenti = [a.get("testo", "") for a in (metadata.get("abbinamenti_suggeriti") or [])]
    return SCHEDA.format(
        titolo=metadata.get("title") or "(senza titolo)",
        descrizione=descrizione[:4000],
        tecnica=metadata.get("scheda_tecnica") or "(non disponibile)",
        abbinamenti=", ".join(a for a in abbinamenti if a) or "(nessuno)")


def foto_ridotta(product_dir: Path):
    """La prima foto leggibile della galleria, ridotta e ricompressa JPEG."""
    for p in sorted(product_dir.iterdir()):
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((LATO_MASSIMO, LATO_MASSIMO), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                return buf.getvalue()
        except Exception:
            continue
    return None


def _primo_oggetto_json(testo: str) -> str:
    """Il primo oggetto JSON bilanciato del testo, chiuso se troncato.

    Serve perche' il modello ogni tanto aggiunge righe dopo l'oggetto, o lo
    tronca a meta'. Il conteggio delle graffe ignora quelle dentro le stringhe:
    contarle alla cieca sbagliava sulle descrizioni che contengono virgolette.
    """
    testo = (testo or "").strip()
    inizio = testo.find("{")
    if inizio < 0:
        return testo
    prof, in_stringa, escape = 0, False, False
    for i in range(inizio, len(testo)):
        c = testo[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif c == '"':
            in_stringa = not in_stringa
        elif not in_stringa:
            if c == "{":
                prof += 1
            elif c == "}":
                prof -= 1
                if prof == 0:
                    return testo[inizio:i + 1]
    # troncato: chiudo quello che resta aperto
    coda = testo[inizio:].rstrip().rstrip(",")
    if in_stringa:
        coda += '"'
    return coda + "}" * prof


def valida_risposta(dati: dict):
    """Normalizza e valida il JSON del modello; None se inutilizzabile."""
    if not isinstance(dati, dict):
        return None
    grezzi = dati.get("style_scores")
    if not isinstance(grezzi, dict):
        return None
    scores = {}
    for tag, v in grezzi.items():
        tag = str(tag).strip().lower()
        if tag in STYLE_TAGS:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                scores[tag] = round(min(v, 1.0), 2)
    if not scores:
        return None
    out = {"style_scores": scores}
    f = dati.get("formality")
    if isinstance(f, (int, float)) and 1 <= f <= 5:
        out["formality"] = int(round(f))
        # La motivazione non serve alla pipeline: serve a poter controllare a
        # campione un giudizio che ora e' delegato, senza rifare la chiamata.
        perche = str(dati.get("formality_perche") or "").strip()
        if perche:
            out["formality_perche"] = perche[:160]
    s = dati.get("season")
    if s in ("estate", "inverno", "mezza_stagione", "tutte"):
        out["season"] = s
    p = dati.get("pattern")
    if p in ("tinta_unita", "righe", "quadri", "camouflage", "stampato", "animalier"):
        out["pattern"] = p
    return out


def revisiona(client, metadata: dict, product_dir: Path):
    foto = foto_ridotta(product_dir)
    if foto is None:
        return None, "nessuna foto leggibile"
    parti = [
        types.Part.from_bytes(data=foto, mime_type="image/jpeg"),
        testo_scheda(metadata),
    ]
    # thinking_budget=0: gemini-3.5-flash di default "ragiona", e i token di
    # ragionamento mangiano il budget di output — le risposte arrivavano
    # TRONCATE a metà JSON ('..."pattern": "tinta_unita' senza graffe finali).
    # Per una classificazione a 10 etichette il ragionamento non compra nulla.
    config = types.GenerateContentConfig(
        system_instruction=ISTRUZIONI,
        response_mime_type="application/json", temperature=0.0,
        max_output_tokens=1500,
        thinking_config=types.ThinkingConfig(thinking_budget=0))
    ultimo_errore = None
    for tentativo in range(1, TENTATIVI + 1):
        try:
            resp = client.models.generate_content(model=MODEL, contents=parti, config=config)
            dati = valida_risposta(json.loads(_primo_oggetto_json(resp.text)))
            if dati is None:
                return None, f"risposta non valida: {resp.text[:120]}"
            return dati, None
        except Exception as e:
            ultimo_errore = str(e)[:160]
            time.sleep(2 * tentativo)
    return None, ultimo_errore


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prova", action="store_true", help="solo 3 capi, risposte in chiaro")
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--rifai", action="store_true",
                    help="rifa' anche i capi gia' revisionati sotto una guida precedente "
                         "(vision_review con fonte diversa da quella attuale): il giudizio "
                         "vecchio nasceva senza descrizione completa e senza il procedimento "
                         "sulla formalita', e la pipeline lo ignora comunque")
    ap.add_argument("--tutti", action="store_true",
                    help="rivede TUTTO il catalogo, non solo la quarantena: il segnale "
                         "visivo si somma a quello testuale per ogni capo (vedi "
                         "build_style_vector), cosi' un capo ben descritto e uno muto "
                         "stanno sulla stessa scala")
    args = ap.parse_args()

    client = genai.Client(api_key=load_api_key(),
                          http_options=types.HttpOptions(timeout=60_000))

    da_fare = []
    for meta_path in sorted(CATALOGO.rglob("metadata.json")):
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        fatto = m.get("vision_review") or {}
        if fatto and not (args.rifai and fatto.get("fonte") != FONTE):
            continue
        if args.tutti or m.get("needs_vision_review"):
            da_fare.append((meta_path, m))
    ambito = "nel catalogo" if args.tutti else "in quarantena"
    print(f"[*] capi {ambito} senza revisione: {len(da_fare)}", flush=True)

    if args.prova:
        da_fare = da_fare[:3]
    elif args.limite:
        da_fare = da_fare[:args.limite]

    ok = errori = 0
    for i, (meta_path, m) in enumerate(da_fare, 1):
        dati, err = revisiona(client, m, meta_path.parent)
        if err:
            errori += 1
            print(f"  [{i}/{len(da_fare)}] ERRORE {m.get('relpath','?')[:50]}: {err}", flush=True)
        else:
            ok += 1
            m["vision_review"] = {
                "model": MODEL,
                "fonte": FONTE,
                "quando": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                **dati,
            }
            meta_path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.prova or i % 25 == 0 or i == len(da_fare):
                tags = ", ".join(f"{t}={v}" for t, v in dati["style_scores"].items())
                print(f"  [{i}/{len(da_fare)}] {(m.get('title') or '')[:44]:44s} -> {tags}", flush=True)
        time.sleep(PAUSA)

    print(f"\n[OK] revisionati {ok}, errori {errori}", flush=True)
    if ok:
        print("Prossimi passi: recompute_style_vectors -> run_clustering -> pool -> ripesca_orfane")


if __name__ == "__main__":
    main()

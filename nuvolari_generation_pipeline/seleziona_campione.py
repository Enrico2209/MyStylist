#!/usr/bin/env python3
"""
Sceglie un campione di outfit che copra i casi limite, invece dei primi N.

Generare i primi N outfit in ordine di pool costa quanto generarne N mirati,
ma mostra molto meno: gli outfit vicini nel file si somigliano, perché nascono
dalle stesse ancore. Un difetto che riguarda solo gli outfit invernali, o solo
quelli con capospalla, può non comparire affatto in 100 foto consecutive — ed
è esattamente così che sciarpe estive e borse fuori registro sono arrivate
fino alle immagini.

Qui si prende invece qualche outfit per ciascuna categoria che vale la pena
guardare, includendo i casi che nella revisione si sono già rivelati fragili.

Uso:
    python3 seleziona_campione.py                 # stampa gli id
    python3 seleziona_campione.py --n 30
    python3 seleziona_campione.py --comando       # riga pronta da eseguire
"""

import argparse
import json
import math
import re
from pathlib import Path

import outfit_generation as og
from generate_outfit_images import (MAX_REF_IMAGES, ROOT, load_formality,
                                     outfit_formality)
from scoring import hue_circular_distance

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401


def palette(df, relpath):
    p = df.loc[relpath, "color_palette"]
    return json.loads(p) if isinstance(p, str) else p


def ha_complementari(df, outfit) -> bool:
    """Vero se due capi hanno tinte opposte: è la regola appena riabilitata
    (soglia 130°), quindi va guardata per prima nelle immagini."""
    capi = [v["relpath"] for v in outfit["slots"].values() if v]
    for i in range(len(capi)):
        for j in range(i + 1, len(capi)):
            a, b = palette(df, capi[i])[0]["lab"], palette(df, capi[j])[0]["lab"]
            if min(math.hypot(a[1], a[2]), math.hypot(b[1], b[2])) < 10:
                continue
            ha = math.degrees(math.atan2(a[2], a[1])) % 360
            hb = math.degrees(math.atan2(b[2], b[1])) % 360
            if hue_circular_distance(ha, hb) > 130:
                return True
    return False


def titoli(outfit) -> str:
    return " ".join(v["title"].upper() for v in outfit["slots"].values() if v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25, help="quanti outfit nel campione")
    ap.add_argument("--comando", action="store_true", help="stampa il comando pronto")
    args = ap.parse_args()

    df = og.load_and_prepare(str(BASE / "features_clustered.parquet")).set_index("relpath")
    pool = [json.loads(l) for l in open(BASE / "outfits_pool.jsonl", encoding="utf-8")]
    tab = load_formality()

    for o in pool:
        o["_form"] = outfit_formality(o, tab)
        o["_foto"] = sum(len(v.get("all_images") or []) for v in o["slots"].values() if v)
        o["_slot"] = {s for s, v in o["slots"].items() if v}
        o["_titoli"] = titoli(o)

    # Ogni voce: (nome del caso, filtro, come ordinare per prendere i più estremi)
    categorie = [
        ("formalità minima", lambda o: True, lambda o: o["_form"]),
        ("formalità massima", lambda o: True, lambda o: -o["_form"]),
        ("score più alto", lambda o: True, lambda o: -o["outfit_score"]),
        ("score al limite", lambda o: True, lambda o: o["outfit_score"]),
        ("oltre il tetto di 14 foto", lambda o: o["_foto"] > MAX_REF_IMAGES, lambda o: -o["_foto"]),
        ("outfit donna", lambda o: o.get("gender") == "donna", lambda o: -o["outfit_score"]),
        ("con capospalla", lambda o: "outerwear" in o["_slot"], lambda o: -o["outfit_score"]),
        ("senza capospalla", lambda o: "outerwear" not in o["_slot"], lambda o: -o["outfit_score"]),
        # casi che la revisione ha già mostrato fragili
        ("con sciarpa (stagione)", lambda o: "SCIARPA" in o["_titoli"], lambda o: -o["outfit_score"]),
        ("con borsa o tracolla (genere)", lambda o: re.search(r"BORSA|TRACOLLA|MARSUPIO", o["_titoli"]), lambda o: -o["outfit_score"]),
        ("brand street (formalità)", lambda o: "SPRAYGROUND" in o["_titoli"], lambda o: -o["outfit_score"]),
        ("colori complementari", lambda o: ha_complementari(df, o), lambda o: -o["outfit_score"]),
        ("pantaloni corti", lambda o: re.search(r"BERMUDA|PANTALONCIN|SHORT", o["_titoli"]), lambda o: -o["outfit_score"]),
    ]

    per_categoria = max(1, args.n // len(categorie))
    scelti, visti = [], set()
    for nome, filtro, chiave in categorie:
        cand = sorted([o for o in pool if filtro(o)], key=chiave)
        presi = 0
        for o in cand:
            if o["outfit_id"] in visti:
                continue
            visti.add(o["outfit_id"])
            scelti.append((nome, o))
            presi += 1
            if presi >= per_categoria:
                break

    print(f"Campione di {len(scelti)} outfit su {len(pool)}, scelti per coprire i casi limite:\n")
    for nome, o in scelti:
        print(f"  {o['outfit_id']}  [{nome:28s}] form {o['_form']:.2f} · "
              f"score {o['outfit_score']:.3f} · {o['_foto']:2d} foto · {len(o['_slot'])} capi")

    ids = " ".join(o["outfit_id"] for _, o in scelti)
    costo = len(scelti) * 0.134
    print(f"\ncosto stimato: ${costo:.2f}")
    if args.comando:
        print("\nnosleep python3 generate_outfit_images.py --photos all --only " + ids)
    else:
        print("\nid: " + ids)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Sceglie N outfit del pool massimizzando la varieta', non il punteggio.

Prendere i primi N per outfit_score da' un campione ingannevole: i punteggi
alti si concentrano sugli stessi capi (nel pool precedente un gilet compariva
in 314 outfit e un accessorio in 312), quindi si pagherebbero N foto per
vedere N volte quasi lo stesso outfit.

Due leve, in ordine di forza:

  1. nessun capo si ripete fra gli outfit scelti. E' il vincolo che da' solo
     la maggior parte della varieta': 100 outfit diventano ~400 capi distinti.
  2. estrazione a giro fra gli strati (genere x cluster stilistico x stagione),
     cosi' nessun registro monopolizza la selezione.

Il punteggio resta come pavimento di qualita': dentro ogni strato si va dal
migliore in giu', e sotto una soglia non si scende.

Uso:
    python3 scegli_eterogenei.py --quanti 100
    python3 scegli_eterogenei.py --quanti 100 --ids-out ids.txt
"""

import argparse
import collections
import json
from pathlib import Path

import pandas as pd

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
POOL = BASE / "outfits_pool.jsonl"
CLUSTERED = BASE / "features_clustered.parquet"
SLOT_ORDER = ["top", "bottom", "shoes", "outerwear", "accessory"]


def _stagione(riga) -> str:
    for s in ("estate", "inverno", "mezza_stagione", "tutte"):
        if riga.get(f"season_{s}") == 1.0:
            return s
    return "tutte"


def scegli(quanti: int, soglia: float = None):
    outfits = [json.loads(l) for l in open(POOL, encoding="utf-8")]
    df = pd.read_parquet(CLUSTERED).set_index("relpath")
    cluster = df["style_cluster"].to_dict()
    stagione = {r: _stagione(v) for r, v in df.to_dict("index").items()}

    if soglia is None:  # pavimento: la mediana del pool
        soglia = sorted(o["outfit_score"] for o in outfits)[len(outfits) // 2]
    ammessi = [o for o in outfits if o["outfit_score"] >= soglia]

    strati = collections.defaultdict(list)
    for o in ammessi:
        anchor = o["anchor_relpath"]
        chiave = (o["gender"], cluster.get(anchor, -1), stagione.get(anchor, "tutte"))
        strati[chiave].append(o)
    for lista in strati.values():
        lista.sort(key=lambda o: -o["outfit_score"])

    print(f"[*] {len(outfits)} outfit nel pool, {len(ammessi)} sopra la soglia "
          f"{soglia:.3f}, {len(strati)} strati")

    # Tetto di riuso per capo, che sale solo quando serve.
    #
    # Con tetto 1 (nessun capo ripetuto) la selezione si blocca presto, e il
    # motivo e' misurabile: sopra la mediana ci sono 1410 capi distinti e 1033
    # compaiono una volta sola, ma quasi ogni outfit contiene anche uno dei
    # pochi capi-perno — il piu' usato sta in 199 outfit. Consumati quei perni,
    # ogni altro candidato ne condivide uno. Alzare il tetto di un gradino alla
    # volta da' la varieta' massima effettivamente raggiungibile, invece di
    # fermarsi a meta' o di rinunciare del tutto al vincolo.
    usi = collections.Counter()
    scelti = []
    presi = set()
    ordine = sorted(strati, key=lambda k: -len(strati[k]))
    tetto = 1
    while len(scelti) < quanti and tetto <= 4:
        indici = {k: 0 for k in strati}
        while len(scelti) < quanti:
            preso = False
            for k in ordine:
                if len(scelti) >= quanti:
                    break
                lista = strati[k]
                while indici[k] < len(lista):
                    o = lista[indici[k]]
                    indici[k] += 1
                    if o["outfit_id"] in presi:
                        continue
                    capi = {s["relpath"] for s in o["slots"].values() if s}
                    if any(usi[c] >= tetto for c in capi):
                        continue
                    scelti.append(o)
                    presi.add(o["outfit_id"])
                    usi.update(capi)
                    preso = True
                    break
            if not preso:
                break
        if len(scelti) < quanti:
            tetto += 1
            print(f"    [tetto di riuso alzato a {tetto} — a {len(scelti)}/{quanti}]")
    usati = set(usi)
    print(f"[*] tetto di riuso finale: {tetto}")
    return scelti, usati


def riepilogo(scelti, usati):
    print(f"\n[OK] {len(scelti)} outfit, {len(usati)} capi distinti "
          f"({len(usati)/max(len(scelti),1):.1f} per outfit)")
    for campo in ("gender", "anchor_slot"):
        c = collections.Counter(o[campo] for o in scelti)
        print(f"  {campo:12}: {dict(c.most_common())}")
    n_slot = collections.Counter(sum(1 for s in o["slots"].values() if s) for o in scelti)
    print(f"  capi per outfit: {dict(sorted(n_slot.items()))}")
    punteggi = sorted(o["outfit_score"] for o in scelti)
    print(f"  score: min {punteggi[0]:.3f} / mediana {punteggi[len(punteggi)//2]:.3f} "
          f"/ max {punteggi[-1]:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quanti", type=int, default=100)
    ap.add_argument("--soglia", type=float, default=None)
    ap.add_argument("--ids-out", default="ids_eterogenei.txt")
    a = ap.parse_args()
    scelti, usati = scegli(a.quanti, a.soglia)
    riepilogo(scelti, usati)
    Path(a.ids_out).write_text("\n".join(o["outfit_id"] for o in scelti))
    print(f"\n[OK] id scritti in {a.ids_out}")


if __name__ == "__main__":
    main()

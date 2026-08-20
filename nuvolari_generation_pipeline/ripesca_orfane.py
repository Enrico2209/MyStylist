#!/usr/bin/env python3
"""
Riporta nel pool gli outfit con immagine già pagata che le regole ATTUALI
approverebbero ancora.

Il problema che risolve: la beam search sceglie il massimo, e a ogni ritocco
dei punteggi il massimo si sposta di un soffio. L'outfit della foto — quattro
capi su cinque identici al nuovo vincitore — usciva dal pool per 0,016 di
differenza, e con lui l'immagine da 0,134 $. Moltiplicato per una
rigenerazione intera: 69 orfane, di cui 68 ancora perfettamente valide.

Il criterio NON è "ha una foto, quindi resta": ogni composizione viene
rivalutata da zero con i punteggi e le regole di oggi — soglia del pool,
needs_vision_review, genere, stagione a coppie, dispersione di formalità,
R1-R4. Chi non passa resta orfano, ed è giusto così: la foto è un costo
sommerso, non un lasciapassare.

Le composizioni si cercano in tutti i pool storici (outfits_pool.jsonl*): un
outfit generato una volta è descritto in almeno un backup. I punteggi salvati
vengono RICALCOLATI, non copiati.

Uso:
    python3 ripesca_orfane.py --prova    # mostra e basta
    python3 ripesca_orfane.py
"""

import argparse
import glob
import itertools
import json
import os
import shutil
from pathlib import Path

import outfit_generation as og
import scoring as sc

# la soglia non è cablata: segue quella del pool (vedi run_outfit_pipeline)
SOGLIA = og.OPTIONAL_SLOT_MIN_SCORE
# valida() vive in outfit_generation: la usa anche l'accumulo del pool
valida = og.valida_composizione

from percorsi import DATI as BASE, CODICE, RADICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
POOL = BASE / "outfits_pool.jsonl"
IMMAGINI = BASE / "outfit_images"
MINIATURE = BASE / "outfit_thumbs"
MINIATURE_SUPERATE = RADICE / "outfit_thumbs_superate"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prova", action="store_true")
    args = ap.parse_args()

    df = og.load_and_prepare(str(BASE / "features_clustered.parquet")).set_index("relpath", drop=False)
    pool = [json.loads(l) for l in open(POOL, encoding="utf-8")]
    nel_pool = {o["outfit_id"] for o in pool}

    # I pool storici sono la sola memoria di come era composto un outfit di cui
    # resta solo l'immagine. Dopo la riorganizzazione del 19 agosto il pool vivo
    # sta in nuvolari_db ma i .bak sono rimasti in radice: si guardano entrambi,
    # altrimenti ogni orfana risulta "composizione ignota".
    composizioni = {}
    sorgenti = sorted(glob.glob(str(BASE / "outfits_pool.jsonl*"))
                      + glob.glob(str(RADICE / "outfits_pool.jsonl*")))
    for f in sorgenti:
        for l in open(f, encoding="utf-8"):
            o = json.loads(l)
            composizioni.setdefault(o["outfit_id"], o)

    # Ultima fonte: il manifest delle immagini. Descrive gli stessi outfit con
    # nomi di campo diversi (capi/cartella/titolo invece di slots/relpath/title)
    # e sopravvive ai pool che sono stati sovrascritti senza .bak. Vale solo per
    # gli outfit_id che i pool non hanno gia' spiegato.
    manifest = BASE / "outfits_manifest.json"
    if manifest.exists():
        for o in json.load(open(manifest, encoding="utf-8")).get("outfit", []):
            if o["outfit_id"] in composizioni:
                continue
            slots = {s: {"product_id": c["product_id"],
                         "relpath": c["cartella"],
                         "title": c["titolo"],
                         "url": c["url_prodotto"],
                         "display_image": c["foto_copertina"],
                         "all_images": c.get("foto_disponibili", [])}
                     for s, c in o["capi"].items() if c}
            composizioni[o["outfit_id"]] = {
                "outfit_id": o["outfit_id"], "label": o["nome"],
                "gender": o["genere"], "outfit_score": o["score_compatibilita"],
                "anchor_slot": "top", "anchor_relpath": slots["top"]["relpath"],
                "slots": slots, "pairwise_scores": o.get("score_coppie", {})}

    pagate = set()
    for cartella in (IMMAGINI, IMMAGINI_SUPERATE):
        pagate |= {p.stem for p in cartella.glob("*.png")} if cartella.exists() else set()
    orfane = sorted(pagate - nel_pool)
    print(f"[*] immagini pagate: {len(pagate)}, fuori dal pool: {len(orfane)}")

    ripescati, respinti = [], []
    for oid in orfane:
        o = composizioni.get(oid)
        if o is None:
            respinti.append((oid, "composizione ignota")); continue
        m, motivo = valida(o, df)
        if motivo:
            respinti.append((oid, motivo)); continue
        # punteggi ricalcolati, non copiati dal pool vecchio
        items = [(s, df.loc[v["relpath"]]) for s, v in o["slots"].items() if v]
        coppie = {f"{sa}-{sb}": round(sc.score_pair(a, b)["score"], 3)
                  for (sa, a), (sb, b) in itertools.combinations(items, 2)}
        o = dict(o, outfit_score=round(min(coppie.values()), 3), pairwise_scores=coppie)
        ripescati.append(o)

    print(f"[*] ripescabili: {len(ripescati)}, respinti: {len(respinti)}")
    for oid, motivo in respinti:
        print(f"    [no] {oid}  {motivo}")
    for o in ripescati:
        print(f"    [ok] {o['outfit_id']}  {o['outfit_score']:.3f}  {o['label'][:60]}")

    if args.prova:
        print("\n(prova: non ho toccato niente)")
        return
    if not ripescati:
        return

    shutil.copy2(POOL, POOL.with_suffix(".jsonl.prima_ripescaggio.bak"))
    with open(POOL, "a", encoding="utf-8") as f:
        for o in ripescati:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    # le immagini eventualmente finite fra le superate tornano operative
    # Le cartelle _superate sono rimaste in radice dopo la riorganizzazione,
    # mentre outfit_images/ e' in nuvolari_db: il percorso va preso da percorsi.py,
    # non dedotto dal nome della cartella di destinazione.
    tornati = 0
    for o in ripescati:
        for cartella, superate, suff in ((IMMAGINI, IMMAGINI_SUPERATE, ".png"),
                                         (MINIATURE, MINIATURE_SUPERATE, ".webp")):
            sup = superate / f"{o['outfit_id']}{suff}"
            if sup.exists():
                shutil.move(str(sup), str(cartella / sup.name)); tornati += 1
    print(f"\n[OK] pool: {len(pool)} -> {len(pool) + len(ripescati)} outfit")
    print(f"     {tornati} file recuperati da *_superate")


if __name__ == "__main__":
    main()

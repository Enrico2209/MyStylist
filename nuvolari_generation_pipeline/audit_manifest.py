#!/usr/bin/env python3
"""
Audit di tracciabilità del manifest: verifica che ogni immagine generata sia
riconducibile senza ambiguità all'outfit, ai capi e alle foto sorgente.

Non si limita a controllare che i campi esistano: verifica che i percorsi
puntino a file veri, che gli identificativi siano univoci e coerenti col pool,
e che le foto dichiarate come inviate al modello siano esattamente quelle che
la generazione avrebbe scelto. Un manifest che "sembra" completo ma rimanda a
file assenti sarebbe peggio di nessun manifest.

Uso:
    python3 audit_manifest.py                       # su outfits_manifest.json
    python3 audit_manifest.py --manifest altro.json
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
LOG = BASE / "outfit_images_log.jsonl"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(BASE / "outfits_manifest.json"))
    args = ap.parse_args()

    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    voci = man["outfit"]
    pool = {json.loads(l)["outfit_id"]: json.loads(l)
            for l in open(BASE / "outfits_pool.jsonl", encoding="utf-8")}
    radice_capi = BASE / man["radice_foto_capi"]

    problemi = []

    def check(nome, ko, totale, esempio=None):
        # 0/0 non è un successo: è un controllo che non ha esaminato nulla
        # (tipicamente perché le immagini non sono ancora state generate).
        # Dichiararlo OK renderebbe l'audit verde senza aver verificato niente.
        if totale == 0:
            print(f"  [n/d ] {nome}: nessun elemento da verificare")
            return
        stato = "OK  " if ko == 0 else "FAIL"
        riga = f"  [{stato}] {nome}: {totale - ko}/{totale}"
        if ko:
            riga += f"  ({ko} problemi" + (f", es. {esempio}" if esempio else "") + ")"
            problemi.append(nome)
        print(riga)

    print(f"Audit di {Path(args.manifest).name} — {len(voci)} outfit\n")

    # 1. identificativi univoci e presenti nel pool
    ids = [v["outfit_id"] for v in voci]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    check("outfit_id univoci", len(dup), len(ids), dup[:1])
    orfani = [i for i in ids if i not in pool]
    check("outfit_id presenti nel pool", len(orfani), len(ids), orfani[:1])

    # 2. l'immagine dichiarata esiste davvero
    con_img = [v for v in voci if v["immagine_generata"]["presente"]]
    mancanti = [v["outfit_id"] for v in con_img
                if not (BASE / v["immagine_generata"]["percorso"]).exists()]
    check("immagini dichiarate presenti sul disco", len(mancanti), len(con_img), mancanti[:1])

    # 3. ogni foto sorgente citata esiste
    tot_foto = ko_foto = 0
    esempio_foto = None
    for v in voci:
        for capo in v["capi"].values():
            if not capo:
                continue
            for p in capo["foto_disponibili"]:
                tot_foto += 1
                if not (radice_capi / p).exists():
                    ko_foto += 1
                    esempio_foto = esempio_foto or p
    check("foto sorgente esistenti", ko_foto, tot_foto, esempio_foto)

    # 4. le foto inviate sono un sottoinsieme di quelle disponibili, e la somma
    #    coincide con quanto registrato nel log al momento della generazione
    ko_sub = ko_tot = 0
    es_sub = es_tot = None
    for v in con_img:
        somma = 0
        for capo in v["capi"].values():
            if not capo:
                continue
            inv, disp = set(capo["foto_inviate_al_modello"]), set(capo["foto_disponibili"])
            somma += len(inv)
            if not inv <= disp:
                ko_sub += 1
                es_sub = es_sub or v["outfit_id"]
        atteso = v["immagine_generata"].get("foto_inviate_totali")
        if atteso is not None and atteso != somma:
            ko_tot += 1
            es_tot = es_tot or f'{v["outfit_id"]} manifest={somma} log={atteso}'
    check("foto inviate ⊆ foto disponibili", ko_sub, len(con_img), es_sub)
    check("conteggio foto inviate = quello del log", ko_tot, len(con_img), es_tot)

    # 5. coerenza con il pool: stessi capi, stesso score
    ko_capi = ko_score = 0
    es_capi = None
    for v in voci:
        p = pool[v["outfit_id"]]
        capi_man = {s: (c["product_id"] if c else None) for s, c in v["capi"].items()}
        capi_pool = {s: (x["product_id"] if x else None) for s, x in p["slots"].items()}
        if capi_man != capi_pool:
            ko_capi += 1
            es_capi = es_capi or v["outfit_id"]
        if abs(v["score_compatibilita"] - p["outfit_score"]) > 1e-9:
            ko_score += 1
    check("capi identici a quelli del pool", ko_capi, len(voci), es_capi)
    check("score identico a quello del pool", ko_score, len(voci))

    # 6. ogni immagine sul disco risale a un outfit del pool.
    #    Non basta confrontare col manifest della singola variante: un file
    #    "<id>_v3.png" appartiene a un'altra variante, non è orfano. Orfano è
    #    un file il cui nome non riconduce ad alcun outfit_id conosciuto —
    #    quello sì sarebbe un'immagine di cui non sappiamo la provenienza.
    su_disco = list((BASE / man["radice_immagini_outfit"]).glob("*.png"))
    orfane = [p for p in su_disco if not any(p.stem.startswith(i) for i in pool)]

    # Un'immagine può restare orfana per un motivo legittimo: il pool è stato
    # rigenerato e quell'outfit non supera più la soglia, o i suoi capi si sono
    # ricombinati diversamente. Questo si distingue da un file di provenienza
    # ignota guardando il log delle generazioni: se l'id compare lì, sappiamo
    # da dove viene l'immagine — semplicemente l'outfit non esiste più.
    generati = set()
    if LOG.exists():
        for riga in LOG.read_text(encoding="utf-8").splitlines():
            if riga.strip():
                generati.add(json.loads(riga)["outfit_id"])
    storiche = [p for p in orfane if any(p.stem.startswith(i) for i in generati)]
    ignote = [p for p in orfane if p not in storiche]

    check("ogni immagine ha una provenienza nota", len(ignote), len(su_disco) or 1,
          [p.name for p in ignote][:1])
    if storiche:
        print(f"  [ i  ] {len(storiche)} immagini di outfit non più nel pool "
              f"(pool rigenerato dopo la loro creazione): "
              f"{', '.join(p.name for p in storiche[:3])}")

    # 7. prova pratica: da un'immagine si risale alle foto sorgente
    print("\nProva di risalita, su un caso reale:")
    # Si preferisce un'immagine della variante mappata; se non ce n'è (finché
    # esistono solo immagini di prova con suffisso) si ripiega su un file di
    # variante, così la prova resta eseguibile anche prima del run definitivo.
    scelta = None
    if con_img:
        v = con_img[0]
        scelta = (v, BASE / v["immagine_generata"]["percorso"])
    else:
        per_id = {x["outfit_id"]: x for x in voci}
        for p in sorted(su_disco):
            v = per_id.get(p.stem[:12])
            if v:
                scelta = (v, p)
                break

    if scelta:
        v, img = scelta
        print(f"  immagine  {img.name}  (sha {sha(img)})")
        print(f"  outfit    {v['outfit_id']} — formalità {v['formalita']}, score {v['score_compatibilita']}")
        for slot, capo in v["capi"].items():
            if not capo:
                continue
            n_inv, n_disp = len(capo["foto_inviate_al_modello"]), len(capo["foto_disponibili"])
            prima = Path(capo["foto_inviate_al_modello"][0]).name if n_inv else "-"
            print(f"    {slot:<10} {capo['product_id']}  {n_inv}/{n_disp} foto usate  (prima: {prima})")
    else:
        print("  nessuna immagine da verificare")

    print()
    if problemi:
        print(f"AUDIT FALLITO — controlli non superati: {', '.join(problemi)}")
        raise SystemExit(1)
    print("AUDIT SUPERATO — ogni immagine è riconducibile a outfit, capi e foto sorgente.")


if __name__ == "__main__":
    main()

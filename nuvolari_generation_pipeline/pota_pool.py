#!/usr/bin/env python3
"""
Toglie dal pool gli outfit che una regola nuova non ammette più.

Serve per non dover rigenerare tutto. Rigenerare il pool rifà la beam search da
capo: le composizioni cambiano, gli outfit_id con loro, e le immagini già pagate
diventano orfane a centinaia. Quando una regola nuova riguarda pochi outfit,
toglierli e basta costa zero e non tocca nessuno degli altri.

Quello che questo script NON fa è rimpiazzarli: i posti liberati restano vuoti
fino alla prossima rigenerazione completa. È un potatura, non una ricostruzione.

Uso:
    python3 pota_pool.py --prova      # dice cosa toglierebbe, non tocca niente
    python3 pota_pool.py
"""

import argparse
import json
import shutil
from pathlib import Path

import outfit_generation as og

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
POOL = BASE / "outfits_pool.jsonl"
IMMAGINI = BASE / "outfit_images"
MINIATURE = BASE / "outfit_thumbs"


def _coppia(outfit: dict, slot_a: str, insieme_a: set, slot_b: str, insieme_b: set) -> bool:
    """Vero se l'outfit contiene entrambi i capi vietati insieme."""
    a, b = outfit["slots"].get(slot_a), outfit["slots"].get(slot_b)
    return bool(a and b and a["relpath"] in insieme_a and b["relpath"] in insieme_b)


def regole_violate(outfit: dict, gruppi: dict) -> list:
    """Nome delle regole che questo outfit non rispetta più."""
    rotte = []
    if _coppia(outfit, "shoes", gruppi["mocassini"], "top", gruppi["top_informali"]):
        rotte.append("R3 mocassino+t-shirt")
    if _coppia(outfit, "outerwear", gruppi["cappotti"], "bottom", gruppi["bottom_tuta"]):
        rotte.append("R4 cappotto+tuta")
    return rotte


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prova", action="store_true", help="mostra e basta")
    args = ap.parse_args()

    df = og.load_and_prepare(str(BASE / "features_clustered.parquet"))
    gruppi = {
        "mocassini": set(df.loc[df["mocassino"], "relpath"]),
        "top_informali": set(df.loc[df["top_senza_collo"], "relpath"]),
        "cappotti": set(df.loc[df["cappotto"], "relpath"]),
        "bottom_tuta": set(df.loc[df["bottom_da_tuta"], "relpath"]),
    }
    print(f"[*] {len(gruppi['mocassini'])} calzature mocassino/barca, "
          f"{len(gruppi['cappotti'])} capispalla con forma da cappotto")

    pool = [json.loads(l) for l in open(POOL, encoding="utf-8")]
    da_togliere = [(o, r) for o in pool if (r := regole_violate(o, gruppi))]

    print(f"[*] {len(da_togliere)} outfit su {len(pool)} violano una regola nuova "
          f"({len(da_togliere) / len(pool) * 100:.1f}%)")
    con_foto = [o for o, _ in da_togliere if (IMMAGINI / f"{o['outfit_id']}.png").exists()]
    print(f"    di cui con immagine già generata: {len(con_foto)}")
    for o, rotte in da_togliere:
        segno = "foto" if (IMMAGINI / f"{o['outfit_id']}.png").exists() else "   —"
        print(f"    [{segno}] {o['outfit_id']}  {', '.join(rotte):22s}  {o['label'][:64]}")
    da_togliere = [o for o, _ in da_togliere]

    if args.prova:
        print("\n(prova: non ho toccato niente)")
        return
    if not da_togliere:
        return

    shutil.copy2(POOL, POOL.with_suffix(".jsonl.prima_calzature.bak"))
    tolti = {o["outfit_id"] for o in da_togliere}
    with open(POOL, "w", encoding="utf-8") as f:
        for o in pool:
            if o["outfit_id"] not in tolti:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")

    # le immagini degli outfit tolti seguono la stessa sorte di quelle superate
    # da una rigenerazione: si spostano, non si cancellano — sono costate
    for cartella, suffisso in ((IMMAGINI, ".png"), (MINIATURE, ".webp")):
        superate = cartella.parent / f"{cartella.name}_superate"
        superate.mkdir(exist_ok=True)
        for oid in tolti:
            p = cartella / f"{oid}{suffisso}"
            if p.exists():
                shutil.move(str(p), str(superate / p.name))

    print(f"\n[OK] pool: {len(pool)} -> {len(pool) - len(tolti)} outfit")
    print(f"     backup in {POOL.with_suffix('.jsonl.prima_calzature.bak').name}")
    print(f"     {len(con_foto)} immagini spostate in outfit_images_superate")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Rifà la pipeline da capo dopo un cambio che tocca i dati, non solo le regole.

Serve quando la correzione sta a monte del pool: un attributo che cambia
(formalità, colore) o una formula di punteggio. In quei casi potare non basta —
le composizioni stesse vanno ricalcolate.

Quello che rifà, nell'ordine in cui le cose dipendono l'una dall'altra:

  1. attributi nei metadata   (formalità, stile, stagione — nessuna rete)
  2. features.parquet         (colore: qui entra lo scontorno adattivo)
  3. features_clustered       (il clustering usa i 10 tag + la formalità)
  4. outfits_pool.jsonl       (punteggi e regole di compatibilità)

Alla fine dice quante immagini già pagate sopravvivono: un outfit tiene la sua
foto solo se l'insieme dei capi è rimasto identico, perché l'outfit_id è
l'hash di quell'insieme.

Uso:
    python3 rigenera_tutto.py
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
CATALOGO = BASE / "nuvolari_full_organizzato"
FEATURES = BASE / "features.parquet"
CLUSTERED = BASE / "features_clustered.parquet"
POOL = BASE / "outfits_pool.jsonl"
IMMAGINI = BASE / "outfit_images"


def salva_copia(percorso: Path, marca: str):
    if percorso.exists():
        destinazione = percorso.with_suffix(percorso.suffix + f".prima_{marca}.bak")
        shutil.copy2(percorso, destinazione)
        print(f"    copia di sicurezza: {destinazione.name}")


def id_outfit_esistenti() -> set:
    if not POOL.exists():
        return set()
    return {json.loads(l)["outfit_id"] for l in open(POOL, encoding="utf-8")}


def main():
    marca = datetime.now().strftime("%d%b").lower()
    inizio = datetime.now()

    prima = id_outfit_esistenti()
    con_foto = {p.stem for p in IMMAGINI.glob("*.png")}
    print(f"[*] prima: {len(prima)} outfit, {len(con_foto)} con immagine\n")

    for p in (FEATURES, CLUSTERED, POOL):
        salva_copia(p, marca)

    print("\n── 1/4  attributi nei metadata ──────────────────────────────────")
    from scrape_with_attributes import refix_brand_and_attributes
    refix_brand_and_attributes(str(CATALOGO))

    print("\n── 2/4  colore e features ───────────────────────────────────────")
    from feature_engineering import add_display_and_gallery_columns, build_feature_table
    build_feature_table(root=str(CATALOGO), out_parquet=str(FEATURES))
    # Passo separato e facile da dimenticare: build_feature_table non mette i
    # percorsi delle foto. Saltandolo la pipeline arriva fino in fondo senza un
    # errore e produce un pool con display_image a None per ogni capo, cioè
    # outfit di cui non si può generare l'immagine. Successo apparente, dati
    # inutilizzabili — per questo sta qui dentro e non nelle istruzioni.
    add_display_and_gallery_columns(str(FEATURES), str(CATALOGO), out_parquet=str(FEATURES))

    print("\n── 3/4  clustering ──────────────────────────────────────────────")
    from clustering import run_clustering
    run_clustering(features_parquet=str(FEATURES), out_parquet=str(CLUSTERED))

    print("\n── 4/4  pool di outfit ──────────────────────────────────────────")
    import outfit_generation as og
    df = og.load_and_prepare(str(CLUSTERED))
    og.run_outfit_pipeline(df, out_jsonl=str(POOL))

    dopo = id_outfit_esistenti()
    sopravvissute = con_foto & dopo
    print("\n══════════════════════════════════════════════════════════════════")
    print(f"outfit: {len(prima)} -> {len(dopo)}")
    print(f"immagini: {len(con_foto)} pagate, {len(sopravvissute)} ancora valide, "
          f"{len(con_foto - dopo)} orfane")
    print(f"durata: {datetime.now() - inizio}")
    print("\nLe orfane non sono state toccate: stanno ancora in outfit_images.")
    print("Spostarle in outfit_images_superate è un passo a parte, da fare a mente fredda.")


if __name__ == "__main__":
    main()

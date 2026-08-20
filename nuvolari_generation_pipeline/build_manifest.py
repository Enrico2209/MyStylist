#!/usr/bin/env python3
"""
Costruisce outfits_manifest.json: un unico file che collega ogni immagine
generata all'outfit, ai capi e alle foto sorgente che l'hanno prodotta.

Senza questo, il legame fra immagine e outfit è solo la convenzione sul nome
del file, e soprattutto si perde l'informazione su QUALI foto sono finite
davvero nella richiesta: sotto il tetto di 14 immagini di riferimento
(vedi allocate_photos in generate_outfit_images.py) non sempre entrano tutte,
e per capire perché un capo è venuto in un certo modo serve sapere quali sue
foto ha visto il modello.

Uso:
    python3 build_manifest.py                     # tutto il pool
    python3 build_manifest.py --suffix _v3        # variante specifica
    python3 build_manifest.py --solo-generati     # solo outfit con immagine
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from generate_outfit_images import (MAX_REF_IMAGES, ROOT, allocate_photos,
                                     load_formality, outfit_formality)

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
JSONL = BASE / "outfits_pool.jsonl"
IMG_DIR = BASE / "outfit_images"
LOG = BASE / "outfit_images_log.jsonl"
OUT = BASE / "outfits_manifest.json"

SLOTS = ["top", "bottom", "shoes", "outerwear", "accessory"]


def foto_inviate(outfit: dict, tutte_le_foto: bool = True,
                  max_ref: int = MAX_REF_IMAGES) -> dict:
    """Ricostruisce, slot per slot, quali foto sono state passate al modello.

    Deterministico: applica la stessa ripartizione a giro usata in generazione,
    quindi il risultato coincide con ciò che ha visto davvero il modello.

    `tutte_le_foto` deve corrispondere alla modalità con cui l'immagine è stata
    generata (registrata nel log come "modalita_foto"): in modalità copertina
    entra una sola foto per capo, e ricostruirla come se fossero entrate tutte
    attribuirebbe all'immagine foto che il modello non ha mai visto.
    """
    ordine, per_capo = [], []
    for slot in SLOTS:
        v = outfit["slots"].get(slot)
        if not v:
            continue
        if tutte_le_foto:
            rel = list(v.get("all_images") or [])
        else:
            rel = [v["display_image"]] if v.get("display_image") else []
        esistenti = [p for p in rel if (ROOT / p).exists()]
        if not esistenti:
            continue
        ordine.append(slot)
        per_capo.append(esistenti)
    scelte = allocate_photos(per_capo, max_ref)
    return dict(zip(ordine, scelte))


def esiti_dal_log(suffix: str) -> dict:
    """Esito più recente per ogni outfit, preferendo la variante richiesta.

    Il log è in append: righe successive sovrascrivono le precedenti. Un outfit
    può comparire più volte (varianti di prova, rigenerazioni), quindi si tiene
    la riga della variante che stiamo mappando; se manca, l'ultima disponibile —
    così il manifest non attribuisce a un'immagine i dati di un'altra prova.
    """
    per_variante, ultimo = {}, {}
    if LOG.exists():
        for riga in LOG.read_text(encoding="utf-8").splitlines():
            if not riga.strip():
                continue
            r = json.loads(riga)
            oid = r["outfit_id"]
            ultimo[oid] = r
            if r.get("variante", "").endswith(suffix):
                per_variante[oid] = r
    return {oid: per_variante.get(oid, ultimo[oid]) for oid in ultimo}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", default="", help="variante delle immagini da mappare (es. _v3)")
    ap.add_argument("--solo-generati", action="store_true",
                    help="includi solo gli outfit che hanno l'immagine su disco")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    outfits = [json.loads(l) for l in open(JSONL, encoding="utf-8")]
    tabella = load_formality()
    esiti = esiti_dal_log(args.suffix)

    voci, generati = [], 0
    for o in outfits:
        oid = o["outfit_id"]
        img = IMG_DIR / f"{oid}{args.suffix}.png"
        esiste = img.exists()
        if args.solo_generati and not esiste:
            continue
        generati += esiste

        log_r = esiti.get(oid)
        # la modalità la detta il log, non un'ipotesi: le righe scritte prima
        # dell'introduzione del campo si riconoscono dal prefisso di "variante"
        modalita = (log_r or {}).get("modalita_foto")
        if modalita is None:
            modalita = "display" if (log_r or {}).get("variante", "").startswith("display") else "all"
        inviate = foto_inviate(o, tutte_le_foto=(modalita == "all"),
                                max_ref=(log_r or {}).get("max_ref") or MAX_REF_IMAGES)

        capi = {}
        for slot in SLOTS:
            v = o["slots"].get(slot)
            if not v:
                capi[slot] = None
                continue
            tutte = list(v.get("all_images") or [])
            usate = inviate.get(slot, [])
            capi[slot] = {
                "product_id": v["product_id"],
                "titolo": v["title"],
                "url_prodotto": v["url"],
                "cartella": v["relpath"],
                "foto_copertina": v.get("display_image"),
                "foto_disponibili": tutte,
                "foto_inviate_al_modello": usate,
                # esplicito: sotto il tetto di 14 riferimenti alcune foto
                # possono non essere entrate nella richiesta
                "foto_escluse": [p for p in tutte if p not in usate],
            }

        voci.append({
            "outfit_id": oid,
            "nome": o.get("label"),
            "genere": o.get("gender"),
            "score_compatibilita": o["outfit_score"],
            "score_coppie": o["pairwise_scores"],
            "formalita": round(outfit_formality(o, tabella), 2),
            "immagine_generata": {
                "percorso": str(img.relative_to(BASE)) if esiste else None,
                "presente": esiste,
                "regole_versione": (log_r or {}).get("regole_versione"),
                "modalita_foto": modalita,
                "foto_inviate_totali": (log_r or {}).get("foto_inviate"),
                "esito_ultimo_tentativo": (log_r or {}).get("esito"),
                "errore": (log_r or {}).get("errore"),
            },
            "capi": capi,
        })

    manifest = {
        "generato_il": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "radice_foto_capi": str(ROOT.relative_to(BASE)),
        "radice_immagini_outfit": str(IMG_DIR.relative_to(BASE)),
        "variante": args.suffix or "(principale)",
        "outfit_totali": len(voci),
        "immagini_presenti": generati,
        "outfit": voci,
    }
    Path(args.out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {args.out} — {len(voci)} outfit, {generati} con immagine")


if __name__ == "__main__":
    main()

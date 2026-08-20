#!/usr/bin/env python3
"""
Produce il JSON che alimenta la UI, a partire da pool, metadata e immagini.

Non è il manifest: quello serve a dimostrare da quali foto è nata un'immagine,
porta anche le foto escluse dalla richiesta e pesa 10 MB. Una UI ha bisogno
dell'opposto — poco peso, campi pronti da mostrare (prezzo, brand, link al
prodotto), e i filtri già calcolati per non doverli derivare a ogni caricamento.

Vengono prodotti due file:

  outfits_ui.json        elenco completo, un oggetto per outfit
  outfits_ui_index.json  solo copertine e filtri, per la prima schermata

L'indice esiste perché una griglia iniziale non deve scaricare i capi di tutti
gli outfit: mostra le anteprime, e il dettaglio si carica quando serve.

Uso:
    python3 build_ui_json.py                    # solo outfit con immagine
    python3 build_ui_json.py --tutti            # anche quelli non generati
    python3 build_ui_json.py --base-url https://cdn.esempio.it/
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from generate_outfit_images import load_formality, outfit_formality

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
ROOT = BASE / "nuvolari_full_organizzato"
JSONL = BASE / "outfits_pool.jsonl"
IMG_DIR = BASE / "outfit_images"

SLOTS = ["top", "bottom", "shoes", "outerwear", "accessory"]
SLOT_IT = {"top": "Capo superiore", "bottom": "Pantaloni", "shoes": "Scarpe",
           "outerwear": "Capospalla", "accessory": "Accessorio"}


def fascia_formalita(f: float) -> str:
    """Etichetta leggibile: una UI filtra per 'casual', non per 2.15."""
    if f <= 1.8:
        return "streetwear"
    if f <= 2.4:
        return "casual"
    if f <= 3.0:
        return "smart casual"
    return "elegante"


def carica_metadata() -> dict:
    """relpath -> metadata del prodotto, per prezzo/brand/link."""
    out = {}
    for p in ROOT.rglob("metadata.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        rel = m.get("relpath") or str(p.parent.relative_to(ROOT))
        out[rel] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tutti", action="store_true",
                    help="includi anche gli outfit senza immagine generata")
    ap.add_argument("--base-url", default="",
                    help="prefisso per i percorsi immagine (es. URL di un CDN)")
    ap.add_argument("--out", default=str(BASE / "outfits_ui.json"))
    args = ap.parse_args()

    meta = carica_metadata()
    tabella = load_formality()
    outfits = [json.loads(l) for l in open(JSONL, encoding="utf-8")]

    def url(p: str) -> str:
        return args.base_url + p if args.base_url else p

    voci = []
    for o in outfits:
        oid = o["outfit_id"]
        img = IMG_DIR / f"{oid}.png"
        if not img.exists() and not args.tutti:
            continue

        capi, prezzo_totale, brands, prezzo_completo = [], 0.0, [], True
        for slot in SLOTS:
            v = o["slots"].get(slot)
            if not v:
                continue
            m = meta.get(v["relpath"], {})
            prezzo = m.get("price")
            if prezzo is None:
                prezzo_completo = False
            else:
                prezzo_totale += prezzo
            if m.get("brand"):
                brands.append(m["brand"])
            capi.append({
                "slot": slot,
                "slot_etichetta": SLOT_IT[slot],
                "id": v["product_id"],
                "titolo": v["title"],
                "brand": m.get("brand"),
                "prezzo": prezzo,
                "valuta": m.get("currency") or "EUR",
                "url_prodotto": v["url"],
                "immagine": url(str(Path(ROOT.name) / v["display_image"])) if v.get("display_image") else None,
                "immagini": [url(str(Path(ROOT.name) / p)) for p in (v.get("all_images") or [])],
                "colore": m.get("color_hints"),
                "fantasia": m.get("pattern"),
            })

        f = outfit_formality(o, tabella)
        voci.append({
            "id": oid,
            "nome": o.get("label"),
            "immagine": url(str(Path(IMG_DIR.name) / f"{oid}.png")) if img.exists() else None,
            "genere": o.get("gender") or "unisex",
            "stile": fascia_formalita(f),
            "formalita": round(f, 2),
            "compatibilita": o["outfit_score"],
            # prezzo_completo=False segnala che almeno un capo non ha prezzo:
            # la UI deve poter dire "da 120 €" invece di un totale sbagliato
            "prezzo_totale": round(prezzo_totale, 2),
            "prezzo_completo": prezzo_completo,
            "valuta": "EUR",
            "brand": sorted(set(brands)),
            "numero_capi": len(capi),
            "capi": capi,
        })

    voci.sort(key=lambda v: -v["compatibilita"])

    # Filtri già pronti: una UI non deve scorrere tutti gli outfit per sapere
    # quali valori esistono e quanti elementi ha ciascuno.
    filtri = {
        "genere": dict(Counter(v["genere"] for v in voci)),
        "stile": dict(Counter(v["stile"] for v in voci)),
        "brand": dict(Counter(b for v in voci for b in v["brand"]).most_common(50)),
        "numero_capi": dict(Counter(v["numero_capi"] for v in voci)),
        "prezzo": {
            "min": min((v["prezzo_totale"] for v in voci), default=0),
            "max": max((v["prezzo_totale"] for v in voci), default=0),
        },
    }

    documento = {
        "versione": 1,
        "generato_il": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totale": len(voci),
        "filtri": filtri,
        "outfit": voci,
    }
    Path(args.out).write_text(json.dumps(documento, ensure_ascii=False), encoding="utf-8")

    # indice leggero per la prima schermata
    indice = {
        "versione": 1,
        "generato_il": documento["generato_il"],
        "totale": len(voci),
        "filtri": filtri,
        "outfit": [{k: v[k] for k in
                    ("id", "nome", "immagine", "genere", "stile", "formalita",
                     "compatibilita", "prezzo_totale", "prezzo_completo", "numero_capi")}
                   for v in voci],
    }
    out_idx = Path(args.out).with_name(Path(args.out).stem + "_index.json")
    out_idx.write_text(json.dumps(indice, ensure_ascii=False), encoding="utf-8")

    kb = Path(args.out).stat().st_size / 1024
    kbi = out_idx.stat().st_size / 1024
    print(f"[OK] {args.out} — {len(voci)} outfit, {kb:,.0f} KB")
    print(f"[OK] {out_idx} — indice, {kbi:,.0f} KB")


if __name__ == "__main__":
    main()

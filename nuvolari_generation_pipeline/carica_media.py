#!/usr/bin/env python3
"""
Carica su object storage (Cloudflare R2, o qualunque S3) le sole cose che
servono alla UI online.

Non tutto il catalogo: `nuvolari_full_organizzato` pesa 1,6 GB e serve alla
pipeline, non alla revisione. Online bastano le immagini generate, le loro
miniature e le foto dei capi che compaiono nei 151 outfit con immagine —
circa 435 MB in tutto.

Le credenziali si leggono dall'ambiente e non passano mai dalla riga di
comando. Su Cloudflare si creano in R2 → Manage API Tokens:

    export R2_ACCOUNT_ID=...
    export R2_ACCESS_KEY_ID=...
    export R2_SECRET_ACCESS_KEY=...
    export R2_BUCKET=mystylist-media

Uso:
    python3 carica_media.py --prova      # dice cosa farebbe, senza caricare
    python3 carica_media.py              # carica solo ciò che manca
    python3 carica_media.py --solo-json  # dopo una rigenerazione del JSON
"""

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
UI_JSON = BASE / "outfits_ui.json"

# Le foto non cambiano mai a parità di nome: un anno di cache è sicuro e
# toglie di mezzo quasi tutte le richieste al bucket.
CACHE_IMMAGINI = "public, max-age=31536000, immutable"
# Il JSON invece cambia a ogni tornata di generazione.
CACHE_JSON = "public, max-age=60"


def cliente():
    mancanti = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                            "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                if not os.environ.get(v)]
    if mancanti:
        sys.exit("Variabili d'ambiente mancanti: " + ", ".join(mancanti) +
                 "\nVedi le istruzioni in cima a questo file.")
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def da_caricare(solo_json: bool):
    """Percorsi relativi alla cartella della pipeline, che diventano le chiavi
    nel bucket: così gli URL corrispondono ai percorsi già scritti nel JSON."""
    if not UI_JSON.exists():
        sys.exit(f"{UI_JSON.name} non trovato: lancia prima build_ui_json.py")
    documento = json.loads(UI_JSON.read_text(encoding="utf-8"))

    voci = [UI_JSON.relative_to(BASE)]
    if solo_json:
        return voci

    for o in documento["outfit"]:
        if o["immagine"]:
            voci.append(Path(o["immagine"]))
            miniatura = Path("outfit_thumbs") / f"{o['id']}.webp"
            if (BASE / miniatura).exists():
                voci.append(miniatura)
        # solo le foto dei capi che stanno in questi outfit, non il catalogo
        for capo in o["capi"]:
            voci.extend(Path(p) for p in capo["immagini"])

    visti, unici = set(), []
    for v in voci:
        if v not in visti and (BASE / v).exists():
            visti.add(v)
            unici.append(v)
    return unici


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prova", action="store_true", help="non carica, elenca soltanto")
    ap.add_argument("--solo-json", action="store_true", help="solo outfits_ui.json")
    ap.add_argument("--tutto", action="store_true", help="ricarica anche ciò che c'è già")
    args = ap.parse_args()

    voci = da_caricare(args.solo_json)
    peso = sum((BASE / v).stat().st_size for v in voci)
    print(f"{len(voci)} file, {peso / 1048576:.0f} MB")

    if args.prova:
        for v in voci[:8]:
            print("   ", v)
        print("   ...")
        return

    s3 = cliente()
    bucket = os.environ["R2_BUCKET"]

    # Cosa c'è già nel bucket: ricaricare 280 MB di PNG identici a ogni giro
    # è tempo e banda buttati.
    presenti = {}
    if not args.tutto:
        pagine = s3.get_paginator("list_objects_v2")
        for pagina in pagine.paginate(Bucket=bucket):
            for oggetto in pagina.get("Contents", []):
                presenti[oggetto["Key"]] = oggetto["Size"]

    caricati = saltati = 0
    for i, v in enumerate(voci, 1):
        chiave = str(v)
        percorso = BASE / v
        dimensione = percorso.stat().st_size
        if presenti.get(chiave) == dimensione and chiave != UI_JSON.name:
            saltati += 1
            continue
        tipo = mimetypes.guess_type(chiave)[0] or "application/octet-stream"
        s3.upload_file(
            str(percorso), bucket, chiave,
            ExtraArgs={
                "ContentType": tipo,
                "CacheControl": CACHE_JSON if chiave.endswith(".json") else CACHE_IMMAGINI,
            })
        caricati += 1
        if caricati % 25 == 0:
            print(f"   [{i}/{len(voci)}] caricati {caricati}...", flush=True)

    print(f"[OK] caricati {caricati}, già presenti {saltati}")
    print("\nOra imposta sul servizio Render:")
    print("   MEDIA_BASE_URL   = https://<dominio-del-bucket>")
    print("   OUTFITS_JSON_URL = https://<dominio-del-bucket>/outfits_ui.json")


if __name__ == "__main__":
    main()

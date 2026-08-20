#!/usr/bin/env python3
"""
Miniature delle immagini generate, per le liste della UI.

Le immagini di Nano Banana sono 1792x2400 e pesano 1,9 MB l'una: in una
colonna di revisione con 151 card sono 280 MB di download per mostrare
riquadri da 68 pixel. La foto piena serve al centro, dove lo stilista giudica;
nelle liste serve una miniatura.

Sono file derivati: si possono cancellare e rifare in qualsiasi momento.

Uso:
    python3 make_thumbs.py            # solo le mancanti
    python3 make_thumbs.py --tutte    # rifà anche quelle già presenti
"""

import argparse
from pathlib import Path

from PIL import Image

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
SORGENTE = BASE / "outfit_images"
DESTINAZIONE = BASE / "outfit_thumbs"
LARGHEZZA = 360


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tutte", action="store_true", help="rigenera anche le esistenti")
    ap.add_argument("--larghezza", type=int, default=LARGHEZZA)
    args = ap.parse_args()

    DESTINAZIONE.mkdir(exist_ok=True)
    fatte, saltate, byte = 0, 0, 0

    for src in sorted(SORGENTE.glob("*.png")):
        dst = DESTINAZIONE / f"{src.stem}.webp"
        if dst.exists() and not args.tutte:
            saltate += 1
            continue
        im = Image.open(src)
        altezza = round(im.height * args.larghezza / im.width)
        im.convert("RGB").resize((args.larghezza, altezza), Image.LANCZOS).save(
            dst, "WEBP", quality=82, method=4)
        fatte += 1
        byte += dst.stat().st_size

    totali = list(DESTINAZIONE.glob("*.webp"))
    peso = sum(p.stat().st_size for p in totali) / 1048576
    print(f"[OK] {fatte} miniature create, {saltate} già presenti")
    print(f"     {len(totali)} file in {DESTINAZIONE.name}, {peso:.1f} MB in tutto "
          f"(media {peso * 1024 / max(len(totali), 1):.0f} KB)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Prepara la versione web: immagini ridimensionate e JSON che le indica.

Le immagini generate sono 1792x2400 e pesano 1,9 MB; le foto dei capi sono
JPEG da e-commerce. In tutto sono 435 MB, che vorrebbero object storage e un
account in più. Ma la UI mostra l'immagine dentro un riquadro da ~500 px e le
foto dei capi dentro riquadri da 40: a 1200 px in WebP la stessa immagine
pesa 38 KB, quarantasei volte meno, e a schermo non si distingue.

Il risultato sta in mystylist/public/media, quindi lo serve direttamente il
sito statico — niente CDN, niente bucket, niente terzo account. Gli originali
restano in locale: questi sono file derivati, si rifanno quando si vuole.

Uso:
    python3 prepara_web.py
    python3 prepara_web.py --larghezza 1400   # se si vuole più definizione
"""

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
UI = PROGETTI / "nuvolari ui" / "mystylistprojectdating-main"
DEST_MEDIA = (UI / "mystylist" / "public" / "media").resolve()
DEST_JSON = (UI / "server" / "dati").resolve()

QUALITA = 82


def ridimensiona(sorgente: Path, destinazione: Path, larghezza: int) -> int:
    """Restituisce i byte scritti; salta se il file è già aggiornato."""
    if destinazione.exists() and destinazione.stat().st_mtime >= sorgente.stat().st_mtime:
        return destinazione.stat().st_size
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    im = Image.open(sorgente)
    if im.width > larghezza:
        altezza = round(im.height * larghezza / im.width)
        im = im.resize((larghezza, altezza), Image.LANCZOS)
    im.convert("RGB").save(destinazione, "WEBP", quality=QUALITA, method=4)
    return destinazione.stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--larghezza", type=int, default=1200,
                    help="lato lungo delle immagini generate")
    ap.add_argument("--larghezza-capi", type=int, default=700,
                    help="lato lungo delle foto dei capi")
    args = ap.parse_args()

    sorgente = json.loads((BASE / "outfits_ui.json").read_text(encoding="utf-8"))
    if DEST_MEDIA.exists():
        shutil.rmtree(DEST_MEDIA)

    # per percorso di destinazione, non per conversione: lo stesso capo compare
    # in più outfit e veniva contato una volta per outfit. Il totale a schermo
    # diceva 3663 file e 119 MB dove sul disco ce n'erano 1493 e 60 MB — ed è il
    # numero su cui si decide se le immagini possono restare nel repository.
    scritti = {}

    def converti(relativo: str, larghezza: int) -> str:
        """Converte un percorso del JSON e restituisce quello nuovo (.webp)."""
        nuovo = str(Path(relativo).with_suffix(".webp"))
        origine = BASE / relativo
        if not origine.exists():
            return None
        peso = ridimensiona(origine, DEST_MEDIA / nuovo, larghezza)
        scritti[nuovo] = peso
        return nuovo

    for o in sorgente["outfit"]:
        if o.get("immagine"):
            o["immagine"] = converti(o["immagine"], args.larghezza)
        for capo in o["capi"]:
            if capo.get("immagine"):
                capo["immagine"] = converti(capo["immagine"], args.larghezza_capi)
            capo["immagini"] = [p for p in
                                (converti(i, args.larghezza_capi) for i in capo["immagini"])
                                if p]

    # le miniature sono già WebP da 8 KB: si copiano e basta
    origine_thumb = BASE / "outfit_thumbs"
    if origine_thumb.exists():
        destinazione_thumb = DEST_MEDIA / "outfit_thumbs"
        destinazione_thumb.mkdir(parents=True, exist_ok=True)
        for p in origine_thumb.glob("*.webp"):
            shutil.copy2(p, destinazione_thumb / p.name)
            scritti[f"outfit_thumbs/{p.name}"] = p.stat().st_size

    DEST_JSON.mkdir(parents=True, exist_ok=True)
    (DEST_JSON / "outfits_ui.json").write_text(
        json.dumps(sorgente, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] {len(scritti)} file in {DEST_MEDIA.relative_to(UI.resolve())}, "
          f"{sum(scritti.values()) / 1048576:.1f} MB")
    print(f"[OK] JSON in {(DEST_JSON / 'outfits_ui.json').relative_to(UI.resolve())}")
    print("\nSu Render il servizio API va configurato con:")
    print("   OUTFITS_JSON  = ./dati/outfits_ui.json")
    print("   MEDIA_BASE_URL = https://<indirizzo-del-frontend>/media")


if __name__ == "__main__":
    main()

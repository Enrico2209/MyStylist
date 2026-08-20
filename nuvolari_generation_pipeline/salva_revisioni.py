#!/usr/bin/env python3
"""Copia le revisioni visive in un JSON a parte.

Esiste per una ragione precisa: le revisioni vivono dentro i metadata.json, cioe'
dentro la stessa cartella da 1,6 GB che il 19 agosto e' stata cancellata durante
una riorganizzazione, portandosi via tre ore di chiamate a Gemini. Il JSON pesa
un megabyte e sta fuori dal catalogo: se la cartella sparisce di nuovo, si
ricostruisce lo scrape ma non si ripaga la revisione.

Va lanciato SUBITO dopo ogni revisione, non "quando capita".

    python3 salva_revisioni.py                 # salva
    python3 salva_revisioni.py --ripristina F  # rimette le revisioni nei metadata
"""

import argparse
import json
from datetime import datetime

from percorsi import CATALOGO, DATI


def salva() -> int:
    dati = {}
    for mp in CATALOGO.rglob("metadata.json"):
        m = json.loads(mp.read_text(encoding="utf-8"))
        if m.get("vision_review"):
            dati[str(mp.relative_to(CATALOGO))] = m["vision_review"]
    fonti = {v.get("fonte", "?") for v in dati.values()}
    marca = "_".join(sorted(f.split("+")[-1] for f in fonti)) or "vuoto"
    out = DATI / f"revisioni_{marca}_{datetime.now():%d%b%H%M}".lower().replace(" ", "")
    out = out.with_suffix(".json")
    out.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] {len(dati)} revisioni salvate in {out.name} ({out.stat().st_size//1024} KB)")
    print(f"     fonti: {', '.join(sorted(fonti))}")
    return len(dati)


def ripristina(percorso: str) -> int:
    dati = json.loads((DATI / percorso).read_text(encoding="utf-8"))
    n = 0
    for rel, revisione in dati.items():
        mp = CATALOGO / rel
        if not mp.exists():
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        m["vision_review"] = revisione
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        n += 1
    print(f"[OK] {n} revisioni rimesse nei metadata")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ripristina", metavar="FILE", help="nome del JSON dentro nuvolari_db")
    a = ap.parse_args()
    ripristina(a.ripristina) if a.ripristina else salva()

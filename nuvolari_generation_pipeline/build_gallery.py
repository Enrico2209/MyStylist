#!/usr/bin/env python3
"""
Galleria HTML delle immagini generate, da aprire nel browser.

Legge outfits_manifest.json e produce un file che mostra, per ogni outfit,
l'immagine generata accanto alle foto sorgente dei singoli capi: così si
verifica a occhio quello che l'audit verifica sui dati, cioè che il modello
indossi davvero i capi di quell'outfit e non altri.

I percorsi delle immagini sono relativi al file HTML e restano sul disco:
la pagina va aperta in locale, non condivisa, perché le foto dei capi non
viaggiano con lei.

Uso:
    python3 build_gallery.py                 # tutti gli outfit con immagine
    python3 build_gallery.py --limit 20
"""

import argparse
import html
import json
from pathlib import Path

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
MANIFEST = BASE / "outfits_manifest.json"
OUT = BASE / "galleria_outfit.html"

CSS = """
:root { --bg:#faf9f7; --card:#fff; --ink:#1a1a1a; --muted:#6b6b6b; --linea:#e3e0db; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#17171a; --card:#1f1f23; --ink:#ececec; --muted:#9a9a9a; --linea:#32323a; }
}
* { box-sizing:border-box; }
body { margin:0; padding:32px 24px; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:var(--muted); margin-bottom:28px; font-size:14px; }
.outfit { background:var(--card); border:1px solid var(--linea); border-radius:12px;
          padding:18px; margin-bottom:20px; display:grid; gap:20px;
          grid-template-columns:minmax(220px,300px) 1fr; align-items:start; }
@media (max-width:820px) { .outfit { grid-template-columns:1fr; } }
.generata { width:100%; border-radius:8px; display:block; }
.titolo { font-weight:600; margin:0 0 6px; }
.meta { color:var(--muted); font-size:13px; margin-bottom:14px; }
.meta b { color:var(--ink); font-weight:600; }
.capo { border-top:1px solid var(--linea); padding:10px 0; }
.capo:first-of-type { border-top:none; }
.slot { font-size:11px; text-transform:uppercase; letter-spacing:.06em;
        color:var(--muted); margin-bottom:5px; }
.nome { font-size:13px; margin-bottom:8px; }
.foto { display:flex; gap:7px; flex-wrap:wrap; }
.foto img { height:78px; width:auto; border-radius:5px; border:1px solid var(--linea);
            background:#fff; }
.foto img.esclusa { opacity:.32; filter:grayscale(1); }
.nota { font-size:12px; color:var(--muted); margin-top:6px; }
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    radice = man["radice_foto_capi"]
    voci = [v for v in man["outfit"] if v["immagine_generata"]["presente"]]
    if args.limit:
        voci = voci[:args.limit]

    parti = [f"<style>{CSS}</style>",
             "<h1>Outfit generati — MyStylist</h1>",
             f"<div class='sub'>{len(voci)} outfit · a sinistra l'immagine prodotta, "
             "a destra le foto sorgente inviate al modello. "
             "Le foto in grigio erano disponibili ma escluse dal tetto di 14 riferimenti.</div>"]

    for v in voci:
        img = html.escape(v["immagine_generata"]["percorso"])
        parti.append("<div class='outfit'>")
        parti.append(f"<div><img class='generata' src='{img}' loading='lazy' alt=''></div>")
        parti.append("<div>")
        parti.append(f"<p class='titolo'>{html.escape(v['nome'] or v['outfit_id'])}</p>")
        parti.append(
            f"<div class='meta'>id <b>{v['outfit_id']}</b> · "
            f"compatibilità <b>{v['score_compatibilita']:.3f}</b> · "
            f"formalità <b>{v['formalita']}</b> · "
            f"foto inviate <b>{v['immagine_generata']['foto_inviate_totali']}</b></div>")

        for slot, capo in v["capi"].items():
            if not capo:
                continue
            parti.append("<div class='capo'>")
            parti.append(f"<div class='slot'>{html.escape(slot)}</div>")
            parti.append(f"<div class='nome'>{html.escape(capo['titolo'])}</div>")
            parti.append("<div class='foto'>")
            for p in capo["foto_disponibili"]:
                esclusa = "" if p in capo["foto_inviate_al_modello"] else " class='esclusa'"
                parti.append(f"<img{esclusa} src='{html.escape(radice + '/' + p)}' loading='lazy' alt=''>")
            parti.append("</div>")
            if capo["foto_escluse"]:
                parti.append(f"<div class='nota'>{len(capo['foto_escluse'])} foto non inviate "
                             "(tetto di 14 riferimenti)</div>")
            parti.append("</div>")
        parti.append("</div></div>")

    Path(args.out).write_text("\n".join(parti), encoding="utf-8")
    print(f"[OK] {args.out} — {len(voci)} outfit")


if __name__ == "__main__":
    main()

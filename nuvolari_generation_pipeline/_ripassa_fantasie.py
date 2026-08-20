"""Ricontrolla la sola FANTASIA dei capi marcati con un motivo forte.

Le classificazioni di stile della revisione a 5 registri sono buone e non vanno
toccate: qui si riscrive solo il campo `pattern`, con la guida stretta su logo
e color-blocking.
"""
import json, sys
sys.path.insert(0, ".")
from google import genai
import vision_review as vr
from generate_outfit_images import load_api_key
from percorsi import CATALOGO

FORTI = ("stampato", "camouflage", "animalier")
bersagli = []
for mp in CATALOGO.rglob("metadata.json"):
    m = json.loads(mp.read_text(encoding="utf-8"))
    if ((m.get("vision_review") or {}).get("pattern")) in FORTI:
        bersagli.append((mp, m))
print(f"[*] {len(bersagli)} capi da ricontrollare", flush=True)

client = genai.Client(api_key=load_api_key())
cambiati = errori = 0
for i, (mp, m) in enumerate(bersagli, 1):
    dati, err = vr.revisiona(client, m, mp.parent)
    if err:
        errori += 1
        continue
    prima = m["vision_review"].get("pattern")
    dopo = dati.get("pattern")
    if dopo and dopo != prima:
        m["vision_review"]["pattern"] = dopo
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        cambiati += 1
    if i % 50 == 0 or i == len(bersagli):
        print(f"  [{i}/{len(bersagli)}] {cambiati} corretti, {errori} errori", flush=True)
print(f"\n[OK] {cambiati} fantasie corrette su {len(bersagli)}, {errori} errori", flush=True)

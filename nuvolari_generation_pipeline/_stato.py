import json, collections, itertools, statistics as st
import outfit_generation as og, scoring
from percorsi import DATI, CATALOGO
from pathlib import Path

pool = [json.loads(l) for l in open(DATI/"outfits_pool.jsonl", encoding="utf-8")]
ids = {o["outfit_id"] for o in pool}
print(f"POOL: {len(pool)} outfit | score mediano "
      f"{sorted(o['outfit_score'] for o in pool)[len(pool)//2]:.3f}")
c = collections.Counter()
for o in pool:
    for s in o["slots"].values():
        if s: c[s["relpath"]] += 1
print(f"  {len(c)} capi distinti, capo piu' usato {max(c.values())}x")

print(f"\nOUTFIT FOTOGRAFATO fa724644e790: {'PRESENTE' if 'fa724644e790' in ids else 'ASSENTE'}")
vecchio = [json.loads(l) for l in open(DATI/"outfits_pool.jsonl.prima_18aug_guida.bak", encoding="utf-8")] \
    if (DATI/"outfits_pool.jsonl.prima_18aug_guida.bak").exists() else []
o = next((x for x in vecchio if x["outfit_id"]=="fa724644e790"), None)
if o:
    df = og.load_and_prepare(str(DATI/"features_clustered.parquet")).set_index("relpath")
    capi = [(s, df.loc[v["relpath"]]) for s, v in o["slots"].items() if v]
    print(f"  {'coppia':24}{'colore':>8}{'stile':>8}{'totale':>8}")
    peggiore = 1
    for (sa,a),(sb,b) in itertools.combinations(capi,2):
        r = scoring.score_pair(a,b); peggiore = min(peggiore, r["score"])
        flag = "  <-- sotto soglia" if r["score"] < og.OPTIONAL_SLOT_MIN_SCORE else ""
        print(f"  {sa+'-'+sb:24}{r['color_harmony']:8.3f}{r['style_match']:8.3f}{r['score']:8.3f}{flag}")
    print(f"  minimo {peggiore:.3f} vs soglia {og.OPTIONAL_SLOT_MIN_SCORE}")

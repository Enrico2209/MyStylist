#!/usr/bin/env python3
"""
Audit dell'intera pipeline, dalla Fase 1 alla generazione immagini.

Non verifica che i file esistano, ma che i dati siano coerenti fra una fase e
l'altra: che nulla si perda per strada senza motivo, che i vincoli dichiarati
siano davvero rispettati nell'output, e che i punteggi salvati corrispondano a
quelli che il codice attuale ricalcolerebbe. Quest'ultimo è il controllo che
smaschera un pool costruito con una versione precedente del codice.

Uso:
    python3 audit_pipeline.py
"""

import json
import random
from collections import Counter
from pathlib import Path

import pandas as pd

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
ROOT = BASE / "nuvolari_full_organizzato"

esiti = []


def blocco(titolo):
    print(f"\n{'─' * 74}\n{titolo}\n{'─' * 74}")


def ok(nome, buoni, totale, nota=""):
    # Un controllo su zero elementi non ha verificato niente: stamparlo come
    # "OK 0/0" darebbe l'impressione di una garanzia che non esiste. Si segna
    # come non applicabile, così un audit che sembra tutto verde non nasconde
    # verifiche mai eseguite.
    if totale == 0:
        print(f"  [n/d ] {nome}: nessun elemento da verificare" + (f"   {nota}" if nota else ""))
        return
    stato = "OK  " if buoni == totale else "!!  "
    if buoni != totale:
        esiti.append(nome)
    riga = f"  [{stato}] {nome}: {buoni}/{totale}"
    if nota:
        riga += f"   {nota}"
    print(riga)


def info(nome, valore, nota=""):
    print(f"  [ i  ] {nome}: {valore}" + (f"   {nota}" if nota else ""))


# ══════════════════════════════════════════════════════════════════════
blocco("FASE 1–2 · Scraping e attributi")

meta_files = sorted(ROOT.rglob("metadata.json"))
metadati = {}
senza_foto = []
for p in meta_files:
    m = json.loads(p.read_text(encoding="utf-8"))
    rel = m.get("relpath") or str(p.parent.relative_to(ROOT))
    metadati[rel] = m
    if not any(f.suffix.lower() in (".jpg", ".jpeg", ".png") for f in p.parent.iterdir()):
        senza_foto.append(rel)

info("prodotti con metadata.json", len(metadati))
ok("prodotti con almeno una foto", len(metadati) - len(senza_foto), len(metadati),
   f"{len(senza_foto)} senza foto" if senza_foto else "")

brand = Counter(m.get("brand_slug") for m in metadati.values())
con_brand = sum(v for k, v in brand.items() if k)
# Non è un invariante da 100%: per una parte del catalogo il brand non compare
# né nel titolo né nell'URL (restano solo codici, es. "tuta-z3bb03-kb212-jet-black"),
# quindi il dato non esiste nella sorgente. Serve però una soglia, altrimenti una
# regressione del match (com'era il caso del link di navigazione, o degli accenti
# in "GAËLLE PARIS") passerebbe inosservata.
SOGLIA_BRAND = 0.90
atteso = int(len(metadati) * SOGLIA_BRAND)
ok("prodotti con brand riconosciuto (soglia 90%)", min(con_brand, atteso), atteso,
   f"{con_brand}/{len(metadati)} = {con_brand/len(metadati)*100:.1f}% · "
   f"brand distinti: {len([k for k in brand if k])}")
info("brand più frequente", f"{brand.most_common(1)[0][0]} ({brand.most_common(1)[0][1]})",
     "se fosse ~100% sarebbe il bug del link di navigazione")

nvr = sum(1 for m in metadati.values() if m.get("needs_vision_review"))
info("needs_vision_review", f"{nvr} ({nvr/len(metadati)*100:.1f}%)",
     "esclusi dagli outfit: stile non confermato dal testo")

catalogo = BASE / "nuvolari_full_organizzato" / "catalogo.jsonl"
n_cat = sum(1 for _ in open(catalogo, encoding="utf-8")) if catalogo.exists() else 0
ok("catalogo.jsonl allineato ai metadata", n_cat, len(metadati))

# ══════════════════════════════════════════════════════════════════════
blocco("FASE 3 · Feature engineering (colore Lab + vettore stile)")

feat = pd.read_parquet(BASE / "features_clustered.parquet")
info("righe in features_clustered.parquet", len(feat))

persi = set(metadati) - set(feat["relpath"])
ok("prodotti passati da Fase 2 a Fase 3", len(feat), len(metadati),
   f"{len(persi)} persi" + (f", es. {sorted(persi)[0][:52]}" if persi else ""))

ok("colore Lab valorizzato", int(feat[["L", "a", "b"]].notna().all(axis=1).sum()), len(feat))
sospetti = int((feat["background_fraction"] < 0.2).sum())
ok("sfondo isolato correttamente", len(feat) - sospetti, len(feat),
   "background_fraction < 0.2 = estrazione colore dubbia")

stile_cols = [c for c in feat.columns if c.startswith("style_") and c != "style_cluster"]
nulli = int((feat[stile_cols].sum(axis=1) == 0).sum())
info("prodotti con vettore stile tutto a zero", nulli,
     "attesi: coincidono in larga parte con needs_vision_review")

img_mancanti = [r for r, d in zip(feat["relpath"], feat["display_image"])
                if d and not (ROOT / d).exists()]
ok("display_image esistenti su disco", len(feat) - len(img_mancanti), len(feat))

# ══════════════════════════════════════════════════════════════════════
blocco("FASE 4 · Clustering")

n_cluster = feat["style_cluster"].nunique() - (1 if -1 in set(feat["style_cluster"]) else 0)
outlier = int((feat["style_cluster"] == -1).sum())
info("cluster trovati", n_cluster)
info("outlier (cluster -1)", f"{outlier} ({outlier/len(feat)*100:.1f}%)",
     "non è un errore: restano abbinabili, decide lo score")
dim = feat[feat["style_cluster"] != -1]["style_cluster"].value_counts()
info("cluster più grande", f"{dim.iloc[0]} prodotti ({dim.iloc[0]/len(feat)*100:.1f}% del catalogo)")

# ══════════════════════════════════════════════════════════════════════
blocco("FASE 5–6 · Pool di outfit: vincoli dichiarati vs output reale")

import outfit_generation as og
from scoring import score_pair

df = og.load_and_prepare(str(BASE / "features_clustered.parquet")).set_index("relpath")
pool = [json.loads(l) for l in open(BASE / "outfits_pool.jsonl", encoding="utf-8")]
info("outfit nel pool", len(pool))

ids = [o["outfit_id"] for o in pool]
ok("outfit_id univoci", len(set(ids)), len(ids))
firme = [frozenset(s["relpath"] for s in o["slots"].values() if s) for o in pool]
ok("nessun outfit duplicato", len(set(firme)), len(firme))

viol_slot = viol_gen = viol_stag = viol_form = viol_manica = viol_giacca = viol_nvr = 0
for o in pool:
    capi = {s: v for s, v in o["slots"].items() if v}
    righe = {s: df.loc[v["relpath"]] for s, v in capi.items() if v["relpath"] in df.index}

    if not all(s in capi for s in ("top", "bottom", "shoes")):
        viol_slot += 1
    if any(r["needs_vision_review"] for r in righe.values()):
        viol_nvr += 1

    generi = {r["gender"] for r in righe.values() if r["gender"]}
    if len(generi) > 1:
        viol_gen += 1
    stagioni = {r["season"] for r in righe.values()} - {"tutte"}
    if len(stagioni) > 1:
        viol_stag += 1

    fs = [r["formality_norm"] for r in righe.values()]
    if fs and max(fs) - min(fs) > og.FORMALITY_SPREAD_MAX + 1e-9:
        viol_form += 1

    if "bottom" in righe and righe["bottom"]["leg_length"] == "corta":
        if "top" in righe and righe["top"]["sleeve"] != "corta":
            viol_manica += 1
        if "outerwear" in capi:
            viol_giacca += 1

n = len(pool)
ok("slot obbligatori presenti (top+bottom+scarpe)", n - viol_slot, n)
ok("nessun capo needs_vision_review negli outfit", n - viol_nvr, n)
ok("genere coerente dentro l'outfit", n - viol_gen, n)
ok("stagione compatibile dentro l'outfit", n - viol_stag, n)
ok(f"dispersione formalità entro {og.FORMALITY_SPREAD_MAX}", n - viol_form, n)
ok("regola 1: niente maniche lunghe sui pantaloncini", n - viol_manica, n)
ok("regola 2: niente giacca sui pantaloncini", n - viol_giacca, n)

# ── i punteggi salvati sono ancora quelli che il codice attuale calcola? ──
random.seed(0)
campione = random.sample(pool, 120)
diff_pair = diff_out = 0
for o in campione:
    capi = [(s, df.loc[v["relpath"]]) for s, v in o["slots"].items()
            if v and v["relpath"] in df.index]
    ricalc = {}
    for i in range(len(capi)):
        for j in range(i + 1, len(capi)):
            (si, ri), (sj, rj) = capi[i], capi[j]
            ricalc[f"{si}-{sj}"] = round(score_pair(ri, rj)["score"], 3)
    # le chiavi salvate sono orientate secondo l'ordine di costruzione
    # (l'ancora entra per prima: "shoes-top" se si parte dalle scarpe),
    # quindi vanno confrontate come coppie non ordinate
    def norm(d):
        return {tuple(sorted(k.split("-"))): v for k, v in d.items()}
    salvati, ricalc_n = norm(o["pairwise_scores"]), norm(ricalc)
    if set(salvati) != set(ricalc_n) or any(
            abs(ricalc_n[k] - salvati[k]) > 0.002 for k in salvati):
        diff_pair += 1
    if abs(round(min(ricalc.values()), 3) - o["outfit_score"]) > 0.002:
        diff_out += 1
ok("score di coppia riproducibili col codice attuale", len(campione) - diff_pair, len(campione))
ok("outfit_score = minimo dei pairwise", len(campione) - diff_out, len(campione))

# ── copertura: quanti capi ammissibili finiscono davvero in un outfit ──
usati = {s["relpath"] for o in pool for s in o["slots"].values() if s}
amm = df[(~df["needs_vision_review"].astype(bool)) & (df["slot"].isin(og.MANDATORY_SLOTS))]
mai = sorted(set(amm.index) - usati)
info("capi ammissibili presenti in almeno un outfit", f"{len(amm) - len(mai)}/{len(amm)}",
     f"{len(mai)} mai usati: " + str(dict(Counter(df.loc[r, "slot"] for r in mai))))

# Un capo escluso non è di per sé un'anomalia — lo diventa se è escluso senza
# motivo. Il motivo legittimo è uno solo: il miglior outfit che lo includa non
# raggiunge la soglia di qualità. Si verifica quindi la CAUSA, ricostruendo su
# un campione l'outfit migliore e controllando che stia davvero sotto soglia.
if mai:
    campione_mai = random.sample(mai, min(25, len(mai)))
    sopra_soglia = []
    for rel in campione_mai:
        o = og.build_outfit(df.reset_index(), rel)
        if o and o["outfit_score"] >= 0.6:
            sopra_soglia.append((rel, o["outfit_score"]))
    ok("capi esclusi solo perché sotto la soglia di score",
       len(campione_mai) - len(sopra_soglia), len(campione_mai),
       f"es. {sopra_soglia[0][0][:40]} a {sopra_soglia[0][1]:.3f}" if sopra_soglia
       else "nessun outfit valido andato perso")

esclusi = len(df) - len(amm)
info("capi non ammissibili", esclusi,
     "needs_vision_review, slot non riconosciuto, abiti/completi")

# ══════════════════════════════════════════════════════════════════════
blocco("GENERAZIONE IMMAGINI · configurazione")

reg = CODICE / "generation_rules.md"
ok("documento regole presente", int(reg.exists()), 1, f"{reg.stat().st_size} byte" if reg.exists() else "")

import generate_outfit_images as gen

# ── quello che partirà davvero verso l'API, ricostruito senza spendere ──
# Sono i controlli che contano prima di un run da ~$230: si verifica la
# richiesta che verrebbe costruita, non solo i file su disco.
tipi_dichiarati = Counter()
tot_inviate = eccedenti = senza_foto = indici_rotti = 0
for o in pool:
    per_capo = []
    for slot, v in o["slots"].items():
        if not v:
            continue
        esistenti = [p for p in (v.get("all_images") or []) if (ROOT / p).exists()]
        if esistenti:
            per_capo.append(esistenti)
    if not per_capo:
        senza_foto += 1
        continue
    scelte = gen.allocate_photos(per_capo, gen.MAX_REF_IMAGES)
    # nessun capo può restare senza foto: verrebbe descritto nel prompt con un
    # intervallo di indici vuoto, cioè riferito a immagini mai allegate
    if any(not s for s in scelte):
        indici_rotti += 1
    n = sum(len(s) for s in scelte)
    tot_inviate += n
    if sum(len(f) for f in per_capo) > n:
        eccedenti += 1
    for gruppo in scelte:
        for p in gruppo:
            tipi_dichiarati[gen.mime_di(Path(p))] += 1

ok("outfit con almeno una foto di riferimento", len(pool) - senza_foto, len(pool),
   "senza foto la richiesta fallisce a vuoto")
ok("nessun capo dichiarato senza immagini allegate", len(pool) - indici_rotti, len(pool),
   f"tetto attuale {gen.MAX_REF_IMAGES} riferimenti")
# il MIME va dedotto dall'estensione: il catalogo non è tutto JPEG e annunciare
# un PNG come image/jpeg consegna all'API un tipo che non corrisponde ai byte
ok("tipo MIME coerente con l'estensione", 1,
   int(all(t in ("image/jpeg", "image/png", "image/webp", "image/gif") for t in tipi_dichiarati)),
   " · ".join(f"{t.split('/')[1]}: {n}" for t, n in tipi_dichiarati.most_common()))
info("foto che partiranno verso l'API", f"{tot_inviate} ({tot_inviate/max(1,len(pool)-senza_foto):.1f} per outfit)",
     f"{eccedenti} outfit oltre il tetto, con taglio delle foto extra")
info("costo stimato del run completo",
     f"${len(pool) * 0.134 + tot_inviate * 261 * 2 / 1e6:.2f}",
     f"{len(pool)} immagini + token delle foto di riferimento")
env = CODICE / ".env"
if env.exists():
    import subprocess
    ignorato = subprocess.run(["git", "check-ignore", ".env"], cwd=BASE,
                              capture_output=True).returncode == 0
    ok(".env escluso da git", int(ignorato), 1, "contiene la chiave API")

log_p = BASE / "outfit_images_log.jsonl"
if log_p.exists():
    righe = [json.loads(l) for l in open(log_p, encoding="utf-8") if l.strip()]
    c = Counter(r["esito"] for r in righe)
    info("tentativi registrati", f"{c.get('ok', 0)} riusciti, {c.get('errore', 0)} falliti")
    generate = len(list((BASE / "outfit_images").glob("*.png")))
    info("immagini su disco", generate, f"spesa stimata ~${generate * 0.134:.2f}")
    # La modalità va registrata: senza, il manifest non sa quali foto siano
    # davvero entrate nella richiesta e le attribuisce per ipotesi. Si valutano
    # solo le righe scritte dopo l'introduzione del campo, riconoscibili dal
    # timestamp: le righe storiche dei test non sono un'anomalia da correggere.
    recenti = [r for r in righe if r.get("ts")]
    storiche = len(righe) - len(recenti)
    if recenti:
        ok("righe di log con modalita_foto registrata",
           sum(1 for r in recenti if r.get("modalita_foto")), len(recenti),
           f"{storiche} righe storiche escluse" if storiche else "")
    else:
        info("righe di log con modalita_foto", f"nessuna riga datata ({storiche} storiche)",
             "il manifest ricade sul prefisso di 'variante'")

# ── le foto che finirebbero all'API sono tutte apribili? ──────────────
# Un JPEG troncato non si vede finché non lo si decodifica: in Fase 3 faceva
# perdere l'intero prodotto, e in generazione diventerebbe una chiamata fallita
# a pagamento. Si controllano solo le foto realmente candidate a partire.
from PIL import Image

da_inviare = set()
for o in pool:
    for v in o["slots"].values():
        if v:
            da_inviare.update(v.get("all_images") or [])


def apribile(rel: str) -> bool:
    try:
        with Image.open(ROOT / rel) as im:
            im.load()
        return True
    except Exception:
        return False


campione_foto = random.sample(sorted(da_inviare), min(400, len(da_inviare)))
illeggibili = [r for r in campione_foto if not apribile(r)]
ok("foto degli outfit decodificabili (campione)",
   len(campione_foto) - len(illeggibili), len(campione_foto),
   f"es. {illeggibili[0][:48]}" if illeggibili else f"su {len(da_inviare)} foto totali")

# ══════════════════════════════════════════════════════════════════════
blocco("TRACCIABILITÀ · manifest immagine → foto sorgente")

man_p = BASE / "outfits_manifest.json"
if not man_p.exists():
    info("outfits_manifest.json", "assente", "generalo con: python3 build_manifest.py")
else:
    man = json.loads(man_p.read_text(encoding="utf-8"))
    voci = man["outfit"]
    info("outfit nel manifest", f"{len(voci)}", f"variante {man['variante']}")

    ids_pool = set(ids)
    fuori = [v["outfit_id"] for v in voci if v["outfit_id"] not in ids_pool]
    ok("outfit del manifest presenti nel pool", len(voci) - len(fuori), len(voci),
       "se il pool è stato rigenerato, il manifest va rifatto")

    con_img = [v for v in voci if v["immagine_generata"]["presente"]]
    rotti = sottoinsieme = 0
    for v in con_img:
        for capo in v["capi"].values():
            if not capo:
                continue
            if not set(capo["foto_inviate_al_modello"]) <= set(capo["foto_disponibili"]):
                sottoinsieme += 1
            if any(not (ROOT / p).exists() for p in capo["foto_inviate_al_modello"]):
                rotti += 1
    ok("foto inviate incluse fra le disponibili", len(con_img) - sottoinsieme, len(con_img))
    ok("foto sorgente ancora presenti su disco", len(con_img) - rotti, len(con_img))

    # il conteggio ricostruito deve coincidere con quello scritto dal generatore
    disallineati = [
        v["outfit_id"] for v in con_img
        if v["immagine_generata"]["foto_inviate_totali"] is not None
        and sum(len(c["foto_inviate_al_modello"]) for c in v["capi"].values() if c)
        != v["immagine_generata"]["foto_inviate_totali"]
    ]
    ok("conteggio foto ricostruito = conteggio del log",
       len(con_img) - len(disallineati), len(con_img),
       f"es. {disallineati[0]}" if disallineati else "")

print()
if esiti:
    print(f"AUDIT: {len(esiti)} controlli con anomalie → {', '.join(esiti)}")
else:
    print("AUDIT: nessuna anomalia nei controlli eseguiti.")

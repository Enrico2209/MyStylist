#!/bin/bash
# Rimette il catalogo nello stato di prima della cancellazione del 19 agosto:
# descrizioni dal sito, poi revisione a 5 registri, poi la pipeline fino al pool.
# Si ferma prima delle immagini e del push, che costano e si pubblicano.
set -e
cd "$(dirname "$0")"
LOG=/private/tmp/claude-501/-Users-enricociaralli-Desktop-nuvolari-files/153b8a54-cf02-4edf-9a35-bcb4d7ac6a36/scratchpad

echo "── 1/6  descrizioni complete dal sito ───────────────────────────"
python3 -c "
from scrape_with_attributes import scarica_descrizioni
from percorsi import CATALOGO
scarica_descrizioni(str(CATALOGO), delay=0.3)"

echo "── 2/6  revisione Gemini a 5 registri ───────────────────────────"
python3 vision_review.py --tutti --rifai

echo "── 3/6  BACKUP delle revisioni (subito, non dopo) ───────────────"
python3 salva_revisioni.py

echo "── 4/6  catalogo.jsonl + features ───────────────────────────────"
python3 -c "
from scrape_with_attributes import rebuild_catalog
from feature_engineering import build_feature_table, add_display_and_gallery_columns
from percorsi import CATALOGO, DATI
rebuild_catalog(str(CATALOGO))
CAT, FEA = str(CATALOGO), str(DATI/'features.parquet')
build_feature_table(root=CAT, out_parquet=FEA)
add_display_and_gallery_columns(FEA, CAT, out_parquet=FEA)
# Niente applica_idf: vedi il commento in feature_engineering.applica_idf."

echo "── 5/6  clustering ──────────────────────────────────────────────"
python3 -c "
from clustering import run_clustering
from percorsi import DATI
run_clustering(features_parquet=str(DATI/'features.parquet'),
               out_parquet=str(DATI/'features_clustered.parquet'))"

echo "── 6/6  pool + selezione dei 100 eterogenei ─────────────────────"
python3 -c "
import outfit_generation as og
from percorsi import DATI
df = og.load_and_prepare(str(DATI/'features_clustered.parquet'))
og.run_outfit_pipeline(df, out_jsonl=str(DATI/'outfits_pool.jsonl'))"
python3 scegli_eterogenei.py --quanti 100

echo
echo "════════ RECUPERO CONCLUSO — foto e push NON fatti ════════"

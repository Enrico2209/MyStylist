#!/bin/bash
# Catena completa: aspetta lo scrape delle descrizioni, poi rifa' tutto fino al
# pool. Si ferma prima delle immagini e del push, che costano e si pubblicano.
set -e
cd "$(dirname "$0")"
CODICE="$(pwd)"
# I dati non stanno piu' accanto al codice: la cartella la dice percorsi.py,
# cosi' shell e Python leggono la stessa definizione e non possono divergere.
DB="$(python3 -c 'from percorsi import DATI; print(DATI)')"
MARCA="$(date +%d%b | tr 'A-Z' 'a-z')_guida"

echo "── 0/6  attendo la fine dello scrape descrizioni ────────────────"
while pgrep -f "scarica_descrizioni" > /dev/null; do sleep 30; done
echo "    scrape concluso: $(tail -1 /private/tmp/claude-501/-Users-enricociaralli-Desktop-nuvolari-files/153b8a54-cf02-4edf-9a35-bcb4d7ac6a36/scratchpad/descrizioni.log)"

for f in "$DB"/features.parquet "$DB"/features_clustered.parquet "$DB"/outfits_pool.jsonl; do
  [ -f "$f" ] && cp -p "$f" "$f.prima_$MARCA.bak" && echo "    copia: $(basename "$f").prima_$MARCA.bak"
done

echo "── 1/6  catalogo.jsonl con le descrizioni nuove ─────────────────"
python3 -c "import sys;sys.path.insert(0,'.');from scrape_with_attributes import rebuild_catalog;from percorsi import CATALOGO; rebuild_catalog(str(CATALOGO))"

echo "── 2/6  revisione Gemini su tutto il catalogo (guida nuova) ─────"
python3 vision_review.py --tutti --rifai

echo "── 3/6  features (colore + vettori di stile + formalita') ───────"
python3 -c "
import sys;sys.path.insert(0,'.')
from feature_engineering import build_feature_table, add_display_and_gallery_columns, applica_idf
from percorsi import CATALOGO, DATI
CAT, FEA = str(CATALOGO), str(DATI/'features.parquet')
build_feature_table(root=CAT, out_parquet=FEA)
add_display_and_gallery_columns(FEA, CAT, out_parquet=FEA)
applica_idf(FEA, out_parquet=FEA)
"

echo "── 4/6  clustering ──────────────────────────────────────────────"
python3 -c "import sys;sys.path.insert(0,'.');from clustering import run_clustering;from percorsi import DATI; run_clustering(features_parquet=str(DATI/'features.parquet'),out_parquet=str(DATI/'features_clustered.parquet'))"

echo "── 5/6  pool di outfit ──────────────────────────────────────────"
python3 -c "
import sys;sys.path.insert(0,'.')
import outfit_generation as og
from percorsi import DATI
df = og.load_and_prepare(str(DATI/'features_clustered.parquet'))
og.run_outfit_pipeline(df, out_jsonl=str(DATI/'outfits_pool.jsonl'))
"

echo "── 6/6  selezione dei 100 eterogenei ────────────────────────────"
python3 scegli_eterogenei.py --quanti 100 --ids-out "$DB"/ids_eterogenei.txt

echo
echo "════════ CATENA CONCLUSA — foto e push NON fatti ════════"

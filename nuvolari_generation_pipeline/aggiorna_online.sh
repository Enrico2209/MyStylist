#!/bin/bash
#
# Porta online tutto quello che è cambiato in locale, nell'ordine giusto.
#
# Esiste perché i file pubblicati da Render non sono quelli della pipeline ma
# due copie derivate — il JSON in server/dati e le foto ridimensionate in
# mystylist/public/media. Pushare senza averle rifatte non dà nessun errore:
# il deploy riesce e online resta il dato vecchio. Questo script rende quel
# passaggio impossibile da saltare.
#
# Uso:
#     ./aggiorna_online.sh
#     ./aggiorna_online.sh "messaggio del commit"

set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
DB="$(python3 -c 'from percorsi import DATI; print(DATI)')"
# Due livelli sopra e non uno: il codice sta in nuvolari_generation_pipeline,
# il repo della UI e' fratello di "nuvolari backend".
UI="$BASE/../../nuvolari ui/mystylistprojectdating-main"
MESSAGGIO="${1:-Aggiornamento immagini e dati}"

cd "$BASE"

echo "── 1/6  JSON per la UI ──────────────────────────────────────────"
python3 build_ui_json.py

echo "── 2/6  miniature ───────────────────────────────────────────────"
python3 make_thumbs.py

echo "── 3/6  versione web (immagini ridotte + JSON nel repo) ─────────"
python3 prepara_web.py

echo "── 4/6  manifest di tracciabilità ───────────────────────────────"
python3 build_manifest.py

echo "── 5/6  database: gli outfit nuovi entrano in revisione ─────────"
# dalla sua cartella, altrimenti dotenv non trova il .env e fallisce in
# silenzio con un errore vuoto
(cd "$UI/server" && node seed_outfits.js)

echo "── 6/6  copia dei sorgenti nel repo ─────────────────────────────"
# Il codice della pipeline vive qui fuori, che non è in nessun repository.
# Ricopiarlo a ogni pubblicazione è l'unico modo perché la versione archiviata
# non resti indietro rispetto a quella che ha davvero prodotto gli outfit
# online. Solo sorgenti: niente dati, niente foto, niente .env (vedi
# pipeline/LEGGIMI.md).
mkdir -p "$UI/pipeline"
cp *.py *.md requirements.txt aggiorna_online.sh "$UI/pipeline/"
cp "$DB/brand_list.json" "$UI/pipeline/"

# rete di sicurezza: se una chiave finisse in un sorgente, qui si fermerebbe
# prima del commit invece di finire su GitHub
# --exclude su sé stesso: questo file contiene i pattern per definizione, e
# senza l'esclusione il controllo si autodenuncia a ogni esecuzione
if grep -rniE --exclude=aggiorna_online.sh \
     "AQ\.Ab8|AIza[0-9A-Za-z_-]{20,}|npg_|postgres(ql)?://[^ ]*:[^ ]*@" "$UI/pipeline" ; then
    echo "[!] possibile credenziale nei sorgenti copiati: non pubblico." >&2
    exit 1
fi

echo "── controllo prima di pubblicare ────────────────────────────────"
python3 audit_pipeline.py | tail -1
python3 audit_manifest.py | tail -1

cd "$UI"

# build_ui_json riscrive anche "generato_il", quindi il JSON risulta modificato
# a ogni esecuzione anche quando i dati sono identici. Senza questo controllo
# ogni lancio produrrebbe un commit finto e un rideploy inutile di Render.
if [ "$(git status --porcelain | wc -l | tr -d ' ')" = "1" ] \
   && [ -n "$(git status --porcelain server/dati/outfits_ui.json)" ]; then
    if python3 - <<'PY'
import json, pathlib, subprocess, sys
vecchio = subprocess.run(["git", "show", "HEAD:server/dati/outfits_ui.json"],
                         capture_output=True, text=True)
if vecchio.returncode != 0:
    sys.exit(1)                       # file nuovo: c'è davvero da pubblicare
a = json.loads(vecchio.stdout)
b = json.loads(pathlib.Path("server/dati/outfits_ui.json").read_text(encoding="utf-8"))
a.pop("generato_il", None)
b.pop("generato_il", None)
sys.exit(0 if a == b else 1)
PY
    then
        git checkout -- server/dati/outfits_ui.json
    fi
fi

if [ -z "$(git status --porcelain)" ]; then
    echo
    echo "[=] niente da pubblicare: online è già allineato."
    exit 0
fi

git add -A
git commit -q -m "$MESSAGGIO"
git push origin main

echo
echo "[OK] pubblicato. Render ricostruisce da sé i due servizi (qualche minuto)."
echo "     Verifica: https://mystylist-online.onrender.com/api/salute"
echo "     La UI:    https://mystylist-ui.onrender.com"

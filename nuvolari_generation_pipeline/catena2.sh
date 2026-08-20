#!/bin/bash
# Seconda meta': immagini a pagamento + pubblicazione. Parte quando catena.sh
# ha finito. I controlli sotto non sono burocrazia: fra qui e il push ci sono
# ~$13 di immagini e un deploy su Render, e un pool venuto male si riconosce
# prima di spendere, non dopo.
set -e
cd "$(dirname "$0")"
DB="$(python3 -c 'from percorsi import DATI; print(DATI)')"
LOG=/private/tmp/claude-501/-Users-enricociaralli-Desktop-nuvolari-files/153b8a54-cf02-4edf-9a35-bcb4d7ac6a36/scratchpad

echo "── attendo la fine di catena.sh ─────────────────────────────────"
while pgrep -f "catena.sh" > /dev/null; do sleep 60; done

if ! grep -q "CATENA CONCLUSA" "$LOG/catena.log"; then
  echo "[STOP] catena.sh non e' arrivata in fondo. Niente foto, niente push."
  tail -20 "$LOG/catena.log"; exit 1
fi

N_POOL=$(wc -l < "$DB"/outfits_pool.jsonl | tr -d ' ')
N_IDS=$(wc -l < "$DB"/ids_eterogenei.txt | tr -d ' ')
echo "    pool: $N_POOL outfit | selezionati: $N_IDS"
if [ "$N_POOL" -lt 500 ] || [ "$N_IDS" -lt 50 ]; then
  echo "[STOP] pool o selezione troppo piccoli: qualcosa e' andato storto a monte."
  exit 1
fi

echo "── 7/8  immagini (gemini-3-pro-image) ───────────────────────────"
python3 generate_outfit_images.py --resume --only $(tr '\n' ' ' < "$DB"/ids_eterogenei.txt)

echo "── 8/8  pubblicazione (web + sorgenti + git push) ───────────────"
./aggiorna_online.sh

echo
echo "════════ FATTO: pool, foto e pubblicazione ════════"

"""Dove stanno le cose, ora che codice e dati vivono in cartelle separate.

Prima ogni modulo faceva `BASE = Path(__file__).resolve().parent` e cercava i
dati come fratelli del sorgente. Separando le due cose quella riga smette di
funzionare in venti file insieme — quindi la risposta sta qui, in un posto solo.

La riorganizzazione e' pensata per essere a diff minimo: i moduli continuano a
chiamare `BASE` e a scrivere `BASE / "outfits_pool.jsonl"`, solo che ora `BASE`
arriva da qui e punta ai dati. Le poche righe che volevano davvero il codice —
la guida di classificazione, le regole di generazione, il .env — usano CODICE.

    nuvolari backend/
      ├── nuvolari_generation_pipeline/   <- CODICE (questo file ci sta dentro)
      ├── nuvolari_db/                    <- DATI
      └── ...                             <- RADICE: log, backup, roba superata
"""

from pathlib import Path

CODICE = Path(__file__).resolve().parent
RADICE = CODICE.parent
DATI = RADICE / "nuvolari_db"

# Il catalogo NON sta in nuvolari_db, e la ragione vale piu' dell'ordine.
#
# Il 19 agosto una riorganizzazione lo ha cancellato — 1,6 GB, 2901 metadata,
# tre ore di revisione Gemini — e l'unica cosa che lo ha riportato indietro e'
# stato `git restore`, perche' foto e metadata sono tracciati qui via LFS.
# Spostarlo dentro nuvolari_db significa portarlo fuori dalla working tree e
# rinunciare a quella rete. La cartella ordinata non vale 1,6 GB.
CATALOGO = RADICE / "nuvolari_full_organizzato"

# Restano in radice per scelta esplicita: sono materiale superato che nessun
# outfit riferisce piu', ma ripesca_orfane ci guarda dentro quando ricontrolla
# le composizioni, quindi il percorso deve restare giusto.
IMMAGINI_SUPERATE = RADICE / "outfit_images_superate"

# Il repository della UI sta fuori da "nuvolari backend", accanto ad esso.
PROGETTI = RADICE.parent

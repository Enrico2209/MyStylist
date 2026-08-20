#!/usr/bin/env python3
"""
Sceglie N outfit il più diversi possibile fra loro, per una tornata di revisione.

Differenza rispetto a seleziona_campione.py: quello prende gli estremi di una
lista di casi decisi a mano (il più formale, quello con la sciarpa, quello con
la borsa...). Va bene per verificare difetti già noti, ma non copre quello che
non abbiamo pensato di mettere in lista — ed è esattamente lì che si nascondono
i pattern di errore che uno stilista riconosce e noi no.

Qui la diversità non è un elenco: è una distanza. Ogni outfit diventa un punto
in uno spazio che mette insieme stile, colore, formalità, stagione, capi
presenti e genere; poi si prende il punto centrale e si aggiunge ogni volta
l'outfit PIÙ LONTANO da tutti quelli già scelti (farthest point sampling). Il
risultato copre lo spazio invece di addensarsi dove il catalogo è più fitto:
con 50 outfit presi in ordine di pool si guardano cinquanta variazioni della
stessa idea, con 50 presi così si guardano cinquanta idee.

Si parte dal centro e non da un estremo di proposito: il primo outfit deve
essere rappresentativo del catalogo, non un caso limite. Gli estremi arrivano
comunque subito dopo, perché sono i più lontani.

Uso:
    python3 campione_diverso.py --n 50
    python3 campione_diverso.py --n 50 --comando     # riga pronta da eseguire
    python3 campione_diverso.py --n 50 --includi-fatti
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from clustering import STYLE_TAG_COLUMNS
from generate_outfit_images import load_formality, outfit_formality

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
POOL = BASE / "outfits_pool.jsonl"
IMG_DIR = BASE / "outfit_images"
PARQUET = BASE / "features_clustered.parquet"

SLOTS = ["top", "bottom", "shoes", "outerwear", "accessory"]
SEASON_COLUMNS = ["season_estate", "season_inverno", "season_mezza_stagione", "season_tutte"]

# Quanto pesa ciascun blocco nella distanza. Sono tutti riportati a scarto
# unitario prima di essere pesati, altrimenti i dieci tag di stile
# schiaccerebbero da soli la formalità, che è un numero solo ma conta quanto
# loro nel dire se due outfit si somigliano.
PESI = {
    "stile": 1.0,
    "colore": 1.0,
    "formalita": 1.0,
    "stagione": 0.6,
    "slot": 0.7,
    "genere": 0.7,
}

# Tetto di outfit presi dallo stesso cluster di stile. Senza, una zona molto
# popolata e molto "larga" può prendersi parecchi posti pur restando internamente
# varia: il campione resterebbe diverso in senso geometrico ma poco utile a chi
# lo guarda, che vedrebbe sfilare sempre lo stesso registro.
MAX_PER_CLUSTER = 6


def vettore_outfit(outfit: dict, df: pd.DataFrame, formalita: float) -> np.ndarray:
    """Un outfit -> un punto. I capi vengono mediati: due outfit che condividono
    tre capi su quattro devono risultare vicini, ed è quello che serve qui."""
    capi = [s["relpath"] for s in outfit["slots"].values() if s]
    righe = df.reindex([c for c in capi if c in df.index])

    stile = righe[STYLE_TAG_COLUMNS].to_numpy(dtype=float).mean(axis=0)
    stagione = righe[SEASON_COLUMNS].to_numpy(dtype=float).mean(axis=0)

    # Colore: media dei dominanti in Lab, più la croma massima dell'outfit.
    # La croma da sola distingue un total black da un outfit con una tinta
    # accesa, che in Lab medio possono finire vicini.
    lab = righe[["L", "a", "b"]].to_numpy(dtype=float)
    croma = float(np.hypot(lab[:, 1], lab[:, 2]).max()) if len(lab) else 0.0
    colore = np.concatenate([lab.mean(axis=0), [croma]])

    slot = np.array([1.0 if outfit["slots"].get(s) else 0.0 for s in SLOTS])
    genere = np.array([1.0 if outfit.get("gender") == "donna" else 0.0])

    return np.concatenate([stile, colore, [formalita], stagione, slot, genere])


def blocchi(dim_stile: int) -> list:
    """(nome del blocco, indici che occupa) — serve per pesarli separatamente."""
    i = 0
    fette = []
    for nome, larghezza in [("stile", dim_stile), ("colore", 4), ("formalita", 1),
                            ("stagione", len(SEASON_COLUMNS)), ("slot", len(SLOTS)),
                            ("genere", 1)]:
        fette.append((nome, slice(i, i + larghezza)))
        i += larghezza
    return fette


def normalizza(X: np.ndarray) -> np.ndarray:
    """Standardizza colonna per colonna e applica i pesi di blocco.

    Senza standardizzare, la distanza sarebbe dominata dalla luminosità L (0-100)
    e dalla croma: due outfit differirebbero soprattutto per quanto sono chiari.
    """
    sigma = X.std(axis=0)
    sigma[sigma == 0] = 1.0
    Z = (X - X.mean(axis=0)) / sigma
    for nome, fetta in blocchi(len(STYLE_TAG_COLUMNS)):
        # diviso per la radice della larghezza: un blocco di dieci colonne non
        # deve contare dieci volte un blocco di una sola
        larghezza = len(range(*fetta.indices(X.shape[1])))
        Z[:, fetta] *= PESI[nome] / math.sqrt(larghezza)
    return Z


def farthest_point(Z: np.ndarray, n: int, cluster: list, max_per_cluster: int) -> list:
    """Indici scelti: prima il più centrale, poi ogni volta il più lontano da
    tutti i già scelti (max-min)."""
    centro = Z.mean(axis=0)
    primo = int(np.argmin(np.linalg.norm(Z - centro, axis=1)))

    scelti = [primo]
    conteggio = {cluster[primo]: 1}
    distanza = np.linalg.norm(Z - Z[primo], axis=1)

    while len(scelti) < n:
        ordine = np.argsort(-distanza)
        for idx in ordine:
            idx = int(idx)
            if idx in scelti:
                continue
            if conteggio.get(cluster[idx], 0) >= max_per_cluster:
                continue
            break
        else:
            break  # nessun candidato ammissibile: il tetto per cluster ha esaurito il pool
        scelti.append(idx)
        conteggio[cluster[idx]] = conteggio.get(cluster[idx], 0) + 1
        distanza = np.minimum(distanza, np.linalg.norm(Z - Z[idx], axis=1))
    return scelti


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--includi-fatti", action="store_true",
                    help="considera anche gli outfit che hanno già l'immagine")
    ap.add_argument("--comando", action="store_true", help="stampa il comando pronto")
    ap.add_argument("--out", default="", help="scrive gli id, uno per riga, in questo file")
    args = ap.parse_args()

    df = pd.read_parquet(PARQUET).set_index("relpath")
    pool = [json.loads(l) for l in open(POOL, encoding="utf-8")]

    if not args.includi_fatti:
        fatti = {p.stem for p in IMG_DIR.glob("*.png")}
        pool = [o for o in pool if o["outfit_id"] not in fatti]
        print(f"[*] {len(fatti)} outfit hanno già l'immagine, restano {len(pool)} candidati")

    if not pool:
        raise SystemExit("Nessun outfit da scegliere.")

    tab = load_formality()
    forma = [outfit_formality(o, tab) for o in pool]
    X = np.vstack([vettore_outfit(o, df, f) for o, f in zip(pool, forma)])
    Z = normalizza(X)

    # cluster dominante fra i capi dell'outfit, solo per il tetto per cluster
    cluster = []
    for o in pool:
        capi = [s["relpath"] for s in o["slots"].values() if s and s["relpath"] in df.index]
        etichette = [int(df.loc[c, "style_cluster"]) for c in capi] or [-1]
        cluster.append(max(set(etichette), key=etichette.count))

    scelti = farthest_point(Z, args.n, cluster, MAX_PER_CLUSTER)

    print(f"\nCampione di {len(scelti)} outfit scelti per massima diversità reciproca:\n")
    for posto, idx in enumerate(scelti, 1):
        o = pool[idx]
        capi = sum(1 for s in o["slots"].values() if s)
        print(f"  {posto:2d}. {o['outfit_id']}  form {forma[idx]:.2f} · "
              f"score {o['outfit_score']:.3f} · {capi} capi · "
              f"{(o.get('gender') or 'unisex'):6s} · cl {cluster[idx]:2d} · {o['label'][:66]}")

    ids = [pool[i]["outfit_id"] for i in scelti]

    # controllo onesto: la distanza media fra i scelti va confrontata con quella
    # del pool intero, altrimenti "diversi" resta una parola
    def media_coppie(indici):
        P = Z[indici]
        D = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
        return D[np.triu_indices(len(indici), 1)].mean()

    campione_casuale = np.random.default_rng(0).choice(len(pool), size=len(scelti), replace=False)
    print(f"\ndistanza media fra gli scelti : {media_coppie(scelti):.2f}")
    print(f"distanza media a caso         : {media_coppie(campione_casuale):.2f}")
    print(f"costo stimato                 : ${len(ids) * 0.134:.2f}")

    if args.out:
        Path(args.out).write_text("\n".join(ids) + "\n", encoding="utf-8")
        print(f"\nid scritti in {args.out}")
    if args.comando:
        print("\nnohup python3 generate_outfit_images.py --photos all --only " + " ".join(ids))
    else:
        print("\nid: " + " ".join(ids))


if __name__ == "__main__":
    main()

# Nuvolari — Pipeline di clusterizzazione stilistica

Algoritmo che, a partire dal catalogo fotografico di [nuvolari.biz](https://www.nuvolari.biz) (e-commerce Magento), seleziona automaticamente capi che stanno bene insieme e genera outfit completi — senza usare i suggerimenti di abbinamento già presenti sul sito.

Scraping autorizzato via partnership; condivisione interna del catalogo fotografico autorizzata. Il repo contiene sia il codice sia foto + metadata del catalogo (via Git LFS, vedi [Dati inclusi / esclusi](#dati-inclusi--esclusi)) — clona con `git lfs` installato, altrimenti le foto restano puntatori testuali non risolti.

## Architettura

La pipeline è divisa in 6 fasi, ciascuna in un modulo separato ed eseguibile indipendentemente (leggono/scrivono file su disco, non c'è un orchestratore unico).

| Fase | Modulo | Cosa fa |
|---|---|---|
| 1–2 | [`scrape_with_attributes.py`](scrape_with_attributes.py) | Scarica foto + metadata da ogni pagina prodotto (una sola richiesta per prodotto), estrae attributi testuali (brand, composizione, vestibilità, prezzo) e applica le regole di tagging stilistico |
| 3 | [`feature_engineering.py`](feature_engineering.py) | Per ogni prodotto: colore dominante in Lab space (k-means sui pixel, sfondo escluso via flood-fill) + vettore stile (10 tag pesati + formalità normalizzata + stagione) |
| 4 | [`clustering.py`](clustering.py) | HDBSCAN sul vettore stile — raggruppa i prodotti per registro stilistico, riduce lo spazio di ricerca per la Fase 6 |
| 5 | [`scoring.py`](scoring.py) | `score(A,B) = w_colore·color_harmony(A,B) + w_stile·style_match(A,B)` — compatibilità pairwise tra due capi, via teoria del colore (Lab/LCh) e coseno tra vettori stile |
| 6 | [`outfit_generation.py`](outfit_generation.py) | Genera outfit completi (top/bottom/scarpe/outerwear opz./accessorio opz.) via beam search a partire da un capo ancora |

Il documento [`style_tagging_rules.md`](style_tagging_rules.md) descrive la tassonomia di `style_tags`, i dizionari keyword italiani e le regole di formalità/stagione usate in Fase 2.

## Requisiti

```bash
pip install -r requirements.txt
```

Python 3.10+. Serve anche [Git LFS](https://git-lfs.com) per le foto: `git lfs install` una volta sola sulla macchina, poi `git clone`/`git pull` normali scaricano anche i binari. Senza Git LFS, i file in `nuvolari_full_organizzato/*.jpg` restano puntatori testuali (poche righe, non l'immagine vera).

## Utilizzo

Ogni modulo è pensato per essere eseguito in un notebook/REPL (ognuno stampa un esempio d'uso quando importato). Le fasi vanno eseguite in ordine, perché ognuna legge l'output della precedente.

### Fase 1–2: scraping

```python
from scrape_with_attributes import run_scrape_with_attributes

run_scrape_with_attributes(
    output="nuvolari_full_organizzato",
    cache_file="category_cache.json",     # mappa prodotto -> categoria, riusata tra run
    progress_file="progress.txt",          # per riprendere se interrotto
    max_products=20,                       # togli per lo scraping completo
)
```

Genera per ogni prodotto `<categoria>/<sottocategoria>/<slug>/foto*.jpg` + `metadata.json`, più un `catalogo.jsonl` consolidato con tutto il catalogo.

Se in seguito correggi la logica di tagging/estrazione senza toccare le pagine (es. i regex di composizione/vestibilità), c'è `refresh_metadata()` per ri-visitare solo le pagine (no foto) e `refix_brand_and_attributes()` / `rebuild_catalog()` per ricalcolare tutto in locale senza rete, quando la correzione riguarda solo testo già salvato.

### Fase 3: feature engineering

```python
from feature_engineering import build_feature_table

df = build_feature_table(
    root="nuvolari_full_organizzato",
    out_parquet="features.parquet",
)
```

Se cambi solo la logica del vettore stile (non il colore), `recompute_style_vectors()` ricalcola senza rifare la costosa estrazione colore. `add_display_and_gallery_columns()` aggiunge `display_image` (foto generale, per mostrare il capo — diversa da `representative_image`, scelta per il k-means colore) e `all_images` (galleria completa).

### Fase 4: clustering

```python
from clustering import run_clustering

df = run_clustering(
    features_parquet="features.parquet",
    out_parquet="features_clustered.parquet",
)
```

### Fase 5: scoring

```python
import pandas as pd
from scoring import score_pair

df = pd.read_parquet("features_clustered.parquet")
score_pair(df.iloc[10], df.iloc[57])
# {'color_harmony': 0.9, 'style_match': 0.86, 'score': 0.88}
```

### Fase 6: generazione outfit

```python
from outfit_generation import load_and_prepare, generate_outfits

df = load_and_prepare("features_clustered.parquet")
outfits = generate_outfits(df, n_outfits=10, min_score=0.6)

for o in outfits:
    print(o["outfit_score"], {slot: item["title"] for slot, item in o["slots"].items() if item})
```

Regole applicate nella selezione dei candidati per ogni slot: genere, stagione, cluster stilistico compatibile, dispersione di formalità contenuta, coerenza manica/gamba (niente maniche lunghe o giacche sui pantaloncini), e `needs_vision_review=False` (esclude capi il cui stile non ha riscontro testuale affidabile). Lo score outfit finale è il **minimo** dei punteggi pairwise tra tutti i capi scelti, non la media — un singolo abbinamento pessimo non deve poter nascondersi dietro tanti buoni.

## Dati inclusi / esclusi

**Incluso** (via Git LFS, cartella `nuvolari_full_organizzato/`): foto + `metadata.json` per prodotto + `catalogo.jsonl` consolidato — l'output di Fase 1–2, condivisione autorizzata internamente.

**Escluso** dal `.gitignore` (output rigenerabile localmente, non serve versionarlo):

- `nuvolari_full/` — dump v2 superato, pre-refactor, ridondante con `nuvolari_full_organizzato/`
- `test_output/`, `test_organizzato/` — cartelle di run di test
- `category_cache.json`, `brand_list.json` — cache (rigenerabili da `discover_category_seeds`/`discover_brand_list`)
- `*.parquet` — output di Fase 3/4, si rigenera da `nuvolari_full_organizzato/` in pochi minuti
- `progress_*.txt`, `*.log` — stato/log delle run

Per rigenerare le fasi successive dalle foto già incluse: parti direttamente dalla Fase 3 (`build_feature_table`), non serve rifare lo scraping.

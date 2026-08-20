#!/usr/bin/env python3
"""
Nuvolari — Fase 4: clustering stilistico (HDBSCAN)
=====================================================

Raggruppa i prodotti per "registro stilistico" a partire dal vettore stile
della Fase 3, per ridurre lo spazio di ricerca combinatorio nella Fase 6
(generazione outfit) — invece di valutare score(A,B) su ogni possibile
coppia del catalogo, in Fase 6 si valutano solo le coppie dentro cluster
compatibili.

Perché HDBSCAN e non KMeans: non serve fissare k a priori, gestisce densità
variabili tra cluster, e isola gli outlier (label -1) invece di forzarli in
un cluster sbagliato — capi genuinamente fuori standard (mix di stili
insoliti, o già flaggati needs_vision_review) è meglio lasciarli fuori che
deformare un cluster per accoglierli.

Nota sulle feature usate: il clustering lavora di default sui 10 style_tags
+ formality_norm (11 dimensioni) — non su season. La stagione è già un
filtro esplicito e separato in Fase 6 ("candidati filtrati per
categoria+stagione+cluster compatibile"), quindi includerla anche nel
clustering rischierebbe di frammentare inutilmente capi stilisticamente
identici ma di stagioni diverse (es. un blazer elegante invernale e uno
estivo finirebbero in cluster diversi solo per la stagione, pur
appartenendo allo stesso registro stilistico). Per includerla comunque,
passa include_season=True.

Uso in notebook
-----------------
    %run clustering.py

    df = run_clustering(
        features_parquet='/Users/enricociaralli/Desktop/nuvolari/features.parquet',
        out_parquet='/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet',
    )
"""

import pandas as pd
from sklearn.cluster import HDBSCAN

STYLE_TAGS = [
    "sportivo", "casual", "elegante", "streetwear", "da_mare",
]
SEASONS = ["estate", "inverno", "mezza_stagione", "tutte"]

STYLE_TAG_COLUMNS = [f"style_{t}" for t in STYLE_TAGS]
SEASON_COLUMNS = [f"season_{s}" for s in SEASONS]


def clustering_feature_columns(include_season: bool = False) -> list:
    cols = STYLE_TAG_COLUMNS + ["formality_norm"]
    if include_season:
        cols += SEASON_COLUMNS
    return cols


def run_clustering(
    features_parquet: str,
    out_parquet: str = None,
    min_cluster_size: int = 10,
    min_samples: int = None,
    cluster_selection_method: str = "eom",
    cluster_selection_epsilon: float = 0.15,
    include_season: bool = False,
) -> pd.DataFrame:
    df = pd.read_parquet(features_parquet)
    feature_cols = clustering_feature_columns(include_season)
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Colonne mancanti in {features_parquet}: {missing}")

    X = df[feature_cols].to_numpy(dtype=float)

    print(
        f"[*] Clustering HDBSCAN su {len(df)} prodotti, {X.shape[1]} feature "
        f"({'con' if include_season else 'senza'} stagione), "
        f"min_cluster_size={min_cluster_size}, epsilon={cluster_selection_epsilon}...",
        flush=True,
    )

    # cluster_selection_epsilon=0.15 (invece del default 0.0) fonde i
    # micro-cluster quasi duplicati che il puro "excess of mass" di HDBSCAN
    # lascia separati su questo vettore stile a bassa cardinalità — vedi
    # analisi in conversazione: senza, si ottengono decine di cluster con
    # profili praticamente identici invece di gruppi genuinamente distinti.
    clusterer = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
        cluster_selection_method=cluster_selection_method,
        cluster_selection_epsilon=cluster_selection_epsilon,
    )
    labels = clusterer.fit_predict(X)

    df = df.copy()
    df["style_cluster"] = labels
    if hasattr(clusterer, "probabilities_"):
        df["cluster_probability"] = clusterer.probabilities_

    n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    n_outliers = int((labels == -1).sum())
    print(
        f"[OK] {n_clusters} cluster trovati, {n_outliers} outlier "
        f"({n_outliers / len(df) * 100:.1f}%)",
        flush=True,
    )

    sizes = df["style_cluster"].value_counts().sort_values(ascending=False)
    print("\nDimensione cluster (top 20):")
    print(sizes.head(20))

    print("\nProfilo medio per cluster (tag di stile dominanti):")
    profile = df.groupby("style_cluster")[STYLE_TAG_COLUMNS + ["formality_norm"]].mean()
    for cluster_id, row in profile.iterrows():
        top_tags = row[STYLE_TAG_COLUMNS].sort_values(ascending=False).head(3)
        tags_str = ", ".join(f"{t.replace('style_', '')} ({v:.2f})" for t, v in top_tags.items())
        label = "outlier" if cluster_id == -1 else f"cluster {cluster_id}"
        n = int(sizes.get(cluster_id, 0))
        print(f"  {label:>12} (n={n:4d}, formalità={row['formality_norm']:.2f}): {tags_str}")

    if out_parquet:
        df.to_parquet(out_parquet, index=False)
        print(f"\n[OK] Salvato in {out_parquet}", flush=True)

    return df


print(
    "Modulo caricato (Fase 4 — clustering stilistico HDBSCAN). Esempio:\n"
    "df = run_clustering(\n"
    "    features_parquet='/Users/enricociaralli/Desktop/nuvolari/features.parquet',\n"
    "    out_parquet='/Users/enricociaralli/Desktop/nuvolari/features_clustered.parquet',\n"
    ")"
)

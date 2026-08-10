#!/usr/bin/env python3
"""
Nuvolari — Fase 3: feature engineering
========================================

Per ogni prodotto già scaricato da scrape_with_attributes.py (metadata.json +
foto), costruisce due vettori separati pronti per il clustering (Fase 4):

1. Vettore colore in Lab space — k-means sui pixel della foto più adatta
   della galleria, escludendo lo sfondo studio (bianco/uniforme) tramite
   flood-fill dai quattro angoli dell'immagine.
2. Vettore stile — multi-hot sui 10 style_tags (pesati dal punteggio di
   confidenza calcolato in derive_attributes), formality_score normalizzato,
   season one-hot.

Uso in notebook
-----------------
    %run feature_engineering.py

    df = build_feature_table(
        root='/Users/enricociaralli/Desktop/nuvolari/nuvolari_full_organizzato',
        out_parquet='/Users/enricociaralli/Desktop/nuvolari/features.parquet',
    )
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image
from skimage.color import rgb2lab
from skimage.segmentation import flood
from sklearn.cluster import KMeans

from scrape_with_attributes import compute_style_scores

STYLE_TAGS = [
    "elegante", "casual", "streetwear", "sportivo", "workwear",
    "outdoor_tecnico", "vintage_prep", "minimal", "military", "boho_fantasia",
]
SEASONS = ["estate", "inverno", "mezza_stagione", "tutte"]
STYLE_SCORE_CAP = 3.0  # oltre questo valore il punteggio satura a 1.0 (vedi derive_attributes)

MAX_DIM = 220                 # ridimensiona le foto prima del k-means (velocità)
N_COLOR_CLUSTERS = 3
BG_TOLERANCE = 18             # distanza euclidea RGB per il flood-fill dello sfondo
MIN_FOREGROUND_FRAC = 0.03    # sotto questa soglia il background removal si considera fallito

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


# =====================================================================
# Vettore colore (Lab space, k-means, sfondo escluso)
# =====================================================================

def _load_rgb_array(image_path: Path, max_dim: int = MAX_DIM) -> np.ndarray:
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        return np.asarray(im)


def _background_mask(rgb: np.ndarray, tolerance: float = BG_TOLERANCE) -> np.ndarray:
    """Flood-fill dai quattro angoli per isolare lo sfondo studio (bianco/uniforme).

    Usa la distanza euclidea RGB dal colore dell'angolo come mappa per il
    flood-fill: la maschera resta connessa al bordo e non "salta" a zone
    bianche isolate dentro il capo (es. stampe/loghi/etichette bianche),
    perché quelle non sono topologicamente collegate allo sfondo.
    """
    h, w = rgb.shape[:2]
    img_f = rgb.astype(float)
    corners = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    mask = np.zeros((h, w), dtype=bool)
    for cy, cx in corners:
        seed_color = img_f[cy, cx]
        diff = np.linalg.norm(img_f - seed_color, axis=-1)
        mask |= flood(diff, (cy, cx), tolerance=tolerance)
    return mask


def _background_fraction(image_path: Path) -> float:
    # stessa risoluzione usata poi per l'estrazione colore (MAX_DIM): a risoluzioni
    # più basse i pattern fini (righe, micro-texture) si sfocano in un campo quasi
    # uniforme e il flood-fill li scambia per sfondo, gonfiando il punteggio di
    # foto ravvicinate che in realtà non hanno sfondo studio in inquadratura.
    rgb = _load_rgb_array(image_path, max_dim=MAX_DIM)
    return float(_background_mask(rgb).mean())


def pick_representative_image(image_paths: list) -> Path:
    """Sceglie, tra le foto della galleria, quella con più sfondo uniforme.

    Tipicamente è il flat-lay/ghost-shot più pulito — il migliore per il
    colore reale del capo — mentre scatti indossati o di dettaglio hanno
    sfondo meno uniforme (pelle, capelli, ambiente, zoom ravvicinato).
    """
    if len(image_paths) == 1:
        return image_paths[0]
    scored = [(p, _background_fraction(p)) for p in image_paths]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[0][0]


def dominant_colors_lab(image_path: Path, n_colors: int = N_COLOR_CLUSTERS) -> dict:
    """K-means in Lab space sui pixel del capo (sfondo escluso).

    Ritorna il colore dominante e la palette completa con le proporzioni,
    più la frazione di sfondo rilevata (utile per il controllo qualità).
    """
    rgb = _load_rgb_array(image_path)
    mask = _background_mask(rgb)
    foreground = rgb[~mask]

    if foreground.shape[0] < MIN_FOREGROUND_FRAC * rgb.shape[0] * rgb.shape[1]:
        # background removal fallito (es. capo quasi tutto bianco) -> usa l'immagine intera
        foreground = rgb.reshape(-1, 3)

    lab_pixels = rgb2lab((foreground / 255.0).reshape(-1, 1, 3)).reshape(-1, 3)

    k = min(n_colors, len(np.unique(foreground.reshape(-1, 3), axis=0)))
    k = max(k, 1)
    km = KMeans(n_clusters=k, n_init=4, random_state=0).fit(lab_pixels)

    counts = np.bincount(km.labels_, minlength=k)
    order = np.argsort(counts)[::-1]

    palette = [
        {"lab": km.cluster_centers_[i].tolist(), "proportion": float(counts[i] / counts.sum())}
        for i in order
    ]
    return {
        "dominant_lab": palette[0]["lab"],
        "palette": palette,
        "background_fraction": float(mask.mean()),
    }


# =====================================================================
# Vettore stile (multi-hot style_tags + formalità normalizzata + stagione)
# =====================================================================

def build_style_vector(metadata: dict) -> dict:
    # punteggi grezzi (non sogliati) ricalcolati dal testo già in metadata.json —
    # metadata["style_tags"] è invece filtrato a soglia 0.5 in Fase 2 (badge per
    # needs_vision_review) e azzererebbe segnale reale sotto soglia se usato qui
    raw_scores, _text_matched_tags = compute_style_scores(metadata)
    tag_values = {
        f"style_{tag}": min(raw_scores.get(tag, 0.0), STYLE_SCORE_CAP) / STYLE_SCORE_CAP
        for tag in STYLE_TAGS
    }

    formality = metadata.get("formality_score")
    formality_norm = (formality - 1) / 4 if formality is not None else 0.5

    season = metadata.get("season") or "tutte"
    season_values = {f"season_{s}": (1.0 if season == s else 0.0) for s in SEASONS}

    return {**tag_values, "formality_norm": formality_norm, **season_values}


STYLE_VECTOR_COLUMNS = (
    [f"style_{t}" for t in STYLE_TAGS] + ["formality_norm"] + [f"season_{s}" for s in SEASONS]
)


# =====================================================================
# Pipeline: scansiona metadata.json, calcola entrambi i vettori, salva
# =====================================================================

def build_feature_table(root: str, out_parquet: str = None, limit: int = None) -> pd.DataFrame:
    root_dir = Path(root)
    metadata_files = sorted(root_dir.rglob("metadata.json"))
    if limit:
        metadata_files = metadata_files[:limit]
    print(f"[*] Trovati {len(metadata_files)} metadata.json sotto {root_dir}", flush=True)

    rows = []
    for i, meta_path in enumerate(metadata_files, 1):
        product_dir = meta_path.parent
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))

        image_paths = sorted(
            p for p in product_dir.iterdir()
            if p.suffix.lower() in IMAGE_SUFFIXES
        )
        if not image_paths:
            print(f"    [!] {product_dir.name}: nessuna foto, salto", flush=True)
            continue

        try:
            rep_image = pick_representative_image(image_paths)
            color = dominant_colors_lab(rep_image)
        except Exception as e:
            print(f"    [!] {product_dir.name}: errore estrazione colore ({e}), salto", flush=True)
            continue

        style_vec = build_style_vector(metadata)

        row = {
            "relpath": metadata.get("relpath", str(product_dir.relative_to(root_dir))),
            "url": metadata.get("url"),
            "brand_slug": metadata.get("brand_slug"),
            "title": metadata.get("title"),
            "needs_vision_review": metadata.get("needs_vision_review"),
            "representative_image": str(rep_image.relative_to(root_dir)),
            "L": color["dominant_lab"][0],
            "a": color["dominant_lab"][1],
            "b": color["dominant_lab"][2],
            "color_palette": json.dumps(color["palette"]),
            "background_fraction": color["background_fraction"],
            **style_vec,
        }
        rows.append(row)

        if i % 50 == 0 or i == len(metadata_files):
            print(f"    [{i}/{len(metadata_files)}] elaborati...", flush=True)

    df = pd.DataFrame(rows)

    if out_parquet:
        df.to_parquet(out_parquet, index=False)
        print(f"[OK] Salvato {len(df)} prodotti in {out_parquet}", flush=True)

    return df


def recompute_style_vectors(features_parquet: str, root: str, out_parquet: str = None) -> pd.DataFrame:
    """Ricalcola solo le colonne del vettore stile (style_*, formality_norm,
    season_*) da metadata.json, riusando L/a/b/color_palette già estratti in
    features_parquet. Utile dopo una modifica alla logica di build_style_vector
    (es. build_style_vector), senza rifare la costosa estrazione colore."""
    df = pd.read_parquet(features_parquet).set_index("relpath")
    root_dir = Path(root)

    for relpath in df.index:
        meta_path = root_dir / relpath / "metadata.json"
        if not meta_path.exists():
            print(f"    [!] {relpath}: metadata.json non trovato, salto", flush=True)
            continue
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        for col, val in build_style_vector(metadata).items():
            df.loc[relpath, col] = val
        df.loc[relpath, "brand_slug"] = metadata.get("brand_slug")
        df.loc[relpath, "needs_vision_review"] = metadata.get("needs_vision_review")

    df = df.reset_index()

    if out_parquet:
        df.to_parquet(out_parquet, index=False)
        print(f"[OK] Salvato {len(df)} prodotti in {out_parquet}", flush=True)

    return df


def add_display_and_gallery_columns(features_parquet: str, root: str, out_parquet: str = None) -> pd.DataFrame:
    """Aggiunge due colonne, entrambe in locale senza rete:

    - display_image: la PRIMA foto della galleria (ordine alfabetico dei
      file), per mostrare il capo a un umano — a differenza di
      representative_image (Fase 3), che è scelta per la massima frazione
      di sfondo pulito ai fini del k-means colore e spesso è un dettaglio
      ravvicinato, non la foto d'insieme del capo.
    - all_images: lista di TUTTE le foto della galleria — servirà quando
      passeremo i dati a un'eventuale API di generazione immagini, che
      potrebbe voler usare più angolazioni dello stesso capo, non solo una.
    """
    df = pd.read_parquet(features_parquet)
    root_dir = Path(root)

    display_images, all_images_col = [], []
    for relpath in df["relpath"]:
        product_dir = root_dir / relpath
        images = sorted(
            str(p.relative_to(root_dir)) for p in product_dir.iterdir()
            if p.suffix.lower() in IMAGE_SUFFIXES
        ) if product_dir.exists() else []
        display_images.append(images[0] if images else None)
        all_images_col.append(images)

    df["display_image"] = display_images
    df["all_images"] = all_images_col

    if out_parquet:
        df.to_parquet(out_parquet, index=False)
        print(f"[OK] Salvato {len(df)} prodotti in {out_parquet}", flush=True)

    return df


print(
    "Modulo caricato (Fase 3 — feature engineering). Esempio:\n"
    "df = build_feature_table(\n"
    "    root='/Users/enricociaralli/Desktop/nuvolari/nuvolari_full_organizzato',\n"
    "    out_parquet='/Users/enricociaralli/Desktop/nuvolari/features.parquet',\n"
    ")"
)

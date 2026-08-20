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
from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401

STYLE_TAGS = [
    "sportivo", "casual", "elegante", "streetwear", "da_mare",
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


# Quota di capo sotto la quale il flood-fill si sta mangiando il prodotto e non
# solo lo sfondo. La mediana del catalogo è ~29% di capo; sotto il 15% non è più
# una foto ravvicinata, è uno scontorno sbagliato.
FOREGROUND_PLAUSIBILE = 0.15
# ...e sopra la quale non è più un capo su fondale ma un dettaglio ravvicinato.
# Una foto di catalogo ha sempre dello sfondo: se non se ne trova, il flood-fill
# non è riuscito a propagarsi, non è sparito il fondale.
FOREGROUND_MASSIMO = 0.80
# Tolleranze provate in ordine, dalla più larga alla più stretta.
TOLLERANZE_SFONDO = (BG_TOLERANCE, 14, 12, 10, 8, 6, 5, 4, 3)


def _maschera_sfondo_adattiva(rgb: np.ndarray):
    """Sceglie la tolleranza che lascia in piedi un capo di dimensioni credibili.

    Con la tolleranza fissa a 18 un capo pallido rientra nella distanza dal
    bianco e viene inghiottito insieme al fondale. Misurato su una t-shirt
    celeste: 96% dell'immagine classificato come sfondo (mediana del catalogo
    71%), e del 4% rimasto la maggior parte era la stampa gialla sul petto —
    così il colore dominante a registro diventava un giallo pallido e la
    maglia risultava abbinabile al nero come se fosse panna.

    Il criterio ha però bisogno di un tetto oltre che di un pavimento. Con il
    solo pavimento, su 26 capi la tolleranza scendeva fino a 3, il riempimento
    non si propagava più e la maschera restava vuota: sfondo 0,00, capo "100%
    dell'immagine", e il colore calcolato sul bianco del fondale. Un difetto
    speculare a quello che si voleva togliere.

    Se nessuna tolleranza produce una quota credibile si prende la meno
    sbagliata — quella che ci va più vicino — invece della più stretta.
    """
    migliore, distanza_migliore = None, float("inf")
    for tolleranza in TOLLERANZE_SFONDO:
        maschera = _background_mask(rgb, tolleranza)
        capo = (~maschera).mean()
        if FOREGROUND_PLAUSIBILE <= capo <= FOREGROUND_MASSIMO:
            return maschera
        distanza = (FOREGROUND_PLAUSIBILE - capo if capo < FOREGROUND_PLAUSIBILE
                    else capo - FOREGROUND_MASSIMO)
        if distanza < distanza_migliore:
            migliore, distanza_migliore = maschera, distanza
    return migliore


def _background_fraction(image_path: Path) -> float:
    # stessa risoluzione usata poi per l'estrazione colore (MAX_DIM): a risoluzioni
    # più basse i pattern fini (righe, micro-texture) si sfocano in un campo quasi
    # uniforme e il flood-fill li scambia per sfondo, gonfiando il punteggio di
    # foto ravvicinate che in realtà non hanno sfondo studio in inquadratura.
    rgb = _load_rgb_array(image_path, max_dim=MAX_DIM)
    return float(_background_mask(rgb).mean())


def is_readable(image_path: Path) -> bool:
    """Verifica che il file sia un'immagine leggibile per intero.

    Alcuni download sono troncati e sollevano OSError solo alla decodifica dei
    pixel, non all'apertura: vanno individuati qui, prima che facciano fallire
    l'estrazione colore o, peggio, finiscano fra le foto inviate all'API.
    """
    try:
        with Image.open(image_path) as im:
            im.load()
        return True
    except Exception:
        return False


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
    mask = _maschera_sfondo_adattiva(rgb)
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

# Peso del punteggio visivo nella scala dei punteggi grezzi di
# compute_style_scores, dove un riscontro testuale vale 0.8. La revisione
# visiva guarda il capo, non le parole che lo accompagnano: un tag pieno
# (1.0) vale 2.4, cioè tre riscontri testuali — abbastanza da dare al capo
# una norma sopra la mediana del catalogo (0.55) e quindi piena confidenza
# in style_match, che è esattamente ciò che la revisione deve comprare.
VISION_STYLE_WEIGHT = 2.4


def needs_review_effettivo(metadata: dict) -> bool:
    """La quarantena vale finché non c'è una revisione visiva riuscita.

    Il flag testuale needs_vision_review resta com'è (è la diagnosi:
    "il testo non dice nulla"); qui si decide la prognosi: se Gemini ha
    visto la foto e ha risposto, il capo torna abbinabile.
    """
    if not metadata.get("needs_vision_review"):
        return False
    revisione = metadata.get("vision_review") or {}
    return not revisione.get("style_scores")


# La revisione fatta sotto la guida attuale. Il controllo sulla fonte non e'
# pedanteria: i giudizi precedenti nascevano da un prompt che non aveva ne' la
# descrizione completa ne' il procedimento sulla formalita', e divergevano dal
# testo di almeno un livello nel 35% dei capi. Finche' un capo non e' stato
# rivisto sotto la guida nuova, la sua formalita' resta quella testuale.
FONTE_GUIDA = "foto+descrizione-5tag"


def formalita_effettiva(metadata: dict):
    """La formalita' da usare: quella di Gemini se disponibile, altrimenti quella
    delle regole testuali.

    Le regole testuali pesavano per meta' su una parola del titolo, che descrive
    la FORMA del capo e non il registro — "giacca" vale sia per un blazer sia per
    un antivento K-Way. I tetti di brand esistevano solo per rattoppare questo,
    marchio per marchio. Gemini vede foto, composizione e descrizione insieme, e
    il procedimento di quelle regole ce l'ha scritto nella guida.
    """
    revisione = metadata.get("vision_review") or {}
    if revisione.get("fonte") == FONTE_GUIDA:
        f = revisione.get("formality")
        if isinstance(f, (int, float)) and 1 <= f <= 5:
            return int(f)
    return metadata.get("formality_score")


def mistura_di_stile(metadata: dict) -> dict:
    """La ripartizione fra i registri, normalizzata a somma 1.

    Con cinque registri la classificazione non è più un insieme di flag
    indipendenti ma una RIPARTIZIONE: una camicia è metà elegante e metà casual,
    una tuta metà sportiva e metà casual. Sommare i punteggi del testo a quelli
    della revisione romperebbe proprio la cosa che li rende leggibili — un capo
    dato "casual 1.0" diventerebbe casual 0.75 / sportivo 0.25 solo perché nella
    prosa compare la parola "running". Quindi: se la revisione c'è, è lei la
    classificazione; il testo resta solo per i capi che la revisione non ha.
    """
    revisione = metadata.get("vision_review") or {}
    mistura = {t: float(v) for t, v in (revisione.get("style_scores") or {}).items()
               if t in STYLE_TAGS and float(v) > 0}
    if not mistura:
        grezzi, _ = compute_style_scores(metadata)
        mistura = {t: v for t, v in grezzi.items() if t in STYLE_TAGS and v > 0}
    totale = sum(mistura.values())
    if not totale:
        return {t: 0.0 for t in STYLE_TAGS}
    return {t: mistura.get(t, 0.0) / totale for t in STYLE_TAGS}


def fantasia_effettiva(metadata: dict) -> str:
    """La fantasia del capo: quella vista nella foto, o quella del testo.

    La revisione visiva viene prima perche' la fantasia e' la cosa che il testo
    nomina peggio — spesso non la nomina affatto, o nomina un dettaglio.
    """
    revisione = metadata.get("vision_review") or {}
    return revisione.get("pattern") or metadata.get("pattern") or "tinta_unita"


def build_style_vector(metadata: dict) -> dict:
    tag_values = {f"style_{tag}": v for tag, v in mistura_di_stile(metadata).items()}

    formality = formalita_effettiva(metadata)
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

        tutte = sorted(
            p for p in product_dir.iterdir()
            if p.suffix.lower() in IMAGE_SUFFIXES
        )
        # si scarta la singola foto corrotta, non l'intero prodotto: un download
        # troncato su una foto non deve far perdere un capo che ne ha altre buone
        image_paths = [p for p in tutte if is_readable(p)]
        if len(image_paths) < len(tutte):
            print(f"    [!] {product_dir.name}: {len(tutte) - len(image_paths)} foto illeggibili, "
                  f"uso le altre {len(image_paths)}", flush=True)
        if not image_paths:
            print(f"    [!] {product_dir.name}: nessuna foto utilizzabile, salto", flush=True)
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
            "needs_vision_review": needs_review_effettivo(metadata),
            "pattern": fantasia_effettiva(metadata),
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
        df.loc[relpath, "needs_vision_review"] = needs_review_effettivo(metadata)

    df = df.reset_index()

    if out_parquet:
        df.to_parquet(out_parquet, index=False)
        print(f"[OK] Salvato {len(df)} prodotti in {out_parquet}", flush=True)

    return df


IDF_FILE = BASE / "style_idf.json"


def applica_idf(features_parquet: str, out_parquet: str = None) -> pd.DataFrame:
    """NON PIU' IN USO dal 20 agosto. Vedi sotto prima di rimetterla in catena.

    Serviva quando lo stile era dieci flag indipendenti e "casual" compariva sul
    96% dei capi senza voler dire niente: quel tono di fondo schiacciava lo
    spazio, e pesare i tag per la loro rarita' lo riapriva.

    Con cinque registri assegnati come RIPARTIZIONE che somma a 1, "casual" non
    e' piu' un tono di fondo: e' la base condivisa del guardaroba, dichiarata
    apposta. L'IDF gli dava peso 0,16 e cancellava proprio quella — un giubbotto
    "casual 0.9" e un cappello "casual 0.7" scendevano da 0,914 a 0,226, cioe'
    due capi che stanno benissimo insieme risultavano incompatibili.

    Toglierla non ha riaperto la porta al rumore, misurato su 4000 coppie a
    caso: le coppie sbagliate restano a zero (giacca elegante + running 0,012,
    + costume 0,000). La separazione ora viene dalla tassonomia, non dai pesi.

    --- documentazione originale ---

    Ripesa i 10 tag di stile per quanto sono RARI nel catalogo.

    Serve perché la revisione visiva ha un fortissimo tono di fondo: Gemini
    assegna "casual" al 96% dei capi e lo fa dominante nel 57%. Un tag che
    quasi tutti hanno non distingue niente — e finché pesava come gli altri
    schiacciava lo spazio: il coseno fra due capi a caso saliva a 0,71 (tutto
    somigliava a tutto) e HDBSCAN produceva un cluster solo da 1548 capi con
    intorno il 78% di outlier.

    Il peso è l'IDF classico, log(1/prevalenza): "casual" (96%) scende a 0,04,
    "boho_fantasia" o "elegante" (5%) salgono a ~3. Condividere un tratto
    raro è prova di somiglianza molto più forte che condividerne uno comune.
    Dopo la ripesatura il coseno mediano torna a 0,28 e i cluster a 25, con il
    più grande al 17% del catalogo.

    I pesi si ricalcolano dal catalogo corrente e si salvano in style_idf.json,
    così si può leggere quanto vale ogni tag senza rieseguire nulla. Va
    rilanciata dopo ogni cambio ai vettori di stile, PRIMA del clustering.
    formality_norm e le colonne di stagione non si toccano: non sono tag.
    """
    df = pd.read_parquet(features_parquet)
    colonne = [f"style_{t}" for t in STYLE_TAGS]
    prevalenza = (df[colonne] > 0).mean(axis=0)
    # la prevalenza si stima sui valori GIÀ ripesati se si rilancia due volte:
    # per questo il file dei pesi è la fonte di verità e si riparte dal grezzo
    # Radice dell'IDF, non IDF pieno. Misurato su coppie di capi già giudicate
    # a mano (felpa NASA + jeans, + K-Way, + Saucony come BUONE; blazer o
    # camicia + tuta, mocassino + t-shirt come CATTIVE), lo stacco fra le due
    # famiglie disegna una U rovesciata con il massimo a esponente 0,5:
    #     esponente 0.00 -> buone 0.623, cattive 0.637  (stacco -0.014!)
    #     esponente 0.50 -> buone 0.363, cattive 0.268  (stacco +0.095)
    #     esponente 1.00 -> buone 0.187, cattive 0.179  (stacco +0.007)
    # I due estremi sbagliano per motivi opposti e simmetrici: senza pesi il
    # tono di fondo "casual" rende tutto simile a tutto (e le coppie sbagliate
    # scorano perfino PIÙ alte delle giuste); con l'IDF pieno condividere un
    # tratto comune non vale più niente, ma due capi entrambi casual stanno
    # insieme davvero — la compatibilità non è solo distintività.
    ESPONENTE_IDF = 0.5
    pesi = np.log(1.0 / prevalenza.clip(lower=1e-3)) ** ESPONENTE_IDF
    df[colonne] = df[colonne].to_numpy(float) * pesi.to_numpy(float)

    IDF_FILE.write_text(json.dumps(
        {t: round(float(w), 4) for t, w in zip(colonne, pesi)}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("[*] pesi IDF (tag piu' raro = piu' pesante):", flush=True)
    for t, w in sorted(zip(colonne, pesi), key=lambda kv: -kv[1]):
        print(f"      {t.replace('style_',''):18s} prevalenza {prevalenza[t]*100:5.1f}%  peso {w:.2f}", flush=True)

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
        # stesso filtro di build_feature_table: una foto illeggibile qui
        # finirebbe fra quelle inviate al modello in fase di generazione
        images = sorted(
            str(p.relative_to(root_dir)) for p in product_dir.iterdir()
            if p.suffix.lower() in IMAGE_SUFFIXES and is_readable(p)
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

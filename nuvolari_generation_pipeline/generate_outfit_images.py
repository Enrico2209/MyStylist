#!/usr/bin/env python3
"""
Genera con Nano Banana Pro (Gemini 3 Pro Image) una foto per ogni outfit del
pool: un modello che indossa l'outfit completo, su fondo studio neutro.

Le foto dei singoli capi (fondo bianco, scattate dal catalogo) vengono passate
come immagini di riferimento — il modello ne accetta fino a 6 e un outfit ne ha
al massimo 5, quindi bastano una chiamata e un giro solo.

La chiave API non compare mai nel codice: si legge da GEMINI_API_KEY
nell'ambiente oppure dal file .env accanto a questo script (che è in
.gitignore e non finisce nei commit).

Uso:
    python3 generate_outfit_images.py --limit 10        # pilota
    python3 generate_outfit_images.py                   # pool completo
    python3 generate_outfit_images.py --resume          # riprende, salta i fatti

Il salvataggio è per outfit_id, quindi il run è riprendibile in qualsiasi
momento: basta rilanciarlo con --resume.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types

from percorsi import DATI as BASE, CODICE, CATALOGO as _CATALOGO, IMMAGINI_SUPERATE, PROGETTI  # noqa: F401
ROOT = BASE / "nuvolari_full_organizzato"
JSONL = BASE / "outfits_pool.jsonl"
OUT_DIR = BASE / "outfit_images"
LOG_JSONL = BASE / "outfit_images_log.jsonl"

MODEL = "gemini-3-pro-image"
COST_PER_IMAGE = 0.134  # USD, tariffa 1K/2K — serve solo per la stima a schermo

# Tetto duro dell'API: 14 immagini di riferimento per richiesta.
# (Google raccomanda <=6 per la massima fedelta' degli oggetti: oltre quella
#  soglia la resa del singolo capo puo' degradare, per questo il pilota
#  confronta le due modalita' prima di lanciare il pool intero.)
MAX_REF_IMAGES = 14

SLOT_IT = {"top": "capo superiore", "bottom": "pantaloni", "shoes": "scarpe",
           "outerwear": "capospalla", "accessory": "accessorio"}
MAX_SLOTS = len(SLOT_IT)   # capi al massimo in un outfit: soglia minima per max_ref

RULES_FILE = CODICE / "generation_rules.md"

# Le regole comuni a tutte le immagini stanno in generation_rules.md, non qui:
# si modificano senza toccare il codice e sono leggibili da chi non programma.
# Questo blocco porta solo i dati specifici del singolo outfit.
PROMPT = """Genera la fotografia di questo outfit seguendo le regole del documento qui sopra.

Soggetto: {soggetto}.
Formalità dell'outfit: {formalita:.1f} su 5 → applica la fascia d'età corrispondente (regola 2) e la fascia di sfondo corrispondente (regola 3).

I capi da indossare, con le immagini di riferimento che li ritraggono:
{elenco}"""

SOGGETTO = {
    "uomo": "un modello uomo adulto",
    "donna": "una modella donna adulta",
    None: "un modello adulto",
}


def load_rules() -> str:
    if not RULES_FILE.exists():
        sys.exit(f"Manca il documento delle regole: {RULES_FILE}")
    return RULES_FILE.read_text(encoding="utf-8")


class Rules:
    """Tiene generation_rules.md allineato al file su disco durante il run.

    Il testo delle regole viaggia in OGNI richiesta all'API (e' la prima parte
    del contenuto, prima dei dati dell'outfit e delle foto). Qui si ricontrolla
    la data di modifica prima di ogni chiamata: cosi' una correzione al .md
    entra in vigore sulle immagini successive senza dover fermare e far
    ripartire un run che dura ore. Il cambio viene annunciato a schermo e
    registrato nel log, perche' due immagini dello stesso run generate con
    regole diverse devono restare riconoscibili.
    """

    def __init__(self):
        self.testo = load_rules()
        self.mtime = RULES_FILE.stat().st_mtime
        self.versione = 1

    def corrente(self) -> str:
        try:
            mtime = RULES_FILE.stat().st_mtime
        except OSError:
            return self.testo          # file momentaneamente assente: si tiene l'ultima versione buona
        if mtime != self.mtime:
            self.testo = load_rules()
            self.mtime = mtime
            self.versione += 1
            print(f"    [i] {RULES_FILE.name} modificato: da qui in poi vale la versione "
                  f"{self.versione} ({len(self.testo)} caratteri)", flush=True)
        return self.testo


def load_formality() -> dict:
    """Formalità per capo (scala 1-5) da features_clustered.parquet.

    Nel pool degli outfit non c'è: il JSONL porta lo score di compatibilità,
    non la formalità. Si ricava dal parquet di Fase 3, dove è normalizzata 0-1.
    """
    import pandas as pd
    df = pd.read_parquet(BASE / "features_clustered.parquet")
    return {r: v * 4 + 1 for r, v in zip(df["relpath"], df["formality_norm"])}


def outfit_formality(outfit: dict, tabella: dict) -> float:
    """Media della formalità dei capi dell'outfit.

    La media è significativa perché la generazione del pool impone già una
    dispersione massima di 0,3 fra i capi (vedi FORMALITY_SPREAD_MAX in
    outfit_generation.py): non ci sono outfit che mescolano registri opposti.
    """
    valori = [tabella[s["relpath"]] for s in outfit["slots"].values()
              if s and s["relpath"] in tabella]
    return sum(valori) / len(valori) if valori else 3.0


def load_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    env_file = CODICE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY=") or line.startswith("GOOGLE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(
        "Chiave API non trovata.\n"
        "Impostala con:  export GEMINI_API_KEY=...\n"
        f"oppure scrivila in {env_file} come  GEMINI_API_KEY=..."
    )


MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif"}


def mime_di(path: Path) -> str:
    """Tipo MIME dedotto dall'estensione.

    Non è un dettaglio: il catalogo non è tutto JPEG (ci sono 34 PNG veri,
    presenti in 33 outfit) e dichiararli come image/jpeg significa consegnare
    all'API un byte stream che non corrisponde al tipo annunciato.
    """
    return MIME.get(path.suffix.lower(), "image/jpeg")


def allocate_photos(per_capo: list, max_totale: int) -> list:
    """Ripartisce il budget di immagini fra i capi a giro (round-robin).

    Serve perché la somma di tutte le foto di un outfit supera spesso il tetto
    dell'API (mediana 12, massimo 20, tetto 14). Assegnando un posto per volta
    a ciascun capo, la prima foto di OGNI capo entra sempre: il taglio colpisce
    solo le foto aggiuntive dei capi che ne hanno molte, e mai un capo intero —
    cosa che accadrebbe tagliando la lista in coda.
    """
    quote = [0] * len(per_capo)
    rimasti = max_totale
    while rimasti > 0 and any(q < len(f) for q, f in zip(quote, per_capo)):
        for i, foto in enumerate(per_capo):
            if rimasti == 0:
                break
            if quote[i] < len(foto):
                quote[i] += 1
                rimasti -= 1
    return [foto[:q] for foto, q in zip(per_capo, quote)]


def build_parts(outfit: dict, tutte_le_foto: bool = True, max_ref: int = MAX_REF_IMAGES,
                 regole: str = "", formalita: float = 3.0):
    """Prompt + immagini di riferimento dei capi.

    Con tutte_le_foto=True si inviano tutte le foto disponibili di ogni capo
    (angolazioni e dettagli diversi aiutano il modello a riprodurlo fedelmente),
    entro il tetto di max_ref immagini complessive. Le immagini sono RAGGRUPPATE
    per capo e il prompt dichiara quali indici appartengono a quale capo: senza
    quella mappatura il modello non saprebbe che tre foto diverse sono lo stesso
    capo visto da angolazioni diverse.
    """
    capi, per_capo = [], []
    for slot, v in outfit["slots"].items():
        if not v:
            continue
        if tutte_le_foto:
            rel = list(v.get("all_images") or [])
        else:
            rel = [v["display_image"]] if v.get("display_image") else []
        esistenti = [ROOT / p for p in rel if (ROOT / p).exists()]
        if not esistenti:
            continue
        capi.append((slot, v))
        per_capo.append(esistenti)

    if not capi:
        return None, 0, 0

    scelte = allocate_photos(per_capo, max_ref)

    voci, immagini, esclusi = [], [], []
    for (slot, v), foto in zip(capi, scelte):
        # Un capo senza foto assegnate non va dichiarato: il suo intervallo di
        # indici risulterebbe vuoto ("immagini da 4 a 3") e indicherebbe al
        # modello immagini che non esistono nella richiesta. Succede solo se
        # max_ref è più basso del numero di capi — configurazione che main()
        # rifiuta, ma la difesa resta qui perché build_parts è chiamabile da sé.
        if not foto:
            esclusi.append(SLOT_IT.get(slot, slot))
            continue
        primo = len(immagini) + 1
        for p in foto:
            immagini.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime_di(p)))
        ultimo = len(immagini)
        rif = f"immagine {primo}" if primo == ultimo else f"immagini da {primo} a {ultimo}"
        voci.append(f"- {SLOT_IT.get(slot, slot)} ({rif}): {v['title']}")

    if esclusi:
        print(f"      ! {outfit['outfit_id']}: {', '.join(esclusi)} senza foto allegate "
              f"(max_ref={max_ref} troppo basso), capo escluso dal prompt", flush=True)

    disponibili = sum(len(f) for f in per_capo)
    prompt = PROMPT.format(
        soggetto=SOGGETTO.get(outfit.get("gender"), SOGGETTO[None]),
        formalita=formalita,
        elenco="\n".join(voci),
    )
    # le regole precedono tutto: il modello le legge prima dei dati dell'outfit
    testa = [regole, prompt] if regole else [prompt]
    return [*testa, *immagini], len(immagini), disponibili


def generate_one(client, outfit: dict, image_size: str, tutte_le_foto: bool = True,
                  max_ref: int = MAX_REF_IMAGES, max_retries: int = 4,
                  regole: str = "", formalita: float = 3.0):
    parts, n_ref, disponibili = build_parts(outfit, tutte_le_foto, max_ref, regole, formalita)
    if not n_ref:
        return None, "nessuna immagine di riferimento disponibile", 0, 0

    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio="3:4",          # verticale: figura intera senza tagliare le scarpe
            image_size=image_size,
            # niente person_generation: e' accettato solo in modalita' Gemini
            # Enterprise Agent Platform, con la Developer API fa fallire la richiesta
        ),
    )

    for tentativo in range(1, max_retries + 1):
        try:
            resp = client.models.generate_content(model=MODEL, contents=parts, config=config)
            for cand in resp.candidates or []:
                for part in cand.content.parts or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        return inline.data, None, n_ref, disponibili
            return None, "risposta senza immagine (probabile blocco dei filtri di sicurezza)", n_ref, disponibili
        except Exception as e:
            msg = str(e)
            # Un 429 ha due significati opposti. Se è il rate limit al minuto,
            # basta aspettare e riprovare. Se è la quota GIORNALIERA esaurita
            # ("exceeded your current quota"), nessuna attesa ragionevole la
            # rimette a posto: ritentare significa solo accumulare fallimenti,
            # come è successo con 55 richieste bruciate in 25 minuti. Si segnala
            # al chiamante perché fermi il run invece di proseguire a vuoto.
            if "429" in msg and "exceeded your current quota" in msg:
                return None, "QUOTA_GIORNALIERA_ESAURITA: " + msg[:200], n_ref, disponibili
            # Transitori: rate limit, sovraccarico e scadenze lato server.
            # Il 504/DEADLINE_EXCEEDED va incluso: con molte immagini di
            # riferimento la generazione sfora spesso la deadline del server,
            # ma allo scatto successivo passa.
            transitorio = any(s in msg for s in (
                "429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500",
                "504", "DEADLINE_EXCEEDED",
                # timeout lato client: la richiesta scade sul socket prima che
                # il server risponda ("The read operation timed out"). È
                # transitorio quanto un 504, ma non porta codice HTTP e senza
                # questa voce veniva abbandonato al primo tentativo.
                "timed out", "timeout", "TimeoutError", "Connection reset",
                "Connection aborted", "RemoteDisconnected",
            ))
            if not transitorio or tentativo == max_retries:
                return None, msg[:300], n_ref, disponibili
            attesa = min(60, 2 ** tentativo) + random.uniform(0, 1.5)
            print(f"      ! {msg[:90]} — riprovo tra {attesa:.0f}s ({tentativo}/{max_retries})", flush=True)
            time.sleep(attesa)
    return None, "tentativi esauriti", n_ref, disponibili


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="genera solo i primi N outfit (pilota)")
    ap.add_argument("--resume", action="store_true", help="salta gli outfit già generati")
    ap.add_argument("--image-size", default="2K", choices=["1K", "2K", "4K"])
    ap.add_argument("--sleep", type=float, default=0.5, help="pausa fra chiamate, in secondi")
    ap.add_argument("--sort-by-score", action="store_true",
                    help="parte dagli outfit con score più alto invece che dall'ordine del pool")
    ap.add_argument("--photos", default="all", choices=["all", "display"],
                    help="'all': tutte le foto disponibili di ogni capo (default). "
                         "'display': solo la foto di copertina, per confronto")
    ap.add_argument("--max-ref", type=int, default=MAX_REF_IMAGES,
                    help=f"tetto di immagini di riferimento per richiesta (API: {MAX_REF_IMAGES})")
    ap.add_argument("--suffix", default="", help="suffisso sul nome file, per confrontare varianti")
    ap.add_argument("--timeout", type=int, default=300,
                    help="timeout per richiesta in secondi (default 300)")
    ap.add_argument("--only", nargs="+", metavar="OUTFIT_ID",
                    help="genera solo gli outfit_id indicati (per test o rigenerazioni mirate)")
    args = ap.parse_args()

    if args.max_ref > MAX_REF_IMAGES:
        sys.exit(f"--max-ref non può superare {MAX_REF_IMAGES}: è il limite dell'API.")
    # Sotto il numero di capi di un outfit, la ripartizione a giro lascerebbe
    # gli ultimi capi senza nessuna foto: verrebbero generati senza che il
    # modello li abbia mai visti. Meglio fermarsi qui che pagare immagini in cui
    # un capo è inventato.
    if args.max_ref < MAX_SLOTS:
        sys.exit(f"--max-ref deve essere almeno {MAX_SLOTS}, quanti sono gli slot di un "
                 f"outfit: con meno, gli ultimi capi resterebbero senza foto di riferimento.")
    tutte = args.photos == "all"

    # Un timeout esplicito e' indispensabile: senza, una chiamata rallentata resta
    # appesa a tempo indeterminato invece di fallire e passare al retry (successo
    # con 10 immagini di riferimento: una richiesta ferma 11 minuti senza esito).
    # Le richieste con molte immagini arrivano a ~2 minuti, quindi il tetto e' largo.
    client = genai.Client(
        api_key=load_api_key(),
        http_options=types.HttpOptions(timeout=args.timeout * 1000),
    )
    OUT_DIR.mkdir(exist_ok=True)

    regole = Rules()
    tabella_formalita = load_formality()
    print(f"[*] regole caricate da {RULES_FILE.name} ({len(regole.testo)} caratteri), "
          f"rilette a ogni chiamata se il file cambia", flush=True)

    outfits = [json.loads(l) for l in open(JSONL, encoding="utf-8")]
    if args.only:
        voluti = set(args.only)
        outfits = [o for o in outfits if o["outfit_id"] in voluti]
        mancanti = voluti - {o["outfit_id"] for o in outfits}
        if mancanti:
            sys.exit(f"outfit_id non trovati nel pool: {', '.join(sorted(mancanti))}")
    if args.sort_by_score:
        outfits.sort(key=lambda o: -o["outfit_score"])
    def dest(oid):
        return OUT_DIR / f"{oid}{args.suffix}.png"

    if args.resume:
        outfits = [o for o in outfits if not dest(o["outfit_id"]).exists()]
    if args.limit:
        outfits = outfits[:args.limit]

    stima = len(outfits) * (0.24 if args.image_size == "4K" else COST_PER_IMAGE)
    print(f"[*] {len(outfits)} outfit · {args.image_size} · foto per capo: {args.photos} "
          f"(max {args.max_ref} riferimenti) · stima ~${stima:.2f}", flush=True)

    ok = err = 0
    tagliati = 0
    quota_finita = False
    t0 = time.time()
    with open(LOG_JSONL, "a", encoding="utf-8") as log:
        for i, outfit in enumerate(outfits, 1):
            oid = outfit["outfit_id"]
            fmt = outfit_formality(outfit, tabella_formalita)
            data, errore, n_ref, disponibili = generate_one(
                client, outfit, args.image_size, tutte, args.max_ref,
                regole=regole.corrente(), formalita=fmt)

            if disponibili > n_ref:
                tagliati += 1

            if data:
                # Scrittura atomica: si scrive di fianco e si rinomina. Se il
                # processo muore a metà (batteria, sleep, kill) resterebbe
                # altrimenti un PNG troncato che --resume considera "già fatto"
                # per sempre, saltandolo a ogni ripartenza.
                tmp = dest(oid).with_suffix(".png.parziale")
                tmp.write_bytes(data)
                tmp.replace(dest(oid))
                ok += 1
                esito = "ok"
            else:
                err += 1
                esito = "errore"
                print(f"    [{i}/{len(outfits)}] {oid} FALLITO: {errore}", flush=True)
                if errore and errore.startswith("QUOTA_GIORNALIERA_ESAURITA"):
                    quota_finita = True

            log.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  "outfit_id": oid, "esito": esito, "errore": errore,
                                  "label": outfit.get("label"), "score": outfit["outfit_score"],
                                  "foto_inviate": n_ref, "foto_disponibili": disponibili,
                                  "formalita": round(fmt, 2),
                                  "regole_versione": regole.versione,
                                  # serve al manifest per ricostruire ESATTAMENTE
                                  # quali foto sono state inviate: l'allocazione
                                  # cambia fra le due modalità
                                  "modalita_foto": args.photos,
                                  "max_ref": args.max_ref,
                                  "variante": args.photos + args.suffix},
                                 ensure_ascii=False) + "\n")
            log.flush()

            if i % 10 == 0 or i == len(outfits):
                el = time.time() - t0
                print(f"    [{i}/{len(outfits)}] ok={ok} errori={err} · "
                      f"{el/i:.1f}s/img · spesa ~${ok * COST_PER_IMAGE:.2f}", flush=True)

            if quota_finita:
                print(f"\n[!] Quota giornaliera dell'API esaurita dopo {ok} immagini.\n"
                      f"    Il run si ferma qui invece di accumulare errori: la quota si\n"
                      f"    ripristina al cambio giorno del piano (mezzanotte Pacific Time).\n"
                      f"    Riprendi con lo stesso comando e --resume: le {ok} già fatte\n"
                      f"    non verranno rigenerate.\n"
                      f"    Limiti del tuo piano: https://ai.dev/rate-limit", flush=True)
                break

            time.sleep(args.sleep)

    print(f"\n[OK] generate {ok}, fallite {err} · immagini in {OUT_DIR}", flush=True)
    if tagliati:
        print(f"     {tagliati} outfit avevano più foto del tetto di {args.max_ref}: "
              f"per ognuno è entrata almeno la prima foto di ogni capo (vedi allocate_photos)",
              flush=True)


if __name__ == "__main__":
    main()

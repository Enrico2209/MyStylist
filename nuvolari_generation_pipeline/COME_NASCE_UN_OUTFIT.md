# Come nasce un outfit

Documento di riferimento sul processo che, partendo da 2901 schede prodotto
scaricate da nuvolari.biz, arriva a 1762 outfit completi e alla foto di
ognuno. Elenca **ogni regola applicata**, il valore numerico che la governa e
il motivo per cui esiste — quasi tutte sono nate da un difetto osservato, non
da una teoria.

I numeri riportati sono quelli del catalogo attuale, misurati, non stimati.

---

## Indice

1. [Il percorso in breve](#1-il-percorso-in-breve)
2. [Fase 1 — Le schede prodotto](#2-fase-1--le-schede-prodotto)
3. [Fase 2 — Gli attributi](#3-fase-2--gli-attributi)
4. [Fase 3 — I vettori](#4-fase-3--i-vettori)
5. [Fase 4 — I gruppi stilistici](#5-fase-4--i-gruppi-stilistici)
6. [Fase 5 — Il punteggio fra due capi](#6-fase-5--il-punteggio-fra-due-capi)
7. [Fase 6 — La costruzione dell'outfit](#7-fase-6--la-costruzione-delloutfit)
8. [Fase 7 — La foto](#8-fase-7--la-foto)
9. [Tutte le regole in una pagina](#9-tutte-le-regole-in-una-pagina)
10. [Cosa il sistema ancora non sa fare](#10-cosa-il-sistema-ancora-non-sa-fare)

---

## 1. Il percorso in breve

| fase | file | cosa produce |
|---|---|---|
| 1 · scraping | `scrape_with_attributes.py` | foto + `metadata.json` per 2901 prodotti |
| 2 · attributi | `scrape_with_attributes.py` | stile, formalità, stagione, fantasia, vestibilità |
| 3 · vettori | `feature_engineering.py` | colore in Lab + vettore stile a 15 dimensioni |
| 4 · gruppi | `clustering.py` | 44 cluster stilistici + outlier |
| 5 · punteggio | `scoring.py` | `score(A,B)` fra due capi qualsiasi |
| 6 · outfit | `outfit_generation.py` | 1762 outfit completi |
| 7 · foto | `generate_outfit_images.py` | l'immagine dell'outfit indossato |

Ogni fase legge il risultato della precedente. Le fasi 2, 3 e 4 sono
**concatenate in modo stretto**: cambiare un valore di formalità in fase 2
cambia il vettore in fase 3, cambia i cluster in fase 4 e ricompone tutto il
pool in fase 6. È il motivo per cui una correzione apparentemente locale
costringe a rigenerare tutto.

---

## 2. Fase 1 — Le schede prodotto

Per ogni prodotto si scaricano foto, titolo, descrizione, prezzo,
composizione, vestibilità e URL. Le foto vengono filtrate:

- **Scartate per percorso**: `/media/wysiwyg/`, `/media/logo`, `/media/PopUp`,
  `/static/`, e ogni nome contenente `placeholder`. Sono elementi grafici del
  sito, non fotografie del capo.
- **Scartate se illeggibili**: un file che PIL non riesce ad aprire.

### Il brand

Il collegamento "brand" nella pagina era quasi sempre sbagliato, quindi il
marchio si riconosce **dal titolo e dall'URL**, in quest'ordine:

1. Confronto con l'elenco marchi, **senza accenti** — la normalizzazione
   Unicode NFD recupera 33 prodotti `GAËLLE PARIS` che il confronto letterale
   perdeva.
2. Se il titolo non basta, si cerca lo slug nell'URL con confini `[/-]slug[-/.]`
   — i confini evitano che il dominio `nuvolari.biz` faccia scattare il marchio
   "Nuvolari" su ogni prodotto del sito. Recupera 182 marchi.

**Risultato**: 2671 prodotti su 2901 (92,1%) hanno un marchio. I ~230 restanti
non lo dichiarano da nessuna parte.

---

## 3. Fase 2 — Gli attributi

Da titolo, descrizione, composizione, vestibilità e marchio si derivano gli
attributi che governano tutto il resto.

### 3.1 I dieci tag di stile

`elegante`, `casual`, `streetwear`, `sportivo`, `workwear`,
`outdoor_tecnico`, `vintage_prep`, `minimal`, `military`, `boho_fantasia`.

Ogni tag accumula punti da quattro sorgenti, con pesi diversi:

| sorgente | peso | esempio |
|---|---|---|
| parola nel titolo o nella descrizione | **+0,8** | "sartoriale" → elegante |
| materiale nella composizione | **+0,5** | lana → elegante, vintage_prep |
| vestibilità | **+0,3** | oversize → streetwear |
| marchio | **+0,4** | Carhartt WIP → workwear, streetwear |

Il testo pesa il doppio del materiale perché descrive *quel capo*, mentre
"98% cotone" descrive mezzo catalogo.

**Materiali riconosciuti**: lana, cashmere, piumino, nylon, pelle, denim,
lino, cotone, poliestere, viscosa, elastan. Ognuno porta con sé anche una
stagione (lana → inverno, lino → estate).

**Vestibilità riconosciute**: slim, skinny, regular, oversize, baggy, relaxed.
Oltre allo stile, spostano la formalità: slim **+1**, oversize e baggy **−1**.

### 3.2 Il capo "da rivedere" — `needs_vision_review`

Un capo viene marcato da rivedere, ed **escluso da ogni outfit**, quando:

- nessun tag supera la soglia di **0,5**, oppure
- i tag che la superano **non hanno riscontro nel testo**, cioè arrivano solo
  da materiale, vestibilità o marchio.

La seconda condizione è la più importante. Un capo con descrizione vuota, "98%
cotone" e un marchio trasversale otterrebbe comunque `casual` sopra soglia —
ma non perché sappiamo com'è fatto: perché non sappiamo nulla e i segnali
generici hanno riempito il vuoto. Trattarlo come "taggato con sicurezza" lo
renderebbe abbinabile a tutto.

**Esclusi così: 422 prodotti su 2901 (14,5%).**

### 3.3 La formalità — scala 1-5

Il punteggio nasce dalla combinazione di tre segnali:

```
formalità_grezza = 0,50 × tipo_di_capo
                 + 0,35 × media_formalità_dei_tag_di_stile
                 + 0,15 × (tipo_di_capo + delta_vestibilità)

formalità = arrotonda, limitata fra 1 e 5
```

Poi si applica il **tetto per marchio**, che vince su tutto (§3.4).

#### Il tipo di capo, riconosciuto dal titolo

| livello | capi |
|---|---|
| **1** | zaino, zainetto, marsupio, tuta, costume, bikini, infradito, ciabatte, canotta, canottiera, pareo, pochette |
| **2** | t-shirt, felpa, jeans, sneakers, bermuda, pantaloncino, shorts, bomber, giubbotto, cappello, cappellino, tracolla, sandalo |
| **3** | camicia, polo, maglia, maglione, cardigan, pantalone, chino, gilet, borsa, sciarpa, dolcevita, gonna, vestito |
| **4** | giacca, blazer, cappotto, mocassini, parka, trench, stringate, derby, oxford |
| **5** | completo, smoking, frac, cravatta, papillon |

La scansione va **dal livello 5 al livello 1** e si ferma al primo riscontro,
così in "GIACCA CAMICIA" vince `giacca`: è un capospalla, non una camicia.

#### I termini specifici, che vengono controllati PRIMA

| termine | livello |
|---|---|
| cargo | 2 |
| kway, k-way, antivento | 2 |
| trackpant, tracktop | 2 |

**Perché esiste questa eccezione.** La regola "vince il più formale" era
scritta per un caso solo, e negli altri fa danno: la parola generica è quasi
sempre la più formale, quindi vinceva sempre lei. In "PANTALONE CARGO" il
sistema si fermava su `pantalone` (livello 3, come una camicia) senza mai
guardare `cargo`, che è workwear; in "GIACCA K-WAY LE VRAI" si fermava su
`giacca` (livello 4, come un blazer) invece di riconoscere un antivento.
Erano 68 cargo su 73 e 49 K-Way su 51.

#### Se il titolo non dice niente

Si ricade sul percorso di categoria: `completi` 5, `giacche` 4, `camicie` 3,
`polo` 3, `maglieria` 3, `pantaloni` 3, `jeans` 2, `t-shirt` 2, `felpe` 2,
`sneakers` 2, `giubbotti` 2, `cappotti` 4, `bomber` 2, `gilet` 3, `tute` 1,
`costumi` 1. Se nemmeno lì si trova nulla: **3**, il neutro.

#### La formalità dei tag di stile

`elegante` 5 · `vintage_prep`, `minimal` 3 · `casual`, `workwear`,
`boho_fantasia`, `military` 2 · `streetwear`, `sportivo`, `outdoor_tecnico` 1.

**Distribuzione attuale** — 1: 94 capi · 2: 1475 · 3: 1257 · 4: 75 · 5: nessuno.

### 3.4 Il tetto per marchio

Le parole della tabella descrivono la **forma** del capo, non il registro.
"Borsa" vale sia per una borsa in pelle da donna sia per la REDBOX CARRY BAG
di The North Face, che è una sacca da montagna verde lime. La forma non le
distingue, il marchio sì.

| marchi | tetto |
|---|---|
| Sprayground, Bape, A Bathing Ape, Off-White, Stüssy, Supreme | **1** |
| K-Way, The North Face, Napapijri, Nike, adidas, adidas Originals, New Balance, Asics, Saucony, Hoka, Puma, Under Armour | **2** |

È un **tetto**, non un valore fisso: un costume K-Way è già 1 e ci resta.

**Eccezione dentro l'eccezione.** Se il titolo di uno di questi marchi contiene
`borsa`, `sacca`, `zaino`, `zainetto`, `marsupio`, `tracolla`, `pochette`,
`backpack`, `bkpack`, `daypack`, `duffel` o `carry bag`, il tetto scende a
**1** anche per i marchi che ne avrebbero 2. Un piumino The North Face con un
maglione ci sta ancora; una sacca da montagna no — è della stessa famiglia del
marsupio, che è già livello 1.

Nasce da un'incoerenza misurata: `ZAINO THE NORTH FACE BASE CAMP` stava a 1 e
`BORSA THE NORTH FACE REDBOX CARRY BAG` a 2, stessa marca e stessa funzione,
solo perché un titolo diceva "zaino" e l'altro "borsa".

### 3.5 La stagione

Tre segnali, in ordine di precedenza:

**1. Il tipo di capo** — il più affidabile, perché non è ambiguo:

| stagione | capi |
|---|---|
| **inverno** | sciarpa, scaldacollo, guanti, muffole, cappotto, piumino, montone, parka, pile, maglione, dolcevita, lupetto, colbacco, passamontagna, berretto di lana |
| **estate** | costume, bikini, boxer mare, pareo, canotta, canottiera, top a fascia, infradito, ciabatte, sandalo, bermuda, pantaloncino, shorts, cappello di paglia |

**Volutamente esclusi**: cardigan, felpa, giacca. Sono capi trasversali — un
cardigan leggero sta su una t-shirt in primavera — e marcarli "inverno"
escluderebbe abbinamenti corretti invece di correggerne.

**2. Le parole della scheda** — "estivo", "invernale", "mezza stagione".

**3. Il materiale** — lana e cashmere → inverno, lino → estate.

Se nessuno dei tre dice niente: **`tutte`**, che nell'abbinamento è un jolly
compatibile con qualunque stagione.

> Questo era il difetto della sciarpa estiva. La stagione si deduceva solo da
> parole e materiale, mai dal tipo di capo: 179 capi invernali su 222 finivano
> in `tutte`, e una sciarpa di cotone diventava abbinabile a una t-shirt.

### 3.6 Il genere

1. `DONNA` nel titolo → donna. `UOMO` nel titolo → uomo.
2. Percorso `abbigliamento-donna` → donna.
3. Titolo contenente `borsa`, `tracolla` o `marsupio` senza genere dichiarato
   → **uomo**.
4. Altrimenti **non determinato**, cioè compatibile con entrambi.

La regola 3 esiste perché una tracolla street senza indicazione risultava
neutra e finiva su outfit donna. Cappelli, occhiali, zaini e sciarpe restano
genuinamente trasversali e non compaiono nella regola.

### 3.7 Manica e lunghezza gamba

Servono alla Regola 1 (§7.4).

**Manica** — `t-shirt`, `polo`, `canotta`, `maglietta`, `top` sono sempre
corte; `maglia`, `felpa`, `maglione`, `pullover`, `cardigan`, `overshirt`
sempre lunghe. Per camicia, blusa, camicetta e body, che sono ambigue, si
guarda la sottocategoria del sito (`.../manica-lunga/...` o
`.../mezza-manica/...`, che copre il 97% delle camicie) e poi il testo. Se non
si determina: **lunga**, scelta conservativa — meglio escludere un top per
sbaglio che metterlo su un bermuda se in realtà è invernale.

**Gamba** — `bermuda`, `short`, `shorts`, `pantaloncini` sono corti, tutto il
resto lungo.

---

## 4. Fase 3 — I vettori

### 4.1 Il colore

Da ogni foto rappresentativa si estraggono i colori dominanti:

1. La foto si riduce a **220 px** di lato lungo, per velocità.
2. Si individua lo **sfondo** con un flood-fill dai bordi, tolleranza **18**
   di distanza euclidea RGB, e lo si esclude. Senza questo passaggio il colore
   dominante di ogni capo sarebbe il bianco dello studio fotografico.
3. Sui pixel restanti si esegue un **K-Means a 3 colori**.
4. Ogni colore si converte in **Lab** — L luminosità, a e b le due componenti
   cromatiche — che è lo spazio in cui le distanze si avvicinano a come
   l'occhio percepisce le differenze.

Ogni colore porta con sé la sua **proporzione** nell'immagine.

Come foto rappresentativa si sceglie quella con **meno sfondo**, cioè quella
in cui il capo occupa più spazio.

### 4.2 Il vettore stile — 15 dimensioni

- **10 tag di stile**, ognuno normalizzato dividendo per **3,0** e limitato a
  1,0. Il punteggio grezzo non è sogliato: qui serve il segnale continuo, non
  il badge sopra/sotto 0,5 della fase 2.
- **1 formalità normalizzata**: `(livello − 1) / 4`, quindi 1→0,00 · 2→0,25 ·
  3→0,50 · 4→0,75 · 5→1,00.
- **4 stagioni** in codifica one-hot.

---

## 5. Fase 4 — I gruppi stilistici

HDBSCAN sulle prime **11 dimensioni** — i dieci tag più la formalità. La
stagione è esclusa di proposito: è già un filtro esplicito in fase 6, e
includerla qui separerebbe un blazer invernale da uno estivo pur essendo lo
stesso registro stilistico.

| parametro | valore | perché |
|---|---|---|
| `min_cluster_size` | 10 | sotto i dieci capi non è un registro, è un caso |
| `cluster_selection_epsilon` | 0,15 | senza, il vettore a bassa cardinalità produce decine di micro-cluster con profili identici |
| `metric` | euclidea | |

**Perché HDBSCAN e non K-Means**: non richiede di fissare il numero di gruppi
in anticipo, regge densità diverse, e soprattutto **isola gli outlier**
(etichetta −1) invece di forzarli in un gruppo sbagliato.

**Risultato attuale: 44 cluster, 1354 outlier (47%).**

Il 47% di outlier non è un difetto: è un catalogo generalista dove metà dei
capi non appartiene a un registro netto. Come vengono trattati è materia della
fase 6.

---

## 6. Fase 5 — Il punteggio fra due capi

```
score(A,B) = 0,5 × armonia_colore(A,B) + 0,5 × affinità_stile(A,B)
```

### 6.1 L'armonia di colore

Si confrontano i **2 colori principali** di ogni capo, quindi 4 coppie, e si
fa la media **pesata per il prodotto delle proporzioni** — un colore
marginale, come un dettaglio, conta meno di quello dominante.

Per ogni coppia di colori si calcola: **ΔE** (distanza complessiva in Lab),
**croma** (saturazione, distanza dal grigio) e **Δh** (distanza sul cerchio
delle tonalità, 0-360°).

#### Il punteggio di tonalità

| condizione | punteggio | lettura |
|---|---|---|
| ΔE < 3 | **0,85** | praticamente lo stesso colore: monocromatico voluto |
| Δh < 20° e ΔE < 15 | **0,25** | quasi uguale ma non uguale: sembra un errore |
| Δh < 20° e ΔE ≥ 15 | **0,80** | stessa tonalità, luminosità diversa: monocromatico vero |
| Δh < 60° | **0,75** | tonalità vicine: analogo |
| Δh > 130° | **0,70** | tonalità opposte: complementare |
| Δh > 100° | **0,60** | split-complementare |
| tutto il resto | **0,45** | zona senza una regola forte |

> **La soglia del complementare è 130°, non 150°.** In spazio Lab le coppie
> complementari classiche non arrivano a 180°: blu e arancio, l'esempio da
> manuale, distano 135°. Con la soglia a 150 la regola non scattava quasi mai
> e il 9,9% delle coppie sature finiva nella zona neutra a 0,45. Correggendola,
> le coppie complementari nel pool sono passate da 26 a 94.

#### Il bonus del colore neutro

Un colore poco saturo — nero, bianco, grigio, beige — sta bene con quasi
tutto. Il bonus non è un interruttore ma una sfumatura: sotto croma **10**,

```
punteggio = peso × 0,90 + (1 − peso) × punteggio_di_tonalità
dove peso = 1 − croma_minima / 10
```

Un grigio puro (croma ~0) è neutro davvero; un grigio-blu appena sotto soglia
ha comunque un sottotono freddo che può stonare con la sabbia.

#### L'eccezione del sottotono scuro

```
se  max(L) < 40  e  |ΔL| < 25  e  max(croma) ≥ 5   →   0,30
```

È il nero con il blu notte. Il colore neutro c'è — il nero ha croma ~0 — ma
non assorbe niente: senza stacco di luminosità l'occhio legge solo la
differenza di sottotono, e la legge come sbaglio.

> Senza questa eccezione, `nero + blu navy` prendeva **0,90**, il massimo,
> mentre `blu navy + blu navy` prendeva 0,25. Le coppie scure quasi-uguali nel
> pool sono passate da 669 su 1799 (37,2%) a 19 su 1759 (**1,1%**).

### 6.2 L'affinità di stile

```
affinità = max(0, coseno × affidabilità − 0,5 × |Δformalità|)
```

**Il coseno** fra i due vettori di stile misura se puntano nella stessa
direzione.

**L'affidabilità** è `min(1, norma_minima / 0,55)`, dove 0,55 è la norma
mediana del catalogo. Serve perché il coseno guarda la direzione ma non la
forza: un capo dal segnale debolissimo — tutti i tag vicini a zero — può
risultare "nella stessa direzione" di qualunque cosa e ottenere un coseno
altissimo, sembrando compatibile con tutto quando in realtà non sappiamo cosa
sia. Smorzando per la norma, un capo dal segnale debole non è più un jolly
automatico.

**La penalità di formalità** sottrae metà della differenza: un blazer
sartoriale e dei pantaloncini da running non si abbinano nemmeno se
condividono qualche tag.

---

## 7. Fase 6 — La costruzione dell'outfit

### 7.1 Gli slot

**Obbligatori**: `top`, `bottom`, `shoes`.
**Facoltativi**: `outerwear`, `accessory`.

Lo slot si riconosce scandendo il titolo **parola per parola da sinistra** e
prendendo il primo riscontro — non "la prima categoria del dizionario che
compare da qualche parte nel testo". La differenza conta: nel titolo di una
sneaker il nome del colore `N.BLAZER/B TAN` conteneva `blazer` e la faceva
classificare come capospalla; nei capi Guess Jeans, `jeans` nel nome del
marchio veniva letto come tipo di capo.

Il trattino resta dentro la parola, altrimenti `t-shirt` si spezza in `t` e
`shirt`, nessuno dei due riconoscibile.

**Capi ammissibili per slot** (esclusi i 422 da rivedere):

| slot | capi |
|---|---|
| top | 1218 |
| bottom | 547 |
| outerwear | 196 |
| shoes | 195 |
| accessory | 117 |
| *abiti, completi, tute, costumi* | *68 — esclusi* |
| *slot non riconosciuto* | *138 — esclusi* |

Abiti e completi coprono da soli top e bottom: la costruzione a slot non sa
gestirli e restano fuori.

### 7.2 La ricerca — beam search

Si parte da un capo **ancora** e si riempiono gli slot uno alla volta. A ogni
passo si tengono aperte le **5 combinazioni parziali migliori**, non solo la
migliore.

Perché non basta la migliore: un bottom perfetto con l'ancora può rivelarsi un
vicolo cieco quando si arriva alle scarpe. Tenendo cinque strade aperte, una
scelta localmente ottima ma sterile non blocca la ricerca.

Per velocità, a ogni passo i candidati si pre-ordinano per punteggio contro la
sola ancora e se ne valutano a fondo — cioè contro tutto il parziale — solo i
**primi 30**.

**Le ancore sono tutti i capi di tutti e tre gli slot obbligatori**, in ordine
deterministico per percorso. Ancorare solo dai top garantirebbe copertura ai
top, non a bottom e scarpe, che potrebbero perdere sempre come candidati senza
mai essere loro stessi il punto di partenza. Nel run attuale: **1960 ancore**.

### 7.3 I filtri sui candidati

Un capo entra fra i candidati di uno slot solo se supera **tutti** questi
controlli:

| # | filtro | regola |
|---|---|---|
| 1 | slot | è del tipo giusto |
| 2 | affidabilità | `needs_vision_review` è falso |
| 3 | riuso | non è già nell'outfit |
| 4 | genere | compatibile con **tutti** i capi già scelti |
| 5 | stagione | compatibile con **tutti** i capi già scelti |
| 6 | cluster | stesso cluster dell'ancora, oppure uno dei due è outlier |
| 7 | formalità | la dispersione dell'outfit resta ≤ **0,3** |
| 8 | coerenza stagionale | vedi §7.4 |

> **Perché "tutti i capi già scelti" e non solo l'ancora.** Se l'ancora ha il
> valore indeterminato — genere `None`, stagione `tutte` — è compatibile con
> chiunque, e due capi incompatibili *fra loro* possono entrare entrambi
> passando ciascuno il confronto con lei. Succedeva: 31 outfit mescolavano
> uomo e donna, 138 mescolavano estate e inverno.

#### Il filtro cluster e gli outlier

Se uno dei due capi è outlier, il filtro **non si applica** e decide solo il
punteggio. Escludere a priori il 47% del catalogo sarebbe uno spreco.

Ma per il **capospalla** il cluster reale è obbligatorio: un pezzo stonato lì
pesa su tutta la figura, e un capo può superare la soglia di punteggio proprio
per debolezza di segnale.

Per gli **accessori** no. Il 68% è outlier — non perché sbagliato, ma perché
borse e zaini hanno un vettore di stile poco denso — e pretendere il cluster
li escludeva in blocco, comprese le borse street che devono poter comparire
sugli outfit street. A trattenere i pezzi fuori registro basta ora il vincolo
di formalità, che dopo la correzione del prior discrimina davvero.

#### La dispersione di formalità

```
(formalità_massima − formalità_minima) ≤ 0,3
```

Su una scala con gradini di 0,25 questo tollera **un solo gradino**. Un capo
di livello 2 può stare con livelli 1, 2 e 3; uno di livello 1 solo con 1 e 2.

Non basta che il candidato stia bene *in coppia* con ogni capo già scelto:
l'insieme deve restare coerente. Un panciotto sartoriale non convive con
sneakers e cappellino anche se il coseno con ciascuno sembrava accettabile.

### 7.4 Le due regole di coerenza stagionale

**Regola 1 — pelle scoperta e capi invernali.**

- Se il bottom è a gamba corta, il top **deve** avere maniche corte.
- Nessun capo `inverno`, in **nessuno slot**, se le gambe o le braccia sono
  scoperte.
- Nessun capo a gamba o manica corta se nell'outfit c'è già un capo `inverno`.

La regola è **unidirezionale**: un top a maniche corte con pantaloni lunghi
resta ammesso, è normale in mezza stagione.

> Il controllo esisteva solo fra top e bottom. Accessori e capospalla non lo
> attraversavano mai, ed erano l'unica via per cui una sciarpa entrava in un
> outfit estivo. Ora si applica a tutti gli slot: gli outfit misti sono passati
> da 31 a **0**.

**Regola 2 — niente capospalla sui pantaloncini.** Se il bottom è a gamba
corta, lo slot `outerwear` viene saltato del tutto.

La parte "niente felpe sui pantaloncini" della stessa richiesta è già coperta
dalla Regola 1, perché una felpa ha manica lunga per definizione.

### 7.5 Il punteggio dell'outfit

```
punteggio_outfit = MINIMO fra tutti i punteggi di coppia
```

**Il minimo, non la media.** Una media può nascondere un singolo abbinamento
pessimo dietro tanti buoni. Il punteggio riflette l'anello più debole della
catena, non la sua forza media. Lo stesso criterio guida la beam search
durante la costruzione, non solo la valutazione finale.

### 7.6 Le soglie di accettazione

| soglia | valore | effetto |
|---|---|---|
| outfit completo | **0,60** | sotto, l'outfit si scarta |
| slot facoltativo | **0,50** | sotto, meglio nessun accessorio che uno stonato |

Il capo facoltativo entra **solo se migliora o non peggiora** oltre soglia: si
sceglie il candidato che massimizza il minimo dei punteggi di coppia, e lo si
aggiunge solo se resta sopra 0,50.

### 7.7 La deduplica

L'insieme `{A, B, C}` può risultare il migliore partendo da A, da B o da C. La
firma dell'outfit è l'**insieme non ordinato** dei percorsi dei capi, e ogni
insieme si tiene una volta sola.

L'`outfit_id` è l'hash SHA-256 (primi 12 caratteri) della stessa firma
ordinata. Ne discende una proprietà utile: **un outfit la cui composizione non
cambia mantiene lo stesso identificativo anche dopo una rigenerazione**, e con
esso la sua foto. Quando invece cambia la composizione, l'identificativo
cambia e la foto va rifatta.

### 7.8 Il risultato

| | |
|---|---|
| ancore processate | 1960 |
| outfit unici prodotti | **1762** |
| punteggio minimo | 0,600 |
| punteggio mediano | 0,753 |
| punteggio massimo | 0,932 |
| outfit da 4 capi | 788 |
| outfit da 5 capi | 974 |

---

## 8. Fase 7 — La foto

Ogni outfit diventa una fotografia con Gemini 3 Pro Image. Alla richiesta si
allegano le **foto reali dei capi** — l'immagine non è inventata, è la
composizione di prodotti esistenti.

| vincolo | valore |
|---|---|
| foto di riferimento per richiesta | massimo **14** |
| formato | 3:4 |
| costo | $0,134 a immagine |
| tetto giornaliero osservato | ~245 richieste |

Quando i capi hanno più di 14 foto in totale, le foto si distribuiscono **a
turno fra i capi** invece di prenderle in ordine: così ogni capo dell'outfit è
rappresentato, invece di esaurire il budget sul primo.

**Media attuale: 12,3 foto per outfit.** 355 outfit superano il tetto e
subiscono il taglio.

---

## 9. Tutte le regole in una pagina

**Esclusioni preliminari**
1. Foto di sistema del sito (`/media/wysiwyg/`, logo, placeholder) — scartate
2. Capi senza segnale di stile affidabile — 422 esclusi
3. Abiti, completi, tute, costumi — 68 esclusi
4. Capi di cui non si riconosce il tipo — 138 esclusi

**Compatibilità fra capi**
5. Stesso genere, o genere non determinato — verificato contro tutti i capi
6. Stessa stagione, o stagione `tutte` — verificato contro tutti i capi
7. Stesso cluster stilistico, salvo outlier
8. Cluster reale obbligatorio per il capospalla, non per gli accessori
9. Dispersione di formalità ≤ 0,3 su tutto l'outfit
10. Nessun capo può comparire due volte nello stesso outfit

**Coerenza stagionale**
11. Bottom corto → top a maniche corte obbligatorio
12. Gambe o braccia scoperte → nessun capo invernale, in nessuno slot
13. Capo invernale presente → nessun capo a manica o gamba corta
14. Bottom corto → nessun capospalla

**Formalità**
15. Livello dal tipo di capo letto nel titolo, scansione dal più formale
16. I termini specifici (cargo, kway, antivento, trackpant) vengono prima
17. Tetto 1 per i marchi street, tetto 2 per gli sportivi
18. Tetto 1 per borse e zaini di quei marchi, anche se il marchio ha tetto 2
19. Vestibilità slim +1, oversize e baggy −1

**Colore**
20. Colori quasi identici ma non uguali — penalizzati a 0,25
21. Due scuri distinti solo dal sottotono — penalizzati a 0,30
22. Analoghi (< 60°) 0,75 · split-complementari (> 100°) 0,60 · complementari (> 130°) 0,70
23. Bonus neutro sfumato, non a interruttore

**Stile**
24. Coseno smorzato dalla forza del segnale — niente jolly per debolezza
25. Penalità pari a metà della differenza di formalità

**Punteggio e soglie**
26. Punteggio outfit = minimo dei punteggi di coppia
27. Outfit scartato sotto 0,60
28. Capo facoltativo aggiunto solo sopra 0,50
29. Outfit identici deduplicati per insieme di capi

---

## 10. Cosa il sistema ancora non sa fare

Elenco onesto dei limiti noti, misurati.

**La formalità è un asse solo.** Non può ammettere il cargo e respingere la
camicia, perché per lei sono entrambi livello 3. È il motivo per cui il 9%
degli outfit con un K-Way ha ancora un capo di livello 3: per chiuderlo
servirebbe una regola sul **tipo** di capo — un antivento sportivo non va
sopra una camicia — sul modello di quelle stagionali.

**Il `denim` sfugge ancora.** 22 capi hanno "denim" nel titolo e stanno a
livello 3 per la parola generica che li accompagna. Non è stato corretto
perché fra loro ci sono 7 camicie e 5 maglie, e decidere se una camicia di
jeans sia livello 2 o 3 è una scelta di dominio, non tecnica.

**127 capi ammissibili non compaiono in nessun outfit** (62 top, 37 bottom, 28
scarpe). Verificato: non è un difetto della ricerca — ricostruendo il miglior
outfit possibile per ciascuno, tutti restano sotto la soglia di 0,60.

**Il colore viene dalla foto, non dalla scheda.** Un capo fotografato con
illuminazione calda porta quella dominante nel suo profilo colore.

**Il sistema non sa cosa sia una silhouette.** Sa che due capi condividono un
registro e che i colori si accordano; non sa se le proporzioni funzionano
insieme.

---

*Documento generato leggendo il codice sorgente. I riferimenti:
`scrape_with_attributes.py` per attributi e tabelle, `feature_engineering.py`
per i vettori, `clustering.py` per i gruppi, `scoring.py` per il punteggio,
`outfit_generation.py` per le regole di costruzione,
`generate_outfit_images.py` per la generazione delle foto.*

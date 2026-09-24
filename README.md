# mcp-for-althingi-stjornarradid-lagasafn

MCP-server för isländsk riksdags- och rättsdata. Exponerar fyra källor till
Claude och andra MCP-kompatibla AI-assistenter:

- **Alþingi** — riksdagsdokument, ärendehistorik och voteringar via det
  officiella XML-API:et (`althingi.is/altext/xml/`). Täckning från det första
  þing 1845 fram till pågående þing.
- **Lagasafn** — den konsoliderade isländska lagsamlingen
  (`althingi.is/lagasafn/`). 1 708 gällande lagar med spann 1275–2025 (Jónsbók
  från 1275 är fortfarande delvis i kraft).
- **Reglugerð** — isländska förordningar via `api.reglugerd.is`. Officiellt
  REST-API drivet av Stafrænt Ísland. Täckning 1957 till idag.
- **Stjórnarráðið rit og skýrslur** — regeringspublikationer (rapporter,
  utredningar, åtgärdsplaner) från `stjornarradid.is`. Webbplatsens listning
  omfattar omkring 780 publikationer 1991–idag, och sitemapen ytterligare
  omkring 2 000 äldre (främst 1976–2017); de indexeras lokalt eftersom
  sajtens egen sökmotor är trasig.

## Funktion

Servern är skriven i Python och kommunicerar via Model Context Protocol
(MCP). Den exponerar tio MCP-verktyg som täcker både parlament,
rättsinformation och regeringspublikationer. Verktygen följer ett kedjbart
mönster — sök, hämta ärende-/dokumentträd, hämta enskilda fulltexter — så
att ID-fält kan föras vidare mellan verktygen utan manuell översättning.

Servern lagrar metadata och fulltext i PostgreSQL (med pgvector för
semantisk sökning) eller SQLite (lokal fil, ingen serverinstallation).
Embeddings genereras med `intfloat/multilingual-e5-base` (768 dim) som
fungerar för isländska eftersom ingen aktivt underhållen isländsk
sentence-transformer finns idag.

## MCP-verktyg

| Verktyg | Beskrivning |
|---|---|
| `is_lista_thing` | Listar alla isländska riksmöten (þing 1, 1845 → pågående) |
| `is_sok_althingi` | Söker þingmál (ärenden) för ett givet riksmöte |
| `is_hamta_arende` | Hämtar fullständig ärendehistorik — mál + alla þingskjöl |
| `is_hamta_dokument` | Hämtar metadata för ett enskilt þingskjal |
| `is_hamta_log` | Hämtar konsoliderad isländsk lag från lagasafn |
| `is_sok_reglugerd` | Fritextsökning i isländska förordningar (källans Elasticsearch över titel och text) |
| `is_hamta_reglugerd` | Hämtar en förordning med text (markdown) och PDF-länk när källan har en |
| `is_sok_skyrslur` | FTS-sökning i regeringspublikationer (rit og skýrslur) |
| `is_hamta_skyrsla` | Hämtar fulltext för en publikation (DB-cache → live PDF) |
| `is_sok_i_dokument` | Semantisk sökning via pgvector (multilingual-e5-base) |

## Installation

1. Klona repot:

   ```bash
   git clone https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn.git
   cd mcp-for-althingi-stjornarradid-lagasafn
   ```

2. Skapa Python-venv och installera beroenden (kräver Python 3.10+ och
   `mcp` 2.x, som `requirements.txt` anger):

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. Kopiera konfigurationsmallen och fyll i värdena:

   ```bash
   cp config.example.env .env
   ```

   Sätt minst `DATABASE_URL` (PostgreSQL eller SQLite). Andra variabler är
   valfria — se kommentarer i `config.example.env`.

4. Lägg till servern i Claude Desktops konfiguration
   (`~/Library/Application Support/Claude/claude_desktop_config.json` på macOS):

   ```json
   "island": {
     "command": "/absolut/sökväg/till/.venv/bin/python3",
     "args": ["/absolut/sökväg/till/mcp_server.py"],
     "cwd": "/absolut/sökväg/till/mcp-for-althingi-stjornarradid-lagasafn"
   }
   ```

5. Starta om Claude Desktop. De tio MCP-verktygen blir tillgängliga.

## Datakällor och täckning

Den första gången en lag, förordning eller skýrsla efterfrågas hämtas den
on-demand från källan och cachas lokalt. För större datavolymer finns
synkskript:

- `01_synka_lagasafn.py` — bulk-synk av alla 1 708 gällande lagar via
  althingi.is/lagasafn/zip/. Tar ~5–10 min.
- `02_synka_reglugerd.py` — hämtar alla förordningar med text i ett anrop
  (`/regulations/all/current/full`, ~6 200 förordningar, ~57 MB) och lagrar
  texten för den semantiska sökningen. Förordningar vars text ändrats får sina
  chunks ombyggda i nästa embeddingsteg. Sökning och hämtning går direkt mot
  källan och behöver inte den lokala kopian.
- `04_rensa_reglugerd.py` — nollställer okontrollerade PDF-länkar som äldre
  synkar lagrade för förordningar. Kör med `--torrkorning` först.
- `03_chunka_och_embedda.py` — chunkning och embedding för semantisk sökning.
  Krävs bara med PostgreSQL + pgvector.

## Daglig synk

`synk_daglig.sh` är en bash-wrapper som körs av launchd (macOS) varje natt
03:00. Den hämtar nya regeringspublikationer från stjornarradid.is, kör
chunkning och embedding för nya dokument och rensar gamla loggar.

Rit-steget (`stjornarradid_rit.py`) går igenom webbplatsens listning
(`/gogn/rit-og-skyrslur/?index=N`), lägger in metadata och extraherar PDF:er
för poster som saknar fulltext. En tom listning ger exitkod 1, så att en
ändrad webbplats syns i loggen. Första körningen efter en tom databas hämtar
alla publikationer med 5 s mellan anropen och tar därför länge.

Med `--inkludera-sitemap` tas också publikationer med som bara finns i
webbplatsens sitemap (`/sitemap.xml`, omkring 2 700 publikations-URL:er mot
listningens 780, främst äldre rapporter 1976–2017). För varje sådan hämtas
publikationssidan (titel, PDF-länk) och PDF:en. Synken är inkrementell via
sitemapens `lastmod`: en publikation prövas igen bara när dess `lastmod`
ändrats. Takten är robots.txt:s Crawl-delay, dock minst 2 s. Alla adresser ur
sitemapen kontrolleras mot värden innan de hämtas. Många äldre poster saknar
PDF och får då bara titel. `--max-sitemap N` begränsar antalet per körning.

```bash
python3 stjornarradid_rit.py --inkludera-sitemap --max-sitemap 200
```

### Äldre publikations-URL:er

Publikationer som lagrats med webbplatsens äldre adressform
(`/gogn/rit-og-skyrslur/stakt-rit/ÅÅÅÅ/MM/DD/slug/`) flyttas till den
nuvarande (`/gogn/rit-og-skyrslur/rit/ÅÅÅÅ-MM-DD-slug/`) i ett separat,
uttryckligt steg. Webbplatsens omdirigering följs för varje post. Poster som
synken redan lagt in under den nya adressen slås ihop med de gamla, utan att
fulltext eller chunks går förlorade. Har webbplatsen gett en publikation nytt
datum, så att omdirigeringen leder fel, godtas en post med samma slug och
identisk titel (vid flera sådana krävs samma år). Nätverksfel redovisas som
fel och leder aldrig till sammanslagning.

Kör först synken, så att listningens poster finns, och sedan:

```bash
python3 stjornarradid_rit.py --uppdatera-urler --torrkorning   # visa planen
python3 stjornarradid_rit.py --uppdatera-urler                 # genomför
```

Uppdateringen är idempotent. Den ingår inte i den dagliga synken; tills den
körts finns äldre och nya poster för samma publikation sida vid sida.

`is_hamta_skyrsla` tar emot båda adressformerna.

Generera och installera launchd-schema automatiskt:

```bash
python3 01_synka_lagasafn.py --installera-schema
launchctl load ~/Library/LaunchAgents/se.magnuskolsjo.mcp-island-synk-daglig.plist
```

## Backend och transport

Servern stöder två lagringsbackender och två MCP-transporter — välj vid
installation. Båda valen är symmetriska, inget är "fallback".

- **PostgreSQL** (rekommenderas för seriös användning): ger pgvector och
  parallella anslutningar. Kräver Docker eller lokal Postgres-installation.
- **SQLite** (enkel att komma igång): en lokal fil, ingen serverprocess.
  Vektorsökning kräver Postgres — den faller då tillbaka till FTS.
- **stdio**: MCP-klienten startar servern direkt.
- **http**: delad drift (Streamable HTTP på `/mcp`, standardport 8006) med
  Bearer-tokenautentisering. Konfigureras via `MCP_TRANSPORT=http` i `.env`.
  `MCP_API_KEY` är obligatorisk: utan nyckel avbryts uppstarten med
  exitkod 2. Klienten skickar `Authorization: Bearer <nyckel>`; saknad header
  ger 401 och fel nyckel 403.

### Vektorlagring (PostgreSQL)

Embeddings (`island.chunks`, `island.chunks_rit`) lagras som `halfvec(768)`
med HNSW-index (`halfvec_cosine_ops`, m=16, ef_construction=64). Det är
hälften så stort som `vector(768)` och ger samma topp-10 som exakt sökning
i provmätningen; `IS_HNSW_EF_SEARCH` (standard 100) sätts per fråga.

Servern läser kolumntypen vid varje sökning och fungerar både före och efter
konverteringen. En ny eller nästan tom databas (högst 10 000 chunks per
tabell) konverteras automatiskt vid uppstart. En större databas konverteras
i ett uttryckligt steg, eftersom tabellen skrivs om och är låst under tiden.
Uppstarten tar också bort ett dubblettindex som äldre versioner av
`03_chunka_och_embedda.py` byggde på samma kolumn.

## Uppgradering av en befintlig installation (från 1.1.0)

Ordningen spelar roll; stegen 3–6 ändrar databasen.

1. Installera den nya koden och `requirements.txt` (mcp 2.x) och starta
   servern en gång. Uppstarten tar bort dubblettindexen på `island.chunks`
   och `island.chunks_rit`; lagrar tabellerna embeddings som `vector` loggas
   att `05_konvertera_vektorer.py` behövs. Servern fungerar ändå.
2. Kör rit-synken: `python3 stjornarradid_rit.py` (den nya listningen).
3. Flytta publikationer med äldre adresser:
   `python3 stjornarradid_rit.py --uppdatera-urler --torrkorning`, därefter
   utan `--torrkorning`.
4. Byt vektorlagringen till `halfvec` med HNSW:
   `python3 05_konvertera_vektorer.py --torrkorning`, därefter
   `python3 05_konvertera_vektorer.py`. Tabellerna är låsta under
   omskrivningen; semantiska sökningar väntar tills den är klar.
5. Hämta förordningarna med text: `python3 02_synka_reglugerd.py`, och
   rensa gamla PDF-länkar: `python3 04_rensa_reglugerd.py --torrkorning`,
   därefter utan flaggan.
6. Chunka och embedda det som saknas: `python3 03_chunka_och_embedda.py`.
   Förordningarna ger tiotusentals nya chunks, som lagras som `halfvec`.
7. Valfritt: äldre rapporter via sitemapen,
   `python3 stjornarradid_rit.py --inkludera-sitemap`, i omgångar med
   `--max-sitemap N`.

## Känd begränsning — Alþingi-verktygen returnerar HTTP 403

Sedan 2026-05-18 returnerar Alþingis Cloudflare-shield `HTTP 403` med flaggan
`cf-mitigated: challenge` för alla anrop till `althingi.is`-domänen, oavsett
User-Agent. Det påverkar fem av tio MCP-verktyg:

- `is_lista_thing`
- `is_sok_althingi`
- `is_hamta_arende`
- `is_hamta_dokument`
- `is_hamta_log` (för icke-cachade lagar)

De övriga fem verktygen (`is_sok_reglugerd`, `is_hamta_reglugerd`,
`is_sok_skyrslur`, `is_hamta_skyrsla`, `is_sok_i_dokument`) fungerar normalt
— blockeringen är specifik för `althingi.is`.

### Vad som har testats utan framgång

- `curl_cffi` med Chrome-, Safari- och Edge-TLS-fingerprints (alla varianter
  ger 403).
- Manuellt hämtad `cf_clearance`-cookie från Safari (avvisas när den
  återanvänds från en serverside-klient).
- `Mozilla/5.0 (compatible; ...)`-prefixade User-Agent-strängar.

Cloudflare-challengen kräver troligen client-side JavaScript-rendering, vilket
inte kan reproduceras från ett serverside-klientbibliotek.

### Tillfällig genväg

Om du har en `cf_clearance`-cookie från Safari kan du sätta:

```bash
ALTHINGI_CF_CLEARANCE=<värdet från Safari>
ALTHINGI_USER_AGENT=<Safaris exakta User-Agent>
```

i `.env`. Klienten väljer då Safari-fingerprint automatiskt. Lyckas inte
universellt — fungerar i vissa konfigurationer.


## Fel och svarsformat

Verktygen returnerar strukturerade svar med utdataschema. Förväntade fel —
okänd beteckning, publikation som inte finns, källan svarar inte, databasen
är nere — ges som MCP-fel (`isError`) med ett meddelande på svenska, inte som
ett svar med `fel`-nyckel.

`is_hamta_reglugerd`: texten kommer ur källans `text`-fält (HTML omvandlad
till markdown, med bilagor) och kapas som i övriga hämtverktyg (`max_tecken`,
`fran_tecken`, `las_vidare`). För en del förordningar har källans detaljsvar
bara titel och länkar. Svaret har då `fullstandig: false`, `webb_url` till
reglugerd.is och en `notering`; texten tas i så fall från den lokala kopian
om den finns (`text_kalla: "lokal_kopia"`). `pdf_url` är källans konsoliderade PDF
(`pdf_typ: "konsoliderad"`), annars den ursprungliga kungörelsen i
Stjórnartíðindi (`"originalkungorelse"`), annars `null`.

`is_sok_reglugerd`: källans Elasticsearch-sökning över titel och text med
isländsk stamning (frågan följer query_string-syntax). Som standard tas
ändringsförordningar och upphävda förordningar med (`iA`/`iR`);
`med_andringsforordningar=false` och `med_upphavda=false` ger källans egen
standard, bara gällande grundförordningar. Källan har fast sidstorlek 30;
`max_treff` (1–30) och `page` räknas om därefter. En sökning kräver sökterm
eller år.

## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Den största publikationen i cachen är **1 301 674 tecken** och den största lagen 244 268.
`is_hamta_skyrsla` och `is_hamta_log` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken; `0` ger så mycket som ryms. Högst 200 000 tecken per anrop, eftersom svaret skickas två gånger (text och struktur) och ska hålla sig under 1 MB. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett kapat svar säger alltid ifrån med fälten `trunkerad`, `tecken_totalt`, `tecken_visade` och `fortsatt_fran_tecken`, som anger utdragets faktiska slut. `las_vidare` är det kompletta anropet för nästa utdrag. Kapningen sker på ordgräns, aldrig mitt i
ett ord.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `IS_MAX_TECKEN`.

## Licens

GNU Affero General Public License v3.0 (AGPL-3.0). Se `LICENSE` i repots rot
för fullständig text.

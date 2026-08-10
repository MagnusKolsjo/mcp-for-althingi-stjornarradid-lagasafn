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
  utredningar, åtgärdsplaner) från `stjornarradid.is`. 380 publikationer
  2021–2026, lokalt indexerade eftersom sajtens egen sökmotor är trasig.

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
| `is_sok_reglugerd` | Söker i isländska förordningar via api.reglugerd.is |
| `is_hamta_reglugerd` | Hämtar metadata och PDF-länk för en enskild förordning |
| `is_sok_skyrslur` | FTS-sökning i regeringspublikationer (rit og skýrslur) |
| `is_hamta_skyrsla` | Hämtar fulltext för en publikation (DB-cache → live PDF) |
| `is_sok_i_dokument` | Semantisk sökning via pgvector (multilingual-e5-base) |

## Installation

1. Klona repot:

   ```bash
   git clone https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn.git
   cd mcp-for-althingi-stjornarradid-lagasafn
   ```

2. Skapa Python-venv och installera beroenden:

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
- `02_synka_reglugerd.py` — bulk-synk av metadata för alla förordningar via
  api.reglugerd.is. Tar ~2–5 min.
- `03_chunka_och_embedda.py` — chunkning och embedding för semantisk sökning.
  Krävs bara med PostgreSQL + pgvector.

## Daglig synk

`synk_daglig.sh` är en bash-wrapper som körs av launchd (macOS) varje natt
03:00. Den hämtar nya regeringspublikationer från stjornarradid.is, kör
chunkning och embedding för nya dokument och rensar gamla loggar.

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
- **stdio**: standard. Claude Desktop startar servern direkt.
- **http**: hostad drift med Bearer-tokenautentisering. Konfigureras via
  `MCP_TRANSPORT=http` i `.env`.

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


## Svarsstorlek och trunkering

MCP-protokollet har en övre storleksgräns per svar. Den största publikationen i cachen är **528 278 tecken**.
`is_hamta_skyrsla` och `is_hamta_log` tar därför två parametrar:

| Parameter | Innebörd |
|---|---|
| `max_tecken` | Teckentak för texten. Standard 60 000 tecken; `0` ger hela texten som ett uttryckligt val. |
| `fran_tecken` | Börja vid denna teckenposition — för att läsa vidare där ett kapat svar slutade. |

Ett kapat svar säger alltid ifrån med fälten `trunkerad`, `tecken_totalt`, `tecken_visade` och `fortsatt_fran_tecken`. Kapningen sker på ordgräns, aldrig mitt i
ett ord.

**Vid ordagranna citat:** citera aldrig ur ett svar som är markerat som kapat.
Läs vidare med `fran_tecken` tills hela passagen är hämtad. Standardvärdet kan
sättas i `.env` med `IS_MAX_TECKEN`.

## Licens

GNU Affero General Public License v3.0 (AGPL-3.0). Se `LICENSE` i repots rot
för fullständig text.

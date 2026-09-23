# Ändringslogg — mcp-for-althingi-stjornarradid-lagasafn

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet
tillämpar [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Ändrat

- **Brytande:** kräver `mcp` 2.x (`mcp>=2.0,<3`). Servern bygger på
  `MCPServer`; transporten startas via `mcp_transport.py`.
- **Brytande:** http-läget kräver `MCP_API_KEY`. Utan nyckel avbryts
  uppstarten med exitkod 2 i stället för att servern startar oskyddad.
  Saknad `Authorization`-header ger 401, fel nyckel 403. http-läget
  (Streamable HTTP på `/mcp`, port 8006) startade inte alls tidigare.
- **Brytande:** `max_tecken` i `is_hamta_log` och `is_hamta_skyrsla` har ett
  övre tak på 200 000 tecken per anrop, även med `max_tecken=0` (som tidigare
  gav hela texten). Den största publikationen är över 1,3 miljoner tecken, och
  svaret skickas både som text och struktur; utan tak blev det långt över
  1 MB. Kapade svar har fältet `las_vidare` med det kompletta anropet för
  nästa utdrag.
- **Brytande:** förväntade fel (okänd beteckning, okänt þing, dokument som
  inte finns, källan svarar inte, databasen nere) ges som MCP-fel med
  `isError` och ett svenskt meddelande, i stället för ett svar med
  `fel`-nyckel.
- **Brytande:** publikationer från stjornarradid.is har nya adresser.
  `url` i `is_sok_skyrslur` och `is_hamta_skyrsla` har formen
  `/gogn/rit-og-skyrslur/rit/ÅÅÅÅ-MM-DD-slug/` i stället för
  `/gogn/rit-og-skyrslur/stakt-rit/ÅÅÅÅ/MM/DD/slug/` när `--uppdatera-urler`
  har körts. Sparade äldre adresser tas fortfarande emot av
  `is_hamta_skyrsla`.
- **Brytande:** `is_hamta_reglugerd` — `ministerium` är alltid namnet som
  sträng (tidigare ibland objektet `{slug, name}`). `pdf_url` kan vara `null`.
- **Brytande:** `is_sok_reglugerd` — sökning utan både sökterm och år avvisas
  (källan gav då alltid 0 träffar). `max_treff` gäller nu, 1–30.
- Alla verktyg har titel, annotationer (läsande; öppen eller sluten värld)
  och utdataschema.
- Stjórnarráðið hämtas med vanlig HTTP-klient och projektets User-Agent;
  webbläsarimitationen via `curl_cffi` behövs inte där. (Alþingi och
  lagasafn använder den fortfarande.)
- Lås kring lat inläsning av embeddingmodellen, þing-listans cache och
  takthinkarna mot althingi.is och stjornarradid.is, eftersom verktygen körs
  på arbetstrådar.

### Tillagt

- `stjornarradid_rit.py --uppdatera-urler [--torrkorning]`: flyttar lagrade
  publikationer från webbplatsens äldre `/stakt-rit/`-adresser till de
  nuvarande `/rit/`-adresserna via omdirigeringen och slår ihop dem med poster
  som synken redan lagt in under den nya adressen. Leder omdirigeringen till en
  mallsida godtas bara en post med samma slug och identisk titel (vid flera
  sådana även samma år). Nätverksfel och 5xx redovisas som fel. Idempotent;
  körs uttryckligen, inte av den dagliga synken. `--torrkorning` visar planen
  utan att skriva.
- `is_hamta_reglugerd`: fälten `pdf_typ`, `webb_url`, `fullstandig` och, för
  förordningar där källan bara har titel och länkar, `notering`.

### Rättat

- Stjórnarráðið-skrapningen fungerar mot den ombyggda webbplatsen: ny
  listning med paginering (`?index=N`, omkring 780 publikationer 1991–idag),
  nya publikationsadresser och PDF-länkar under `/library/?itemid=`.
  Webbplatsens mallsida för okända adresser känns igen.
- `is_hamta_skyrsla` tar emot både äldre och nya publikationsadresser.
- Metadatasynken för rit og skýrslur skrev över redan extraherad fulltext med
  tomt värde. Befintlig fulltext och PDF-länk behålls nu.
- `is_hamta_reglugerd` använder källans `pdfVersion` och bygger aldrig en
  PDF-länk som ger 404. `02_synka_reglugerd.py` lagrar inga konstruerade
  PDF-länkar.
- `is_sok_reglugerd` räknar om sidindelningen efter källans fasta sidstorlek
  30 (`perPage` ignoreras av källan).
- `is_sok_i_dokument` svarade med tomma träfflistor när Postgres var nere.
  Databasfel ger nu ett MCP-fel med orsaken.
- `--help` och okända flaggor till `stjornarradid_rit.py`,
  `01_synka_lagasafn.py`, `02_synka_reglugerd.py` och `mcp_server.py` startade
  synken respektive servern. Argumenten tolkas nu med argparse innan något
  körs; `--help` visar hjälpen och en okänd flagga ger fel (exitkod 2).
- Absoluta SQLite-sökvägar (`sqlite:////abs/fil.db`) tolkades som relativa.
- En tom rit-listning ger exitkod 1 i stället för att synken tyst lyckas.

### Säkerhet

- `is_hamta_skyrsla` och Stjórnarráðið-klienten kontaktar bara
  `https://www.stjornarradid.is` (och `stjornarradid.is`). Schema, värd, port
  och sökväg kontrolleras innan något hämtas, och varje omdirigeringsmål
  kontrolleras på samma sätt i stället för att följas blint. Tidigare kunde
  en anropare få servern att hämta godtyckliga adresser, även i det lokala
  nätet. Adresser med `http://` avvisas.

### Borttaget

- Den egna Starlette-appen för http-läget (`create_starlette_app` finns inte
  i mcp).
- `reglugerd.hamta_reglugerd_pdf_url()`, som byggde PDF-länkar utan kontroll.

---

## [1.1.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `is_hamta_skyrsla` och `is_hamta_log`**, med
  standardtaket `IS_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). Den största
  publikationen i cachen är **528 278 tecken**; verktygen returnerade hela texten utan
  möjlighet att begränsa, vilket för långa skýrslur ger onödigt stora svar och närmar
  sig MCP-protokollets storleksgräns. Med standardtaket blir samma anrop 61 294 tecken.
  Kapade svar bär `trunkerad`, `tecken_totalt`, `tecken_visade` och
  `fortsatt_fran_tecken`; kapningen sker på ordgräns, aldrig mitt i ett ord.
  `max_tecken=0` ger hela texten som ett uttryckligt val.
- Taket tillämpas på **båda kodvägarna** i `is_hamta_skyrsla` — DB-cacheträff och
  live-hämtning från stjornarradid.is — så att svarsstrukturen är densamma oavsett
  var texten kom ifrån.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar och fält; inga brytande
ändringar och inga schemaändringar. Databasen lagrar fortfarande hela texten —
trunkeringen gäller bara svaret till anroparen, så `is_sok_i_dokument` och den
semantiska sökningen påverkas inte.

---

## [1.0.0] — 2026-05-21

Första publicerade versionen. Tio MCP-verktyg mot fyra isländska källor.

### Tillagt

- **Alþingi** (`althingi.is/altext/xml/`) — `is_lista_thing`, `is_sok_althingi`,
  `is_hamta_arende`, `is_hamta_dokument`. Täcker þingskjöl, þingmál och voteringar
  från det första þing 1845.
- **Lagasafn** (`althingi.is/lagasafn/`) — `is_hamta_log` för konsoliderade lagar,
  bulk-synkade via `01_synka_lagasafn.py` (1 708 lagar 1275–2025).
- **Reglugerðir** (`api.reglugerd.is`) — `is_hamta_reglugerd`, `is_sok_reglugerd`.
- **Regeringspublikationer** (`stjornarradid.is`) — `is_sok_skyrslur`,
  `is_hamta_skyrsla`. 380 publikationer med OCR-extraherad text.
- **Semantisk sökning** — `is_sok_i_dokument` via pgvector med
  `intfloat/multilingual-e5-base`.
- PostgreSQL-schema `island` med SQLite som alternativ backend.
- Daglig synk via `synk_daglig.sh` i fyra steg: lagasafn, reglugerðir,
  rit og skýrslur, chunkning och embedding.

### Kända begränsningar

- Alþingi-baserade verktyg kan svara HTTP 403 när källans bot-shield är aktivt.
  Anropen använder `curl_cffi` med webbläsarfingeravtryck; vid blockering
  rapporterar `HamtaFel` orsaken separat i stället för ett generiskt fel.
- `is_sok_reglugerd` respekterar inte `max_treff` fullt ut — källan returnerar
  alltid `per_page=30`.

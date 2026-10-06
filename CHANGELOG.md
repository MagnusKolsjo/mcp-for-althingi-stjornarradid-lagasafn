# Ändringslogg — mcp-for-althingi-stjornarradid-lagasafn

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet
tillämpar [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [2.0.1] — 2026-10-06

### Fixat

- onnxruntime, som `pymupdf4llm` laddar för layout och OCR, skickade som standard användningsdata
  till Microsoft (`mobile.events.data.microsoft.com`) utan att användaren tillfrågats. Telemetrin
  stängs nu av i PDF-extraktionens barnprocess (`ORT_DISABLE_TELEMETRY=1` och
  `disable_telemetry_events()`) innan biblioteket laddas. Det tar också bort en krasch (SIGABRT)
  i telemetrins nedstängning när barnprocessen avslutades.
- Samtidiga sökanrop kunde krascha servern med SIGSEGV när embeddingmodellen kördes på
  Apple-GPU:n (MPS). PyTorchs MPS-backend fyller sina kärncacher utan lås första gången de
  används, och verktygen körs på parallella arbetstrådar. Alla `encode()`-anrop i processen
  går nu genom ett gemensamt lås.

## [2.0.0] — 2026-09-26

### Dokumentation
- README och `config.example.env` beskriver att althingi.is åter svarar på automatiserade anrop sedan 2026-09-24 och att alla tio verktygen fungerar. Avsnittet om den tidigare blockeringen (2026-05-18 och framåt) är omskrivet; `ALTHINGI_USER_AGENT` och `ALTHINGI_CF_CLEARANCE` finns kvar som valfria inställningar.

### Ändrat

- Frågeexpansion på serversidan har inget förvalt modellnamn. `QUERY_EXPANSION_MODEL` anges alltid i `.env` (platshållare `<modellnamn>` i `config.example.env`); saknas det hoppas expansionen över.
- Texterna är produktneutrala: README, konfigurationsexempel, kommentarer och äldre CHANGELOG-poster nämner MCP-klienten i stället för en viss klient.
- User-Agent-strängen följer huvudversionen: `mcp-for-althingi-stjornarradid-lagasafn/2.0`.
- Embeddings lagras som `halfvec(768)` med HNSW-index (m=16,
  ef_construction=64) i stället för `vector(768)` med IVFFlat. Frågorna läser
  kolumntypen och fungerar före och efter konverteringen; `hnsw.ef_search`
  (`IS_HNSW_EF_SEARCH`, standard 100) och `ivfflat.probes`
  (`IS_IVFFLAT_PROBES`, standard 1) sätts per fråga. Tabeller med högst
  10 000 chunks konverteras vid uppstart; större med
  `05_konvertera_vektorer.py`. I en kopia av en befintlig databas (100 000
  chunks) krympte chunk-tabellerna från 1,2 GB till 0,45 GB, och
  indexsökningens topp-10 stämde med exakt sökning i 153 av 160 fall mot
  51 av 160 med IVFFlat.
- Uppstarten tar bort det dubblerade IVFFlat-indexet
  (`idx_island_chunks_embedding`, `idx_island_chunks_rit_embedding`) som
  äldre `03_chunka_och_embedda.py --bygg-index` byggde vid sidan av
  schemats index.
- `03_chunka_och_embedda.py --bygg-index` bygger om HNSW-indexen; `--lists`
  ersatt av `--minne`.

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
  (källan gav då alltid 0 träffar). `max_treff` gäller nu, 1–30. Som standard
  ingår nu ändringsförordningar och upphävda förordningar, vilket ger fler
  träffar än tidigare.
- `02_synka_reglugerd.py` hämtar förordningarna med text via
  `/regulations/all/current/full` i stället för metadata år för år.
- Alla verktyg har titel, annotationer (läsande; öppen eller sluten värld)
  och utdataschema.
- Stjórnarráðið hämtas med vanlig HTTP-klient och projektets User-Agent;
  webbläsarimitationen via `curl_cffi` behövs inte där. (Alþingi och
  lagasafn använder den fortfarande.)
- Lås kring lat inläsning av embeddingmodellen, þing-listans cache och
  takthinkarna mot althingi.is och stjornarradid.is, eftersom verktygen körs
  på arbetstrådar.

### Tillagt

- `pdftext_skydd.py`: minnes- och tidsvakt kring PDF-extraktionen i
  `stjornarradid_rit.py`. Extraktionen körs i en egen process, i sidblock
  (`IS_PDF_SIDBLOCK`), och avbryts vid `IS_PDF_MAX_MINNE_MB` eller
  `IS_PDF_TIDSGRANS_S` — ett enskilt bildtungt dokument kan då inte längre
  fälla processen. Block som avbryts läses om med ren textutvinning, och
  dokument som fick minst en sida OCR:ad eller föll tillbaka på ren
  textutvinning läggs i en OCR-kö (`IS_OCR_KO_MAPP`) för senare, bättre OCR.
- `stjornarradid_rit.py --inkludera-sitemap [--max-sitemap N]`: tar också
  med publikationer som bara finns i webbplatsens sitemap (omkring 2 000
  fler, främst 1976–2017), inkrementellt via `lastmod`. Takten följer
  robots.txt:s Crawl-delay, dock minst 2 s; adresserna ur sitemapen
  kontrolleras mot värden innan de hämtas.
- PDF-länkar direkt till `.pdf` utanför `/library/` (t.ex. `/media/`) hittas
  på publikationssidorna.

- `is_hamta_reglugerd` returnerar förordningens text (`text_md`, markdown ur
  källans `text`-fält med bilagor), kapad med `max_tecken`/`fran_tecken` och
  `las_vidare` som övriga hämtverktyg. Saknar källans detaljsvar text används
  den lokala kopian (`text_kalla`).
- `is_sok_reglugerd`: parametrarna `med_andringsforordningar` och
  `med_upphavda` (källans `iA`/`iR`), båda True som standard så att sökningen
  täcker hela samlingen. Tidigare söktes bara gällande grundförordningar.
- Förordningar ingår i den semantiska sökningen: `02_synka_reglugerd.py`
  hämtar alla förordningar med text i ett anrop och lagrar texten; ändrad
  text gör att chunks byggs om.
- `05_konvertera_vektorer.py [--torrkorning] [--tabell …] [--bara-index]`:
  byter embeddings till `halfvec(768)`, bygger HNSW-index och tar bort
  dubblettindex. `--torrkorning` visar läge, plan och uppskattad storlek.
- `04_rensa_reglugerd.py [--torrkorning]`: nollställer okontrollerade
  PDF-länkar som äldre synkar lagrade för förordningar.

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

- PDF-extraktionen i `stjornarradid_rit.py` OCR:ade sidor utan textlager med
  standardspråket engelska, eftersom `ocr_language` aldrig sattes. Isländska
  tecken blev därmed fel. OCR-språket är nu uttryckligen `isl+eng`
  (`IS_OCR_SPRAK`).
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
- Crawl-delay på 5 s mot althingi.is hölls inte: hinken räknade väntetiden
  som förbrukad innan väntan var slut, så samtidiga anrop kunde gå iväg
  tätare, och althingi.py och lagasafn.py hade var sin hink mot samma värd.
  En gemensam strypning per värd (`takt.py`) reserverar nu varje starttid
  under lås och håller anropen minst 5 s isär, även mellan modulerna.
  curl_cffi och User-Agent-hanteringen är oförändrade.
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

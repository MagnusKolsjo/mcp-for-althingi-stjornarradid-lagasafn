# Ändringslogg — mcp-for-althingi-stjornarradid-lagasafn

Alla märkbara ändringar i detta projekt dokumenteras här.

Formatet följer [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) och projektet
tillämpar [Semantic Versioning](https://semver.org/).

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

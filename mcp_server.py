# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
mcp_server.py — MCP-server för isländsk riksdags- och rättsdata

Exponerar följande verktyg till MCP-kompatibla AI-verktyg:

  Riksmöten och parlamentsdokument:
    is_lista_thing       — Listar alla riksmöten (þing 1–157+)
    is_sok_althingi      — Söker þingmál i Alþingi via klient-sidesfiltrering
    is_hamta_arende      — Hämtar mál-metadata + komplett skjöl-lista
    is_hamta_dokument    — Hämtar metadata för ett enskilt þingskjal

  Lagtext och förordningar:
    is_hamta_log         — Hämtar konsoliderad lagtext från althingi.is/lagasafn/
    is_hamta_reglugerd   — Hämtar en förordning med text från api.reglugerd.is
    is_sok_reglugerd     — Fritextsökning i förordningar via api.reglugerd.is

  Publikationer:
    is_sok_skyrslur      — Söker i rit og skýrslur via lokal FTS (island.dokument_rit)
    is_hamta_skyrsla     — Hämtar fulltext för en publikation (DB-cache → live PDF)

  Semantisk sökning:
    is_sok_i_dokument    — Semantisk sökning via pgvector (multilingual-e5-base)

Datakällor:
  Alþingi XML-API   — althingi.is/altext/xml/ (þingskjöl, þingmál, voteringar)
  Lagasafn          — althingi.is/lagasafn/ (konsoliderade lagar)
  Reglugerd.is      — api.reglugerd.is (förordningar)
  Stjórnartíðindi   — api.stjornartidindi.is (officiell tidning)
  Stjórnarráðið     — stjornarradid.is, listningen under /gogn/rit-og-skyrslur/

Transport (styrs via MCP_TRANSPORT i .env, se mcp_transport.py):
  stdio:  python3 mcp_server.py
  http:   MCP_TRANSPORT=http MCP_API_KEY=<nyckel> python3 mcp_server.py
"""

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional, TypedDict

from dotenv import load_dotenv

# .env läses innan klientmodulerna importeras, eftersom de läser sin
# konfiguration vid import och servern inte ärver klientens shell-miljö.
load_dotenv(Path(__file__).parent / ".env")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import althingi as al
import lagasafn as ls
import reglugerd as rg
import stjornarradid_rit as sr
import db as db_mod
from db import initiera_schema
from klient_fel import HamtaFel
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

# Standardport i http-läget; MCP_PORT i .env har företräde.
STANDARDPORT = 8006

# ── Query-expansion ────────────────────────────────────────────────────────────
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL", "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL", "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
)

# ── Embeddingmodell ────────────────────────────────────────────────────────────
# intfloat/multilingual-e5-base: bästa tillgängliga för isländska (768 dim)
EMBEDDING_MODEL   = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
_embedding_modell = None
_embedding_las    = threading.Lock()

# PyTorchs MPS-backend är inte trådsäker: MetalShaderLibrary fyller sina
# kärncacher utan lås första gången de används, så två samtidiga encode() från
# arbetstrådarna kan korrumpera dem och krascha hela processen med SIGSEGV.
# Låset gäller hela processen och inte en enskild modell, eftersom cacherna
# delas av alla modeller på samma enhet.
_encode_las = threading.Lock()


class _SerialiseradModell:
    """Omsluter en SentenceTransformer så att encode() alltid tar _encode_las."""

    def __init__(self, modell) -> None:
        self._modell = modell

    def encode(self, *args, **kwargs):
        with _encode_las:
            return self._modell.encode(*args, **kwargs)

    def __getattr__(self, namn):
        return getattr(self._modell, namn)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Standardtak för fulltext i hämtverktygen. Isländska skýrslur och lagtexter når över en halv miljon tecken
# och kan överskrida MCP-protokollets storleksgräns, vilket får anropet att
# misslyckas helt. Anroparen kan höja taket upp till IS_MAX_TECKEN_TAK.
IS_MAX_TECKEN = int(os.getenv("IS_MAX_TECKEN", "60000"))

# Övre tak per svar, även med max_tecken=0. Svaret skickas två gånger (text
# och structuredContent); 200 000 tecken ger omkring 0,5 MB och håller sig
# under MCP-klienternas gräns på 1 MB. Den största publikationen är över
# 1,3 miljoner tecken och läses därför i flera anrop.
IS_MAX_TECKEN_TAK = 200_000


def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder "så mycket som möjligt", men aldrig
    mer än IS_MAX_TECKEN_TAK. Klipper på ordgräns.

    fortsatt_fran_tecken är utdragets faktiska slut. Kapningen på ordgräns gör
    utdraget kortare än taket; en fortsättning vid fran_tecken + max_tecken
    skulle hoppa över det avkapade ordet.
    """
    if not max_tecken or max_tecken <= 0 or max_tecken > IS_MAX_TECKEN_TAK:
        max_tecken = IS_MAX_TECKEN_TAK
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        # Ett utdrag av bara blanktecken skulle ge slut == start, och
        # fortsättningen skulle peka på samma ställe igen.
        utdrag    = utdrag.rstrip() or rest[:max_tecken]
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
        "max_tecken":           max_tecken,
    }


def _las_vidare(verktyg: str, nyckel: str, varde: str, max_tecken: int,
                slut: Optional[int], extra: str = "") -> Optional[str]:
    """Läs-vidare-raden som ett komplett anrop, eller None för sista utdraget."""
    if slut is None:
        return None
    return (f'Läs vidare: {verktyg}({nyckel}="{varde}"{extra}, '
            f"max_tecken={max_tecken}, fran_tecken={slut})")


# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "island",
    version="2.0.1",
    cache_hints=CACHE_HINTAR,
    instructions=(
        "MCP-server för isländsk riksdags- och rättsdata. "
        "Täcker Alþingi (þingskjöl, þingmál, voteringar 1845–idag), "
        "isländsk lagstiftning (lög, reglugerðir), Stjórnartíðindi, "
        "samt regeringspublikationer (rit og skýrslur) från stjornarradid.is. "
        "Verktygen har prefixet is_. "
        "Söktermen kan innehålla kommaseparerade ord — de tolkas som OR-logik. "
        "Isländska specialtecken (ð, þ, æ, á, é, í, ó, ú, ý) är semantiskt "
        "distinktiva och ska bevaras i alla söktermer."
    ),
)

# Cache för þing-lista (hämtas en gång, återanvänds). Verktygen körs på
# arbetstrådar; låset hindrar att flera samtidiga första anrop hämtar listan
# parallellt.
_thing_lista_cache: Optional[list] = None
_thing_lista_las = threading.Lock()


def _thing_lista() -> list[dict]:
    global _thing_lista_cache
    if _thing_lista_cache is None:
        with _thing_lista_las:
            if _thing_lista_cache is None:
                _thing_lista_cache = al.hamta_thing_lista()
    return _thing_lista_cache


# ── Returtyper ─────────────────────────────────────────────────────────────────
# Stabila skal typas fullt ut; träfflistor och källdata med varierande fält
# (null-fält i historiska poster) typas som dict[str, Any].

class ThingLista(TypedDict):
    thing_antal: int
    thing_lista: list[dict[str, Any]]


class AlthingiSok(TypedDict):
    fraga: str
    expansion: list[str]
    lthing: int
    malstegund: str
    treff_antal: int
    treff: list[dict[str, Any]]


class ReglugerdSok(TypedDict):
    fraga: str
    ar: Optional[int]
    med_andringsforordningar: bool
    med_upphavda: bool
    page: int
    per_page: int
    total_sidor: int
    total_antal: int
    data: list[dict[str, Any]]


class SkyrslurSok(TypedDict):
    fraga: str
    expansion: list[str]
    ministerium: Optional[str]
    ar: Optional[int]
    dokumenttyp: Optional[str]
    treff_antal: int
    treff: list[dict[str, Any]]


class SemantiskSok(TypedDict):
    fraga: str
    expansion: list[str]
    tabell: str
    kalla: str
    dokument: list[dict[str, Any]]
    rit: list[dict[str, Any]]


def expandera_fraga(fraga: str) -> list[str]:
    """
    Expanderar söktermen med isländsk parlamentarisk och juridisk terminologi.
    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
    Promptfilen: prompts/expansion_prompt.txt.
    """
    if not QUERY_EXPANSION_ENABLED:
        return []
    if not QUERY_EXPANSION_MODEL:
        log.warning("QUERY_EXPANSION_MODEL saknas i .env; frågeexpansionen hoppas över.")
        return []

    prompt_path = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_path.exists():
        log.warning("Promptfil saknas: %s", prompt_path)
        return []

    try:
        from openai import OpenAI
        prompt_mall = prompt_path.read_text(encoding="utf-8")
        prompt      = prompt_mall.replace("{query}", fraga)
        klient      = OpenAI(
            base_url=QUERY_EXPANSION_BASE_URL or None,
            api_key=QUERY_EXPANSION_API_KEY or "placeholder",
        )
        svar  = klient.chat.completions.create(
            model=QUERY_EXPANSION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1,
        )
        text  = svar.choices[0].message.content or ""
        termer = [t.strip() for t in text.split(",") if t.strip()]
        log.debug("Query-expansion: '%s' → %s", fraga, termer)
        return termer
    except Exception as exc:
        log.warning("Query-expansion misslyckades: %s", exc)
        return []


# ── Felhantering — översätt HamtaFel till ToolError ───────────────────────────

def _toolerror_fran_hamtafel(exc: HamtaFel, ej_hittad_text: str) -> ToolError:
    """
    Översätter en HamtaFel till ett ToolError med begripligt meddelande.
    Differentierar 403 (bot-shield), 404 (resursen finns inte) och övriga fel.
    """
    if exc.reason == "blockerad":
        return ToolError(
            "Källan blockerade anropet (HTTP 403, bot-shield på althingi.is). "
            "Försök igen om en stund; kvarstår felet är althingi.is tillfälligt "
            "otillgänglig."
        )
    if exc.reason == "404":
        return ToolError(ej_hittad_text)
    if exc.reason.startswith("natverk:"):
        return ToolError(f"Nätverksfel mot källan ({exc.reason}). Försök igen.")
    return ToolError(f"Källan svarade med fel ({exc.reason}).")


# ── Verktyg ────────────────────────────────────────────────────────────────────


@mcp.tool(title="Lista Alþingis riksmöten", annotations=LASNING_EXTERN)
def is_lista_thing() -> ThingLista:
    """
    Listar alla isländska riksmöten (þing) från Alþingi XML-API.

    Returnerar en lista med þing sorterade fallande (nyaste först).
    Varje post innehåller: thing_nr, heiti (namn), thingtok_hefst (start),
    thingtok_lykur (slut, kan vara tomt för pågående þing).

    Använd thing_nr som indata till is_sok_althingi och is_hamta_dokument.
    """
    try:
        lista = _thing_lista()
    except HamtaFel as exc:
        log.error("is_lista_thing HamtaFel: reason=%s status=%s", exc.reason, exc.status)
        raise _toolerror_fran_hamtafel(exc, "Riksmöteslistan hittades inte hos källan.")
    return {"thing_antal": len(lista), "thing_lista": lista}


@mcp.tool(title="Sök þingmál i Alþingi", annotations=LASNING_EXTERN)
def is_sok_althingi(
    fraga: str,
    lthing: Optional[int] = None,
    malstegund: str = "",
    max_treff: int = 20,
) -> AlthingiSok:
    """
    Söker i Alþingis þingmál (ärenden) för ett givet riksmöte.

    OBS: Alþingi XML-API har ingen fri-textsökning. Sökningen sker via
    klient-sidesfiltrering på þingmál-titlar.
    Hämta fulltext via is_hamta_dokument efter sökning.

    Parametrar:
      fraga      — Sökterm på isländska. Kommaseparerade ord = OR-logik.
                   Exempel: "skattar, tekjuskattur" hittar om skattar ELLER tekjuskattur.
                   Isländska specialtecken (þ, ð, æ, á, é, í, ó, ú, ý) ska bevaras.
      lthing     — Riksmötesnummer. Standard: senaste aktiva þing.
                   Hämta alla þing via is_lista_thing.
      malstegund — Filtrera på ärendetyp (partiell matchning, skiftlägesoberoende).
                   Vanliga typer:
                     'frumvarp'               — lagförslag (prop + ledamotsmotioner)
                     'stjórnarfrumvarp'       — regeringsproposition
                     'þingmannafrumvarp'      — ledamotsmotionerat lagförslag
                     'þingsályktunartillaga'  — riksdagsbeslut
                     'fyrirspurn'             — fråga
                     'skýrsla'                — ministerredogörelse
                     'nefndarálit'            — utskottsbetänkande
                   Tom = alla typer.
      max_treff  — Max antal träffar (standard 20).

    Returnerar lista med: malnr, efnisgreinar (titel), malstegund, stadamal (status).
    Använd malnr med is_hamta_dokument för att hämta fullständiga handlingar.
    """
    try:
        thing_lista = _thing_lista()
        kanda_nr    = {t["thing_nr"] for t in thing_lista}
        if lthing is None:
            lthing = max(kanda_nr) if kanda_nr else 157
        elif lthing not in kanda_nr:
            raise ToolError(
                f"Okänt þing-nummer: {lthing}. Anropa is_lista_thing för giltiga värden."
            )

        expansion = expandera_fraga(fraga)
        sok_fraga = fraga + ("," + ",".join(expansion) if expansion else "")

        treff = al.sok_thingmal(
            fraga=sok_fraga,
            lthing=lthing,
            malstegund=malstegund,
            max_treff=max_treff,
        )
    except HamtaFel as exc:
        log.error("is_sok_althingi HamtaFel (fraga=%r lthing=%s): reason=%s status=%s",
                  fraga, lthing, exc.reason, exc.status)
        raise _toolerror_fran_hamtafel(exc, f"Inga þingmál hittades för lthing={lthing}.")

    return {
        "fraga":       fraga,
        "expansion":   expansion,
        "lthing":      lthing,
        "malstegund":  malstegund,
        "treff_antal": len(treff),
        "treff":       treff,
    }


@mcp.tool(title="Hämta ärendehistorik för ett þingmál", annotations=LASNING_EXTERN)
def is_hamta_arende(
    lthing: int,
    malnr: int,
) -> dict[str, Any]:
    """
    Hämtar fullständig ärendehistorik för ett þingmál — mål-metadata plus
    samtliga þingskjöl som hör till målet (lagförslag, nefndarálit,
    ändringsförslag, slutligt antagen lagtext).

    Avsett som första steg i utredningsarbete: hitta målet via is_sok_althingi,
    läs hela tråden av dokument via is_hamta_arende, hämta sedan fulltext för
    de enskilda skjöl du vill läsa via is_hamta_dokument.

    Parametrar:
      lthing — Riksmötesnummer (t.ex. 155 för 2024–2025).
      malnr  — Þingmálsnummer (returneras som `malnr` från is_sok_althingi).

    Returnerar:
      lthing, malnr        — målets identifierare
      efnisgreinar         — målets titel/ämnesbeskrivning
      malstegund           — ärendetyp (Frumvarp til laga, Þingsályktunartillaga, m.fl.)
      stadamal             — status (samþykkt, fellt, dregið til baka, í nefnd, ...)
      skjol_antal          — antal þingskjöl knutna till målet
      skjol                — lista med {skjalnr, skjalategund, þingskjal_url, pdf_url}
                             i kronologisk ordning. Använd skjalnr som indata till
                             is_hamta_dokument för att läsa specifika skjöl.

    Ger ett fel om målet inte hittas.

    Exempel: is_hamta_arende(lthing=155, malnr=1) → fjárlög 2025 + alla skjöl
    """
    ej_hittad = (f"Þingmál {lthing}/{malnr} hittades inte. Kontrollera lthing och "
                 "malnr; använd is_sok_althingi för att hitta giltiga mål.")
    try:
        result = al.hamta_thingmal(lthing=lthing, malnr=malnr)
    except HamtaFel as exc:
        log.error("is_hamta_arende HamtaFel (lthing=%s malnr=%s): reason=%s status=%s",
                  lthing, malnr, exc.reason, exc.status)
        raise _toolerror_fran_hamtafel(exc, ej_hittad)
    if not result:
        raise ToolError(ej_hittad)
    return result


@mcp.tool(title="Hämta ett þingskjal", annotations=LASNING_EXTERN)
def is_hamta_dokument(
    lthing: int,
    skjalnr: int,
) -> dict[str, Any]:
    """
    Hämtar metadata för ett enskilt þingskjal från Alþingi XML-API.

    Parametrar:
      lthing   — Riksmötesnummer (t.ex. 155 för 2024-2025).
      skjalnr  — Þingskjalsnummer (t.ex. 1 för fjárlög/statsbudgeten).

    Returnerar:
      skjalnr       — Þingskjalsnummer
      lthing        — Riksmöte
      titill        — Dokumentets titel
      skjalategund  — Dokumenttyp (t.ex. 'stjórnarfrumvarp', 'nefndarálit')
      html_url      — URL till HTML-fulltext på althingi.is
      pdf_url       — URL till PDF på althingi.is
      malnr         — Kopplat ärendenummer (þingmálsnúmer)
      þingmadur     — Upphovsman (om ledamotsmotionerat)

    Fulltext (HTML) hämtas via html_url. PDF-version via pdf_url.
    URL-mönster: althingi.is/altext/{lthing}/s/{skjalnr:04d}.html
    """
    ej_hittad = f"Þingskjal {lthing}/{skjalnr} hittades inte."
    try:
        skjal = al.hamta_thingskjal(lthing=lthing, skjalnr=skjalnr)
    except HamtaFel as exc:
        log.error("is_hamta_dokument HamtaFel (lthing=%s skjalnr=%s): reason=%s status=%s",
                  lthing, skjalnr, exc.reason, exc.status)
        raise _toolerror_fran_hamtafel(exc, ej_hittad)
    if not skjal:
        raise ToolError(ej_hittad)
    return skjal


# ── Lagtext och förordningar ──────────────────────────────────────────────────


def _parse_beteckning(s: str) -> tuple[int, int]:
    """
    Parsar en isländsk beteckning på formen 'NR/ÅR' till (nr, ar).

    Lagar och förordningar har en kombinerad beteckning, t.ex. '33/1944' för
    Stjórnarskráin eller '1008/2011' för en specifik reglugerð. Funktionen
    accepterar mellanslag runt och vid skiljetecknet. Kastar ToolError om
    formatet inte matchar.
    """
    import re as _re
    m = _re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if not m:
        raise ToolError(
            f"Ogiltig beteckning '{s}'. Förväntat format: 'NR/ÅR' (t.ex. '33/1944')."
        )
    return int(m.group(1)), int(m.group(2))


@mcp.tool(title="Hämta konsoliderad isländsk lag", annotations=LASNING_EXTERN)
def is_hamta_log(
    beteckning: str,
    version: str = "nuna",
    max_tecken: int = IS_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict[str, Any]:
    """
    Hämtar en konsoliderad isländsk lag från althingi.is/lagasafn/.

    Lagtexten returneras som strukturerad markdown med kapitelnummer (## I.)
    och paragrafnummer (**1. gr.**). Alla 1 708 gällande lagar från 1275-2025
    är tillgängliga i version 'nuna'. Äldre versioner (per riksmöte) tillåter
    provenienshantering — exakt vilken lag som gällde vid en given tidpunkt.

    Parametrar:
      beteckning  — Lagens beteckning på formen 'NR/ÅR' (t.ex. '33/1944' för
                    Stjórnarskrá lýðveldisins Íslands). Matchar fältet
                    `beteckning` från is_sok_i_dokument-träffar.
      version     — 'nuna' (gällande version, standard) eller riksmötessuffix
                    som '157a', '156b', '155', '154a'.
      max_tecken  — Teckentak för lagtexten (standard 60 000). 0 = så mycket
                    som ryms, högst 200 000 tecken per anrop.
      fran_tecken — Börja vid denna position, för att läsa vidare efter ett
                    kapat svar (se fortsatt_fran_tecken).

    Returnerar:
      nr, ar, version, beteckning (t.ex. '33/1944'), titill,
      fulltext_md (lagtext i markdown), url, tecken_antal, samt
      tecken_totalt, tecken_visade, trunkerad, fortsatt_fran_tecken och
      las_vidare (komplett anrop för nästa utdrag, null i sista utdraget).
    Ger ett fel om lagen inte hittas eller beteckningen är ogiltig.

    Exempel: is_hamta_log(beteckning="33/1944") → Stjórnarskrá (grundlagen)
    Exempel: is_hamta_log(beteckning="75/2000") → skipulagslög (plan- och bygglagen)
    """
    nr, ar = _parse_beteckning(beteckning)
    ej_hittad = (
        f"Lag {nr}/{ar} (version={version}) hittades inte i lagasafn. Verifiera "
        "lagnummer och år mot althingi.is/lagasafn/; för semantisk sökning i "
        "lagtexter, använd is_sok_i_dokument."
    )

    try:
        result = ls.hamta_log(nr=nr, ar=ar, version=version)
    except HamtaFel as exc:
        log.error("is_hamta_log HamtaFel (%s): reason=%s status=%s",
                  beteckning, exc.reason, exc.status)
        raise _toolerror_fran_hamtafel(exc, ej_hittad)
    if not result:
        raise ToolError(ej_hittad)

    # Källan levererar alltid hela lagtexten — trunkeringen gäller bara svaret.
    for nyckel in ("fulltext_md", "text", "lagtext"):
        if result.get(nyckel):
            _u = _skar_ut(result[nyckel], max_tecken, fran_tecken)
            result[nyckel]                 = _u["text"]
            result["tecken_totalt"]        = _u["tecken_totalt"]
            result["tecken_visade"]        = _u["tecken_visade"]
            result["trunkerad"]            = _u["trunkerad"]
            result["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]
            result["las_vidare"] = _las_vidare(
                "is_hamta_log", "beteckning", f"{nr}/{ar}", _u["max_tecken"],
                _u["fortsatt_fran_tecken"],
                "" if version == "nuna" else f', version="{version}"',
            )
            break
    return result


@mcp.tool(title="Hämta isländsk förordning", annotations=LASNING_EXTERN)
def is_hamta_reglugerd(
    beteckning: str,
    version: str = "current",
    max_tecken: int = IS_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict[str, Any]:
    """
    Hämtar en isländsk förordning (reglugerð) med text från api.reglugerd.is.

    Täcker förordningar från 1957 till idag. Texten kommer ur källans
    detaljsvar (HTML omvandlad till markdown, med bilagor), inte ur PDF.

    Parametrar:
      beteckning  — Förordningens beteckning på formen 'NR/ÅR' (t.ex. '725/2020').
                    Matchar fältet `name` från is_sok_reglugerd-träffar.
      version     — 'current' (gällande lydelse med inarbetade ändringar,
                    standard) eller 'original' (ursprungstexten).
      max_tecken  — Teckentak för texten (standard 60 000). 0 = så mycket som
                    ryms, högst 200 000 tecken per anrop.
      fran_tecken — Börja vid denna position, för att läsa vidare efter ett
                    kapat svar (se fortsatt_fran_tecken och las_vidare).

    Returnerar:
      nr, ar, beteckning, version, titill, publicerad, ministerium (namn),
      ikrafttradde_vid, signerades_vid, upphavt, upphavt_vid,
      text_md (förordningstexten i markdown), text_kalla, tecken_totalt,
      tecken_visade, trunkerad, fortsatt_fran_tecken, las_vidare,
      pdf_url, pdf_typ, webb_url, fullstandig, url, meta_raw.

    Kortsvar: för en del förordningar har källans detaljsvar bara titel och
    länkar (fullstandig=false, se `notering`). Finns texten i den lokala
    synkade kopian används den (text_kalla 'lokal_kopia', med version
    'current'); annars är text_md null och webb_url leder till reglugerd.is.

    PDF-länken: pdf_typ 'konsoliderad' (källans PDF med inarbetade
    ändringar), 'originalkungorelse' (den ursprungliga kungörelsen i
    Stjórnartíðindi) eller pdf_url null.
    Ger ett fel om förordningen inte finns eller beteckningen är ogiltig.

    Exempel: is_hamta_reglugerd(beteckning="725/2020")
    """
    nr, ar = _parse_beteckning(beteckning)
    try:
        result = rg.hamta_reglugerd(nr=nr, ar=ar, version=version)
    except Exception as exc:
        log.error("is_hamta_reglugerd misslyckades (%s): %s", beteckning, exc)
        raise ToolError(f"api.reglugerd.is svarade inte som väntat ({exc}). Försök igen.")
    if not result:
        raise ToolError(
            f"Förordning {nr}/{ar} (version={version}) hittades inte. "
            "Använd is_sok_reglugerd för att söka efter förordningar."
        )

    text = result.get("text_md")
    result["text_kalla"] = "api" if text else None
    if not text and version == "current":
        try:
            text = db_mod.hamta_reglugerd_text(f"{nr}/{ar}")
        except Exception as exc:
            log.debug("Lokal text för %s/%s gick inte att läsa: %s", nr, ar, exc)
            text = None
        if text:
            result["text_kalla"] = "lokal_kopia"
            result["notering"] = (
                "Källans detaljsvar saknar text för denna förordning. text_md "
                "kommer från den lokala kopian (senaste synk av "
                "/regulations/all/current/full). Kontrollera gällande lydelse "
                "på webb_url."
            )

    if text:
        _u = _skar_ut(text, max_tecken, fran_tecken)
        result["text_md"]              = _u["text"]
        result["tecken_totalt"]        = _u["tecken_totalt"]
        result["tecken_visade"]        = _u["tecken_visade"]
        result["trunkerad"]            = _u["trunkerad"]
        result["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]
        extra = "" if version == "current" else f', version="{version}"'
        result["las_vidare"] = _las_vidare(
            "is_hamta_reglugerd", "beteckning", f"{nr}/{ar}", _u["max_tecken"],
            _u["fortsatt_fran_tecken"], extra,
        )
    else:
        result["text_md"] = None
    return result


@mcp.tool(title="Sök isländska förordningar", annotations=LASNING_EXTERN)
def is_sok_reglugerd(
    fraga: str,
    ar: Optional[int] = None,
    page: int = 1,
    max_treff: int = 20,
    med_andringsforordningar: bool = True,
    med_upphavda: bool = True,
) -> ReglugerdSok:
    """
    Söker i isländska förordningar (reglugerðir) via api.reglugerd.is.

    Källans egen fritextsökning (Elasticsearch) över titel och fulltext, med
    isländsk stamning, så böjda former hittas. Täcker 1957–idag. En
    beteckning som '179/2018' i frågan matchar förordningen direkt.

    Parametrar:
      fraga     — Sökterm på isländska (t.ex. 'umferðarlag', 'vatnsveitu',
                  'fiskveiðar'). Följer källans query_string-syntax: flera ord
                  kombineras, "citattecken" ger fras, OR/AND fungerar. Tecken
                  som : ( ) [ ] { } ^ ~ har specialbetydelse och kan ge 0 träffar.
                  Tom sträng går bara tillsammans med `ar`.
      ar        — Filtrera på publiceringsår (t.ex. 2020). Alla år om None.
      page      — Sidnummer (1-baserat), räknat i sidor om max_treff poster.
      max_treff — Poster per sida, 1–30 (standard 20).
      med_andringsforordningar — ta med ändringsförordningar (standard True).
      med_upphavda — ta med upphävda förordningar (standard True). Sätt båda
                  till False för att bara söka bland gällande grundförordningar,
                  vilket är källans egen standard.

    Returnerar:
      fraga, ar, med_andringsforordningar, med_upphavda, page, per_page,
      total_sidor, total_antal, data: lista med {name, titill, publicerad,
      ministerium}. Använd name (t.ex. '0179/2018') som beteckning till
      is_hamta_reglugerd.

    Exempel: is_sok_reglugerd('fiskveiðar', ar=2020)
    """
    try:
        return rg.sok_reglugerd(
            fraga=fraga, ar=ar, page=page, per_page=max_treff,
            med_andringsforordningar=med_andringsforordningar,
            med_upphavda=med_upphavda,
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    except Exception as exc:
        log.error("is_sok_reglugerd misslyckades (fraga=%r): %s", fraga, exc)
        raise ToolError(f"api.reglugerd.is svarade inte som väntat ({exc}). Försök igen.")


# ── Rit og skýrslur ───────────────────────────────────────────────────────────


def _las_rit_fran_db(url: str) -> tuple[Optional[dict], str]:
    """
    Slår upp en publikation i lokal DB oavsett vilken URL-form anroparen har.

    Returnerar (post eller None, nyckel-URL). Nyckeln är den URL som posten
    har, eller ska få, i dokument_rit: den kanoniska /rit/-formen. En äldre
    /stakt-rit/-URL söks först som den är (databaser där de äldre URL:erna
    inte har uppdaterats) och annars via webbplatsens omdirigering.
    """
    def _las(u: str) -> Optional[dict]:
        try:
            return db_mod.hamta_dokument_rit(u)
        except Exception as exc:
            log.debug("DB-uppslag misslyckades för %s: %s", u, exc)
            return None

    post = _las(url)
    if post:
        return post, url
    if sr.ar_aldre_url(url):
        try:
            nyckel = sr.folj_omdirigering(url)
        except sr.Natverksfel as exc:
            raise ToolError(f"stjornarradid.is svarade inte ({exc}). Försök igen.")
    else:
        # Kontrollerar också värden: en URL utanför stjornarradid.is ger None
        # och hämtas aldrig.
        nyckel = sr.kanonisk_rit_url(url)
    if not nyckel:
        return None, url
    if nyckel != url:
        post = _las(nyckel)
    return post, nyckel


def _trunkera_rit(post: dict, max_tecken: int, fran_tecken: int) -> dict:
    _u = _skar_ut(post["fulltext_md"], max_tecken, fran_tecken)
    post["fulltext_md"]          = _u["text"]
    post["tecken_antal"]         = _u["tecken_visade"]
    post["tecken_totalt"]        = _u["tecken_totalt"]
    post["trunkerad"]            = _u["trunkerad"]
    post["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]
    post["las_vidare"] = _las_vidare(
        "is_hamta_skyrsla", "url", post.get("url") or "", _u["max_tecken"],
        _u["fortsatt_fran_tecken"],
    )
    return post


@mcp.tool(title="Sök i regeringspublikationer (rit og skýrslur)", annotations=LASNING_DB)
def is_sok_skyrslur(
    fraga: str,
    ministerium: str = "",
    ar: Optional[int] = None,
    dokumenttyp: str = "",
    max_treff: int = 20,
) -> SkyrslurSok:
    """
    Söker i isländska regeringspublikationer (rit og skýrslur) från
    stjornarradid.is via lokal fulltextindexering.

    OBS: Sajtens egna sökmotor är trasig (alla termer → 0 träffar). Sökning
    sker alltid mot lokal DB (island.dokument_rit), som fylls av den dagliga
    synken. Listningen på stjornarradid.is går tillbaka till 1991; täckningen
    lokalt beror på hur långt synken hunnit.

    Parametrar:
      fraga       — Sökterm på isländska. Kommaseparerade ord = OR-logik.
                    Exempel: "loftslag, kolefni" hittar om loftslag ELLER kolefni.
      ministerium — Filtrera på ministerium (partiell matchning, t.ex. 'Umhverfis').
                    Tom = alla ministerier.
      ar          — Filtrera på publiceringsår (t.ex. 2023). None = alla år.
      dokumenttyp — Filtrera på heuristisk dokumenttyp (t.ex. 'Skýrsla',
                    'Ársskýrsla', 'Greinargerð', 'Hvítbók'). Tom = alla typer.
                    OBS: Klassificeringen är heuristisk efter titelprefix —
                    inte officiell metadata.
      max_treff   — Max antal träffar (standard 20).

    Returnerar:
      fraga, expansion, ministerium, ar, dokumenttyp, treff_antal,
      treff: lista med {url, titill, slug, dagsetning, ministerium, tema,
                        ar, dokumenttyp_isl, pdf_url, rank}

    Använd url med is_hamta_skyrsla för att hämta fulltext.
    """
    expansion = expandera_fraga(fraga)
    sok_fraga = fraga + ("," + ",".join(expansion) if expansion else "")
    try:
        treff = db_mod.fts_sok_rit(
            fraga       = sok_fraga,
            ministerium = ministerium or None,
            ar          = ar,
            dokumenttyp = dokumenttyp or None,
            max_treff   = max_treff,
        )
    except Exception as exc:
        log.error("is_sok_skyrslur misslyckades (fraga=%r): %s", fraga, exc)
        raise ToolError(
            "Den lokala databasen gick inte att läsa. Kontrollera DATABASE_URL "
            "och att databasen är igång."
        )

    return {
        "fraga":       fraga,
        "expansion":   expansion,
        "ministerium": ministerium or None,
        "ar":          ar,
        "dokumenttyp": dokumenttyp or None,
        "treff_antal": len(treff),
        "treff":       treff,
    }


@mcp.tool(title="Hämta fulltext för en regeringspublikation", annotations=LASNING_EXTERN)
def is_hamta_skyrsla(
    url: str,
    max_tecken: int = IS_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict[str, Any]:
    """
    Hämtar fulltext för en isländsk regeringspublikation (rit og skýrslur).

    Slår först upp i lokal DB-cache (island.dokument_rit). Om fulltext saknas
    — t.ex. för ny publikation eller misslyckad synk — hämtas PDF live från
    stjornarradid.is, extraheras under minnes- och tidsvakt (pdftext_skydd)
    och returneras.

    Parametrar:
      url         — URL till publikationen på stjornarradid.is, helst från
                    treff-listan i is_sok_skyrslur. Exempel:
                    'https://www.stjornarradid.is/gogn/rit-og-skyrslur/rit/2024-12-12-Endurskodun-a-logum-um-rammaaaetlun-Skyrsla-starfshops/'
                    Äldre /stakt-rit/-URL:er och /rit/YYYY/MM/DD/-formen tas
                    också emot.
      max_tecken  — Teckentak för texten (standard 60 000). 0 = så mycket som
                    ryms, högst 200 000 tecken per anrop.
      fran_tecken — Börja vid denna position, för att läsa vidare efter ett
                    kapat svar (se fortsatt_fran_tecken).

    Returnerar:
      url, titill, pdf_url, fulltext_md (markdown), tecken_antal,
      tecken_totalt, trunkerad, fortsatt_fran_tecken, las_vidare (komplett
      anrop för nästa utdrag, null i sista utdraget), kalla ('db_cache' | 'live').
      Ger ett fel om publikationen, PDF:en eller texten inte går att hämta.

    PDF-filen lagras INTE lokalt — den laddas ned, extraheras och raderas direkt.
    """
    cached, nyckel = _las_rit_fran_db(url)
    if cached and cached.get("fulltext_md"):
        cached["kalla"] = "db_cache"
        # Databasen har alltid hela texten — trunkeringen gäller bara svaret.
        return _trunkera_rit(cached, max_tecken, fran_tecken)

    if sr.ar_aldre_url(nyckel):
        raise ToolError(
            f"Publikationen bakom '{url}' finns inte längre på stjornarradid.is."
        )
    if not sr.kanonisk_rit_url(nyckel):
        raise ToolError(
            f"'{url}' är ingen publikations-URL på stjornarradid.is. Hämta url "
            "ur träfflistan i is_sok_skyrslur."
        )

    try:
        result = sr.hamta_rit_fulltext(nyckel)
    except sr.OtillatenUrl as exc:
        raise ToolError(str(exc))
    except Exception as exc:
        log.error("is_hamta_skyrsla misslyckades (%s): %s", url, exc)
        raise ToolError(f"Hämtningen från stjornarradid.is misslyckades ({exc}).")
    if result.get("fel"):
        raise ToolError(f"{result['fel']} ({result.get('url') or nyckel})")

    result["kalla"] = "live"
    # Spara i DB för framtida anrop. Ett fel här stoppar inte svaret.
    try:
        if cached:
            sr._uppdatera_fulltext(db_mod, nyckel, result["pdf_url"], result["fulltext_md"])
        else:
            slug, dagsetning = sr._slug_och_datum(nyckel)
            titill = result.get("titill") or slug.replace("-", " ")
            db_mod.upsert_dokument_rit(
                url             = nyckel,
                titill          = titill,
                slug            = slug,
                dagsetning      = dagsetning,
                ministerium     = None,
                tema            = None,
                ar              = int(dagsetning[:4]) if dagsetning else None,
                dokumenttyp_isl = sr.extrahera_dokumenttyp(titill),
                pdf_url         = result["pdf_url"],
                fulltext_md     = result["fulltext_md"],
            )
    except Exception as exc:
        log.debug("DB-cache för %s misslyckades (icke-kritiskt): %s", nyckel, exc)

    return _trunkera_rit(result, max_tecken, fran_tecken)


# ── Embeddingmodell ────────────────────────────────────────────────────────────

def _hamta_embedding_modell():
    """
    Laddar intfloat/multilingual-e5-base lat (768 dim), en gång per process.

    Dubbelkontrollerad låsning: verktygen körs på arbetstrådar, och utan låset
    kunde två samtidiga första anrop ladda modellen var för sig. Utskrifter
    från tqdm/transformers under inläsningen leds till logs/embedding.log.
    """
    global _embedding_modell
    if _embedding_modell is not None:
        return _embedding_modell

    with _embedding_las:
        if _embedding_modell is not None:
            return _embedding_modell
        log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
        log_sokvag.parent.mkdir(parents=True, exist_ok=True)
        log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        save_fd1 = os.dup(1)
        try:
            os.dup2(log_fd, 1)
            from sentence_transformers import SentenceTransformer
            _embedding_modell = _SerialiseradModell(SentenceTransformer(EMBEDDING_MODEL))
        finally:
            os.dup2(save_fd1, 1)
            os.close(save_fd1)
            os.close(log_fd)

    return _embedding_modell


@mcp.tool(title="Semantisk sökning i isländska dokument", annotations=LASNING_DB)
def is_sok_i_dokument(
    fraga: str,
    tabell: str = "alla",
    max_treff: int = 10,
) -> SemantiskSok:
    """
    Semantisk sökning i indexerade isländska dokument via pgvector.

    Söker i embeddings genererade med intfloat/multilingual-e5-base (768 dim).
    Kräver PostgreSQL + pgvector. Med SQLite-backend görs fulltextsökning i
    stället (kalla='fts_fallback').

    Parametrar:
      fraga    — Sökfråga på valfritt språk (isländska, svenska, engelska)
      tabell   — 'dokument' (þingskjöl/lög/regl.), 'rit' (rit og skýrslur),
                 eller 'alla' (båda, standard)
      max_treff — Max antal träffar per tabell (standard 10)

    Returnerar:
      fraga, expansion (LLM-genererade tilläggstermer), tabell, kalla,
      dokument-träffar och rit-träffar — varje träff innehåller chunk-text,
      likhetspoäng och metadata.
    """
    if tabell not in ("alla", "dokument", "rit"):
        raise ToolError("tabell ska vara 'alla', 'dokument' eller 'rit'.")

    expansion = expandera_fraga(fraga)
    sok_fraga = fraga + (", " + ", ".join(expansion) if expansion else "")

    try:
        if not db_mod._ar_postgres():
            # SQLite-backend stöder inte pgvector — använd FTS istället
            log.info("is_sok_i_dokument: SQLite-backend — använder FTS")
            dok_treff = db_mod.fts_sok(sok_fraga, max_treff=max_treff) if tabell in ("alla", "dokument") else []
            rit_treff = db_mod.fts_sok_rit(sok_fraga, max_treff=max_treff) if tabell in ("alla", "rit") else []
            kalla = "fts_fallback"
        else:
            # stdio-transporten leder om fd 1 bort från protokollströmmen, så
            # eventuella utskrifter från encode() kan inte störa svaret.
            embedding = _hamta_embedding_modell().encode(
                sok_fraga,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).tolist()
            dok_treff = (db_mod.vektor_sok(embedding, tabell="chunks", max_treff=max_treff)
                         if tabell in ("alla", "dokument") else [])
            rit_treff = (db_mod.vektor_sok(embedding, tabell="chunks_rit", max_treff=max_treff)
                         if tabell in ("alla", "rit") else [])
            kalla = "pgvector"
    except Exception as exc:
        log.error("is_sok_i_dokument misslyckades ('%s'): %s", fraga, exc)
        raise ToolError(
            "Sökningen misslyckades. Kontrollera att databasen är igång och "
            f"att embeddingmodellen går att läsa in ({type(exc).__name__}: "
            f"{str(exc).strip()[:200]})."
        )

    return {
        "fraga":     fraga,
        "expansion": expansion,
        "tabell":    tabell,
        "kalla":     kalla,
        "dokument":  dok_treff,
        "rit":       rit_treff,
    }


# ── Serverstart ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    # Servern tar inga argument; transporten styrs av MCP_TRANSPORT m.fl.
    # Tolkningen gör att --help visar det i stället för att starta servern.
    argparse.ArgumentParser(
        description="MCP-server för isländsk riksdags- och rättsdata. Transport och "
                    "databas styrs av miljövariablerna MCP_TRANSPORT, MCP_HOST, "
                    "MCP_PORT, MCP_API_KEY och DATABASE_URL (se config.example.env).",
    ).parse_args()
    starta(mcp, standardport=STANDARDPORT, initiera=initiera_schema)

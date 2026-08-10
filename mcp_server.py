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
    is_hamta_reglugerd   — Hämtar förordningsmetadata från api.reglugerd.is
    is_sok_reglugerd     — Söker i isländska förordningar via api.reglugerd.is

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
  Stjórnarráðið     — stjornarradid.is RSS + LisasticSearch

Transport-lägen (styrs via MCP_TRANSPORT i .env):
  stdio (standard):   python3 mcp_server.py
  http (hostad):      MCP_TRANSPORT=http python3 mcp_server.py
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import althingi as al
import lagasafn as ls
import reglugerd as rg
import stjornarradid_rit as sr
import db as db_mod
from db import initiera_schema
from klient_fel import HamtaFel

load_dotenv(Path(__file__).parent / ".env")

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST      = os.getenv("MCP_HOST",      "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT",  "8006"))
MCP_API_KEY   = os.getenv("MCP_API_KEY",   "")

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Standardtak för fulltext i hämtverktygen. Isländska skýrslur och lagtexter når över en halv miljon tecken
# och kan överskrida MCP-protokollets storleksgräns, vilket får anropet att
# misslyckas helt. Anroparen kan höja taket eller sätta 0 för hela texten.
IS_MAX_TECKEN = int(os.getenv("IS_MAX_TECKEN", "60000"))

def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if max_tecken and max_tecken > 0 and len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag    = utdrag.rstrip()
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
    }

# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "island",
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

# Cache för þing-lista (hämtas en gång, återanvänds)
_thing_lista_cache: Optional[list] = None


def _thing_lista() -> list[dict]:
    global _thing_lista_cache
    if _thing_lista_cache is None:
        _thing_lista_cache = al.hamta_thing_lista()
    return _thing_lista_cache


def expandera_fraga(fraga: str) -> list[str]:
    """
    Expanderar söktermen med isländsk parlamentarisk och juridisk terminologi.
    Aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
    Promptfilen: prompts/expansion_prompt.txt.
    """
    if not QUERY_EXPANSION_ENABLED:
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
            model=QUERY_EXPANSION_MODEL or "claude-haiku-4-5-20251001",
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


# ── Felhantering — översätt HamtaFel till MCP-svarsformat ─────────────────────

def _fel_fran_hamtafel(exc: HamtaFel, bas: dict, ej_hittad_text: str) -> dict:
    """
    Översätter en HamtaFel till ett MCP-svar (dict med 'fel'-nyckel + extra fält).
    Differentierar 403 (bot-shield), 404 (resursen finns inte) och övriga fel.
    """
    out = dict(bas)
    if exc.reason == "blockerad":
        out["fel"]  = ("Källan blockerade anropet (HTTP 403). "
                       "Servern är tillfälligt skyddad av bot-shield.")
        out["tips"] = ("Försök igen om en stund. Om felet kvarstår är althingi.is "
                       "temporärt otillgänglig.")
    elif exc.reason == "404":
        out["fel"] = ej_hittad_text
    elif exc.reason.startswith("natverk:"):
        out["fel"] = f"Nätverksfel mot källan ({exc.reason})."
    else:
        out["fel"] = f"Källan svarade med fel ({exc.reason})."
    return out


# ── Verktyg ────────────────────────────────────────────────────────────────────


@mcp.tool()
def is_lista_thing() -> dict:
    """
    Listar alla isländska riksmöten (þing) från Alþingi XML-API.

    Returnerar en lista med þing sorterade fallande (nyaste först).
    Varje post innehåller: thing_nr, heiti (namn), thingtok_hefst (start),
    thingtok_lykur (slut, kan vara tomt för pågående þing).

    Använd thing_nr som indata till is_sok_althingi och is_hamta_dokument.
    Nuvarande þing: 155 (2024-2025).
    """
    try:
        lista = _thing_lista()
        return {
            "thing_antal": len(lista),
            "thing_lista": lista,
        }
    except HamtaFel as exc:
        log.error("is_lista_thing HamtaFel: reason=%s status=%s", exc.reason, exc.status)
        return _fel_fran_hamtafel(exc, {}, "Riksmöteslistan hittades inte hos källan.")
    except Exception as exc:
        log.error("is_lista_thing misslyckades: %s", exc)
        return {"fel": str(exc)}


@mcp.tool()
def is_sok_althingi(
    fraga: str,
    lthing: Optional[int] = None,
    malstegund: str = "",
    max_treff: int = 20,
) -> dict:
    """
    Söker i Alþingis þingmál (ärenden) för ett givet riksmöte.

    OBS: Alþingi XML-API har ingen fri-textsökning. Sökningen sker via
    klient-sidesfiltrering på þingmál-titlar. DB-baserad sökning implementeras
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
        # Hämta känd þing-lista; välj senaste om lthing inte angetts
        thing_lista = _thing_lista()
        kanda_nr    = {t["thing_nr"] for t in thing_lista}
        if lthing is None:
            lthing = max(kanda_nr, default=157) if kanda_nr else 157
        elif lthing not in kanda_nr:
            return {
                "fel": f"Okänt þing-nummer: {lthing}. Anropa is_lista_thing för giltiga värden.",
                "fraga": fraga,
                "lthing": lthing,
            }

        # Eventuell query-expansion
        expansion   = expandera_fraga(fraga)
        sok_fraga   = fraga + ("," + ",".join(expansion) if expansion else "")

        treff = al.sok_thingmal(
            fraga=sok_fraga,
            lthing=lthing,
            malstegund=malstegund,
            max_treff=max_treff,
        )

        return {
            "fraga":       fraga,
            "expansion":   expansion,
            "lthing":      lthing,
            "malstegund":  malstegund,
            "treff_antal": len(treff),
            "treff":       treff,
        }

    except HamtaFel as exc:
        log.error("is_sok_althingi HamtaFel (fraga=%r lthing=%s): reason=%s status=%s",
                  fraga, lthing, exc.reason, exc.status)
        return _fel_fran_hamtafel(
            exc,
            {"fraga": fraga, "lthing": lthing},
            f"Inga þingmál hittades för lthing={lthing}.",
        )
    except Exception as exc:
        log.error("is_sok_althingi misslyckades: %s", exc)
        return {"fel": str(exc), "fraga": fraga, "lthing": lthing}


@mcp.tool()
def is_hamta_arende(
    lthing: int,
    malnr: int,
) -> dict:
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

    Returnerar {} med 'fel'-nyckel om målet inte hittas.

    Exempel: is_hamta_arende(lthing=155, malnr=1) → fjárlög 2025 + alla skjöl
    """
    try:
        result = al.hamta_thingmal(lthing=lthing, malnr=malnr)
        if not result:
            return {
                "fel":    f"Þingmál {lthing}/{malnr} hittades inte.",
                "lthing": lthing,
                "malnr":  malnr,
                "tips":   "Kontrollera lthing och malnr. Använd is_sok_althingi för att hitta giltiga mål.",
            }
        return result

    except HamtaFel as exc:
        log.error("is_hamta_arende HamtaFel (lthing=%s malnr=%s): reason=%s status=%s",
                  lthing, malnr, exc.reason, exc.status)
        return _fel_fran_hamtafel(
            exc,
            {"lthing": lthing, "malnr": malnr},
            f"Þingmál {lthing}/{malnr} hittades inte.",
        )
    except Exception as exc:
        log.error("is_hamta_arende misslyckades (lthing=%s malnr=%s): %s",
                  lthing, malnr, exc)
        return {"fel": str(exc), "lthing": lthing, "malnr": malnr}


@mcp.tool()
def is_hamta_dokument(
    lthing: int,
    skjalnr: int,
) -> dict:
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
    try:
        skjal = al.hamta_thingskjal(lthing=lthing, skjalnr=skjalnr)
        if not skjal:
            return {
                "fel":    f"Þingskjal {lthing}/{skjalnr} hittades inte.",
                "lthing":  lthing,
                "skjalnr": skjalnr,
            }
        return skjal

    except HamtaFel as exc:
        log.error("is_hamta_dokument HamtaFel (lthing=%s skjalnr=%s): reason=%s status=%s",
                  lthing, skjalnr, exc.reason, exc.status)
        return _fel_fran_hamtafel(
            exc,
            {"lthing": lthing, "skjalnr": skjalnr},
            f"Þingskjal {lthing}/{skjalnr} hittades inte.",
        )
    except Exception as exc:
        log.error("is_hamta_dokument misslyckades (lthing=%s skjalnr=%s): %s",
                  lthing, skjalnr, exc)
        return {"fel": str(exc), "lthing": lthing, "skjalnr": skjalnr}


# ── Lagtext och förordningar ──────────────────────────────────────────────────


def _parse_beteckning(s: str) -> tuple[int, int]:
    """
    Parsar en isländsk beteckning på formen 'NR/ÅR' till (nr, ar).

    Lagar och förordningar har en kombinerad beteckning, t.ex. '33/1944' för
    Stjórnarskráin eller '1008/2011' för en specifik reglugerð. Funktionen
    accepterar mellanslag runt och vid skiljetecknet. Höjer ValueError om
    formatet inte matchar.
    """
    import re as _re
    m = _re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if not m:
        raise ValueError(
            f"Ogiltig beteckning '{s}'. Förväntat format: 'NR/ÅR' (t.ex. '33/1944')."
        )
    return int(m.group(1)), int(m.group(2))


@mcp.tool()
def is_hamta_log(
    beteckning: str,
    version: str = "nuna",
    max_tecken: int = IS_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict:
    """
    Hämtar en konsoliderad isländsk lag från althingi.is/lagasafn/.

    Lagtexten returneras som strukturerad markdown med kapitelnummer (## I.)
    och paragrafnummer (**1. gr.**). Alla 1 708 gällande lagar från 1275-2025
    är tillgängliga i version 'nuna'. Äldre versioner (per riksmöte) tillåter
    provenienshantering — exakt vilken lag som gällde vid en given tidpunkt.

    Parametrar:
      beteckning — Lagens beteckning på formen 'NR/ÅR' (t.ex. '33/1944' för
                   Stjórnarskrá lýðveldisins Íslands). Matchar fältet
                   `beteckning` från is_sok_i_dokument-träffar.
      version    — 'nuna' (gällande version, standard) eller riksmötessuffix
                   som '157a', '156b', '155', '154a'. Tillgängliga versioner
                   hämtas via hamta_log_lista_versioner() i lagasafn.py.

    Returnerar:
      nr, ar, version, beteckning (t.ex. '33/1944'), titill,
      fulltext_md (lagtext i markdown), url, tecken_antal.
    Returnerar {} med 'fel'-nyckel om lagen ej hittas eller beteckningen är ogiltig.

    Exempel: is_hamta_log(beteckning="33/1944") → Stjórnarskrá (grundlagen)
    Exempel: is_hamta_log(beteckning="75/2000") → skipulagslög (plan- och bygglagen)
    """
    try:
        nr, ar = _parse_beteckning(beteckning)
    except ValueError as exc:
        return {"fel": str(exc), "beteckning": beteckning}

    try:
        result = ls.hamta_log(nr=nr, ar=ar, version=version)
        if not result:
            return {
                "fel":        f"Lag {nr}/{ar} (version={version}) hittades inte i lagasafn.",
                "beteckning": f"{nr}/{ar}",
                "version":    version,
                "tips":       "Verifiera lagnummer och år mot listan på althingi.is/lagasafn/. "
                              "För semantisk sökning i lagtexter, använd is_sok_i_dokument.",
            }

        # Källan levererar alltid hela lagtexten — trunkeringen gäller bara svaret.
        for nyckel in ("fulltext_md", "text", "lagtext"):
            if result.get(nyckel):
                _u = _skar_ut(result[nyckel], max_tecken, fran_tecken)
                result[nyckel]                = _u["text"]
                result["tecken_totalt"]        = _u["tecken_totalt"]
                result["tecken_visade"]        = _u["tecken_visade"]
                result["trunkerad"]            = _u["trunkerad"]
                result["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]
                break
        return result
    except HamtaFel as exc:
        if exc.reason == "404":
            return {
                "fel":        f"Lag {nr}/{ar} (version={version}) hittades inte i lagasafn.",
                "beteckning": f"{nr}/{ar}",
                "version":    version,
                "tips":       "Verifiera lagnummer och år mot listan på althingi.is/lagasafn/. "
                              "För semantisk sökning i lagtexter, använd is_sok_i_dokument.",
            }
        if exc.reason == "blockerad":
            return {
                "fel":        f"Källan blockerade anropet (HTTP 403). "
                              "Servern är tillfälligt skyddad av bot-shield.",
                "beteckning": f"{nr}/{ar}",
                "version":    version,
                "tips":       "Försök igen om en stund. Om felet kvarstår är althingi.is "
                              "temporärt otillgänglig.",
            }
        log.error("is_hamta_log HamtaFel (%s): reason=%s status=%s", beteckning, exc.reason, exc.status)
        return {
            "fel":        f"Källan svarade med fel ({exc.reason}).",
            "beteckning": f"{nr}/{ar}",
        }
    except Exception as exc:
        log.error("is_hamta_log misslyckades (%s): %s", beteckning, exc)
        return {"fel": str(exc), "beteckning": beteckning}


@mcp.tool()
def is_hamta_reglugerd(
    beteckning: str,
    version: str = "current",
) -> dict:
    """
    Hämtar metadata och PDF-länk för en isländsk förordning (reglugerð)
    från api.reglugerd.is.

    Täcker förordningar från 1957 till idag. PDF-fulltexten hämtas via pdf_url
    i svaret.

    Parametrar:
      beteckning — Förordningens beteckning på formen 'NR/ÅR' (t.ex. '179/2018'
                   för Reglugerð um flugvelli). Matchar fältet `name` från
                   is_sok_reglugerd-träffar.
      version    — 'current' (gällande version med inarbetade ändringar, standard)
                   eller 'original' (ursprungstext som publicerades).

    Returnerar:
      nr, ar, beteckning (t.ex. '179/2018'), version, titill,
      publicerad, ministerium, pdf_url (direktlänk till fulltext-PDF), url.
    Returnerar {} med 'fel'-nyckel om förordningen ej hittas eller beteckningen
    är ogiltig.

    Exempel: is_hamta_reglugerd(beteckning="179/2018") → Reglugerð um flugvelli
    """
    try:
        nr, ar = _parse_beteckning(beteckning)
    except ValueError as exc:
        return {"fel": str(exc), "beteckning": beteckning}

    try:
        result = rg.hamta_reglugerd(nr=nr, ar=ar, version=version)
        if not result:
            return {
                "fel":        f"Förordning {nr}/{ar} (version={version}) hittades inte.",
                "beteckning": f"{nr}/{ar}",
                "version":    version,
                "tips":       "Använd is_sok_reglugerd för att söka efter förordningar.",
            }
        return result
    except Exception as exc:
        log.error("is_hamta_reglugerd misslyckades (%s): %s", beteckning, exc)
        return {"fel": str(exc), "beteckning": beteckning}


@mcp.tool()
def is_sok_reglugerd(
    fraga: str,
    ar: Optional[int] = None,
    page: int = 1,
    max_treff: int = 20,
) -> dict:
    """
    Söker i isländska förordningar (reglugerðir) via api.reglugerd.is.

    Söker i förordningstitlar och innehåll. API:et har inbyggd fritext-sökning
    (till skillnad från Alþingi XML-API som saknar det). Täcker 1957–idag.

    Parametrar:
      fraga     — Sökterm på isländska (t.ex. 'umferðarlag', 'skattur', 'fiskveiðar').
                  Tom sträng returnerar alla förordningar (paginerat).
      ar        — Filtrera på publiceringsår (t.ex. 2020). Alla år om None.
      page      — Sidnummer för paginering (standard 1).
      max_treff — Max antal träffar per sida (standard 20).

    Returnerar:
      fraga, ar, page, per_page, total_sidor, total_antal,
      data: lista med {name, titill, publicerad, ministerium}.
    Använd name-fältet (t.ex. '179/2018') för att bryta ut nr och ar
    inför anrop till is_hamta_reglugerd.

    Exempel: is_sok_reglugerd('fiskveiðar', ar=2020)
    """
    try:
        return rg.sok_reglugerd(fraga=fraga, ar=ar, page=page, per_page=max_treff)
    except Exception as exc:
        log.error("is_sok_reglugerd misslyckades (fraga=%r): %s", fraga, exc)
        return {"fel": str(exc), "fraga": fraga}


# ── Rit og skýrslur ───────────────────────────────────────────────────────────


def _hamta_rit_fra_db(url: str) -> Optional[dict]:
    """
    Returnerar cached dokument_rit-post från lokal DB, eller None.
    Används av is_hamta_skyrsla för att undvika redundant PDF-hämtning.
    """
    try:
        cols = ("url", "titill", "slug", "dagsetning", "ministerium",
                "tema", "ar", "dokumenttyp_isl", "pdf_url", "fulltext_md")
        sql = (
            f"SELECT {', '.join(cols)} FROM {db_mod._prefix()}dokument_rit "
            f"WHERE url = {db_mod._ph()}"
        )
        with db_mod._cursor() as cur:
            cur.execute(sql, (url,))
            row = cur.fetchone()

        return dict(zip(cols, row)) if row else None
    except Exception as exc:
        log.debug("_hamta_rit_fra_db misslyckades för %s: %s", url, exc)
        return None


@mcp.tool()
def is_sok_skyrslur(
    fraga: str,
    ministerium: str = "",
    ar: Optional[int] = None,
    dokumenttyp: str = "",
    max_treff: int = 20,
) -> dict:
    """
    Söker i isländska regeringspublikationer (rit og skýrslur) från
    stjornarradid.is via lokal fulltextindexering.

    OBS: Sajtens egna sökmotor är trasig (alla termer → 0 träffar). Sökning
    sker alltid mot lokal DB (island.dokument_rit). Kör synka_rit() i
    stjornarradid_rit.py för att fylla DB med data. Täckning: 380 publikationer
    2021-2026. Äldre publikationer (2007-2020) är utanför scope för v1.

    Parametrar:
      fraga       — Sökterm på isländska. Kommaseparerade ord = OR-logik.
                    Exempel: "loftslag, kolefni" hittar om loftslag ELLER kolefni.
      ministerium — Filtrera på ministerium (partiell matchning, t.ex. 'Umhverfis').
                    Tom = alla ministerier.
      ar          — Filtrera på publiceringsår (t.ex. 2023). None = alla år.
      dokumenttyp — Filtrera på heuristisk dokumenttyp (t.ex. 'Skýrsla',
                    'Ársskýrsla', 'Greinargerð', 'Hvítbók'). Tom = alla typer.
                    OBS: Klassificeringen är heuristisk (~38 % täckning) — inte
                    officiell metadata.
      max_treff   — Max antal träffar (standard 20).

    Returnerar:
      fraga, expansion, ministerium, ar, dokumenttyp, treff_antal,
      treff: lista med {url, titill, slug, dagsetning, ministerium, tema,
                        ar, dokumenttyp_isl, pdf_url, rank}

    Använd url med is_hamta_skyrsla för att hämta fulltext.
    """
    try:
        expansion  = expandera_fraga(fraga)
        sok_fraga  = fraga + ("," + ",".join(expansion) if expansion else "")

        treff = db_mod.fts_sok_rit(
            fraga       = sok_fraga,
            ministerium = ministerium or None,
            ar          = ar,
            dokumenttyp = dokumenttyp or None,
            max_treff   = max_treff,
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

    except Exception as exc:
        log.error("is_sok_skyrslur misslyckades (fraga=%r): %s", fraga, exc)
        return {"fel": str(exc), "fraga": fraga}


@mcp.tool()
def is_hamta_skyrsla(
    url: str,
    max_tecken: int = IS_MAX_TECKEN,
    fran_tecken: int = 0,
) -> dict:
    """
    Hämtar fulltext för en isländsk regeringspublikation (rit og skýrslur).

    Slår först upp i lokal DB-cache (island.dokument_rit). Om fulltext saknas
    — t.ex. för ny publikation eller misslyckad synk — hämtas PDF live från
    stjornarradid.is, extraheras med pymupdf4llm och returneras.

    Parametrar:
      url — URL till publikationen på stjornarradid.is.
                Exempel: 'https://www.stjornarradid.is/stakt-rit/2024/03/15/skyrsla-um-x/'
                Hämta url från treff-listan i is_sok_skyrslur.

    Returnerar:
      url, pdf_url, fulltext_md (markdown), tecken_antal, kalla ('db_cache' | 'live').
      Returnerar {} med 'fel'-nyckel om hämtning misslyckas.

    PDF-filen lagras INTE lokalt — den laddas ned, extraheras och raderas direkt.
    Dokumenttyper: Skýrsla, Ársskýrsla, Greinargerð, Hvítbók, Aðgerðaáætlun m.fl.
    (se is_sok_skyrslur för komplett lista).
    """
    try:
        # Strategi 1: returnera från DB-cache om fulltext redan finns
        cached = _hamta_rit_fra_db(url)
        if cached and cached.get("fulltext_md"):
            cached["kalla"] = "db_cache"
            # Databasen har alltid hela texten — trunkeringen gäller bara svaret.
            _u = _skar_ut(cached["fulltext_md"], max_tecken, fran_tecken)
            cached["fulltext_md"]          = _u["text"]
            cached["tecken_antal"]         = _u["tecken_visade"]
            cached["tecken_totalt"]        = _u["tecken_totalt"]
            cached["trunkerad"]            = _u["trunkerad"]
            cached["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]
            return cached

        # Strategi 2: hämta live (PDF → markdown)
        result = sr.hamta_rit_fulltext(url)
        if result.get("fulltext_md"):
            result["kalla"] = "live"
            # Spara i DB för framtida anrop (bäst-möjlig — ingen blockering vid fel)
            try:
                if cached:
                    # Post finns men saknade fulltext — uppdatera
                    sr._uppdatera_fulltext(
                        db_mod, url,
                        result["pdf_url"],
                        result["fulltext_md"],
                    )
                else:
                    # Post saknas helt — upsert minimal metadata
                    m = sr._STAKT_RIT_RE.search(url)
                    if m:
                        ar_str, mm_str, dd_str, slug = m.groups()
                        db_mod.upsert_dokument_rit(
                            url             = url,
                            titill          = slug.replace("-", " ").capitalize(),
                            slug            = slug,
                            dagsetning      = f"{ar_str}-{mm_str}-{dd_str}",
                            ministerium     = None,
                            tema            = None,
                            ar              = int(ar_str),
                            dokumenttyp_isl = None,
                            pdf_url         = result["pdf_url"],
                            fulltext_md     = result["fulltext_md"],
                        )
            except Exception as exc:
                log.debug("DB-cache för %s misslyckades (icke-kritiskt): %s",
                          url, exc)

        if result.get("fulltext_md"):
            _u = _skar_ut(result["fulltext_md"], max_tecken, fran_tecken)
            result["fulltext_md"]          = _u["text"]
            result["tecken_antal"]         = _u["tecken_visade"]
            result["tecken_totalt"]        = _u["tecken_totalt"]
            result["trunkerad"]            = _u["trunkerad"]
            result["fortsatt_fran_tecken"] = _u["fortsatt_fran_tecken"]

        return result

    except Exception as exc:
        log.error("is_hamta_skyrsla misslyckades (%s): %s", url, exc)
        return {"fel": str(exc), "url": url}


# ── Embeddingmodell ────────────────────────────────────────────────────────────

def _hamta_embedding_modell():
    """
    Laddar intfloat/multilingual-e5-base lazily (768 dim).
    Skyddar FD 1 mot tqdm/transformers-utskrifter som annars kraschar MCP stdio.
    """
    global _embedding_modell
    if _embedding_modell is not None:
        return _embedding_modell

    log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
    log_sokvag.parent.mkdir(parents=True, exist_ok=True)
    log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    save_fd1 = os.dup(1)
    try:
        os.dup2(log_fd, 1)
        from sentence_transformers import SentenceTransformer
        _embedding_modell = SentenceTransformer(EMBEDDING_MODEL)
    finally:
        os.dup2(save_fd1, 1)
        os.close(save_fd1)
        os.close(log_fd)

    return _embedding_modell


@mcp.tool()
def is_sok_i_dokument(
    fraga: str,
    tabell: str = "alla",
    max_treff: int = 10,
) -> dict:
    """
    Semantisk sökning i indexerade isländska dokument via pgvector.

    Söker i embeddings genererade med intfloat/multilingual-e5-base (768 dim).
    Kräver PostgreSQL + pgvector. Vid SQLite-backend returneras FTS-fallback.

    Parametrar:
      fraga    — Sökfråga på valfritt språk (isländska, svenska, engelska)
      tabell   — 'dokument' (þingskjöl/lög/regl.), 'rit' (rit og skýrslur),
                 eller 'alla' (båda, standard)
      max_treff — Max antal träffar per tabell (standard 10)

    Returnerar:
      fraga, expansion (LLM-genererade tilläggstermer), tabell, dokument-träffar
      och rit-träffar — varje träff innehåller chunk-text, likhetspoäng och metadata.
    """
    try:
        expansion = expandera_fraga(fraga)
        sok_fraga = fraga
        if expansion:
            sok_fraga = fraga + ", " + ", ".join(expansion)

        if not db_mod._ar_postgres():
            # SQLite-backend stöder inte pgvector — använd FTS istället
            log.info("is_sok_i_dokument: SQLite-backend — använder FTS")
            dok_treff = db_mod.fts_sok(sok_fraga, max_treff=max_treff) if tabell in ("alla", "dokument") else []
            rit_treff = db_mod.fts_sok_rit(sok_fraga, max_treff=max_treff) if tabell in ("alla", "rit") else []
            return {
                "fraga":       fraga,
                "expansion":   expansion,
                "tabell":      tabell,
                "kalla":       "fts_fallback",
                "dokument":    dok_treff,
                "rit":         rit_treff,
            }

        # PostgreSQL: vektor-sökning
        modell = _hamta_embedding_modell()

        log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
        log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        save_fd1 = os.dup(1)
        try:
            os.dup2(log_fd, 1)
            embedding = modell.encode(
                sok_fraga,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).tolist()
        finally:
            os.dup2(save_fd1, 1)
            os.close(save_fd1)
            os.close(log_fd)

        dok_treff: list[dict] = []
        rit_treff: list[dict] = []

        if tabell in ("alla", "dokument"):
            dok_treff = db_mod.vektor_sok(embedding, tabell="chunks", max_treff=max_treff)

        if tabell in ("alla", "rit"):
            rit_treff = db_mod.vektor_sok(embedding, tabell="chunks_rit", max_treff=max_treff)

        return {
            "fraga":       fraga,
            "expansion":   expansion,
            "tabell":      tabell,
            "kalla":       "pgvector",
            "dokument":    dok_treff,
            "rit":         rit_treff,
        }

    except Exception as exc:
        log.error("is_sok_i_dokument misslyckades ('%s'): %s", fraga, exc)
        return {"fel": str(exc), "fraga": fraga}


# ── Serverstart ────────────────────────────────────────────────────────────────

def main():
    log.info("Island MCP-server startar (transport=%s)", MCP_TRANSPORT)

    # Initiera databas (skapar schema + tabeller om de saknas).
    # initiera_schema() loggar och returnerar tyst om DB är otillgänglig.
    initiera_schema()

    if MCP_TRANSPORT == "http":
        import uvicorn
        from mcp.server.fastmcp import create_starlette_app

        app = create_starlette_app(mcp._mcp_server, debug=False)

        if MCP_API_KEY:
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.responses import Response as StarletteResponse

            class BearerAuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    auth = request.headers.get("Authorization", "")
                    if not auth.startswith("Bearer ") or auth[7:] != MCP_API_KEY:
                        return StarletteResponse("Unauthorized", status_code=401)
                    return await call_next(request)

            app.add_middleware(BearerAuthMiddleware)
            log.info("Bearer-token-autentisering aktiverad")

        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="warning")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

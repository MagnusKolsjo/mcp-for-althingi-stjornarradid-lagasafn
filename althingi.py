# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
althingi.py — Klient mot Alþingi XML-API

Alþingis öppna data-API returnerar XML för alla endpoints.
Bas-URL: https://www.althingi.is/altext/xml/

Bekräftade endpoints:
  loggjafarthing/                              — lista alla þing (1–155+)
  loggjafarthing/yfirstandandi/               — pågående þing
  thingskjol/?lthing={nr}                     — lista þingskjöl för ett þing
  thingskjol/thingskjal/?lthing={nr}&skjalnr={nr} — metadata + HTML/PDF-URL
  thingmalalisti/?lthing={nr}                 — lista þingmál (ärenden)
  thingmalalisti/thingmal/?lthing={nr}&malnr={nr} — fullständig ärendehistorik
  atkvaedagreidslur/?lthing={nr}              — voteringsdata
  raedulisti/?lthing={nr}                     — anförandelista
  thingmenn/?lthing={nr}                      — ledamöter
  thingmenn/thingmadur/?nr={id}               — enskild ledamot

Dataformat: text/xml; charset=utf-8
Uppdateringsfrekvens: dagligen
Crawl-delay: 5 sekunder (robots.txt) — respekteras via token-bucket

Historisk täckning:
  þingmannslistor:              1. þing (1845)
  Skjala-, anförande-, ärendelistor: 20. þing
  Kommittédata:                 74. þing
  Inlagor (erindi):             111. þing
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from curl_cffi import requests as cf_requests
from dotenv import load_dotenv
from lxml import etree

import takt
from klient_fel import HamtaFel

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE   = "https://www.althingi.is/altext/xml"
CRAWL_DELAY = 5.0   # sekunder (robots.txt Crawl-delay)

# Tillfällig cf_clearance-cookie från Safari som passerar Cloudflares JS-challenge.
# Tom = inget bot-shield aktivt eller vitlistning har trätt i kraft.
# Detaljer om hur cookien hämtas: se config.example.env.
ALTHINGI_CF_CLEARANCE = os.getenv("ALTHINGI_CF_CLEARANCE", "")

# User-Agent — måste matcha den webbläsare som löste cf_clearance-challenge,
# annars avvisar Cloudflare cookien. Tom faller tillbaka till projektets egen UA.
ALTHINGI_USER_AGENT = os.getenv(
    "ALTHINGI_USER_AGENT",
    "mcp-for-althingi-stjornarradid-lagasafn/2.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
)


def _impersonate_profil() -> str:
    """
    Väljer curl_cffi-impersonateprofil som matchar User-Agent-strängen.
    Cloudflare kollar TLS-fingerprint tillsammans med UA vid cookie-validering.
    """
    if "Chrome" in ALTHINGI_USER_AGENT:
        return "chrome124"
    if "Firefox" in ALTHINGI_USER_AGENT:
        return "chrome124"  # curl_cffi har historiskt saknat Firefox-profil
    if "Safari" in ALTHINGI_USER_AGENT:
        return "safari17_0"
    return "chrome124"


# ---------------------------------------------------------------------------
# HTTP-primitiver
# ---------------------------------------------------------------------------

def _throttle():
    """
    Respekterar Crawl-delay: 5 mot althingi.is.

    Strypningen delas med de andra modulerna som anropar althingi.is (se
    takt.py), eftersom Crawl-delay gäller värden och inte modulen.
    """
    takt.vanta("althingi.is", CRAWL_DELAY)


def _get_xml(endpoint: str, params: Optional[dict] = None) -> etree._Element:
    """
    GET mot API_BASE/{endpoint} — returnerar lxml-rot.
    Använder curl_cffi med TLS-fingerprint som matchar User-Agent.

    Kastar HamtaFel med reason:
      '404'       — endpointen finns inte
      'blockerad' — källan svarade 403 (bot-shield eller åtkomstbegränsning)
      'http_NNN'  — annat HTTP-fel
      'natverk: …'— nätverks- eller timeout-fel
    """
    _throttle()
    url = f"{API_BASE}/{endpoint}"
    cookies = {"cf_clearance": ALTHINGI_CF_CLEARANCE} if ALTHINGI_CF_CLEARANCE else {}
    try:
        r = cf_requests.get(
            url,
            params=params or {},
            timeout=60,
            impersonate=_impersonate_profil(),
            headers={
                "Accept":     "application/xml, text/xml",
                "User-Agent": ALTHINGI_USER_AGENT,
            },
            cookies=cookies,
        )
        r.raise_for_status()
        return etree.fromstring(r.content)
    except cf_requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            raise HamtaFel("404", status) from exc
        if status == 403:
            raise HamtaFel("blockerad", status) from exc
        raise HamtaFel(f"http_{status}", status) from exc
    except HamtaFel:
        raise
    except Exception as exc:
        raise HamtaFel(f"natverk: {exc}") from exc


def _text(element: Optional[etree._Element], xpath: str, default: str = "") -> str:
    """Hämtar text från ett xpath-uttryck, returnerar default om ej hittad."""
    if element is None:
        return default
    node = element.find(xpath)
    return (node.text or "").strip() if node is not None else default


# ---------------------------------------------------------------------------
# Riksmöten (þing)
# ---------------------------------------------------------------------------

def hamta_thing_lista() -> list[dict]:
    """
    Hämtar alla riksmöten (þing) från loggjafarthing/.
    Returnerar lista med: thing_nr (int), heiti (namn), thingtok_hefst (start),
    thingtok_lykur (slut). Sorterat fallande på thing_nr.

    XML-struktur: <löggjafarþing><þing númer='1'><þingsetning>...<þinglok>...
    Riksmötesnumret är ett attribut, inte ett child-element.
    """
    root = _get_xml("loggjafarthing/")
    result = []
    for item in root.findall(".//þing"):
        nr_text = item.get("númer", "")
        if not nr_text:
            continue
        result.append({
            "thing_nr":       int(nr_text),
            "heiti":          _text(item, "þingskapalegt_heiti") or _text(item, "heiti") or nr_text,
            "thingtok_hefst": _text(item, "þingsetning"),
            "thingtok_lykur": _text(item, "þinglok"),
        })
    return sorted(result, key=lambda x: x["thing_nr"], reverse=True)


def hamta_yfirstandandi_thing() -> dict:
    """
    Hämtar pågående riksmöte (þing) från loggjafarthing/yfirstandandi/.
    Returnerar thing_nr, heiti, start, slut.
    """
    root = _get_xml("loggjafarthing/yfirstandandi/")
    þing = root.find(".//þing")
    if þing is None:
        return {}
    nr_text = þing.get("númer", "")
    return {
        "thing_nr":       int(nr_text) if nr_text else 0,
        "heiti":          _text(þing, "þingskapalegt_heiti") or _text(þing, "heiti") or nr_text,
        "thingtok_hefst": _text(þing, "þingsetning"),
        "thingtok_lykur": _text(þing, "þinglok"),
    }


# ---------------------------------------------------------------------------
# Þingmál (ärenden)
# ---------------------------------------------------------------------------

def hamta_thingmal_lista(lthing: int) -> list[dict]:
    """
    Hämtar lista över þingmál för ett riksmöte.
    Returnerar: malnr, efnisgreinar (titel), málstegund (typ), stadamal.
    OBS: Kan returnera 1000+ poster för ett pågående þing.

    XML-struktur: <málaskrá><mál málsnúmer='1' þingnúmer='155'>
    Ärendenumret är ett attribut. Titeln heter <málsheiti> (inte <efnisgreinar>).
    """
    root = _get_xml("thingmalalisti/", params={"lthing": str(lthing)})
    result = []
    for mal in root.findall(".//mál"):
        malnr_text = mal.get("málsnúmer", "")
        if not malnr_text:
            continue
        result.append({
            "malnr":        int(malnr_text),
            "lthing":       lthing,
            "efnisgreinar": _text(mal, "málsheiti"),
            "malstegund":   _text(mal, "málstegund/heiti"),
            "stadamal":     "",  # ej tillgängligt i listandpunkten
        })
    return result


def hamta_thingmal(lthing: int, malnr: int) -> dict:
    """
    Hämtar fullständig ärendehistorik för ett þingmál.
    Inkluderar alla þingskjöl, status och nefndarálit.
    Returnerar: malnr, efnisgreinar, malstegund, skjol (lista).

    XML-struktur: <þingmál><mál málsnúmer='1'>...<þingskjal skjalsnúmer='1'>
    Skjalsnumret är attribut; skjalategund är text-innehåll (inte child-element).
    """
    root = _get_xml("thingmalalisti/thingmal/",
                    params={"lthing": str(lthing), "malnr": str(malnr)})
    mal  = root.find(".//mál")
    if mal is None:
        return {}

    # XML:en innehåller flera <þingskjal>-element per fysisk skjal — det "riktiga"
    # elementet (med skjalategund, slóð/html, slóð/pdf) plus cross-referenser
    # i andra kontexter (umsagnir, etc) som bara bär attributet skjalsnúmer.
    # Cross-referenserna saknar child-element och måste filtreras bort.
    # Dessutom dedupliceras eventuella återkommande riktiga element på skjalnr,
    # där den entry med fyllt skjalategund vinner.
    skjol_per_nr: dict[int, dict] = {}
    for skjal in root.findall(".//þingskjal"):
        skjalnr_text = skjal.get("skjalsnúmer", "")
        if not skjalnr_text:
            continue
        skjalnr = int(skjalnr_text)
        kategund = _text(skjal, "skjalategund")
        if not kategund:
            # Cross-referens utan innehåll — hoppa över
            continue
        skjol_per_nr[skjalnr] = {
            "skjalnr":       skjalnr,
            "skjalategund":  kategund,
            "þingskjal_url": _text(skjal, "slóð/html"),
            "pdf_url":       _text(skjal, "slóð/pdf"),
        }
    skjol = sorted(skjol_per_nr.values(), key=lambda s: s["skjalnr"])

    return {
        "malnr":        malnr,
        "lthing":       lthing,
        "efnisgreinar": _text(mal, "málsheiti"),
        "malstegund":   _text(mal, "málstegund/heiti"),
        "stadamal":     _text(mal, "staðamáls"),
        "skjol":        skjol,
        "skjol_antal":  len(skjol),
    }


# ---------------------------------------------------------------------------
# Þingskjöl (parlamentsdokument)
# ---------------------------------------------------------------------------

def hamta_thingskjal(lthing: int, skjalnr: int) -> dict:
    """
    Hämtar metadata för ett enskilt þingskjal.
    Returnerar: skjalnr, lthing, titill, skjalategund, html_url, pdf_url,
    malnr (kopplat ärende), þingmadur (upphovsman om ledamotsmotionerat).

    XML-struktur: root=<þingskjal>, child=<þingskjal skjalsnúmer='1'>+<málalisti>
    - Titeln hämtas från <málalisti>/<mál>/<málsheiti>
    - skjalategund är text-innehåll (inte child-element)
    - malnr är attribut på <mál> i <málalisti>
    """
    root = _get_xml("thingskjol/thingskjal/",
                    params={"lthing": str(lthing), "skjalnr": str(skjalnr)})
    skjal = root.find(".//þingskjal")
    if skjal is None:
        return {}

    # HTML och PDF-URL:er
    html_url = _text(skjal, "slóð/html")
    pdf_url  = _text(skjal, "slóð/pdf")

    # Fallback: bygg URL från känt mönster om skjal-XML saknar slóð
    if not html_url:
        html_url = f"https://www.althingi.is/altext/{lthing}/s/{skjalnr:04d}.html"
    if not pdf_url:
        pdf_url  = f"https://www.althingi.is/altext/pdf/{lthing}/s/{skjalnr:04d}.pdf"

    # Titel från málalisti/mál/málsheiti
    titill = ""
    malsheiti_el = root.find(".//málsheiti")
    if malsheiti_el is not None:
        titill = (malsheiti_el.text or "").strip()

    # Kopplat ärendenummer från málalisti/mál-attribut
    malnr = None
    mal_el = root.find(".//mál")
    if mal_el is not None:
        malnr_text = mal_el.get("málsnúmer", "")
        malnr = int(malnr_text) if malnr_text else None

    return {
        "skjalnr":      skjalnr,
        "lthing":       lthing,
        "titill":       titill,
        "skjalategund": _text(skjal, "skjalategund"),
        "html_url":     html_url,
        "pdf_url":      pdf_url,
        "malnr":        malnr,
        "þingmadur":    _text(skjal, "flutningsmenn/flutningsmaður/nafn"),
    }


def hamta_thingskjol_lista(lthing: int) -> list[dict]:
    """
    Hämtar lista över alla þingskjöl för ett riksmöte.
    OBS: Kan vara mycket stor (1000+ poster). Används för bulk-synk, inte live-sökning.
    Returnerar: skjalnr, titill, skjalategund, html_url, pdf_url, malnr.
    """
    root = _get_xml("thingskjol/", params={"lthing": str(lthing)})
    result = []
    for skjal in root.findall(".//þingskjal"):
        skjalnr_text = skjal.get("skjalsnúmer", "")
        if not skjalnr_text:
            continue
        skjalnr  = int(skjalnr_text)
        html_url = _text(skjal, "slóð/html")
        pdf_url  = _text(skjal, "slóð/pdf")
        if not html_url:
            html_url = f"https://www.althingi.is/altext/{lthing}/s/{skjalnr:04d}.html"
        if not pdf_url:
            pdf_url  = f"https://www.althingi.is/altext/pdf/{lthing}/s/{skjalnr:04d}.pdf"
        malnr_text = skjal.get("málsnúmer", "")
        result.append({
            "skjalnr":      skjalnr,
            "lthing":       lthing,
            "titill":       _text(skjal, "málsheiti"),  # ej alltid tillgängligt i bulk-lista
            "skjalategund": _text(skjal, "skjalategund"),
            "html_url":     html_url,
            "pdf_url":      pdf_url,
            "malnr":        int(malnr_text) if malnr_text else None,
        })
    return result


# ---------------------------------------------------------------------------
# Sökning (klient-sidesfiltrering)
# ---------------------------------------------------------------------------

def sok_thingmal(
    fraga: str,
    lthing: int,
    malstegund: str = "",
    max_treff: int = 20,
) -> list[dict]:
    """
    Söker i þingmál för ett riksmöte via klient-sidesfiltrering på titlar.

    OBS: Alþingi XML-API har ingen fri-textsökning. Denna funktion hämtar
    hela þingmál-listan och filtrerar lokalt.

    Parametrar:
      fraga      — Sökterm. Kommaseparerade ord = OR-logik.
      lthing     — Riksmötesnummer (t.ex. 155).
      malstegund — Filtrera på ärendetyp (partiell matchning, skiftlägesoberoende).
                   Exempel: "frumvarp", "fyrirspurn", "þingsályktunartillaga".
                   Tom = alla typer.
      max_treff  — Max antal returnerade poster (standard 20).

    Returnerar lista med matchande þingmál (malnr, efnisgreinar, malstegund, stadamal).
    """
    mal_lista = hamta_thingmal_lista(lthing)

    # Söktermer (OR-logik)
    termer = [t.strip().lower() for t in fraga.split(",") if t.strip()]

    def matchar(mal: dict) -> bool:
        titel = mal.get("efnisgreinar", "").lower()
        if termer and not any(t in titel for t in termer):
            return False
        if malstegund and malstegund.lower() not in mal.get("malstegund", "").lower():
            return False
        return True

    return [m for m in mal_lista if matchar(m)][:max_treff]

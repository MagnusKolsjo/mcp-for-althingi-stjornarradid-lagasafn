# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
lagasafn.py — Klient mot Alþingis konsoliderade lagsamling (lagasafn)

Hämtar enskilda lagar on-demand från althingi.is/lagasafn/.

Bas-URL: https://www.althingi.is/lagas/{version}/{ar}{nr:03d}.html
  - version: 'nuna' (aktuell, standard) eller riksmötessuffix t.ex. '155', '154a'
  - ar:      publiceringsår, t.ex. 1944
  - nr:      lagnummer, t.ex. 33 → {33:03d} = '033' → URL: 1944033.html

Exempel:
  althingi.is/lagas/nuna/1944033.html — Stjórnarskrá lýðveldisins Íslands (grundlagen)

HTML-struktur (gammal men konsistent):
  - Lagtext-behållare: div.article.box.login > div.boxbody
  - Kapitel (avsnitt): <b>I.</b>, <b>II.</b> etc.
  - Paragrafnummer:    <b>1. gr.</b>, <b>2. gr.</b> etc.
  - Paragraftext:      img.tail efter <img src="/lagas/hk.jpg"> element
  - Fotnoter:          <i>L. 56/1991, 1. gr.</i> (lagändringsreferenser)
  - Dekorbilder:       <img src="/lagas/sk.jpg"> (ignoreras)

Robots.txt: ClaudeBot: Disallow: / och ai-train=no.
Crawl-delay: 5 sekunder (tillämpas via token-bucket i althingi.py — återanvänds här).
User-Agent: mcp-for-althingi-stjornarradid-lagasafn/1.0 (krävs — 403 utan).
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from curl_cffi import requests as cf_requests
from dotenv import load_dotenv
from lxml import html as lhtml

import takt
from klient_fel import HamtaFel

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

LAGASAFN_BASE = "https://www.althingi.is/lagas"
CRAWL_DELAY   = 5.0  # sekunder (robots.txt Crawl-delay)

# Tillfällig cf_clearance-cookie från Safari som passerar Cloudflares JS-challenge.
# Samma värde som althingi.py använder — hela althingi.is omfattas av shielden.
# Tom = inget bot-shield aktivt eller vitlistning har trätt i kraft.
ALTHINGI_CF_CLEARANCE = os.getenv("ALTHINGI_CF_CLEARANCE", "")

# User-Agent — måste matcha den webbläsare som löste cf_clearance-challenge,
# annars avvisar Cloudflare cookien. Tom faller tillbaka till projektets egen UA.
ALTHINGI_USER_AGENT = os.getenv(
    "ALTHINGI_USER_AGENT",
    "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
)

HEADERS = {
    "User-Agent": ALTHINGI_USER_AGENT,
    "Accept":     "text/html,application/xhtml+xml",
}


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


def _cookies() -> dict:
    """Returnerar cookies-dict med cf_clearance om värdet är satt, annars tom dict."""
    return {"cf_clearance": ALTHINGI_CF_CLEARANCE} if ALTHINGI_CF_CLEARANCE else {}


# ---------------------------------------------------------------------------
# Internverktyg
# ---------------------------------------------------------------------------

def _throttle():
    """
    Respekterar Crawl-delay: 5 mot althingi.is.

    Strypningen delas med de andra modulerna som anropar althingi.is (se
    takt.py), eftersom Crawl-delay gäller värden och inte modulen.
    """
    takt.vanta("althingi.is", CRAWL_DELAY)


def _lag_url(nr: int, ar: int, version: str = "nuna") -> str:
    """
    Bygger URL för en enskild lag.
    Filnamn: {ar}{nr:03d}.html — t.ex. 1944033.html för nr 33/1944.
    Nr > 999 paddas inte (1000 → '1000', ej '000').
    """
    return f"{LAGASAFN_BASE}/{version}/{ar}{nr:03d}.html"


def _hamta_html(url: str) -> lhtml.HtmlElement:
    """
    Hämtar och parsear HTML från en lagasafn-URL.
    Använder curl_cffi med Chrome TLS-fingerprint för att passera Cloudflare bot-shield.

    Kastar HamtaFel med reason:
      '404'       — lagen finns inte på källan
      'blockerad' — källan svarade 403 (bot-shield eller åtkomstbegränsning)
      'http_NNN'  — annat HTTP-fel
      'natverk: …'— nätverks- eller timeout-fel
    """
    _throttle()
    try:
        r = cf_requests.get(
            url,
            headers=HEADERS,
            timeout=30,
            impersonate=_impersonate_profil(),
            cookies=_cookies(),
        )
        r.raise_for_status()
        return lhtml.fromstring(r.content)
    except cf_requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            raise HamtaFel("404", status) from exc
        if status == 403:
            raise HamtaFel("blockerad", status) from exc
        raise HamtaFel(f"http_{status}", status) from exc
    except Exception as exc:
        raise HamtaFel(f"natverk: {exc}") from exc


# ---------------------------------------------------------------------------
# HTML → Markdown-parser
# ---------------------------------------------------------------------------

def _parse_lagtext(tree: lhtml.HtmlElement) -> tuple[str, str]:
    """
    Extraherar titel och fulltext (markdown) ur en lagasafn-HTML-sida.

    HTML-mönster:
      <b>I.</b>          → ## I.          (kapitelrubrik — stora bokstäver eller romerskt tal)
      <b>1. gr.</b>      → **1. gr.**     (paragrafnummer)
      <img hk.jpg>.tail  → paragraftextens faktiska innehåll
      <i>L. 56/1991...   → fotnotshänvisning (inkluderas som kursiverad not)

    Returnerar: (titill, fulltext_md)
    """
    # Hämta lagtext-behållaren
    # Webbversionen har div.article.box.login > div.boxbody;
    # bulk-ZIP:ens HTML är rå lagtext utan site-wrapper — fall back till <body>.
    boxar = tree.xpath('//div[contains(@class,"article box login")]//div[@class="boxbody"]')
    if boxar:
        box = boxar[0]
    else:
        body = tree.find('.//body')
        box  = body if body is not None else tree

    # Titel från h2
    h2_noder = box.xpath('.//h2')
    titill = (h2_noder[0].text_content() or "").strip() if h2_noder else ""

    # Bygg markdown rad för rad
    rader: list[str] = []
    i_header_sektion = True  # Hoppa över inledande header-rader (Lagasafn., datum etc.)

    for el in box.iter():
        tag  = el.tag
        text = (el.text or "").strip()
        tail = (el.tail or "").strip()

        if tag == "p":
            # Inledande header-stycken hoppas över ("Lagasafn. Íslensk lög..." och "1944 nr. 33")
            ptext = el.text_content().strip()
            if i_header_sektion and ("Lagasafn" in ptext or re.match(r'^\d{4}\s+nr\.', ptext)):
                continue

        elif tag == "h2":
            # Lagen titel är redan extraherad — hoppa
            i_header_sektion = False
            continue

        elif tag == "hr":
            i_header_sektion = False
            continue

        elif tag == "small":
            # Ändringshistorik — inkludera som kursivt block
            if not i_header_sektion:
                atext = el.text_content().strip()
                if atext:
                    rader.append(f"\n*{atext}*\n")
            continue

        elif tag == "b" and not i_header_sektion:
            # Hoppa över <b>-element som är barn av <small> (ändringshistorik)
            if el.getparent() is not None and el.getparent().tag == "small":
                continue
            btext = text
            if not btext:
                btext = (el.text_content() or "").strip()
            if btext:
                # Kapitelrubrik: romerska tal (I., II., III.) eller "Ákvæði um stundarsakir."
                if re.match(r'^[IVX]+\.$', btext) or not re.search(r'gr\.', btext):
                    rader.append(f"\n## {btext}\n")
                else:
                    # Paragrafnummer: "1. gr.", "2. gr." etc.
                    rader.append(f"\n**{btext}**")

        elif tag == "img" and not i_header_sektion:
            src = el.get("src", "")
            if "hk.jpg" in src and tail:
                # hk.jpg: paragraftextens start — tail innehåller faktisk lagtext
                rader.append(tail)
            elif "sk.jpg" in src:
                # sk.jpg: dekorativ indragningsindikator — ignorera
                pass
            elif tail:
                rader.append(tail)

        elif tag == "i" and not i_header_sektion:
            # Fotnotshänvisningar t.ex. "1)L. 56/1991, 1. gr."
            itext = (el.text_content() or "").strip()
            if itext:
                rader.append(f"  _{itext}_")

    fulltext_md = "\n".join(rader).strip()

    # Rensa upp överflödiga blankrader (max 2 på rad)
    fulltext_md = re.sub(r'\n{3,}', '\n\n', fulltext_md)

    return titill, fulltext_md


# ---------------------------------------------------------------------------
# Publika funktioner
# ---------------------------------------------------------------------------

def hamta_log(nr: int, ar: int, version: str = "nuna") -> dict:
    """
    Hämtar en enskild konsoliderad lag från althingi.is/lagasafn/.

    Parametrar:
      nr      — Lagnummer (t.ex. 33 för grundlagen nr. 33/1944)
      ar      — Publiceringsår (t.ex. 1944)
      version — 'nuna' (gällande, standard) eller riksmötessuffix som '155', '154a'.
                Tillgängliga versioner: 'nuna' + 157a, 156b, 156a, 155, 154a, 154b, 154c ...

    Returnerar dict med:
      nr, ar, version, beteckning (t.ex. '33/1944'),
      titill, fulltext_md, url, tecken_antal
    Kastar HamtaFel om källan svarar med HTTP-fel eller är otillgänglig.
    Returnerar {} om HTML hämtades men ingen text kunde extraheras (ovanligt).
    """
    url  = _lag_url(nr, ar, version)
    tree = _hamta_html(url)   # Kastar HamtaFel vid HTTP-/nätverksfel

    titill, fulltext_md = _parse_lagtext(tree)
    if not titill and not fulltext_md:
        log.warning("hamta_log: Ingen text extraherad från %s", url)
        return {}

    return {
        "nr":           nr,
        "ar":           ar,
        "version":      version,
        "beteckning":   f"{nr}/{ar}",
        "titill":       titill,
        "fulltext_md":  fulltext_md,
        "url":          url,
        "tecken_antal": len(fulltext_md),
    }


def hamta_log_lista_versioner() -> list[str]:
    """
    Hämtar lista över tillgängliga lagsamlingsversioner från ZIP-listningssidan.
    Returnerar t.ex. ['nuna', '157a', '156b', '156a', '155', '154a', ...].

    Används för att kontrollera vilka versioner som är tillgängliga vid bulk-synk.
    """
    _throttle()
    url = "https://www.althingi.is/lagasafn/zip-skra-af-lagasafni/"
    try:
        r = cf_requests.get(
            url,
            headers=HEADERS,
            timeout=20,
            impersonate=_impersonate_profil(),
            cookies=_cookies(),
        )
        r.raise_for_status()
        tree = lhtml.fromstring(r.content)

        # Extrahera versioner från ZIP-URL:er: .../zip/{version}/allt.zip
        versioner = []
        for a in tree.xpath('//a[@href]'):
            href = a.get("href", "")
            m = re.search(r'/zip/([^/]+)/allt\.zip', href)
            if m:
                versioner.append(m.group(1))

        # Lägg alltid till 'nuna' först om det saknas
        if "nuna" not in versioner:
            versioner.insert(0, "nuna")
        return versioner

    except Exception as exc:
        log.error("hamta_log_lista_versioner misslyckades: %s", exc)
        return ["nuna"]

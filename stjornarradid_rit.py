# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
stjornarradid_rit.py — Klient och synkmodul för stjornarradid.is rit og skýrslur
MCP-server för isländsk riksdags- och rättsdata

Listar regeringens publikationer på stjornarradid.is, extraherar PDF-fulltext
med pymupdf4llm och lagrar allt i island.dokument_rit.

────────────────────────────────────────────────────────────────────────────────
Datakällor
────────────────────────────────────────────────────────────────────────────────

Steg 1 — Lista publikationer:
  GET https://www.stjornarradid.is/gogn/rit-og-skyrslur/?index={N}
  → Serverside-renderad HTML (Blazor med förrendering), 50 poster per sida,
    nyaste först. N börjar på 0; sidans "Síðasta"-länk anger sista index.
  → Varje post är ett <article class="search-result-card"> med länk till
    /gogn/rit-og-skyrslur/rit/{SEGMENT}, datum ("16 september 2026"),
    ministerium (inte alltid angivet) och titel.
  → SEGMENT är oftast "YYYY-MM-DD-Slug", men nyare poster kan sakna datum
    ("adgerdaaaetlun-um-..."). Datumet tas därför ur kortet, inte ur URL:en.

Steg 2 — PDF-URL per publikation:
  GET https://www.stjornarradid.is/gogn/rit-og-skyrslur/rit/{SEGMENT}/
  → Nedladdningslänken är ett <a> mot /library/?itemid={GUID} (rel="download")
    eller /library?itemid={GUID}&type=pdf. Äldre sidor kan länka direkt till
    /library/.../fil.pdf. Andra /library-länkar på sidan (favicon, logotyp)
    är inte <a>-element och räknas inte.
  → Okända sökvägar under /rit/ svarar 200 med en tom mallsida, inte 404.
    En riktig publikationssida känns igen på og:type "article".

Steg 3 — PDF-extraktion:
  GET {pdf_url}  → application/pdf
  → pymupdf4llm.to_markdown(tmp_fil) → fulltext_md
  → tmp_fil raderas direkt efter extraktion (ingen persistent PDF-cache).

URL-former:
  Äldre webbplatsen:  /gogn/rit-og-skyrslur/stakt-rit/YYYY/MM/DD/SLUG/
                      → 301 till /gogn/rit-og-skyrslur/rit/YYYY/MM/DD/SLUG/
  Nuvarande listning: /gogn/rit-og-skyrslur/rit/YYYY-MM-DD-SLUG
  Båda /rit/-formerna visar samma publikation. Den lagrade nyckeln är
  listningens form med avslutande snedstreck, så att synk, omdirigerade
  äldre URL:er och anropares URL:er landar på samma rad.

────────────────────────────────────────────────────────────────────────────────
Takt: ett anrop per 5 s mot webbplatsen (dess tidigare angivna Crawl-delay,
behållen av hänsyn). Vanlig HTTP-klient med projektets User-Agent; webbplatsen
kräver ingen webbläsarimitation.
Sajtens sökmotor är trasig — all sökning sker mot lokal DB (island.dokument_rit).
────────────────────────────────────────────────────────────────────────────────
"""

import hashlib
import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from lxml import html as lhtml

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

BASE_URL    = "https://www.stjornarradid.is"
LISTA_URL   = BASE_URL + "/gogn/rit-og-skyrslur/"
RIT_PREFIX  = BASE_URL + "/gogn/rit-og-skyrslur/rit/"
CRAWL_DELAY = 5.0
HEADERS     = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
    "Accept":     "text/html,application/xhtml+xml,*/*",
}

# Övre gräns för antalet listningssidor, så att en ändrad paginering aldrig
# kan ge en oändlig loop (listningen har i dag 16 sidor).
MAX_LISTSIDOR = 200

# Isländska månadsnamn i listningens datum, t.ex. "01 júlí 2026".
_MANADER = {
    "janúar": 1, "febrúar": 2, "mars": 3, "apríl": 4, "maí": 5, "júní": 6,
    "júlí": 7, "ágúst": 8, "september": 9, "október": 10, "nóvember": 11,
    "desember": 12,
}

# Heuristiska dokumenttypsprefixar — längre kontrolleras FÖRE kortare för att
# förhindra att t.ex. "Skýrsla" matchar "Ársskýrsla".
_TYPORD: list[str] = [
    "Ársskýrsla",      # Årsrapport
    "Aðgerðaáætlun",   # Handlingsplan
    "Stöðuskýrsla",    # Statusrapport
    "Stöðumat",        # Lägesbedömning
    "Stefnumótun",     # Strategi
    "Lokaskýrsla",     # Slutrapport
    "Áfangaskýrsla",   # Delrapport
    "Samantekt",       # Sammanfattning
    "Greinargerð",     # Promemoria (Ds-typ)
    "Tillögur",        # Förslag
    "Grænbók",         # Grönbok
    "Hvítbók",         # Vitbok
    "Drög",            # Utkast
    "Mat",             # Bedömning
    "Skýrsla",         # Rapport (generell)
]

# En klient för hela processen. httpx.Client tål samtidiga GET från flera
# trådar; headers sätts här och ändras aldrig efter start.
_klient = httpx.Client(headers=HEADERS, timeout=httpx.Timeout(30.0, read=120.0))


# ---------------------------------------------------------------------------
# Takt
# ---------------------------------------------------------------------------

_takt_las   = threading.Lock()
_nasta_tid  = 0.0


def _throttle(intervall: float = CRAWL_DELAY) -> None:
    """
    Håller minst `intervall` sekunder mellan anropen mot webbplatsen.

    Varje anrop reserverar nästa lediga tidpunkt under ett lås och sover
    sedan utanför låset. Verktygen körs på arbetstrådar, så utan låset kunde
    två samtidiga anrop läsa samma tidpunkt och gå iväg samtidigt.
    """
    global _nasta_tid
    with _takt_las:
        nu        = time.monotonic()
        start     = max(nu, _nasta_tid)
        _nasta_tid = start + intervall
    vanta = start - nu
    if vanta > 0:
        log.debug("Takt stjornarradid: väntar %.1f s", vanta)
        time.sleep(vanta)


# ---------------------------------------------------------------------------
# Heuristisk dokumenttypsextraktion
# ---------------------------------------------------------------------------

def extrahera_dokumenttyp(titill: str) -> Optional[str]:
    """
    Returnerar heuristisk isländsk dokumenttyp baserat på titelprefixet.
    Returnerar None om okänt.

    Kontrollerar att prefixet följs av mellanslag/siffra (förhindrar delmatch).

    Exempel:
      'Skýrsla um loftslagsmál 2024'  → 'Skýrsla'
      'Ársskýrsla Landspítala 2023'   → 'Ársskýrsla'
      'Aðgerðaáætlun ráðuneytisins'   → 'Aðgerðaáætlun'
      'Stefna ríkisstjórnarinnar'     → None
    """
    if not titill:
        return None
    t = titill.strip()
    for typord in _TYPORD:
        if t.lower().startswith(typord.lower()):
            rest = t[len(typord):]
            if not rest or rest[0] in (" ", "\t", "\n") or rest[0].isdigit():
                return typord
    return None


# ---------------------------------------------------------------------------
# URL-former
# ---------------------------------------------------------------------------

# Äldre webbplatsens form: /stakt-rit/YYYY/MM/DD/SLUG/
_STAKT_RIT_RE = re.compile(r'/stakt-rit/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)/?')

# Omdirigeringsmålets form: /rit/YYYY/MM/DD/SLUG/
_RIT_DATUMSTIG_RE = re.compile(r'/gogn/rit-og-skyrslur/rit/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)/?$')

# Listningens form: /rit/SEGMENT, där SEGMENT kan börja med YYYY-MM-DD-
_RIT_SEGMENT_RE = re.compile(r'/gogn/rit-og-skyrslur/rit/([^/?#]+)/?$')
_DATUMPREFIX_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})-(.+)$')


def ar_aldre_url(url: str) -> bool:
    """True om url har den äldre webbplatsens form (/stakt-rit/…)."""
    return bool(_STAKT_RIT_RE.search(url or ""))


def kanonisk_rit_url(url: str) -> Optional[str]:
    """
    Returnerar den lagrade nyckelformen för en publikations-URL på /rit/,
    eller None om url inte är en /rit/-URL.

      …/rit/2024/12/12/Slug/  → https://www.stjornarradid.is/gogn/rit-og-skyrslur/rit/2024-12-12-Slug/
      …/rit/2024-12-12-Slug   → https://www.stjornarradid.is/gogn/rit-og-skyrslur/rit/2024-12-12-Slug/
      /gogn/rit-og-skyrslur/rit/slug-utan-datum → …/rit/slug-utan-datum/

    Den äldre /stakt-rit/-formen ger None: den måste följas via webbplatsens
    omdirigering (se folj_omdirigering).
    """
    if not url:
        return None
    sokvag = urlparse(urljoin(BASE_URL, url.strip())).path
    m = _RIT_DATUMSTIG_RE.search(sokvag)
    if m:
        ar_s, mm_s, dd_s, slug = m.groups()
        return f"{RIT_PREFIX}{ar_s}-{mm_s}-{dd_s}-{slug}/"
    m = _RIT_SEGMENT_RE.search(sokvag)
    if m:
        return f"{RIT_PREFIX}{m.group(1)}/"
    return None


def _slug_och_datum(kanonisk_url: str) -> tuple[str, Optional[str]]:
    """Delar en kanonisk /rit/-URL i (slug, dagsetning eller None)."""
    segment = kanonisk_url.rstrip("/").rsplit("/", 1)[-1]
    m = _DATUMPREFIX_RE.match(segment)
    if m:
        ar_s, mm_s, dd_s, slug = m.groups()
        return slug, f"{ar_s}-{mm_s}-{dd_s}"
    return segment, None


# ---------------------------------------------------------------------------
# HTTP-hjälpare
# ---------------------------------------------------------------------------

def _hamta(url: str, intervall: float = CRAWL_DELAY) -> Optional[httpx.Response]:
    """
    GET med projektets User-Agent och takt. Följer omdirigeringar.
    Returnerar svaret vid 2xx, annars None (felet loggas).
    """
    _throttle(intervall)
    try:
        r = _klient.get(url, follow_redirects=True)
        r.raise_for_status()
        return r
    except httpx.HTTPStatusError as exc:
        log.warning("HTTP %s vid %s", exc.response.status_code, url)
        return None
    except httpx.HTTPError as exc:
        log.error("Nätverksfel vid %s: %s", url, exc)
        return None


def _ar_publikationssida(tree) -> bool:
    """
    True om sidan är en riktig publikation. Okända sökvägar under /rit/
    svarar 200 med en mallsida utan og:type "article".
    """
    return bool(tree.xpath('//meta[@property="og:type" and @content="article"]'))


def folj_omdirigering(aldre_url: str) -> Optional[str]:
    """
    Följer webbplatsens omdirigering från en äldre /stakt-rit/-URL och
    returnerar den kanoniska /rit/-URL:en, eller None om publikationen inte
    finns kvar.

    Omdirigeringen skriver om sökvägen mönstermässigt, även för sidor som inte
    finns. Därför hämtas målsidan och kontrolleras innan URL:en godtas.
    """
    r = _hamta(aldre_url, intervall=2.0)
    if r is None:
        return None
    try:
        tree = lhtml.fromstring(r.content)
    except Exception as exc:
        log.warning("HTML-parsfel för %s: %s", aldre_url, exc)
        return None
    if not _ar_publikationssida(tree):
        log.info("Ingen publikation bakom %s (mallsida)", aldre_url)
        return None
    return kanonisk_rit_url(str(r.url))


# ---------------------------------------------------------------------------
# Listningsparser
# ---------------------------------------------------------------------------

def _parsa_datum(text: str) -> Optional[str]:
    """'01 júlí 2026' → '2026-07-01'. None om formatet inte känns igen."""
    m = re.match(r'\s*(\d{1,2})\.?\s+(\S+)\s+(\d{4})\s*$', text or "")
    if not m:
        return None
    manad = _MANADER.get(m.group(2).lower())
    if not manad:
        return None
    return f"{int(m.group(3)):04d}-{manad:02d}-{int(m.group(1)):02d}"


def _text(noder) -> str:
    return re.sub(r'\s+', ' ', noder[0].text_content()).strip() if noder else ""


def _parsa_listsida(html_bytes: bytes) -> tuple[list[dict], Optional[int]]:
    """
    Parsar en listningssida.

    Returnerar (poster, sista_index). sista_index läses ur sidans
    "Síðasta"-länk (?index=N) och är None om länken saknas.
    Varje post: url, titill, slug, dagsetning, ar, ministerium, tema,
    dokumenttyp_isl.
    """
    try:
        tree = lhtml.fromstring(html_bytes)
    except Exception as exc:
        log.error("HTML-parsfel i listningen: %s", exc)
        return [], None

    poster: list[dict] = []
    for kort in tree.xpath('//article[contains(@class, "search-result-card")]'):
        lankar = kort.xpath('.//a[contains(@href, "/rit-og-skyrslur/rit/")]')
        if not lankar:
            continue
        url = kanonisk_rit_url(lankar[0].get("href", ""))
        if not url:
            continue

        titill = _text(kort.xpath('.//*[contains(@class, "search-result-card-title")]'))
        if not titill:
            log.debug("Hoppar %s — ingen titel", url)
            continue

        slug, datum_ur_url = _slug_och_datum(url)
        dagsetning = (
            _parsa_datum(_text(kort.xpath('.//*[contains(@class, "search-result-card-date")]')))
            or datum_ur_url
        )
        # Webbplatsen skriver kommat i sammansatta ministerienamn som U+066B
        ministerium = _text(
            kort.xpath('.//*[contains(@class, "search-result-card-category")]')
        ).replace("٫", ",") or None

        poster.append({
            "url":             url,
            "titill":          titill,
            "slug":            slug,
            "dagsetning":      dagsetning,
            "ar":              int(dagsetning[:4]) if dagsetning else None,
            "ministerium":     ministerium,
            "tema":            None,
            "dokumenttyp_isl": extrahera_dokumenttyp(titill),
        })

    sista_index: Optional[int] = None
    for a in tree.xpath('//li[contains(@class, "jump-last")]/a'):
        m = re.search(r'[?&]index=(\d+)', a.get("href", ""))
        if m:
            sista_index = int(m.group(1))
    return poster, sista_index


def hamta_rit_lista() -> list[dict]:
    """
    Hämtar komplett lista med rit og skýrslur från stjornarradid.is.

    Går igenom listningssidorna ?index=0, 1, … till och med sista index enligt
    sidans paginering, eller tills en sida är tom. Avduplicerar på URL.

    Returnerar lista med dict (fält: url, titill, slug, dagsetning, ar,
    ministerium, tema, dokumenttyp_isl). Returnerar [] om första sidan inte
    gick att hämta.
    """
    poster: list[dict] = []
    sedda:  set[str]   = set()
    sista:  Optional[int] = None

    index = 0
    while index < MAX_LISTSIDOR:
        r = _hamta(f"{LISTA_URL}?index={index}")
        if r is None:
            log.warning("hamta_rit_lista: index=%s misslyckades", index)
            break
        ny, sista_pa_sidan = _parsa_listsida(r.content)
        if sista is None:
            sista = sista_pa_sidan
        log.info("index=%s: %d poster", index, len(ny))
        if not ny:
            break
        for p in ny:
            if p["url"] not in sedda:
                sedda.add(p["url"])
                poster.append(p)
        if sista is not None and index >= sista:
            break
        index += 1

    log.info("hamta_rit_lista: %d unika publikationer totalt", len(poster))
    return poster


# ---------------------------------------------------------------------------
# Publikationssidan: metadata och PDF-länk
# ---------------------------------------------------------------------------

def _pdf_kandidater(tree) -> list[str]:
    """
    Returnerar absoluta PDF-länkar från publikationssidan i prioritetsordning:
    uttryckliga nedladdningslänkar (rel="download", type=pdf, .pdf) först,
    övriga /library-länkar i innehållet sist. Länkar i sidhuvud och sidfot
    räknas inte.
    """
    forsta: list[str] = []
    ovriga: list[str] = []
    for a in tree.xpath('//a[@href]'):
        if a.xpath('ancestor::footer or ancestor::header or ancestor::nav'):
            continue
        href = a.get("href", "").strip()
        lagre = href.lower()
        if "/library" not in lagre or not ("itemid=" in lagre or lagre.endswith(".pdf")):
            continue
        absolut = urljoin(BASE_URL + "/", href)
        tydlig = (
            a.get("rel", "") == "download"
            or "type=pdf" in lagre
            or lagre.endswith(".pdf")
        )
        mal = forsta if tydlig else ovriga
        if absolut not in forsta and absolut not in ovriga:
            mal.append(absolut)
    return forsta + ovriga


def hamta_publikationssida(url: str) -> Optional[dict]:
    """
    Hämtar en publikationssida och returnerar {url, titill, pdf_kandidater},
    eller None om sidan inte finns (inkl. webbplatsens mallsida för okända
    sökvägar) eller inte gick att hämta.
    """
    r = _hamta(url)
    if r is None:
        return None
    try:
        tree = lhtml.fromstring(r.content)
    except Exception as exc:
        log.warning("HTML-parsfel för %s: %s", url, exc)
        return None
    if not _ar_publikationssida(tree):
        return None
    titel = tree.xpath('//meta[@property="og:title"]/@content')
    return {
        "url":            kanonisk_rit_url(str(r.url)) or str(r.url),
        "titill":         titel[0].strip() if titel else None,
        "pdf_kandidater": _pdf_kandidater(tree),
    }


def hamta_pdf_url(url: str) -> Optional[str]:
    """
    Hämtar publikationssidan och returnerar den första PDF-länken, eller
    None om ingen finns. Innehållet kontrolleras vid nedladdningen.
    """
    sida = hamta_publikationssida(url)
    if not sida or not sida["pdf_kandidater"]:
        log.info("Ingen PDF-URL hittad på %s", url)
        return None
    return sida["pdf_kandidater"][0]


# ---------------------------------------------------------------------------
# PDF-extraktion
# ---------------------------------------------------------------------------

def _hamta_pdf(pdf_url: str) -> Optional[bytes]:
    """Laddar ner pdf_url. Returnerar bytes bara om innehållet är en PDF."""
    r = _hamta(pdf_url)
    if r is None:
        return None
    if not r.content.startswith(b"%PDF"):
        log.warning("Inte en PDF (%s): %s",
                    r.headers.get("content-type", "okänd typ"), pdf_url)
        return None
    return r.content


def _extrahera_markdown(pdf_bytes: bytes, kalla: str) -> Optional[str]:
    """Extraherar markdown ur PDF-bytes via en temporär fil som alltid raderas."""
    try:
        import pymupdf4llm   # type: ignore
    except ImportError:
        log.error("pymupdf4llm saknas. Installera: pip install pymupdf4llm")
        return None

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False, prefix="island_rit_"
        ) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        fulltext_md: str = pymupdf4llm.to_markdown(str(tmp_path))
        log.debug("PDF extraherad: %s → %d tecken", kalla, len(fulltext_md))
        return fulltext_md or None
    except Exception as exc:
        log.error("pymupdf4llm-fel för %s: %s", kalla, exc)
        return None
    finally:
        # PDF raderas alltid — ingen persistent lokal kopia
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception as exc:
                log.warning("Kunde inte radera tmp-PDF %s: %s", tmp_path, exc)


def extrahera_pdf_fulltext(pdf_url: str) -> Optional[str]:
    """
    Laddar ner PDF från pdf_url och extraherar fulltext som markdown.
    Returnerar fulltext_md eller None vid fel.
    """
    pdf_bytes = _hamta_pdf(pdf_url)
    if not pdf_bytes:
        log.warning("Kunde inte ladda ner PDF: %s", pdf_url)
        return None
    return _extrahera_markdown(pdf_bytes, pdf_url)


def _extrahera_forsta(kandidater: list[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Provar PDF-kandidaterna i tur och ordning. Returnerar (pdf_url, fulltext_md)
    för den första som ger text, annars (första kandidaten eller None, None).
    """
    for pdf_url in kandidater:
        fulltext_md = extrahera_pdf_fulltext(pdf_url)
        if fulltext_md:
            return pdf_url, fulltext_md
    return (kandidater[0] if kandidater else None), None


# ---------------------------------------------------------------------------
# On-demand fulltexthämtning (för MCP-verktyget is_hamta_skyrsla)
# ---------------------------------------------------------------------------

def hamta_rit_fulltext(url: str) -> dict:
    """
    Hämtar fulltext för en enskild publikation on-demand.
    Används av MCP-verktyget is_hamta_skyrsla.

    Parametrar:
      url — kanonisk /rit/-URL, t.ex.
            'https://www.stjornarradid.is/gogn/rit-og-skyrslur/rit/2024-12-12-Slug/'

    Returnerar dict med url, titill, pdf_url, fulltext_md, tecken_antal.
    Saknas publikation, PDF eller text innehåller dict:en nyckeln 'fel'.
    """
    sida = hamta_publikationssida(url)
    if sida is None:
        return {"url": url, "pdf_url": None, "fulltext_md": None, "tecken_antal": 0,
                "fel": "Publikationen hittades inte på stjornarradid.is."}
    if not sida["pdf_kandidater"]:
        return {"url": sida["url"], "titill": sida["titill"], "pdf_url": None,
                "fulltext_md": None, "tecken_antal": 0,
                "fel": "Publikationssidan saknar PDF-länk."}

    pdf_url, fulltext_md = _extrahera_forsta(sida["pdf_kandidater"])
    if not fulltext_md:
        return {"url": sida["url"], "titill": sida["titill"], "pdf_url": pdf_url,
                "fulltext_md": None, "tecken_antal": 0,
                "fel": "PDF-extraktion misslyckades."}

    return {
        "url":          sida["url"],
        "titill":       sida["titill"],
        "pdf_url":      pdf_url,
        "fulltext_md":  fulltext_md,
        "tecken_antal": len(fulltext_md),
    }


# ---------------------------------------------------------------------------
# Äldre URL:er i databasen
# ---------------------------------------------------------------------------

def uppdatera_aldre_urler(db_mod=None) -> dict:
    """
    Flyttar poster i dokument_rit som har den äldre /stakt-rit/-formen till
    den kanoniska /rit/-URL:en.

    Omdirigeringen följs per post och målsidan kontrolleras. Finns redan en
    post med den nya URL:en (från en synk mot den nya listningen) slås de ihop:
    den nya postens fält behålls och luckor fylls från den gamla, liksom
    chunks om den nya saknar sådana. Den gamla posten tas sedan bort.

    Idempotent: poster som redan har /rit/-form berörs inte, och utan äldre
    poster görs inga anrop mot webbplatsen. Poster vars publikation inte
    längre finns lämnas orörda och redovisas.

    Returnerar {aldre, flyttade, sammanslagna, hittades_inte, fel}.
    """
    if db_mod is None:
        import db as db_mod

    stats: dict = {"aldre": 0, "flyttade": 0, "sammanslagna": 0,
                   "hittades_inte": [], "fel": []}
    aldre = db_mod.lista_rit_urler_med_monster("%/stakt-rit/%")
    stats["aldre"] = len(aldre)
    if not aldre:
        return stats

    log.info("Uppdaterar %d äldre rit-URL:er via webbplatsens omdirigering", len(aldre))
    for gammal in aldre:
        ny = folj_omdirigering(gammal)
        if not ny:
            stats["hittades_inte"].append(gammal)
            continue
        slug, _ = _slug_och_datum(ny)
        try:
            utfall = db_mod.flytta_dokument_rit(gammal, ny, slug)
        except Exception as exc:
            log.error("Kunde inte flytta %s → %s: %s", gammal, ny, exc)
            stats["fel"].append({"url": gammal, "fel": str(exc)})
            continue
        stats["flyttade" if utfall == "flyttad" else "sammanslagna"] += 1
        log.debug("%s: %s → %s", utfall, gammal, ny)

    log.info("Äldre URL:er: flyttade=%d, sammanslagna=%d, hittades inte=%d, fel=%d",
             stats["flyttade"], stats["sammanslagna"],
             len(stats["hittades_inte"]), len(stats["fel"]))
    return stats


# ---------------------------------------------------------------------------
# Interna DB-hjälpfunktioner för synk
# ---------------------------------------------------------------------------

def _har_fulltext(db_mod, url: str) -> bool:
    """Returnerar True om url redan har fulltext_md i island.dokument_rit."""
    try:
        sql = (
            f"SELECT 1 FROM {db_mod._prefix()}dokument_rit "
            f"WHERE url = {db_mod._ph()} AND fulltext_md IS NOT NULL"
        )
        with db_mod._cursor() as cur:
            cur.execute(sql, (url,))
            return cur.fetchone() is not None
    except Exception as exc:
        log.debug("_har_fulltext misslyckades för %s: %s", url, exc)
        return False


def _uppdatera_fulltext(
    db_mod,
    url: str,
    pdf_url: Optional[str],
    fulltext_md: Optional[str],
) -> None:
    """
    Uppdaterar pdf_url, fulltext_md, pdf_extraherad_at och sync_status
    för en befintlig post i island.dokument_rit. En redan lagrad fulltext
    skrivs aldrig över med tomt värde.
    """
    ny_status = "pdf_extraherad" if fulltext_md else "pdf_fel"
    nu = "NOW()" if db_mod._ar_postgres() else "datetime('now')"
    ph = db_mod._ph()
    sql = f"""
        UPDATE {db_mod._prefix()}dokument_rit
        SET pdf_url           = COALESCE({ph}, pdf_url),
            fulltext_md       = COALESCE({ph}, fulltext_md),
            pdf_extraherad_at = {nu},
            sync_status       = {ph}
        WHERE url = {ph}
    """
    with db_mod._cursor() as cur:
        cur.execute(sql, (pdf_url, fulltext_md, ny_status, url))


# ---------------------------------------------------------------------------
# Komplett synk
# ---------------------------------------------------------------------------

def synka_rit(
    hoppa_befintliga_pdf: bool = True,
    max_pdf: Optional[int] = None,
) -> dict:
    """
    Synkroniserar rit og skýrslur från stjornarradid.is till lokal DB.

    Flöde:
      1. Flytta eventuella poster med äldre /stakt-rit/-URL (no-op när inga finns).
      2. Hämta hela listningen (alla ?index=-sidor).
      3. Upsert metadata för alla poster. Befintlig fulltext och PDF-länk
         behålls.
      4. För poster utan fulltext: hämta publikationssidan, ladda ner PDF,
         extrahera markdown, radera PDF, uppdatera DB.
      5. Uppdatera island.sync_status.

    Parametrar:
      hoppa_befintliga_pdf — True (standard): hoppa poster som redan har
          fulltext_md i DB. Gör synken inkrementell vid upprepade körningar.
      max_pdf — Begränsa PDF-extraktion per körning (None = obegränsat).

    Returnerar statistikdict:
      {hämtade, nya_metadata, pdf_extraherade, pdf_misslyckade, fel, aldre_urler}
    """
    import db as db_mod  # Importeras här för att undvika cirkulärt beroende vid test

    db_mod.initiera_schema()

    stats: dict = {
        "hämtade":         0,
        "nya_metadata":    0,
        "pdf_extraherade": 0,
        "pdf_misslyckade": 0,
        "fel":             [],
        "aldre_urler":     None,
    }

    # ── Steg 1: Äldre URL:er ─────────────────────────────────────────────────
    try:
        aldre = uppdatera_aldre_urler(db_mod)
        stats["aldre_urler"] = {k: (len(v) if isinstance(v, list) else v)
                                for k, v in aldre.items()}
    except Exception as exc:
        log.error("Uppdatering av äldre URL:er misslyckades: %s", exc)
        stats["fel"].append({"url": None, "fel": f"äldre URL:er: {exc}"})

    # ── Steg 2: Hämta lista ──────────────────────────────────────────────────
    poster = hamta_rit_lista()
    stats["hämtade"] = len(poster)
    if not poster:
        log.warning("synka_rit: Inga publikationer hämtades — avbryter.")
        return stats

    # ── Steg 3: Upsert metadata ──────────────────────────────────────────────
    for p in poster:
        try:
            db_mod.upsert_dokument_rit(
                url             = p["url"],
                titill          = p["titill"],
                slug            = p["slug"],
                dagsetning      = p["dagsetning"],
                ministerium     = p["ministerium"],
                tema            = p["tema"],
                ar              = p["ar"],
                dokumenttyp_isl = p["dokumenttyp_isl"],
                pdf_url         = None,
                fulltext_md     = None,
            )
            stats["nya_metadata"] += 1
        except Exception as exc:
            log.error("upsert misslyckades för %s: %s", p["url"], exc)
            stats["fel"].append({"url": p["url"], "fel": str(exc)})

    log.info("Metadata upsertad för %d publikationer.", stats["nya_metadata"])

    # ── Steg 4: PDF-extraktion ───────────────────────────────────────────────
    pdf_count = 0
    for p in poster:
        if max_pdf is not None and pdf_count >= max_pdf:
            log.info("max_pdf=%d nått — stoppar PDF-extraktion.", max_pdf)
            break

        if hoppa_befintliga_pdf and _har_fulltext(db_mod, p["url"]):
            log.debug("Hoppar (redan extraherad): %s", p["slug"])
            continue

        sida = hamta_publikationssida(p["url"])
        if not sida or not sida["pdf_kandidater"]:
            log.info("Ingen PDF-URL för %s — sparar utan fulltext.", p["slug"])
            stats["pdf_misslyckade"] += 1
            continue

        pdf_url, fulltext_md = _extrahera_forsta(sida["pdf_kandidater"])
        pdf_count += 1

        try:
            _uppdatera_fulltext(db_mod, p["url"], pdf_url, fulltext_md)
        except Exception as exc:
            log.error("DB-uppdatering misslyckades för %s: %s", p["url"], exc)
            stats["fel"].append({"url": p["url"], "fel": str(exc)})
            continue

        if fulltext_md:
            stats["pdf_extraherade"] += 1
            log.info("✓ %s — %d tecken", p["slug"], len(fulltext_md))
        else:
            stats["pdf_misslyckade"] += 1

    # ── Steg 5: Uppdatera sync_status ────────────────────────────────────────
    try:
        checksum = hashlib.md5(
            str(sorted(p["url"] for p in poster)).encode()
        ).hexdigest()[:12]
        db_mod.set_sync_status(
            "stjornarradid_rit",
            checksum=checksum,
            detaljer=stats,
        )
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status: %s", exc)

    log.info(
        "synka_rit klar — hämtade=%d, metadata=%d, pdf=%d, misslyckade=%d",
        stats["hämtade"], stats["nya_metadata"],
        stats["pdf_extraherade"], stats["pdf_misslyckade"],
    )
    return stats


# ---------------------------------------------------------------------------
# Direkt körning — python3 stjornarradid_rit.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # --max-pdf N          begränsa PDF-extraktionen, t.ex. vid test
    # --lista-bara         lista utan att skriva till databasen
    # --uppdatera-urler    bara flytta poster med äldre /stakt-rit/-URL
    max_pdf: Optional[int] = None
    if "--max-pdf" in sys.argv:
        idx = sys.argv.index("--max-pdf")
        if idx + 1 < len(sys.argv):
            max_pdf = int(sys.argv[idx + 1])

    if "--lista-bara" in sys.argv:
        poster = hamta_rit_lista()
        for p in poster[:10]:
            print(f"  {p['dagsetning']}  {p['titill'][:70]}")
        print(f"... totalt {len(poster)} poster")
        sys.exit(0 if poster else 1)

    if "--uppdatera-urler" in sys.argv:
        import db as _db
        _db.initiera_schema()
        s = uppdatera_aldre_urler(_db)
        print(
            f"\nÄldre URL:er:\n"
            f"  Funna:          {s['aldre']}\n"
            f"  Flyttade:       {s['flyttade']}\n"
            f"  Sammanslagna:   {s['sammanslagna']}\n"
            f"  Hittades inte:  {len(s['hittades_inte'])}\n"
            f"  Fel:            {len(s['fel'])}"
        )
        for u in s["hittades_inte"][:20]:
            print(f"    finns inte längre: {u}")
        sys.exit(1 if s["fel"] else 0)

    resultat = synka_rit(hoppa_befintliga_pdf=True, max_pdf=max_pdf)
    print(
        f"\nSynk klar:\n"
        f"  Hämtade:         {resultat['hämtade']}\n"
        f"  Metadata upsert: {resultat['nya_metadata']}\n"
        f"  PDF extraherade: {resultat['pdf_extraherade']}\n"
        f"  PDF misslyckade: {resultat['pdf_misslyckade']}\n"
        f"  Fel:             {len(resultat['fel'])}"
    )
    if resultat["fel"]:
        print("\nFeldetaljer:")
        for f in resultat["fel"][:5]:
            print(f"  {f['url']}: {f['fel']}")
    # En tom listning betyder att webbplatsen ändrats eller inte svarar.
    # Nollskild exitkod gör att det syns i den dagliga synkens logg.
    sys.exit(0 if resultat["hämtade"] else 1)

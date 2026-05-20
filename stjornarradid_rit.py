# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
stjornarradid_rit.py — Klient och synkmodul för stjornarradid.is rit og skýrslur
MCP-server för isländsk riksdags- och rättsdata

Hämtar 380 publikationer (2021-2026) via Episerver LisasticSearch-endpointen,
extraherar PDF-fulltext med pymupdf4llm, och lagrar allt i island.dokument_rit.

────────────────────────────────────────────────────────────────────────────────
Datakällor
────────────────────────────────────────────────────────────────────────────────

Steg 1 — Lista publikationer (LisasticSearch):
  GET https://www.stjornarradid.is/gogn/rit-og-skyrslur/$LisasticSearch/Search/
      ?SearchQuery=&PageIndex={0|1}&SortByDate=False
  → Serverside-renderad HTML (kräver User-Agent) med 200+180 publikationslänkar.
  → Varje post innehåller länk till /stakt-rit/YYYY/MM/DD/SLUG/.
  → SearchQuery-filtret är trasigt (alla söktermer → 0 träffar). Använd ej.
  → PageIndex≥2 returnerar tomt.

Steg 2 — PDF-URL per publikation:
  GET https://www.stjornarradid.is/stakt-rit/YYYY/MM/DD/SLUG/
  → Episerver-sida. PDF-nedladdningslänken (<a href="/library/...pdf">) ingår
    vanligtvis i server-renderad HTML trots att sidan är delvis JS-renderad.
  → Om PDF-URL saknas sparas metadata utan fulltext_md.

Steg 3 — PDF-extraktion:
  GET {pdf_url}  → application/pdf
  → pymupdf4llm.to_markdown(tmp_fil) → fulltext_md
  → tmp_fil raderas direkt efter extraktion (ingen persistent PDF-cache).

────────────────────────────────────────────────────────────────────────────────
robots.txt: Crawl-delay: 5 (token-bucket per anrop).
User-Agent: mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn) — krävs.
Sajtens sökmotor är trasig — all sökning sker mot lokal DB (island.dokument_rit).
────────────────────────────────────────────────────────────────────────────────
"""

import hashlib
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from curl_cffi import requests as cf_requests
from dotenv import load_dotenv
from lxml import html as lhtml

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

BASE_URL     = "https://www.stjornarradid.is"
LISASTIC_URL = (
    BASE_URL
    + "/gogn/rit-og-skyrslur/$LisasticSearch/Search/"
    "?SearchQuery=&PageIndex={page_index}&SortByDate=False"
)
CRAWL_DELAY = 5.0   # sekunder (robots.txt Crawl-delay)
HEADERS     = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
    "Accept":     "text/html,application/xhtml+xml,*/*",
}

# Heuristiska dokumenttypsprefixar — längre kontrolleras FÖRE kortare för att
# förhindra att t.ex. "Skýrsla" matchar "Ársskýrsla".
# Distribution (räknat ur 380 titlar, kolumn 2 = antal):
_TYPORD: list[str] = [
    "Ársskýrsla",      # Årsrapport           14
    "Aðgerðaáætlun",   # Handlingsplan         8
    "Stöðuskýrsla",    # Statusrapport         6
    "Stöðumat",        # Lägesbedömning        3
    "Stefnumótun",     # Strategi              3
    "Lokaskýrsla",     # Slutrapport           2
    "Áfangaskýrsla",   # Delrapport            2
    "Samantekt",       # Sammanfattning        5
    "Greinargerð",     # Promemoria (Ds-typ)   3
    "Tillögur",        # Förslag               5
    "Grænbók",         # Grönbok               2
    "Hvítbók",         # Vitbok                1
    "Drög",            # Utkast                3
    "Mat",             # Bedömning             6
    "Skýrsla",         # Rapport (generell)   68
]

# Token-bucket tillstånd
_bucket_tokens  = 1.0
_bucket_last_ts = time.monotonic()


# ---------------------------------------------------------------------------
# Token-bucket throttle
# ---------------------------------------------------------------------------

def _throttle() -> None:
    """Respekterar robots.txt Crawl-delay: 5 via token-bucket."""
    global _bucket_tokens, _bucket_last_ts
    now     = time.monotonic()
    elapsed = now - _bucket_last_ts
    _bucket_tokens  = min(1.0, _bucket_tokens + elapsed / CRAWL_DELAY)
    _bucket_last_ts = now
    if _bucket_tokens < 1.0:
        sv = (1.0 - _bucket_tokens) * CRAWL_DELAY
        log.debug("Crawl-delay stjornarradid: väntar %.1f s", sv)
        time.sleep(sv)
        _bucket_tokens = 0.0
    else:
        _bucket_tokens -= 1.0


# ---------------------------------------------------------------------------
# Heuristisk dokumenttypsextraktion
# ---------------------------------------------------------------------------

def extrahera_dokumenttyp(titill: str) -> Optional[str]:
    """
    Returnerar heuristisk isländsk dokumenttyp baserat på titelprefixet.
    Täcker ~38 % av 380 publikationer 2021-2026. Returnerar None om okänt.

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
# HTTP-hjälpare
# ---------------------------------------------------------------------------

def _hamta_html(url: str) -> Optional[bytes]:
    """
    Hämtar HTML från url med User-Agent. Returnerar råa bytes, None vid fel.
    Använder curl_cffi med Chrome TLS-fingerprint för att passera Cloudflare bot-shield.
    Respekterar Crawl-delay via token-bucket.
    """
    _throttle()
    try:
        r = cf_requests.get(
            url,
            headers=HEADERS,
            timeout=30,
            impersonate="chrome124",
        )
        r.raise_for_status()
        return r.content
    except cf_requests.exceptions.HTTPError as exc:
        log.warning("HTTP %s vid %s", getattr(exc.response, "status_code", "?"), url)
        return None
    except Exception as exc:
        log.error("Nätverksfel vid %s: %s", url, exc)
        return None


def _hamta_binart(url: str) -> Optional[bytes]:
    """
    Hämtar binärdata (t.ex. PDF).
    Använder curl_cffi med Chrome TLS-fingerprint.
    Respekterar Crawl-delay via token-bucket.
    Returnerar bytes, None vid fel.
    """
    _throttle()
    hdrs = {**HEADERS, "Accept": "application/pdf,*/*"}
    try:
        r = cf_requests.get(
            url,
            headers=hdrs,
            timeout=120,
            impersonate="chrome124",
        )
        r.raise_for_status()
        return r.content
    except cf_requests.exceptions.HTTPError as exc:
        log.warning("HTTP %s vid %s", getattr(exc.response, "status_code", "?"), url)
        return None
    except Exception as exc:
        log.error("Fel vid nedladdning av %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# LisasticSearch-parser
# ---------------------------------------------------------------------------

# URL-mönster: /stakt-rit/YYYY/MM/DD/SLUG/
_STAKT_RIT_RE = re.compile(
    r'/stakt-rit/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)/?'
)

# Ministeriummatcher — letar efter isländska ministerienamn
_MINISTERIUM_RE = re.compile(
    r'([A-ZÁÉÍÓÚÝÐÞÆÖ][^\n,;]{5,60}ráðuneyt[ið]+)',
    re.UNICODE,
)


def _parsa_lisastic_html(html_bytes: bytes) -> list[dict]:
    """
    Parsar en LisasticSearch-resultatsida och returnerar publikationsposter.

    Strategi:
      1. Hitta alla <a href="/stakt-rit/YYYY/MM/DD/SLUG/"> i HTML:en.
      2. Extrahera ar, dagsetning, slug från URL-strukturen.
      3. Hämta titel från ankartexten eller närmaste rubrikelement (h2–h4).
      4. Försök extrahera ministerium ur omgivande block-text.

    Returnerar lista med dict:
      url, titill, slug, dagsetning, ar, ministerium, tema, dokumenttyp_isl
    """
    if not html_bytes:
        return []

    try:
        tree = lhtml.fromstring(html_bytes)
    except Exception as exc:
        log.error("HTML-parsfel i _parsa_lisastic_html: %s", exc)
        return []

    sedda_urls: set[str] = set()
    poster: list[dict]   = []

    for a in tree.xpath('//a[contains(@href, "/stakt-rit/")]'):
        href = a.get("href", "").strip()
        if not href:
            continue

        # Normalisera till absolut URL
        if href.startswith("/"):
            href = BASE_URL + href
        if href in sedda_urls:
            continue
        sedda_urls.add(href)

        # Parsa YYYY/MM/DD/SLUG ur URL
        m = _STAKT_RIT_RE.search(href)
        if not m:
            continue
        ar_str, mm_str, dd_str, slug = m.groups()
        ar         = int(ar_str)
        dagsetning = f"{ar_str}-{mm_str}-{dd_str}"

        # Titel: ankartexten eller närmaste rubrikelement
        titill = re.sub(r'\s+', ' ', a.text_content()).strip()
        if not titill:
            el = a
            for _ in range(6):
                el = el.getparent()
                if el is None:
                    break
                for rubrik in ("h2", "h3", "h4", "h1"):
                    h_noder = el.xpath(f".//{rubrik}")
                    if h_noder:
                        titill = re.sub(r'\s+', ' ',
                                        h_noder[0].text_content()).strip()
                        break
                if titill:
                    break
        if not titill:
            log.debug("Hoppar %s — ingen titel hittad", href)
            continue

        # Ministerium: sök i omgivande block-element (max 4 nivåer upp)
        ministerium: Optional[str] = None
        el = a
        for _ in range(4):
            el = el.getparent()
            if el is None:
                break
            if el.tag in ("li", "article", "div", "section"):
                block_text = el.text_content()
                mm = _MINISTERIUM_RE.search(block_text)
                if mm:
                    ministerium = mm.group(1).strip().replace("٫", ",")
                break

        poster.append({
            "url":             href,
            "titill":          titill,
            "slug":            slug,
            "dagsetning":      dagsetning,
            "ar":              ar,
            "ministerium":     ministerium,
            "tema":            None,   # Kräver JS-rendering för att extrahera
            "dokumenttyp_isl": extrahera_dokumenttyp(titill),
        })

    return poster


def hamta_rit_lista() -> list[dict]:
    """
    Hämtar komplett lista med rit og skýrslur från stjornarradid.is.

    Hämtar PageIndex=0 (200 poster) och PageIndex=1 (180 poster) från
    LisasticSearch-endpointen och avduplicerar på URL.

    Returnerar lista med dict (fält: url, titill, slug, dagsetning, ar,
    ministerium, tema, dokumenttyp_isl). Returnerar [] vid hämtningsfel.

    OBS: SearchQuery-filtret på sidan är trasigt — all sökning mot lokalt index.
    """
    poster: list[dict] = []
    sedda:  set[str]   = set()

    for page_index in (0, 1):
        url        = LISASTIC_URL.format(page_index=page_index)
        html_bytes = _hamta_html(url)
        if not html_bytes:
            log.warning("hamta_rit_lista: PageIndex=%s misslyckades", page_index)
            continue
        ny = _parsa_lisastic_html(html_bytes)
        log.info("PageIndex=%s: %d poster hittade", page_index, len(ny))
        for p in ny:
            if p["url"] not in sedda:
                sedda.add(p["url"])
                poster.append(p)

    log.info("hamta_rit_lista: %d unika publikationer totalt", len(poster))
    return poster


# ---------------------------------------------------------------------------
# PDF-URL-extraktion
# ---------------------------------------------------------------------------

_PDF_HREF_RE = re.compile(r'href=["\']([^"\']*?\.pdf)', re.IGNORECASE)


def hamta_pdf_url(url: str) -> Optional[str]:
    """
    Hämtar publikationssidan och extraherar direktlänken till PDF-filen.

    Episerver-sidor på stjornarradid.is är delvis JS-renderade men
    PDF-nedladdningslänken (<a href="/library/...pdf">) ingår vanligtvis
    i server-renderad HTML.

    Strategi 1: lxml xpath — <a href="*.pdf">
    Strategi 2: regex i råa bytes (fångar t.ex. data-attribut, inbäddad JSON)

    Returnerar absolut URL till PDF, eller None om ingen hittas.
    """
    html_bytes = _hamta_html(url)
    if not html_bytes:
        return None

    # Strategi 1: lxml — explicit PDF-länk
    try:
        tree = lhtml.fromstring(html_bytes)
        # translate() = fallback för case-insensitive match i XPath 1.0
        for a in tree.xpath(
            '//a[contains(translate(@href,"PDF","pdf"), ".pdf")]'
        ):
            href = a.get("href", "").strip()
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE_URL + href
            log.debug("PDF-URL (xpath): %s", href)
            return href
    except Exception as exc:
        log.debug("xpath-strategi misslyckades för %s: %s", url, exc)

    # Strategi 2: regex i råa bytes (hanterar t.ex. href='...' i script-block)
    text = html_bytes.decode("utf-8", errors="replace")
    mm   = _PDF_HREF_RE.search(text)
    if mm:
        href = mm.group(1)
        if href.startswith("/"):
            href = BASE_URL + href
        log.debug("PDF-URL (regex): %s", href)
        return href

    log.info("Ingen PDF-URL hittad på %s", url)
    return None


# ---------------------------------------------------------------------------
# PDF-extraktion
# ---------------------------------------------------------------------------

def extrahera_pdf_fulltext(pdf_url: str) -> Optional[str]:
    """
    Laddar ner PDF från pdf_url och extraherar fulltext som markdown.

    Pipeline:
      1. Ladda ner PDF till temporär fil (streaming).
      2. Extrahera markdown med pymupdf4llm.to_markdown().
      3. Radera temporärfilen (oavsett om extraktion lyckas).

    Returnerar fulltext_md (str) eller None vid fel.
    Kräver: pip install pymupdf4llm --break-system-packages
    """
    try:
        import pymupdf4llm   # type: ignore
    except ImportError:
        log.error(
            "pymupdf4llm saknas. "
            "Installera: pip install pymupdf4llm --break-system-packages"
        )
        return None

    pdf_bytes = _hamta_binart(pdf_url)
    if not pdf_bytes:
        log.warning("Kunde inte ladda ner PDF: %s", pdf_url)
        return None

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False, prefix="island_rit_"
        ) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        fulltext_md: str = pymupdf4llm.to_markdown(str(tmp_path))
        log.debug("PDF extraherad: %s → %d tecken", pdf_url, len(fulltext_md))
        return fulltext_md or None

    except Exception as exc:
        log.error("pymupdf4llm-fel för %s: %s", pdf_url, exc)
        return None

    finally:
        # PDF raderas alltid — ingen persistent lokal kopia
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception as exc:
                log.warning("Kunde inte radera tmp-PDF %s: %s", tmp_path, exc)


# ---------------------------------------------------------------------------
# On-demand fulltexthämtning (för MCP-verktyget is_hamta_skyrsla)
# ---------------------------------------------------------------------------

def hamta_rit_fulltext(url: str) -> dict:
    """
    Hämtar fulltext för en enskild publikation on-demand.
    Används av MCP-verktyget is_hamta_skyrsla.

    Parametrar:
      url — URL till publikationen, t.ex.
                'https://www.stjornarradid.is/stakt-rit/2024/03/15/skyrsla-um-x/'

    Returnerar dict med:
      url, pdf_url, fulltext_md, tecken_antal
    Returnerar {} med 'fel'-nyckel om extraktion misslyckas.
    """
    pdf_url = hamta_pdf_url(url)
    if not pdf_url:
        return {
            "url":     url,
            "pdf_url":     None,
            "fulltext_md": None,
            "tecken_antal": 0,
            "fel": "PDF-URL hittades inte (sidan är möjligen JS-renderad)",
        }

    fulltext_md = extrahera_pdf_fulltext(pdf_url)
    if not fulltext_md:
        return {
            "url":     url,
            "pdf_url":     pdf_url,
            "fulltext_md": None,
            "tecken_antal": 0,
            "fel": "PDF-extraktion misslyckades",
        }

    return {
        "url":      url,
        "pdf_url":      pdf_url,
        "fulltext_md":  fulltext_md,
        "tecken_antal": len(fulltext_md),
    }


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
    pdf_url: str,
    fulltext_md: Optional[str],
) -> None:
    """
    Uppdaterar pdf_url, fulltext_md, pdf_extraherad_at och sync_status
    för en befintlig post i island.dokument_rit.
    """
    ny_status = "pdf_extraherad" if fulltext_md else "pdf_fel"
    if db_mod._ar_postgres():
        sql = """
            UPDATE island.dokument_rit
            SET pdf_url           = %s,
                fulltext_md       = %s,
                pdf_extraherad_at = NOW(),
                sync_status       = %s
            WHERE url = %s
        """
    else:
        sql = """
            UPDATE dokument_rit
            SET pdf_url           = ?,
                fulltext_md       = ?,
                pdf_extraherad_at = datetime('now'),
                sync_status       = ?
            WHERE url = ?
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
      1. Hämta lista (LisasticSearch PageIndex=0+1) — ca 380 poster.
      2. Upsert metadata för alla poster (utan fulltext) i island.dokument_rit.
      3. För varje post: hämta PDF-URL från publikationssidan, ladda ner PDF,
         extrahera markdown, radera PDF, uppdatera DB.
      4. Uppdatera island.sync_status.

    Parametrar:
      hoppa_befintliga_pdf — True (standard): hoppa poster som redan har
          fulltext_md i DB. Gör synken inkrementell vid upprepade körningar.
      max_pdf — Begränsa PDF-extraktion per körning (None = obegränsat).
          Användbart vid minnesbegränsning eller testning (t.ex. max_pdf=5).

    Returnerar statistikdict:
      {hämtade, nya_metadata, pdf_extraherade, pdf_misslyckade, fel}

    OBS: Kör INTE via bash-verktyget — kör i Magnus terminal:
      python3 stjornarradid_rit.py
    PDF-extraktion tar uppskattningsvis 15–30 min för 380 dokument
    (Crawl-delay 5 s + extraktionstid per PDF).
    """
    import db as db_mod  # Importeras här för att undvika cirkulärt beroende vid test

    # Initiera schema (idempotent — CREATE TABLE IF NOT EXISTS)
    db_mod.initiera_schema()

    stats: dict = {
        "hämtade":         0,
        "nya_metadata":    0,
        "pdf_extraherade": 0,
        "pdf_misslyckade": 0,
        "fel":             [],
    }

    # ── Steg 1: Hämta lista ──────────────────────────────────────────────────
    poster = hamta_rit_lista()
    stats["hämtade"] = len(poster)
    if not poster:
        log.warning("synka_rit: Inga publikationer hämtades — avbryter.")
        return stats

    # ── Steg 2: Upsert metadata ──────────────────────────────────────────────
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

    # ── Steg 3: PDF-extraktion ───────────────────────────────────────────────
    pdf_count = 0
    for p in poster:
        if max_pdf is not None and pdf_count >= max_pdf:
            log.info("max_pdf=%d nått — stoppar PDF-extraktion.", max_pdf)
            break

        # Inkrementell: hoppa om fulltext redan finns
        if hoppa_befintliga_pdf and _har_fulltext(db_mod, p["url"]):
            log.debug("Hoppar (redan extraherad): %s", p["slug"])
            continue

        # Hämta PDF-URL
        pdf_url = hamta_pdf_url(p["url"])
        if not pdf_url:
            log.info("Ingen PDF-URL för %s — sparar utan fulltext.", p["slug"])
            stats["pdf_misslyckade"] += 1
            continue

        # Extrahera fulltext
        fulltext_md = extrahera_pdf_fulltext(pdf_url)

        # Uppdatera DB (oavsett utfall — pdf_url sparas alltid)
        try:
            _uppdatera_fulltext(db_mod, p["url"], pdf_url, fulltext_md)
        except Exception as exc:
            log.error("DB-uppdatering misslyckades för %s: %s", p["url"], exc)
            stats["fel"].append({"url": p["url"], "fel": str(exc)})
            continue

        if fulltext_md:
            stats["pdf_extraherade"] += 1
            pdf_count += 1
            log.info("✓ %s — %d tecken", p["slug"], len(fulltext_md))
        else:
            stats["pdf_misslyckade"] += 1

    # ── Steg 4: Uppdatera sync_status ────────────────────────────────────────
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

    # Argument: --max-pdf N   för att begränsa körningen vid test
    max_pdf: Optional[int] = None
    if "--max-pdf" in sys.argv:
        idx = sys.argv.index("--max-pdf")
        if idx + 1 < len(sys.argv):
            max_pdf = int(sys.argv[idx + 1])

    # Argument: --lista-bara   för att enbart lista utan PDF-extraktion
    lista_bara = "--lista-bara" in sys.argv

    if lista_bara:
        poster = hamta_rit_lista()
        for p in poster[:10]:
            print(f"  {p['dagsetning']}  {p['titill'][:70]}")
        print(f"... totalt {len(poster)} poster")
    else:
        resultat = synka_rit(
            hoppa_befintliga_pdf=True,
            max_pdf=max_pdf,
        )
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

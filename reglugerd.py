# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
reglugerd.py — Klient mot api.reglugerd.is (isländska förordningar)

Officiellt REST-API drivet av Digital Iceland (Stafrænt Ísland) under
isländska finansdepartementet. Källkod öppen (MIT): github.com/island-is/regulations

Bas-URL: https://api.reglugerd.is/api/v1/
Format:  JSON (metadata), PDF (fulltext)
Server:  Fastify (ingen Swagger/OpenAPI-dokumentation exponerad)

Bekräftade endpoints:
  GET /api/v1/years                              — lista år (1957–2026)
  GET /api/v1/ministries                         — lista 16 ministerier
  GET /api/v1/search                             — sökning, paginerat JSON
  GET /api/v1/regulation/{nr-ar}/{version}/      — metadata för en förordning
  GET /api/v1/regulation/{nr-ar}/{version}/pdf/  — PDF-fulltext (bara för
      förordningar vars detaljsvar har pdfVersion; övriga ger 404)

Sökresultat-format (verifierat):
  {
    "page": 1, "perPage": 30, "totalPages": ..., "totalItems": ...,
    "data": [{"name": "1424/2020", "title": "...", "publishedDate": "2020-12-30",
              "ministry": "Umhverfis-, orku- og loftslagsráðuneyti"}, ...]
  }
  perPage ignoreras (alltid 30). Utan både q och year blir träffarna 0.
  I detaljsvaret är ministry i stället ett objekt {slug, name}, eller saknas.

Detaljsvaret finns i två former: fullständigt (med pdfVersion) eller ett
kortsvar {name, title, redirectUrl, originalDoc} — se hamta_reglugerd().

URL-format för enskilda förordningar:
  {nr-ar} = nummer-år, ex. '0179-2018' (nr 179/2018, nr paddas till 4 siffror)
  {version} = 'current' (gällande) eller 'original' (ursprungstext)
"""

import logging
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE = "https://api.reglugerd.is/api/v1"
HEADERS  = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
    "Accept":     "application/json",
}


# ---------------------------------------------------------------------------
# Internverktyg
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: Optional[dict] = None) -> dict | list:
    """GET mot API_BASE/{endpoint} — returnerar JSON (dict eller list)."""
    url = f"{API_BASE}/{endpoint}"
    r   = httpx.get(url, params=params or {}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _nr_ar_strang(nr: int, ar: int) -> str:
    """
    Formaterar ett förordningsnummer till API:ts nr-ar-format.
    Exempel: nr=179, ar=2018 → '0179-2018'
    Nummerdelen paddas till 4 siffror.
    """
    return f"{nr:04d}-{ar}"


# ---------------------------------------------------------------------------
# Metadata-endpoints
# ---------------------------------------------------------------------------

def hamta_ar_lista() -> list[str]:
    """
    Hämtar lista över tillgängliga år från /api/v1/years.
    Returnerar lista med år som strängar (nyast först), t.ex. ['2026', '2025', ..., '1957'].
    """
    try:
        data = _get("years")
        ar_lista = list(data) if isinstance(data, list) else []
        return sorted(ar_lista, reverse=True)
    except Exception as exc:
        log.error("hamta_ar_lista misslyckades: %s", exc)
        return []


def hamta_ministerier() -> list[dict]:
    """
    Hämtar lista över isländska ministerier från /api/v1/ministries.
    Returnerar lista med: slug, name, order.
    Exempel: {'slug': 'fsr', 'name': 'Forsætisráðuneyti', 'order': 1}
    """
    try:
        data = _get("ministries")
        return list(data) if isinstance(data, list) else []
    except Exception as exc:
        log.error("hamta_ministerier misslyckades: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Hjälpare för källans fältformat
# ---------------------------------------------------------------------------

# Källan returnerar alltid 30 poster per sida; perPage ignoreras.
API_SIDSTORLEK = 30


def ministerium_namn(varde) -> str:
    """
    Returnerar ministeriets namn oavsett format. Sökresultaten har en sträng,
    detaljsvaret ett objekt {slug, name}, och fältet kan saknas helt.
    """
    if isinstance(varde, dict):
        return str(varde.get("name") or varde.get("slug") or "")
    return str(varde or "")


# ---------------------------------------------------------------------------
# Sökning
# ---------------------------------------------------------------------------

def _sok_sida(fraga: str, ar: Optional[int], api_sida: int) -> dict:
    params: dict = {"page": api_sida}
    if fraga:
        params["q"] = fraga
    if ar is not None:
        params["year"] = ar
    svar = _get("search", params=params)
    return svar if isinstance(svar, dict) else {}


def sok_reglugerd(
    fraga: str = "",
    ar: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """
    Söker i isländska förordningar via /api/v1/search.

    Källan har en fast sidstorlek på 30 och ignorerar perPage. Sidindelningen
    här räknas därför om: sida `page` med `per_page` poster motsvarar poster
    (page-1)*per_page … page*per_page-1, som hämtas från de en eller två
    källsidor de ligger på. per_page begränsas till 1–30.

    Källan ger 0 träffar när både fritext och år saknas, så minst ett av dem
    krävs (ValueError annars).

    Parametrar:
      fraga    — Fritext på isländska (söker i titel och innehåll).
      ar       — Filtrera på publiceringsår (t.ex. 2020).
      page     — Sidnummer (1-baserat).
      per_page — Poster per sida (1–30).

    Returnerar dict med:
      fraga, ar, page, per_page, total_sidor, total_antal, data.
    Varje förordning: name (t.ex. '0725/2020'), titill, publicerad, ministerium.
    Nätverks- och HTTP-fel kastas som httpx-undantag.
    """
    if not fraga and ar is None:
        raise ValueError(
            "Ange en sökterm eller ett år. Källan returnerar inga träffar "
            "när båda saknas."
        )
    per_page = max(1, min(int(per_page), API_SIDSTORLEK))
    page     = max(1, int(page))

    forsta = (page - 1) * per_page
    sista  = forsta + per_page - 1
    api_forsta = forsta // API_SIDSTORLEK + 1
    api_sista  = sista // API_SIDSTORLEK + 1

    poster: list[dict] = []
    total_antal = 0
    for api_sida in range(api_forsta, api_sista + 1):
        svar = _sok_sida(fraga, ar, api_sida)
        # totalItems är 0 för sidor bortom slutet; första svarets värde gäller
        if api_sida == api_forsta or not total_antal:
            total_antal = max(total_antal, int(svar.get("totalItems") or 0))
        data = svar.get("data") or []
        forskjutning = (api_sida - 1) * API_SIDSTORLEK
        for i, item in enumerate(data):
            if forsta <= forskjutning + i <= sista:
                poster.append({
                    "name":        item.get("name", ""),
                    "titill":      item.get("title", ""),
                    "publicerad":  item.get("publishedDate", ""),
                    "ministerium": ministerium_namn(item.get("ministry")),
                })
        if len(data) < API_SIDSTORLEK:
            break

    return {
        "fraga":       fraga,
        "ar":          ar,
        "page":        page,
        "per_page":    per_page,
        "total_sidor": -(-total_antal // per_page) if total_antal else 0,
        "total_antal": total_antal,
        "data":        poster,
    }


# ---------------------------------------------------------------------------
# Enskild förordning
# ---------------------------------------------------------------------------

# Fält som redovisas separat och därför inte upprepas i meta_raw.
_SEPARATA_FALT = {
    "title", "name", "publishedDate", "ministry", "effectiveDate",
    "signatureDate", "repealed", "repealedDate", "pdfVersion", "originalDoc",
    "redirectUrl",
}


def hamta_reglugerd(nr: int, ar: int, version: str = "current") -> dict:
    """
    Hämtar metadata för en enskild förordning via /api/v1/regulation/{nr-ar}/{version}/.

    Källan ger två svarsformer:
      - fullständigt svar med bl.a. pdfVersion (länk till konsoliderad PDF),
      - kortsvar {name, title, redirectUrl, originalDoc} för förordningar som
        inte är inlagda i strukturerad form. Där finns ingen konsoliderad PDF
        (…/pdf/ ger 404); originalDoc är i förekommande fall den ursprungliga
        kungörelsen i Stjórnartíðindi, och redirectUrl sidan på reglugerd.is.

    PDF-länken byggs aldrig själv: pdf_url är pdfVersion, annars originalDoc,
    annars None, och pdf_typ säger vilken ('konsoliderad', 'originalkungorelse').

    Returnerar dict med:
      nr, ar, beteckning, version, titill, publicerad, ministerium,
      ministerium_okant, ikrafttradde_vid, signerades_vid, upphavt, upphavt_vid,
      pdf_url, pdf_typ, webb_url, fullstandig, url, meta_raw
      (+ notering för kortsvar).
    Returnerar {} om förordningen inte finns (404). Andra fel kastas.
    """
    nr_ar = _nr_ar_strang(nr, ar)
    url   = f"{API_BASE}/regulation/{nr_ar}/{version}/"

    r = httpx.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 404:
        log.warning("Förordning %s/%s (version=%s) hittades inte", nr, ar, version)
        return {}
    r.raise_for_status()
    meta = r.json()

    fullstandig = "pdfVersion" in meta or "publishedDate" in meta
    if meta.get("pdfVersion"):
        pdf_url, pdf_typ = meta["pdfVersion"], "konsoliderad"
    elif meta.get("originalDoc"):
        pdf_url, pdf_typ = meta["originalDoc"], "originalkungorelse"
    else:
        pdf_url, pdf_typ = None, None

    ministerium = ministerium_namn(meta.get("ministry"))
    svar = {
        "nr":                nr,
        "ar":                ar,
        "beteckning":        f"{nr}/{ar}",
        "version":           version,
        "titill":            meta.get("title") or meta.get("name", ""),
        "publicerad":        meta.get("publishedDate"),
        "ministerium":       ministerium,
        "ministerium_okant": not ministerium,
        "ikrafttradde_vid":  meta.get("effectiveDate"),
        "signerades_vid":    meta.get("signatureDate"),
        "upphavt":           bool(meta.get("repealed", False)),
        "upphavt_vid":       meta.get("repealedDate"),
        "pdf_url":           pdf_url,
        "pdf_typ":           pdf_typ,
        "webb_url":          meta.get("redirectUrl")
                             or f"https://www.reglugerd.is/reglugerdir/allar/nr/{nr_ar}",
        "fullstandig":       fullstandig,
        "url":               url,
        "meta_raw":          {k: v for k, v in meta.items() if k not in _SEPARATA_FALT},
    }
    if not fullstandig:
        svar["notering"] = (
            "Källan har bara titel och länkar för denna förordning (ingen "
            "strukturerad text och ingen konsoliderad PDF). "
            + ("pdf_url är den ursprungliga kungörelsen i Stjórnartíðindi. "
               if pdf_url else "Ingen PDF finns via API:t. ")
            + "Läs gällande lydelse på webb_url."
        )
    return svar

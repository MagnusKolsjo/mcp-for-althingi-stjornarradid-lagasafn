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
  GET /api/v1/regulation/{nr-ar}/{version}/pdf/  — PDF-fulltext

Sökresultat-format (verifierat):
  {
    "page": 1, "perPage": 30, "totalPages": ..., "totalItems": ...,
    "data": [{"name": "1424/2020", "title": "...", "publishedDate": "2020-12-30",
              "ministry": "Umhverfis-, orku- og loftslagsráðuneyti"}, ...]
  }

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
# Sökning
# ---------------------------------------------------------------------------

def sok_reglugerd(
    fraga: str = "",
    ar: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """
    Söker i isländska förordningar via /api/v1/search.

    Parametrar:
      fraga    — Fritext på isländska (söker i titel och innehåll).
                 Tom sträng returnerar alla förordningar (paginerat).
      ar       — Filtrera på publiceringsår (t.ex. 2020). Inget filter om None.
      page     — Sidnummer (standard 1).
      per_page — Poster per sida (standard 20, API-max okänt).

    Returnerar dict med:
      page, per_page, total_sidor, total_antal, data (lista med förordningar).
    Varje förordning innehåller: name (t.ex. '179/2018'), titill, publicerad, ministerium.
    """
    params: dict = {"page": page, "perPage": per_page}
    if fraga:
        params["q"] = fraga
    if ar is not None:
        params["year"] = ar

    try:
        svar = _get("search", params=params)
        data = svar.get("data", []) if isinstance(svar, dict) else []

        # Normalisera poster till konsekvent nyckelnamn
        poster = []
        for item in data:
            poster.append({
                "name":       item.get("name", ""),
                "titill":     item.get("title", ""),
                "publicerad": item.get("publishedDate", ""),
                "ministerium": item.get("ministry", ""),
            })

        return {
            "fraga":       fraga,
            "ar":          ar,
            "page":        svar.get("page", page),
            "per_page":    svar.get("perPage", per_page),
            "total_sidor": svar.get("totalPages", 0),
            "total_antal": svar.get("totalItems", 0),
            "data":        poster,
        }
    except Exception as exc:
        log.error("sok_reglugerd misslyckades (fraga=%r ar=%s): %s", fraga, ar, exc)
        return {"fraga": fraga, "ar": ar, "data": [], "fel": str(exc)}


# ---------------------------------------------------------------------------
# Enskild förordning
# ---------------------------------------------------------------------------

def hamta_reglugerd(nr: int, ar: int, version: str = "current") -> dict:
    """
    Hämtar metadata för en enskild förordning via /api/v1/regulation/{nr-ar}/{version}/.

    Parametrar:
      nr      — Förordningsnummer (t.ex. 179 för nr 179/2018).
      ar      — Publiceringsår (t.ex. 2018).
      version — 'current' (gällande, standard) eller 'original' (ursprungstext).

    Returnerar dict med:
      nr, ar, beteckning, version, titill, publicerad, ministerium,
      ministerium_okant (True när ministerium saknas i källan),
      ikrafttradde_vid, signerades_vid, upphavt (bool), upphavt_vid,
      pdf_url (direktlänk till PDF-fulltext), url (metadata-URL).
    Returnerar {} om förordningen ej hittas.
    """
    nr_ar = _nr_ar_strang(nr, ar)
    url   = f"{API_BASE}/regulation/{nr_ar}/{version}/"

    try:
        r = httpx.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        meta = r.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            log.warning("Förordning %s/%s (version=%s) hittades inte", nr, ar, version)
        else:
            log.error("HTTP-fel vid hämtning av förordning %s/%s: %s", nr, ar, exc)
        return {}
    except Exception as exc:
        log.error("hamta_reglugerd misslyckades (%s/%s): %s", nr, ar, exc)
        return {}

    pdf_url = f"{API_BASE}/regulation/{nr_ar}/{version}/pdf/"

    ministerium_val = meta.get("ministry", "")
    return {
        "nr":               nr,
        "ar":               ar,
        "beteckning":       f"{nr}/{ar}",
        "version":          version,
        "titill":           meta.get("title", meta.get("name", "")),
        "publicerad":       meta.get("publishedDate", ""),
        "ministerium":      ministerium_val,
        "ministerium_okant": not bool(ministerium_val),
        "ikrafttradde_vid": meta.get("effectiveDate"),
        "signerades_vid":   meta.get("signatureDate"),
        "upphavt":          bool(meta.get("repealed", False)),
        "upphavt_vid":      meta.get("repealedDate"),
        "pdf_url":          pdf_url,
        "url":              url,
        "meta_raw":         {k: v for k, v in meta.items()
                              if k not in ("title", "name", "publishedDate", "ministry",
                                           "effectiveDate", "signatureDate",
                                           "repealed", "repealedDate")},
    }


def hamta_reglugerd_pdf_url(nr: int, ar: int, version: str = "current") -> str:
    """
    Bygger direktlänken till en förordnings PDF utan att göra ett API-anrop.
    Används när pdf_url redan är känd och ingen metadata-hämtning behövs.

    Returnerar: 'https://api.reglugerd.is/api/v1/regulation/{nr-ar}/{version}/pdf/'
    """
    nr_ar = _nr_ar_strang(nr, ar)
    return f"{API_BASE}/regulation/{nr_ar}/{version}/pdf/"

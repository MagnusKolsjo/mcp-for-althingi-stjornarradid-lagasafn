# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
reglugerd.py — Klient mot api.reglugerd.is (isländska förordningar)

Officiellt REST-API drivet av Digital Iceland (Stafrænt Ísland) under
isländska finansdepartementet. Källkod öppen (MIT): github.com/island-is/regulations

Bas-URL: https://api.reglugerd.is/api/v1/
Format:  JSON. Fulltexten finns som HTML i fältet `text` (detaljsvaret och
         bulkhämtningen); PDF är ett alternativt format, inte den enda källan.
Server:  Fastify (ingen Swagger/OpenAPI-dokumentation exponerad)

Bekräftade endpoints:
  GET /api/v1/years                              — lista år (1957–2026)
  GET /api/v1/ministries                         — lista 16 ministerier
  GET /api/v1/search                             — Elasticsearch-sökning över
      title, text och text.stemmed (isländsk stamning), paginerat JSON.
      q = fritext (query_string-syntax), year/yearTo, rn = ministerium-slug,
      ch = lagkapitel-slug, iA=true tar med ändringsförordningar,
      iR=true tar med upphävda. Utan iA/iR söker källan bara gällande
      grundförordningar.
  GET /api/v1/regulations/all/current/full       — alla förordningar med
      metadata och `text` (HTML) i ett svar (~6 200 poster, ~57 MB)
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
import re
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE = "https://api.reglugerd.is/api/v1"
HEADERS  = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/2.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
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
    if isinstance(varde, str) and varde.startswith("{"):
        # Bulksvaret har objektet som Python-repr, t.ex. "{'slug': 'urn', 'name': '…'}"
        try:
            import ast
            varde = ast.literal_eval(varde)
        except (ValueError, SyntaxError):
            pass
    if isinstance(varde, dict):
        return str(varde.get("name") or varde.get("slug") or "")
    return str(varde or "")


# ---------------------------------------------------------------------------
# HTML → markdown
# ---------------------------------------------------------------------------

_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "section",
          "div", "table", "ol", "ul", "hr", "thead", "tbody"}


def html_till_markdown(html: str) -> str:
    """
    Gör förordningstextens HTML till läsbar markdown.

    Källans HTML har ett ord per rad; blanktecken inom ett block slås därför
    ihop till ett mellanslag. Rubriker blir #-rubriker, listpunkter "- ",
    tabellrader celler åtskilda med " | ". Inline-markering (em, strong, sup)
    blir ren text.
    """
    if not html or not html.strip():
        return ""
    from lxml import html as lhtml

    try:
        rot = lhtml.fragment_fromstring(html, create_parent="div")
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    rader: list[str] = []

    def text_av(el) -> str:
        delar = []
        for t in el.itertext():
            delar.append(t)
        return re.sub(r"\s+", " ", "".join(delar)).strip()

    def besok(el) -> None:
        tagg = el.tag if isinstance(el.tag, str) else ""
        if tagg in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = text_av(el)
            if t:
                rader.append("#" * int(tagg[1]) + " " + t)
            return
        if tagg == "p":
            # <br> inom stycket blir radbrytning
            for br in el.iter("br"):
                br.tail = "\n" + (br.tail or "")
            stycke = "\n".join(
                re.sub(r"[ \t\r\f\v]+", " ", r).strip()
                for r in re.sub(r"[ \t]*\n[ \t]*(?=\S)", " ", "".join(el.itertext())).split("\n")
            ).strip()
            stycke = re.sub(r" {2,}", " ", stycke)
            if stycke:
                rader.append(stycke)
            return
        if tagg == "li":
            t = text_av(el)
            if t:
                rader.append("- " + t)
            return
        if tagg == "tr":
            celler = [text_av(c) for c in el if isinstance(c.tag, str) and c.tag in ("td", "th")]
            if any(celler):
                rader.append("| " + " | ".join(celler) + " |")
            return
        if tagg == "hr":
            rader.append("---")
            return
        if tagg in _BLOCK or tagg == "div" or tagg == "":
            if el.text and el.text.strip():
                rader.append(re.sub(r"\s+", " ", el.text).strip())
            for barn in el:
                besok(barn)
                if barn.tail and barn.tail.strip():
                    rader.append(re.sub(r"\s+", " ", barn.tail).strip())
            return
        t = text_av(el)
        if t:
            rader.append(t)

    besok(rot)
    # Tabellrader hålls ihop; övriga block skiljs med en tomrad.
    ut = ""
    for i, rad in enumerate(rader):
        if i:
            ut += "\n" if rad.startswith("|") and rader[i - 1].startswith("|") else "\n\n"
        ut += rad
    return ut.strip()


# ---------------------------------------------------------------------------
# Sökning
# ---------------------------------------------------------------------------

def _sok_sida(fraga: str, ar: Optional[int], api_sida: int,
              med_andringsforordningar: bool, med_upphavda: bool) -> dict:
    params: dict = {"page": api_sida}
    if fraga:
        params["q"] = fraga
    if ar is not None:
        params["year"] = ar
    # Källan tolkar bara exakt 'true'; utelämnad flagga betyder "ta inte med".
    if med_andringsforordningar:
        params["iA"] = "true"
    if med_upphavda:
        params["iR"] = "true"
    svar = _get("search", params=params)
    return svar if isinstance(svar, dict) else {}


def sok_reglugerd(
    fraga: str = "",
    ar: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
    med_andringsforordningar: bool = True,
    med_upphavda: bool = True,
) -> dict:
    """
    Söker i isländska förordningar via /api/v1/search (Elasticsearch över
    titel och fulltext, med isländsk stamning).

    Som standard tas ändringsförordningar (iA) och upphävda förordningar (iR)
    med, så att sökningen täcker hela samlingen. Källan skulle annars bara
    söka bland gällande grundförordningar. Sätt flaggorna till False för att
    begränsa.

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
        svar = _sok_sida(fraga, ar, api_sida, med_andringsforordningar, med_upphavda)
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
        "med_andringsforordningar": med_andringsforordningar,
        "med_upphavda": med_upphavda,
        "page":        page,
        "per_page":    per_page,
        "total_sidor": -(-total_antal // per_page) if total_antal else 0,
        "total_antal": total_antal,
        "data":        poster,
    }


# ---------------------------------------------------------------------------
# Enskild förordning
# ---------------------------------------------------------------------------

# Fält som redovisas separat och därför inte upprepas i meta_raw. text och
# appendixes är HTML och redovisas som markdown i text_md.
_SEPARATA_FALT = {
    "title", "name", "publishedDate", "ministry", "effectiveDate",
    "signatureDate", "repealed", "repealedDate", "pdfVersion", "originalDoc",
    "redirectUrl", "text", "appendixes",
}


def _bilagor_som_markdown(bilagor) -> str:
    """Bilagor (appendixes) som markdown, var och en under en egen rubrik."""
    delar = []
    for b in bilagor or []:
        if isinstance(b, dict):
            titel = re.sub(r"\s+", " ", str(b.get("title") or "")).strip()
            text  = html_till_markdown(str(b.get("text") or ""))
        else:
            titel, text = "", html_till_markdown(str(b))
        if titel or text:
            delar.append(f"## Viðauki{': ' + titel if titel else ''}\n\n{text}".strip())
    return "\n\n".join(delar)


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

    Fulltexten tas ur detaljsvarets `text` (HTML) och bilagorna ur
    `appendixes`, omvandlade till markdown i text_md. Kortsvaret har ingen
    text; text_md är då None.

    Returnerar dict med:
      nr, ar, beteckning, version, titill, publicerad, ministerium,
      ministerium_okant, ikrafttradde_vid, signerades_vid, upphavt, upphavt_vid,
      pdf_url, pdf_typ, webb_url, fullstandig, url, text_md, meta_raw
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
    text_md = html_till_markdown(meta.get("text") or "")
    bilagor = _bilagor_som_markdown(meta.get("appendixes"))
    if bilagor:
        text_md = f"{text_md}\n\n{bilagor}".strip()
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
        "text_md":           text_md or None,
        "meta_raw":          {k: v for k, v in meta.items() if k not in _SEPARATA_FALT},
    }
    if not fullstandig:
        svar["notering"] = (
            "Källans detaljsvar har bara titel och länkar för denna förordning "
            "(ingen text och ingen konsoliderad PDF). "
            + ("pdf_url är den ursprungliga kungörelsen i Stjórnartíðindi. "
               if pdf_url else "Ingen PDF finns via API:t. ")
            + "Läs gällande lydelse på webb_url."
        )
    return svar


# ---------------------------------------------------------------------------
# Bulkhämtning för den lokala sökcachen
# ---------------------------------------------------------------------------

def hamta_alla_med_text() -> list[dict]:
    """
    Hämtar alla förordningar med text i ett anrop via
    /api/v1/regulations/all/current/full (~6 200 poster, ~57 MB).

    Bulksvaret har text även för förordningar vars detaljsvar är ett kortsvar.

    Returnerar lista med dict: name, titill, publicerad, ministerium, typ,
    upphavt, text_md. Fel kastas.
    """
    r = httpx.get(f"{API_BASE}/regulations/all/current/full",
                  headers=HEADERS, timeout=httpx.Timeout(30.0, read=300.0))
    r.raise_for_status()
    data = r.json()
    ut = []
    for item in data if isinstance(data, list) else []:
        ut.append({
            "name":        item.get("name", ""),
            "titill":      item.get("title", ""),
            "publicerad":  item.get("publishedDate"),
            "ministerium": ministerium_namn(item.get("ministry")),
            "typ":         item.get("type"),
            "upphavt":     str(item.get("repealed")).lower() == "true",
            "text_md":     html_till_markdown(item.get("text") or ""),
        })
    return ut

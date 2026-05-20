# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
klient_fel.py — Gemensamma undantag för klientmodulerna mot externa källor.

Används av althingi.py och lagasafn.py för att rapportera HTTP- och nätverks-
fel uppåt i anropskedjan på ett enhetligt sätt. mcp_server.py översätter
undantagen till MCP-svarsformat med differentierade felmeddelanden.
"""

from typing import Optional


class HamtaFel(Exception):
    """
    Källans svar gick inte att tolka eller källan blockerade anropet.

    Attribut:
      reason — kort maskinläsbar sträng:
                 '404'        — sidan/resursen finns inte hos källan
                 'blockerad'  — källan svarade 403 (bot-shield, åtkomst nekad)
                 'http_NNN'   — annat HTTP-fel där NNN är statuskoden
                 'natverk: …' — DNS-, timeout- eller anslutningsfel
      status — HTTP-statuskoden om känd, annars None.
    """

    def __init__(self, reason: str, status: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status

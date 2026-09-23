"""ETF assets under management from AMFI (Association of Mutual Funds in India).

* ISIN -> AMFI scheme code: https://www.amfiindia.com/spages/NAVAll.txt (daily NAV file)
* Scheme-wise AUM:        https://www.amfiindia.com/api/average-aum-schemewise
  (the API behind amfiindia.com/aum-data/average-aum). AMFI publishes scheme-level
  AUM as the *average AUM for a quarter* (e.g. "April - June 2026"), in Rs lakh,
  roughly a month after the quarter ends.

ETFs listed after the latest published quarter have no AUM until the next one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)

NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
AUM_API = "https://www.amfiindia.com/api/average-aum-schemewise"
LAKH = 1e5
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}  # no Accept header: NAVAll changes with it


@dataclass
class AumSnapshot:
    period: str                  # e.g. "Apr-Jun 2026"
    aum_by_isin: dict[str, float]  # rupees


def parse_nav_isins(text: str) -> dict[str, int]:
    """NAVAll.txt -> {ISIN: AMFI scheme code} (both ISIN columns)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].strip().isdigit():
            for isin in (parts[1].strip(), parts[2].strip()):
                if isin.startswith("INF"):
                    out[isin] = int(parts[0])
    return out


def parse_aum_table(payload: dict) -> dict[int, float]:
    """average-aum-schemewise table -> {AMFI code: AUM in rupees}."""
    out: dict[int, float] = {}
    for group in payload.get("data") or []:
        for s in group.get("schemes") or []:
            values = (s.get("AverageAumForTheMonth") or {}).values()
            out[int(s["AMFI_Code"])] = sum(v or 0 for v in values) * LAKH
    return out


def short_period(label: str) -> str:
    """'April - June 2026' -> 'Apr-Jun 2026'."""
    words = label.replace("-", " ").split()
    if len(words) == 3 and words[2].isdigit():
        return f"{words[0][:3]}-{words[1][:3]} {words[2]}"
    return label


class AmfiClient:
    def __init__(self, session: requests.Session | None = None, timeout: int = 60):
        self.http = session or requests.Session()
        self.timeout = timeout

    def _json(self, **params) -> dict:
        resp = self.http.get(AUM_API, params={"strType": "Categorywise", "MF_ID": 0, **params}, headers=_UA,
                             timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def latest_period(self) -> tuple[int, int, str]:
        """(fyId, periodId, label) of the most recent published quarter."""
        years = self._json().get("data") or []
        for fy in sorted(years, key=lambda y: y["id"])[:2]:  # id 1 = current FY; early in a FY it may be empty
            periods = (self._json(fyId=fy["id"]).get("data") or {}).get("periods") or []
            if periods:
                p = max(periods, key=lambda x: x["id"])
                return fy["id"], p["id"], f"{p['period']}"
        raise RuntimeError("AMFI returned no AUM periods")

    def fetch_etf_aum(self, isins: Iterable[str]) -> AumSnapshot:
        wanted = set(isins)
        nav = self.http.get(NAV_URL, headers=_UA, timeout=self.timeout)
        nav.raise_for_status()
        codes = parse_nav_isins(nav.text)
        fy_id, period_id, label = self.latest_period()
        by_code = parse_aum_table(self._json(fyId=fy_id, periodId=period_id))
        aum = {isin: by_code[codes[isin]] for isin in wanted if isin in codes and codes[isin] in by_code}
        log.info("AMFI AUM (%s): %d of %d ETFs", label, len(aum), len(wanted))
        return AumSnapshot(short_period(label), aum)

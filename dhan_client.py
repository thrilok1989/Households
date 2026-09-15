"""
dhan_client.py

Thin, explicit wrapper around the DhanHQ v2 HTTP API. No trading/order
endpoints are implemented here on purpose — this application is read-only
market data + analysis, never execution.

Reference: https://dhanhq.co/docs/v2/
Endpoints used:
    POST /v2/marketfeed/ltp        -> spot LTP
    POST /v2/marketfeed/quote      -> quote incl. OI/volume (used for futures)
    POST /v2/optionchain           -> option chain (strikes, OI, Greeks, IV)
    POST /v2/optionchain/expirylist -> expiry list for an instrument
    GET  <instrument master CSV>   -> resolves the current NIFTY futures
                                       security ID (see futures_data.py)

If DhanHQ changes these paths, update DhanClient only — nothing else in
the app should know about HTTP details.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from config import DhanCredentials, NIFTY_SECURITY_ID, NIFTY_SEGMENT

BASE_URL = "https://api.dhan.co/v2"


class DhanAPIError(Exception):
    """Raised for any non-2xx response or malformed payload from Dhan."""

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class DhanConnectionStatus:
    connected: bool
    message: str
    checked_at: float


class DhanClient:
    """
    Stateless-ish wrapper: one instance per set of credentials. Safe to
    reuse across a Streamlit session.
    """

    def __init__(self, credentials: DhanCredentials, timeout_seconds: float = 8.0):
        if not credentials.is_configured:
            raise DhanAPIError(
                "Dhan credentials are not configured. Set DHAN_CLIENT_ID and "
                "DHAN_ACCESS_TOKEN via Streamlit secrets or a .env file."
            )
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.credentials.access_token,
            "client-id": self.credentials.client_id,
        }

    def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE_URL}{path}"
        try:
            resp = requests.post(url, headers=self._headers(), json=body, timeout=self.timeout_seconds)
        except requests.exceptions.RequestException as exc:
            raise DhanAPIError(f"Network error calling {path}: {exc}") from exc

        if resp.status_code == 429:
            raise DhanAPIError("Dhan API rate limit hit (429). Back off and retry.", status_code=429)
        if resp.status_code >= 400:
            raise DhanAPIError(
                f"Dhan API error {resp.status_code} calling {path}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise DhanAPIError(f"Non-JSON response from {path}") from exc
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_connection(self) -> DhanConnectionStatus:
        try:
            self.get_expiry_list()
            return DhanConnectionStatus(True, "Connected", time.time())
        except DhanAPIError as exc:
            return DhanConnectionStatus(False, str(exc), time.time())

    def get_expiry_list(
        self,
        security_id: int = NIFTY_SECURITY_ID,
        segment: str = NIFTY_SEGMENT,
    ) -> list[str]:
        body = {"UnderlyingScrip": security_id, "UnderlyingSeg": segment}
        data = self._post("/optionchain/expirylist", body)
        return data.get("data", [])

    def get_option_chain(
        self,
        expiry: str,
        security_id: int = NIFTY_SECURITY_ID,
        segment: str = NIFTY_SEGMENT,
    ) -> dict:
        """
        Returns the raw Dhan option-chain payload:
        {
          "data": {
            "last_price": <spot>,
            "oc": {
              "<strike>": {
                "ce": {"last_price":..,"oi":..,"previous_oi":..,"volume":..,
                        "implied_volatility":..,"greeks":{"delta":..,"gamma":..,
                        "theta":..,"vega":..}},
                "pe": {...}
              }, ...
            }
          }
        }
        Parsing into typed rows happens in option_chain.py — this method
        stays a thin transport layer.
        """
        body = {"UnderlyingScrip": security_id, "UnderlyingSeg": segment, "Expiry": expiry}
        return self._post("/optionchain", body)

    def get_spot_ltp(
        self,
        security_id: int = NIFTY_SECURITY_ID,
        segment: str = NIFTY_SEGMENT,
    ) -> float:
        body = {segment: [security_id]}
        data = self._post("/marketfeed/ltp", body)
        try:
            return float(data["data"][segment][str(security_id)]["last_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DhanAPIError("Unexpected LTP payload shape", payload=data) from exc

    def get_quote(self, security_id: int, segment: str) -> dict:
        """
        Returns Dhan's fuller "quote" payload for one instrument — used
        for futures, since (unlike the index LTP feed) it includes OI and
        volume. Expected shape:
            {"data": {segment: {"<security_id>": {"last_price":.., "oi":..,
                                                    "volume":.., ...}}}}
        Raises DhanAPIError on any malformed/missing payload — callers
        (futures_data.py) are responsible for turning that into an
        "unavailable" FuturesSnapshot rather than crashing the app.
        """
        body = {segment: [security_id]}
        data = self._post("/marketfeed/quote", body)
        try:
            return data["data"][segment][str(security_id)]
        except (KeyError, TypeError) as exc:
            raise DhanAPIError("Unexpected quote payload shape", payload=data) from exc

    def get_instrument_master_csv_text(self, url: str, timeout_seconds: float = 20.0) -> str:
        """
        Fetches Dhan's public instrument master CSV (used by
        futures_data.resolve_nifty_futures_security_id to find the
        current-month NIFTY futures security ID dynamically, rather than
        hard-coding one that goes stale every expiry). This is a plain
        GET to a public file, not an authenticated API call — no
        access-token/client-id headers needed or sent.
        """
        try:
            resp = requests.get(url, timeout=timeout_seconds)
        except requests.exceptions.RequestException as exc:
            raise DhanAPIError(f"Network error fetching instrument master: {exc}") from exc
        if resp.status_code >= 400:
            raise DhanAPIError(f"Instrument master fetch failed with {resp.status_code}", status_code=resp.status_code)
        return resp.text

"""GoCardless Bank Account Data (Nordigen) API client.

PSD2-compliant bank connection for real-time transaction sync.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx

from openboek.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"


class GoCardlessClient:
    """Client for the GoCardless Bank Account Data API."""

    def __init__(self, secret_id: str | None = None, secret_key: str | None = None):
        self.secret_id = secret_id or getattr(settings, "gocardless_secret_id", "")
        self.secret_key = secret_key or getattr(settings, "gocardless_secret_key", "")
        self._access_token: str | None = None
        self._token_expires: datetime | None = None

    async def _get_token(self) -> str:
        """Get or refresh access token."""
        if self._access_token and self._token_expires and datetime.now() < self._token_expires:
            return self._access_token

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{BASE_URL}/token/new/",
                json={
                    "secret_id": self.secret_id,
                    "secret_key": self.secret_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access"]
            # Token usually valid for 24h; refresh at 23h
            from datetime import timedelta
            self._token_expires = datetime.now() + timedelta(hours=23)
            return self._access_token

    async def _headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def list_institutions(self, country: str = "NL") -> list[dict[str, Any]]:
        """List available banking institutions for a country."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/institutions/?country={country}",
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def create_requisition(
        self,
        institution_id: str,
        redirect_url: str,
        reference: str = "",
    ) -> dict[str, Any]:
        """Create a bank authorization requisition.

        Returns dict with 'id' (requisition_id) and 'link' (redirect URL for bank consent).
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{BASE_URL}/requisitions/",
                headers=await self._headers(),
                json={
                    "institution_id": institution_id,
                    "redirect": redirect_url,
                    "reference": reference,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def get_requisition(self, requisition_id: str) -> dict[str, Any]:
        """Get requisition status and linked accounts."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/requisitions/{requisition_id}/",
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_account_details(self, account_id: str) -> dict[str, Any]:
        """Get account metadata (IBAN, name, etc.)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/accounts/{account_id}/details/",
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_account_balances(self, account_id: str) -> dict[str, Any]:
        """Get account balances."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE_URL}/accounts/{account_id}/balances/",
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_transactions(
        self,
        account_id: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Fetch transactions for a connected account.

        Returns dict with 'booked' and 'pending' transaction lists.
        """
        params = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{BASE_URL}/accounts/{account_id}/transactions/",
                headers=await self._headers(),
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_requisition(self, requisition_id: str) -> bool:
        """Delete a requisition (disconnect bank)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{BASE_URL}/requisitions/{requisition_id}/",
                headers=await self._headers(),
            )
            return resp.status_code in (200, 204)


def is_configured() -> bool:
    """Check if GoCardless credentials are configured."""
    sid = getattr(settings, "gocardless_secret_id", "")
    skey = getattr(settings, "gocardless_secret_key", "")
    return bool(sid and skey)

"""Authenticated client for the EnergyTrak (Firebase/Firestore) backend.

EnergyTrak has no documented REST API. The mobile app signs in with a Firebase
email magic link and then reads the telemetry straight out of Firestore, so
that is exactly what this client does:

1. ``async_sign_in_with_magic_link`` pulls ``apiKey`` and ``oobCode`` out of
   the link the user received by email and exchanges them for an ID token plus
   a long-lived refresh token.
2. The refresh token is what gets persisted; the short-lived ID token is
   re-minted on demand from ``securetoken.googleapis.com``.
3. ``async_get_document`` / ``async_list_sites`` read the Firestore REST API
   with that ID token.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError, ClientSession

from .const import (
    FIRESTORE_BASE,
    IDENTITY_TOOLKIT_SIGNIN,
    SECURE_TOKEN_REFRESH,
    SITE_COLLECTION,
    TOKEN_EXPIRY_MARGIN,
)

_LOGGER = logging.getLogger(__name__)


class EnergyTrakError(Exception):
    """Base error for the EnergyTrak client."""


class EnergyTrakAuthError(EnergyTrakError):
    """Raised when credentials are missing, rejected or expired."""


class EnergyTrakConnectionError(EnergyTrakError):
    """Raised when the upstream service could not be reached."""


class EnergyTrakClient:
    """Minimal async client for EnergyTrak's Firebase backend."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        *,
        api_key: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self.email = email
        self.api_key = api_key
        self.refresh_token = refresh_token
        self.user_id: str | None = None
        self._id_token: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @staticmethod
    def parse_magic_link(magic_link: str) -> tuple[str, str]:
        """Return ``(api_key, oob_code)`` from a Firebase email sign-in link.

        The link the user pastes is often the *outer* redirect URL, in which
        case the real parameters live inside a nested ``continueUrl`` or
        ``link`` query parameter. Unwrap those before giving up.
        """
        candidates = [magic_link.strip()]
        seen: set[str] = set()

        while candidates:
            current = candidates.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)

            try:
                query = parse_qs(urlparse(current).query)
            except ValueError:
                continue

            api_key = next(iter(query.get("apiKey", [])), None)
            oob_code = next(iter(query.get("oobCode", [])), None)
            if api_key and oob_code:
                return api_key, oob_code

            # Follow nested links (Firebase Dynamic Links wrap the real URL).
            for key in ("continueUrl", "link", "url"):
                candidates.extend(query.get(key, []))

        raise EnergyTrakAuthError("invalid_magic_link")

    async def async_sign_in_with_magic_link(self, magic_link: str) -> None:
        """Exchange a magic link for an ID token and refresh token."""
        api_key, oob_code = self.parse_magic_link(magic_link)

        data = await self._async_request_json(
            "POST",
            IDENTITY_TOOLKIT_SIGNIN,
            params={"key": api_key},
            json={"email": self.email, "oobCode": oob_code},
            auth_call=True,
        )

        self.api_key = api_key
        self.refresh_token = data.get("refreshToken")
        self.user_id = data.get("localId")
        self._id_token = data.get("idToken")
        self._expires_at = time.time() + float(data.get("expiresIn", 3600))

        if not self.refresh_token or not self._id_token:
            raise EnergyTrakAuthError("invalid_auth")

        _LOGGER.debug("EnergyTrak sign-in succeeded for %s", self.email)

    async def async_refresh_token(self) -> None:
        """Mint a fresh ID token from the stored refresh token."""
        if not self.refresh_token or not self.api_key:
            raise EnergyTrakAuthError("missing_credentials")

        data = await self._async_request_json(
            "POST",
            SECURE_TOKEN_REFRESH,
            params={"key": self.api_key},
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            auth_call=True,
        )

        self._id_token = data.get("id_token") or data.get("idToken")
        self.refresh_token = (
            data.get("refresh_token") or data.get("refreshToken") or self.refresh_token
        )
        self.user_id = data.get("user_id") or self.user_id
        expires_in = data.get("expires_in") or data.get("expiresIn") or 3600
        self._expires_at = time.time() + float(expires_in)

        if not self._id_token:
            raise EnergyTrakAuthError("invalid_auth")

    async def async_get_token(self) -> str:
        """Return a valid ID token, refreshing it when needed."""
        if self._id_token and time.time() < self._expires_at - TOKEN_EXPIRY_MARGIN:
            return self._id_token
        await self.async_refresh_token()
        assert self._id_token is not None
        return self._id_token

    # ------------------------------------------------------------------
    # Firestore reads
    # ------------------------------------------------------------------

    async def async_get_document(self, collection: str, doc_id: str) -> dict[str, Any]:
        """Fetch a single Firestore document."""
        # Firestore reference values arrive as full resource paths; we only
        # ever want the trailing document id.
        clean_id = doc_id.split("/")[-1]
        token = await self.async_get_token()
        return await self._async_request_json(
            "GET",
            f"{FIRESTORE_BASE}/{collection}/{clean_id}",
            headers={"authorization": f"Bearer {token}"},
        )

    async def async_list_sites(self) -> list[dict[str, str]]:
        """List every site document the signed-in account can read."""
        sites: list[dict[str, str]] = []
        page_token: str | None = None

        while True:
            params: dict[str, str] = {"pageSize": "300"}
            if page_token:
                params["pageToken"] = page_token

            token = await self.async_get_token()
            data = await self._async_request_json(
                "GET",
                f"{FIRESTORE_BASE}/{SITE_COLLECTION}",
                params=params,
                headers={"authorization": f"Bearer {token}"},
            )

            for doc in data.get("documents") or []:
                site_id = str(doc.get("name", "")).split("/")[-1]
                if not site_id:
                    continue
                sites.append({"site_id": site_id, "name": _site_name(doc, site_id)})

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return sites

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _async_request_json(
        self,
        method: str,
        url: str,
        *,
        auth_call: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Perform a request and return the decoded JSON body."""
        try:
            response = await self._session.request(method, url, **kwargs)
            payload = await response.json(content_type=None)
        except ClientError as err:
            raise EnergyTrakConnectionError(str(err)) from err
        except ValueError as err:  # malformed JSON
            raise EnergyTrakConnectionError(f"invalid response from {url}") from err

        if response.status >= 400:
            message = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message", ""))
                elif error:
                    message = str(error)

            if auth_call or response.status in (401, 403):
                raise EnergyTrakAuthError(message or f"HTTP {response.status}")
            raise EnergyTrakConnectionError(message or f"HTTP {response.status}")

        if not isinstance(payload, dict):
            raise EnergyTrakConnectionError(f"unexpected response from {url}")
        return payload


def _site_name(doc: dict[str, Any], fallback: str) -> str:
    """Pull a human-readable name out of a raw Firestore site document."""
    fields = doc.get("fields") or {}
    for key in ("siteName", "name", "title", "displayName"):
        value = fields.get(key, {})
        if isinstance(value, dict) and isinstance(value.get("stringValue"), str):
            text = value["stringValue"].strip()
            if text:
                return text
    return fallback

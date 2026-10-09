"""ChatGPT account provider adapter (v0.1).

Concrete ``ProviderAdapter`` for a ChatGPT account. Discovery is
metadata-only. Body fetch happens in ``acquire()``.

Transport talks to ChatGPT's unofficial ``backend-api`` using the
protocol documented by ``brianjlacy/export-chatgpt`` (MIT). See
``tools/chatgpt-exporter/UPSTREAM.md``.

Production HTTP lives in ``ChatGPTHttpClient``. Tests inject a fake
client — no network, no secrets.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from nisaba.markdown import (
    conversation_to_markdown,
    extract_file_references,
)
from nisaba.timeutil import in_range, to_iso_utc


PROVIDER_ID = "chatgpt_account"
PROVIDER_KIND = "chatgpt"

API_BASE_DEFAULT = "https://chatgpt.com/backend-api"
SITE_BASE_DEFAULT = "https://chatgpt.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class ChatGPTAuthError(RuntimeError):
    """Token missing, expired, or rejected. Never includes the secret."""


class ChatGPTTransportError(RuntimeError):
    """Non-auth transport failure (network, 5xx, malformed payload)."""


# ── Injectable client ───────────────────────────────────────────

class ChatGPTClient(Protocol):
    """Minimal ChatGPT backend surface the adapter depends on."""

    def list_conversations(
        self,
        *,
        offset: int = 0,
        limit: int = 28,
        archived: bool = False,
        order: str = "updated",
    ) -> Dict[str, Any]:
        """Return ``{"items": [...], "has_more": bool}``.

        Each item: id, title, create_time, update_time, is_archived?.
        """
        ...

    def list_projects(self) -> List[Dict[str, Any]]:
        """Return project dicts with id, name, updated_at, files?."""
        ...

    def list_project_conversations(self, project_id: str) -> List[Dict[str, Any]]:
        """Return conversation listing items for one project.

        Always re-fetches. Never treats a prior 'indexingComplete' as
        authority.
        """
        ...

    def fetch_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Full conversation JSON (mapping + metadata)."""
        ...

    def download_file(
        self, file_id: str, conversation_id: str,
    ) -> Tuple[bytes, str, str]:
        """Return (bytes, filename, content_type)."""
        ...


# ── HTTP client ─────────────────────────────────────────────────

def _jwt_payload(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        import base64
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_account_id_from_jwt(token: str) -> Optional[str]:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        return None
    plan = auth.get("chatgpt_plan_type")
    if plan not in ("team", "enterprise"):
        return None
    acc = auth.get("chatgpt_account_id")
    return str(acc) if acc else None


def resolve_chatgpt_credentials() -> Dict[str, str]:
    """Read env. Never returns secret values in error messages."""
    bearer = (
        os.environ.get("LITOPS_CHATGPT_BEARER_TOKEN")
        or os.environ.get("CHATGPT_BEARER_TOKEN")
        or ""
    ).strip()
    session = (
        os.environ.get("LITOPS_CHATGPT_SESSION_TOKEN")
        or os.environ.get("CHATGPT_SESSION_TOKEN")
        or ""
    ).strip()
    account = (os.environ.get("LITOPS_CHATGPT_ACCOUNT_ID") or "").strip()
    return {"bearer": bearer, "session": session, "account": account}


class ChatGPTHttpClient:
    """urllib client for chatgpt.com/backend-api.

    Secrets stay in instance fields; ``repr`` / error strings never
    include token material.
    """

    def __init__(
        self,
        *,
        access_token: str,
        account_id: Optional[str] = None,
        api_base: str = API_BASE_DEFAULT,
        site_base: str = SITE_BASE_DEFAULT,
        throttle_seconds: float = 2.0,
        session_token: Optional[str] = None,
    ):
        if not access_token:
            raise ChatGPTAuthError("ChatGPT access token is empty")
        self._token = access_token
        self._session_token = session_token or None
        self._account_id = account_id or extract_account_id_from_jwt(access_token)
        self._api_base = api_base.rstrip("/")
        self._site_base = site_base.rstrip("/")
        self._throttle = max(0.0, float(throttle_seconds))
        self._last_request = 0.0
        self._last_refresh = time.monotonic()

    def __repr__(self) -> str:
        acc = "set" if self._account_id else "none"
        return f"ChatGPTHttpClient(account_id={acc}, api_base={self._api_base!r})"

    @classmethod
    def from_env(cls) -> "ChatGPTHttpClient":
        creds = resolve_chatgpt_credentials()
        throttle = float(os.environ.get("LITOPS_CHATGPT_THROTTLE_SECONDS", "2") or "2")
        api_base = os.environ.get("LITOPS_CHATGPT_API_BASE", API_BASE_DEFAULT)
        # Canonical: session cookie is exchanged for a fresh access token.
        # Bearer is fallback only. A present-but-expired bearer must not
        # shadow a usable session token.
        if creds["session"]:
            token = cls._exchange_session(creds["session"], api_base=api_base)
        else:
            token = creds["bearer"]
        if not token:
            raise ChatGPTAuthError(
                "missing ChatGPT credentials: set "
                "LITOPS_CHATGPT_SESSION_TOKEN or LITOPS_CHATGPT_BEARER_TOKEN"
            )
        account = creds["account"] or extract_account_id_from_jwt(token)
        return cls(
            access_token=token,
            account_id=account,
            api_base=api_base,
            throttle_seconds=throttle,
            session_token=creds["session"] or None,
        )

    def _refresh_access_token(self) -> bool:
        """Re-exchange the session cookie for a fresh access token.

        Returns True on success. The caller should retry the failed request
        once after a successful refresh.
        """
        if not self._session_token:
            return False
        try:
            new_token = self._exchange_session(
                self._session_token, api_base=self._api_base
            )
        except Exception:
            return False
        if not new_token:
            return False
        self._token = new_token
        if not self._account_id:
            self._account_id = extract_account_id_from_jwt(new_token)
        self._last_refresh = time.monotonic()
        return True

    @staticmethod
    def _session_cookie_header(session_token: str) -> str:
        """Build Cookie header. ChatGPT may split the session across .0/.1."""
        raw = (session_token or "").strip()
        if "|||" in raw:
            parts = [p.strip() for p in raw.split("|||") if p.strip()]
        else:
            parts = [raw] if raw else []
        if len(parts) >= 2:
            return "; ".join(
                f"__Secure-next-auth.session-token.{i}={part}"
                for i, part in enumerate(parts)
            )
        if parts:
            return f"__Secure-next-auth.session-token={parts[0]}"
        return ""

    @staticmethod
    def _exchange_session(session_token: str, *, api_base: str) -> str:
        """Cookie session -> access token via /api/auth/session.

        Cloudflare fingerprints urllib as a bot on this endpoint and returns
        403+challenge; curl_cffi with Chrome TLS impersonation gets a 200.
        backend-api itself is happy with urllib+Bearer, so only this call
        needs the impersonated transport.
        """
        try:
            from curl_cffi import requests as _crq
        except ImportError as e:
            raise ChatGPTAuthError(
                "curl_cffi not installed; run: python -m pip install --user curl_cffi"
            ) from e

        site = SITE_BASE_DEFAULT
        if "backend-api" in api_base:
            site = api_base.split("/backend-api")[0]
        url = f"{site}/api/auth/session"

        raw = (session_token or "").strip()
        if "|||" in raw:
            parts = [p.strip() for p in raw.split("|||") if p.strip()]
        else:
            parts = [raw] if raw else []
        if not parts:
            raise ChatGPTAuthError("session token is empty")
        if len(parts) >= 2:
            cookies = {
                f"__Secure-next-auth.session-token.{i}": p
                for i, p in enumerate(parts)
            }
        else:
            cookies = {"__Secure-next-auth.session-token": parts[0]}

        headers = {
            "Accept": "application/json",
            "Origin": site,
            "Referer": site + "/",
        }

        last_status: Optional[int] = None
        last_mitigated: str = ""
        for attempt, imp in enumerate(("chrome131", "chrome124", "chrome120")):
            try:
                r = _crq.get(
                    url,
                    cookies=cookies,
                    headers=headers,
                    impersonate=imp,
                    timeout=30,
                )
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                    continue
                raise ChatGPTTransportError(
                    f"session exchange network error: {type(e).__name__}"
                ) from None
            last_status = r.status_code
            last_mitigated = (r.headers.get("cf-mitigated") or "").lower()
            if r.status_code == 200:
                try:
                    body = r.json()
                except Exception as e:
                    raise ChatGPTAuthError(
                        f"session exchange returned non-JSON ({type(e).__name__})"
                    ) from None
                token = (body or {}).get("accessToken") or ""
                if not token:
                    raise ChatGPTAuthError(
                        "session token did not yield an access token"
                    )
                return str(token)
            if r.status_code == 401:
                raise ChatGPTAuthError("session token rejected (401)")
            if r.status_code == 403:
                if attempt < 2:
                    time.sleep(15 if attempt == 0 else 45)
                    continue
                raise ChatGPTAuthError(
                    f"session token rejected (403, cf-mitigated={last_mitigated or 'none'})"
                )
            if attempt < 2:
                time.sleep(5)
                continue
            raise ChatGPTTransportError(
                f"session exchange HTTP {r.status_code}"
            )
        raise ChatGPTAuthError(
            f"session token rejected ({last_status or 403}, cf-mitigated={last_mitigated or 'none'})"
        )

    def _headers(self) -> Dict[str, str]:
        # Origin/Referer are required: urllib has no browser same-origin
        # defaults, and Cloudflare 403s chatgpt.com/backend-api without them.
        # Node fetch in the donor inherits those from the page context.
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "Origin": self._site_base,
            "Referer": self._site_base + "/",
        }
        if self._account_id:
            headers["chatgpt-account-id"] = self._account_id
        return headers

    def _throttle_wait(self) -> None:
        if self._throttle <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        remaining = self._throttle - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _request_json(self, url: str, *, retries: int = 6, timeout: float = 120) -> Any:
        last_err: Optional[Exception] = None
        attempts = max(1, int(retries))
        auth_refreshed = False
        for attempt in range(attempts):
            self._throttle_wait()
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    self._last_request = time.monotonic()
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                self._last_request = time.monotonic()
                if e.code in (401, 403) and self._session_token and not auth_refreshed:
                    # access token expired (~1h) OR a stale bearer causing 403.
                    # Re-exchange the session cookie once and retry.
                    if self._refresh_access_token():
                        auth_refreshed = True
                        time.sleep(1)
                        continue
                if e.code in (401, 403):
                    raise ChatGPTAuthError(
                        f"ChatGPT authentication failed ({e.code})"
                    ) from None
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = int(retry_after) if (retry_after or "").isdigit() else min(60, 15 * (attempt + 1))
                    time.sleep(wait)
                    last_err = e
                    continue
                if e.code == 404:
                    raise ChatGPTTransportError("ChatGPT resource not found (404)") from None
                last_err = e
                if attempt == attempts - 1:
                    raise ChatGPTTransportError(f"ChatGPT HTTP {e.code}") from None
                time.sleep(min(45, 2 ** (attempt + 1)))
            except (
                urllib.error.URLError,
                TimeoutError,
                http.client.IncompleteRead,
                json.JSONDecodeError,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
            ) as e:
                self._last_request = time.monotonic()
                last_err = e
                if attempt == attempts - 1:
                    break
                time.sleep(min(45, 2 ** (attempt + 1)))
            except OSError as e:
                self._last_request = time.monotonic()
                last_err = e
                if attempt == attempts - 1:
                    break
                time.sleep(min(45, 2 ** (attempt + 1)))
        msg = str(last_err or "").lower()
        if isinstance(last_err, TimeoutError) or "timed out" in msg:
            raise ChatGPTTransportError("ChatGPT network error") from last_err
        if isinstance(last_err, json.JSONDecodeError):
            raise ChatGPTTransportError("ChatGPT returned non-JSON") from last_err
        if isinstance(last_err, http.client.IncompleteRead):
            raise ChatGPTTransportError("ChatGPT incomplete response") from last_err
        raise ChatGPTTransportError("ChatGPT request failed after retries") from last_err

    def list_conversations(
        self,
        *,
        offset: int = 0,
        limit: int = 28,
        archived: bool = False,
        order: str = "updated",
    ) -> Dict[str, Any]:
        flag = "true" if archived else "false"
        url = (
            f"{self._api_base}/conversations?offset={int(offset)}"
            f"&limit={int(limit)}&order={order}&is_archived={flag}"
        )
        data = self._request_json(url)
        items = data.get("items") or []
        has_more = bool(data.get("has_more")) or (
            isinstance(items, list) and len(items) == int(limit)
        )
        return {"items": items, "has_more": has_more}

    def list_projects(self) -> List[Dict[str, Any]]:
        """Always re-fetch. Ignores any donor indexingComplete flag."""
        projects: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen = set()
        while True:
            url = (
                f"{self._api_base}/gizmos/snorlax/sidebar"
                f"?owned_only=true&conversations_per_gizmo=0"
            )
            if cursor:
                from urllib.parse import quote
                url += f"&cursor={quote(cursor)}"
            data = self._request_json(url)
            for item in data.get("items") or []:
                g = (item.get("gizmo") or {}).get("gizmo") or item.get("gizmo") or {}
                if not isinstance(g, dict) or not g.get("id"):
                    continue
                pid = str(g["id"])
                if pid in seen:
                    continue
                seen.add(pid)
                display = g.get("display") or {}
                projects.append({
                    "id": pid,
                    "name": display.get("name") or g.get("name") or "Untitled Project",
                    "description": display.get("description") or "",
                    "updated_at": g.get("updated_at"),
                    "created_at": g.get("created_at"),
                    "files": (item.get("gizmo") or {}).get("files") or [],
                })
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return projects

    def list_project_conversations(self, project_id: str) -> List[Dict[str, Any]]:
        conversations: List[Dict[str, Any]] = []
        cursor: str = "0"
        seen = set()
        while True:
            from urllib.parse import quote
            url = (
                f"{self._api_base}/gizmos/{quote(project_id, safe='')}"
                f"/conversations?cursor={quote(cursor)}"
            )
            data = self._request_json(url)
            for conv in data.get("items") or []:
                cid = str(conv.get("id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                conversations.append(conv)
            nxt = data.get("cursor") or None
            if not nxt:
                break
            cursor = str(nxt)
        return conversations

    def fetch_conversation(self, conversation_id: str) -> Dict[str, Any]:
        from urllib.parse import quote
        url = f"{self._api_base}/conversation/{quote(conversation_id, safe='')}"
        data = self._request_json(url, retries=4, timeout=90)
        if not isinstance(data, dict):
            raise ChatGPTTransportError("conversation payload was not an object")
        return data

    def download_file(
        self, file_id: str, conversation_id: str,
    ) -> Tuple[bytes, str, str]:
        from urllib.parse import quote
        meta_url = (
            f"{self._api_base}/files/download/{quote(file_id, safe='')}"
            f"?conversation_id={quote(conversation_id)}&inline=false"
        )
        info = self._request_json(meta_url)
        if info.get("status") != "success" or not info.get("download_url"):
            code = info.get("error_code") or "unknown"
            raise ChatGPTTransportError(f"file download URL unavailable ({code})")
        download_url = str(info["download_url"])
        filename = str(info.get("file_name") or file_id)
        self._throttle_wait()
        req = urllib.request.Request(download_url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._last_request = time.monotonic()
                payload = resp.read()
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ChatGPTAuthError(f"file download auth failed ({e.code})") from None
            raise ChatGPTTransportError(f"file download HTTP {e.code}") from None
        return payload, filename, ctype


# ── Listing helpers ─────────────────────────────────────────────

def normalize_listing_item(
    raw: Dict[str, Any],
    *,
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Dict[str, Any]:
    cid = str(raw.get("id") or raw.get("conversation_id") or "")
    return {
        "conversation_id": cid,
        "title": raw.get("title") or "Untitled",
        "create_time": to_iso_utc(raw.get("create_time")),
        "update_time": to_iso_utc(raw.get("update_time")),
        "project_id": project_id or raw.get("gizmo_id") or raw.get("project_id"),
        "project_name": project_name,
        "archived": (
            bool(raw.get("is_archived")) if archived is None
            else bool(archived)
        ),
        "_raw": raw,
    }



# Patch 0001 — session refresh + curl_cffi exchange

Documents the deltas applied to the baseline ChatGPT transport
(`litops.acquisition.providers.chatgpt`, as of the mid-2026 revision in
the private `conceptarticle` repo) to produce `nisaba/client.py`.

If you fork Nisaba to track upstream changes in the baseline provider,
these are the only Nisaba-specific additions. Everything else in
`client.py` is upstream behavior with imports rewritten.

The patch has **five hunks**, each addresses one symptom encountered
during the 2026-09-30 → 2026-10-04 production run on an account with
1329 conversations.

---

## Hunk 1 — carry the session cookie on the client

**Symptom.** Bearer lives ~1 h. Baseline exchanges the cookie once in
`from_env`, keeps only the bearer. After an hour: `401` on every call,
nowhere to re-exchange without rebuilding the whole client.

**Fix.** Add a `session_token` field that persists for the client's
lifetime.

### `__init__`

```diff
         throttle_seconds: float = 2.0,
+        session_token: Optional[str] = None,
     ):
         if not access_token:
             raise ChatGPTAuthError("ChatGPT access token is empty")
         self._token = access_token
+        self._session_token = session_token or None
         self._account_id = account_id or extract_account_id_from_jwt(access_token)
         self._api_base = api_base.rstrip("/")
         self._site_base = site_base.rstrip("/")
         self._throttle = max(0.0, float(throttle_seconds))
         self._last_request = 0.0
+        self._last_refresh = time.monotonic()
```

### `from_env`

```diff
         return cls(
             access_token=token,
             account_id=account,
             api_base=api_base,
             throttle_seconds=throttle,
+            session_token=creds["session"] or None,
         )
```

---

## Hunk 2 — explicit refresh method

New method on `ChatGPTHttpClient`:

```python
def _refresh_access_token(self) -> bool:
    """Re-exchange the session cookie for a fresh access token.

    Returns True on success. The caller should retry the failed
    request once after a successful refresh.
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
```

---

## Hunk 3 — refresh-on-auth-failure in `_request_json`

**Symptom.** Baseline raises `ChatGPTAuthError` on any 401 OR 403. In
practice 403 also fires when the bearer has silently expired (OpenAI
sometimes returns 403 for expired-token instead of 401).

**Fix.** On 401 OR 403, if we have a session cookie and have not already
refreshed in this call, re-exchange and retry once.

```diff
     def _request_json(self, url: str, *, retries: int = 6, timeout: float = 120) -> Any:
         last_err: Optional[Exception] = None
         attempts = max(1, int(retries))
+        auth_refreshed = False
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
+                if e.code in (401, 403) and self._session_token and not auth_refreshed:
+                    # access token expired (~1h) OR a stale bearer causing 403.
+                    # Re-exchange the session cookie once and retry.
+                    if self._refresh_access_token():
+                        auth_refreshed = True
+                        time.sleep(1)
+                        continue
                 if e.code in (401, 403):
                     raise ChatGPTAuthError(
                         f"ChatGPT authentication failed ({e.code})"
                     ) from None
```

---

## Hunk 4 — `_exchange_session` via curl_cffi

**Symptom.** `/api/auth/session` is Cloudflare-protected. urllib's TLS
fingerprint is unmistakable Python; CF returns
`403 cf-mitigated: challenge`. Browser works, our code does not.

**Fix.** Use `curl_cffi.requests` with `impersonate="chrome131"` for
this one endpoint. The rest of the backend-api accepts urllib + Bearer
without issue — do NOT globally replace the transport.

### Baseline (urllib, breaks on CF)

```python
@staticmethod
def _exchange_session(session_token: str, *, api_base: str) -> str:
    site = SITE_BASE_DEFAULT
    if "backend-api" in api_base:
        site = api_base.split("/backend-api")[0]
    url = f"{site}/api/auth/session"
    cookie = ChatGPTHttpClient._session_cookie_header(session_token)
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": site,
        "Referer": site + "/",
        "Cookie": cookie,
    }
    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ChatGPTAuthError(f"session token rejected ({e.code})") from None
            if e.code == 403:
                if attempt < 2:
                    time.sleep(15 if attempt == 0 else 45)
                    continue
                raise ChatGPTAuthError(f"session token rejected ({e.code})") from None
            raise ChatGPTTransportError(f"session exchange HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise ChatGPTTransportError("session exchange network error") from e
    token = (body or {}).get("accessToken") or ""
    if not token:
        raise ChatGPTAuthError("session token did not yield an access token")
    return str(token)
```

### Nisaba (curl_cffi, Chrome 131 TLS)

```python
@staticmethod
def _exchange_session(session_token: str, *, api_base: str) -> str:
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
    parts = [p.strip() for p in raw.split("|||") if p.strip()] if "|||" in raw else ([raw] if raw else [])
    if not parts:
        raise ChatGPTAuthError("session token is empty")
    if len(parts) >= 2:
        cookies = {f"__Secure-next-auth.session-token.{i}": p for i, p in enumerate(parts)}
    else:
        cookies = {"__Secure-next-auth.session-token": parts[0]}

    headers = {
        "Accept": "application/json",
        "Origin": site,
        "Referer": site + "/",
    }

    last_status, last_mitigated = None, ""
    for attempt, imp in enumerate(("chrome131", "chrome124", "chrome120")):
        try:
            r = _crq.get(url, cookies=cookies, headers=headers,
                         impersonate=imp, timeout=30)
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
            body = r.json()
            token = (body or {}).get("accessToken") or ""
            if not token:
                raise ChatGPTAuthError("session token did not yield an access token")
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
            time.sleep(5); continue
        raise ChatGPTTransportError(f"session exchange HTTP {r.status_code}")
    raise ChatGPTAuthError(
        f"session token rejected ({last_status or 403}, cf-mitigated={last_mitigated or 'none'})"
    )
```

Three impersonations (131 → 124 → 120) are tried in order: when Chrome
major rolls over Cloudflare takes a few days to recognize the new
fingerprint. Having one older fallback avoids a hard outage during that
window.

---

## Hunk 5 — remove dotenv auto-load

**Symptom.** A stray `.env.local` in the current directory silently
overrode `LITOPS_CHATGPT_SESSION_TOKEN` set in the process environment,
pointing the archiver at a months-old cookie.

**Fix.** Drop the `ensure_dotenv_loaded()` call from `from_env`. If the
operator wants dotenv behavior, they can call it themselves before
`from_env`; Nisaba does not take that decision silently.

```diff
     @classmethod
     def from_env(cls) -> "ChatGPTHttpClient":
-        from litops.config import ensure_dotenv_loaded
-        ensure_dotenv_loaded()
         creds = resolve_chatgpt_credentials()
```

(Nisaba also sets `LITOPS_SKIP_DOTENV=1` by convention in the launcher
scripts — belt and braces.)

---

## Verification

After applying hunks 1-4 to a baseline that still has the urllib
exchange from hunk 5-before, the following production run reaches
`full_archive_complete: True`:

```
PASS 1 discover
discover regular=1066 archived=3 project_convs=902 unique=1329 projects=71 pagination_complete=True
PASS 2 conversation JSON
... (many hours, with transparent refreshes every ~hour)
PASS indexes
STATUS COMPLETE
FULL_ARCHIVE: COMPLETE
```

Without the hunks, the same run fails with `BLOCKED_AUTH` within an hour.

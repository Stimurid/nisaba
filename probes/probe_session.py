"""One-shot probe: what does /api/auth/session return with the current cookie?
Prints only HTTP code, response headers, and first 400 chars of body.
Redacts anything JWT-like before printing. Never prints the input cookie.
"""
import json, os, re, sys, urllib.request, urllib.error
from pathlib import Path

TOKEN_FILE = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?")

def redact(s: str) -> str:
    return JWT_RE.sub("<REDACTED-JWT>", s)

raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
if "|||" in raw:
    parts = [p.strip() for p in raw.split("|||") if p.strip()]
else:
    parts = [raw]
print(f"token parts: {len(parts)}, total len={sum(len(p) for p in parts)}")

if len(parts) >= 2:
    cookie = "; ".join(f"__Secure-next-auth.session-token.{i}={p}" for i,p in enumerate(parts))
else:
    cookie = f"__Secure-next-auth.session-token={parts[0]}"

headers = {
    "Accept": "application/json",
    "User-Agent": UA,
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
    "Cookie": cookie,
}

req = urllib.request.Request("https://chatgpt.com/api/auth/session", headers=headers, method="GET")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"HTTP {resp.status}")
        for k, v in resp.getheaders():
            if k.lower() in ("content-type","cf-mitigated","cf-ray","cf-cache-status","server","set-cookie"):
                # never print set-cookie value contents
                if k.lower() == "set-cookie":
                    print(f"  {k}: <set-cookie present, {len(v)} bytes>")
                else:
                    print(f"  {k}: {v}")
        body = resp.read().decode("utf-8", errors="replace")
        print(f"body length: {len(body)}")
        try:
            data = json.loads(body)
            keys = list(data.keys()) if isinstance(data, dict) else "not-a-dict"
            print(f"json keys: {keys}")
            if isinstance(data, dict):
                if "accessToken" in data:
                    tok = data["accessToken"] or ""
                    print(f"accessToken present: yes (len={len(tok)})")
                else:
                    print("accessToken present: NO")
                if "user" in data:
                    u = data["user"] or {}
                    print(f"user.email present: {'yes' if u.get('email') else 'no'}")
                    print(f"user.name  present: {'yes' if u.get('name') else 'no'}")
                if "expires" in data:
                    print(f"expires: {data['expires']}")
        except Exception as e:
            print(f"json parse error: {type(e).__name__}: {e}")
            print("body head (redacted):")
            print(redact(body[:400]))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code} {e.reason}")
    if e.headers:
        for k, v in e.headers.items():
            if k.lower() in ("content-type","cf-mitigated","cf-ray","cf-cache-status","server"):
                print(f"  {k}: {v}")
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    print(f"body length: {len(body)}")
    print("body head (redacted, 400 chars):")
    print(redact(body[:400]))
except urllib.error.URLError as e:
    print(f"URLError: {e}")

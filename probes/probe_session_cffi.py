"""Retry /api/auth/session using curl_cffi with Chrome TLS impersonation."""
import json, re
from pathlib import Path
from curl_cffi import requests as crq

TOKEN_FILE = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?")
def redact(s): return JWT_RE.sub("<JWT>", s)

raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
parts = [p.strip() for p in raw.split("|||") if p.strip()] if "|||" in raw else [raw]
if len(parts) >= 2:
    cookies = {f"__Secure-next-auth.session-token.{i}": p for i, p in enumerate(parts)}
else:
    cookies = {"__Secure-next-auth.session-token": parts[0]}

headers = {
    "Accept": "application/json",
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/",
}

for imp in ("chrome131", "chrome124", "chrome120", "chrome"):
    try:
        r = crq.get("https://chatgpt.com/api/auth/session",
                    cookies=cookies, headers=headers, impersonate=imp, timeout=30)
        print(f"impersonate={imp}: HTTP {r.status_code}  cf-mitigated={r.headers.get('cf-mitigated','-')}  cf-ray={r.headers.get('cf-ray','-')}")
        body = r.text
        if r.status_code == 200:
            try:
                data = json.loads(body)
                print(f"  json keys: {list(data.keys()) if isinstance(data, dict) else '(not dict)'}")
                if isinstance(data, dict):
                    tok = data.get("accessToken") or ""
                    email = (data.get("user") or {}).get("email") or ""
                    print(f"  accessToken len={len(tok)}, user.email set={'yes' if email else 'no'}, expires={data.get('expires')}")
            except Exception as e:
                print(f"  json parse: {e}")
                print("  body head:", redact(body[:300]))
            break
        else:
            print(f"  body head: {redact(body[:200])}")
    except Exception as e:
        print(f"impersonate={imp}: EXC {type(e).__name__}: {e}")

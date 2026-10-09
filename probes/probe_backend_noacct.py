"""Same as probe_backend but withOUT chatgpt-account-id header — mimicking the
worker which skips it for plus/free plans."""
import json, urllib.request, urllib.error
from pathlib import Path
from curl_cffi import requests as crq

TOKEN_FILE = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
parts = [p.strip() for p in raw.split("|||") if p.strip()] if "|||" in raw else [raw]
cookies = {f"__Secure-next-auth.session-token.{i}": p for i, p in enumerate(parts)} if len(parts) >= 2 else {"__Secure-next-auth.session-token": parts[0]}

r = crq.get("https://chatgpt.com/api/auth/session",
            cookies=cookies, headers={"Accept":"application/json","Origin":"https://chatgpt.com","Referer":"https://chatgpt.com/"},
            impersonate="chrome131", timeout=30)
bearer = r.json()["accessToken"]

headers_urllib = {
    "Accept":"application/json","Content-Type":"application/json",
    "Authorization": f"Bearer {bearer}", "User-Agent": UA,
    "Origin":"https://chatgpt.com","Referer":"https://chatgpt.com/",
}

url = "https://chatgpt.com/backend-api/conversations?offset=0&limit=28&order=updated"
print("test A: urllib + Bearer, no chatgpt-account-id  (== what worker does for plus plan)")
try:
    req = urllib.request.Request(url, headers=headers_urllib, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
        print(f"  HTTP {resp.status}  items={len(body.get('items', []))}  total={body.get('total')}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code} {e.reason}  cf-mitigated={e.headers.get('cf-mitigated') if e.headers else '-'}")
    body = e.read()
    print(f"  body head: {body[:200].decode('utf-8', 'replace')}")

print("\ntest B: curl_cffi + Bearer, no chatgpt-account-id")
r2 = crq.get(url, headers=headers_urllib, impersonate="chrome131", timeout=30)
print(f"  HTTP {r2.status_code}  cf-mitigated={r2.headers.get('cf-mitigated','-')}")
if r2.status_code == 200:
    body = r2.json()
    items = body.get('items', [])
    print(f"  items={len(items)}  total={body.get('total')}  has_more={body.get('has_more')}")

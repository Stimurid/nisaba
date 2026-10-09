"""After exchanging session -> accessToken via curl_cffi, try backend-api with:
  (a) urllib + Bearer  -- if this works, we only need to patch exchange
  (b) curl_cffi + Bearer -- fallback if urllib gets challenged
"""
import json, re, urllib.request, urllib.error
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
assert r.status_code == 200, f"session exchange failed: {r.status_code}"
data = r.json()
bearer = data["accessToken"]
account = None
try:
    import base64
    payload = json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "===").decode())
    account = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    plan = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
    print(f"plan={plan}  account_id={'set' if account else 'none'}")
except Exception as e:
    print(f"jwt decode: {e}")

headers = {
    "Accept":"application/json","Content-Type":"application/json",
    "Authorization": f"Bearer {bearer}", "User-Agent": UA,
    "Origin":"https://chatgpt.com","Referer":"https://chatgpt.com/",
}
if account: headers["chatgpt-account-id"] = account

url = "https://chatgpt.com/backend-api/conversations?offset=0&limit=1&order=updated"

print("\n(a) urllib + Bearer:")
try:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"  HTTP {resp.status}")
        body = resp.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(body)
            print(f"  items: {len(j.get('items', []))}, total: {j.get('total')}, has_more: {j.get('has_more')}")
        except Exception:
            print("  body head:", body[:200])
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code} {e.reason}  cf-mitigated={e.headers.get('cf-mitigated') if e.headers else '-'}")

print("\n(b) curl_cffi + Bearer:")
r2 = crq.get(url, headers=headers, impersonate="chrome131", timeout=30)
print(f"  HTTP {r2.status_code}  cf-mitigated={r2.headers.get('cf-mitigated','-')}")
if r2.status_code == 200:
    j = r2.json()
    items = j.get('items', [])
    print(f"  items: {len(items)}, total: {j.get('total')}, has_more: {j.get('has_more')}")
    if items:
        it = items[0]
        # only print harmless meta
        print(f"  first item title-length: {len(it.get('title') or '')}, update_time: {it.get('update_time')}")

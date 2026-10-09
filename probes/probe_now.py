"""What exactly do we get when we call backend-api RIGHT NOW with current creds?
Try: urllib+bearer (what worker uses), curl_cffi+bearer (what probe showed working).
Report HTTP code + cf-mitigated header + body head.
"""
import os, sys, json, urllib.request, urllib.error, re
from pathlib import Path
os.environ["LITOPS_SKIP_DOTENV"] = "1"
sys.path.insert(0, r"C:\projects\conceptarticle")

from curl_cffi import requests as crq
from litops.acquisition.providers.chatgpt import ChatGPTHttpClient

TOKEN_FILE = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt")
os.environ["LITOPS_CHATGPT_SESSION_TOKEN"] = TOKEN_FILE.read_text(encoding="utf-8").strip()

# Fresh exchange via patched client
try:
    c = ChatGPTHttpClient.from_env()
    bearer = c._token
    print(f"session exchange OK, bearer len={len(bearer)}")
except Exception as e:
    print(f"session exchange FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
headers = {
    "Accept":"application/json","Content-Type":"application/json",
    "Authorization": f"Bearer {bearer}", "User-Agent": UA,
    "Origin":"https://chatgpt.com","Referer":"https://chatgpt.com/",
}
if c._account_id:
    headers["chatgpt-account-id"] = c._account_id

# 1) backend list conversations with urllib+Bearer
url_list = "https://chatgpt.com/backend-api/conversations?offset=0&limit=5&order=updated"
print("\n[A] urllib + Bearer /conversations")
try:
    req = urllib.request.Request(url_list, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"  HTTP {resp.status}")
        body = json.loads(resp.read().decode())
        print(f"  items: {len(body.get('items',[]))}  total: {body.get('total')}")
except urllib.error.HTTPError as e:
    cf = (e.headers.get("cf-mitigated") if e.headers else "") or "-"
    ctype = (e.headers.get("Content-Type") if e.headers else "") or "-"
    body = e.read()[:200].decode(errors="replace") if hasattr(e, "read") else ""
    print(f"  HTTP {e.code} {e.reason}  cf-mitigated={cf}  content-type={ctype}")
    print(f"  body: {body}")

# 2) backend list with curl_cffi + Bearer
print("\n[B] curl_cffi(chrome131) + Bearer /conversations")
r = crq.get(url_list, headers=headers, impersonate="chrome131", timeout=30)
cf = r.headers.get("cf-mitigated","-")
print(f"  HTTP {r.status_code}  cf-mitigated={cf}")
if r.status_code != 200:
    print(f"  body: {r.text[:200]}")
else:
    body = r.json()
    print(f"  items: {len(body.get('items',[]))}  total: {body.get('total')}")

# 3) try a conversation body fetch (what archiver does in PASS 2 or indexes verify)
url_conv = "https://chatgpt.com/backend-api/conversation/00000000-0000-0000-0000-000000000000"
print("\n[C] urllib + Bearer /conversation/<fake-uuid> (expect 404 or auth error)")
try:
    req = urllib.request.Request(url_conv, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"  HTTP {resp.status}")
except urllib.error.HTTPError as e:
    cf = (e.headers.get("cf-mitigated") if e.headers else "") or "-"
    print(f"  HTTP {e.code} {e.reason}  cf-mitigated={cf}")

"""Check what my patched _exchange_session returns, and compare with raw curl_cffi."""
import base64, json, sys
from pathlib import Path
sys.path.insert(0, r"C:\projects\conceptarticle")

from curl_cffi import requests as crq
from litops.acquisition.providers.chatgpt import ChatGPTHttpClient, extract_account_id_from_jwt

TOKEN_FILE = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt")
raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
parts = [p.strip() for p in raw.split("|||") if p.strip()] if "|||" in raw else [raw]
cookies = {f"__Secure-next-auth.session-token.{i}": p for i, p in enumerate(parts)} if len(parts) >= 2 else {"__Secure-next-auth.session-token": parts[0]}

r = crq.get("https://chatgpt.com/api/auth/session",
            cookies=cookies, headers={"Accept":"application/json","Origin":"https://chatgpt.com","Referer":"https://chatgpt.com/"},
            impersonate="chrome131", timeout=30)
print(f"raw curl_cffi: HTTP {r.status_code}")
body = r.json()
print(f"  json keys: {list(body.keys())}")
tok_body = body.get("accessToken") or ""
sess_body = body.get("sessionToken") or ""
print(f"  accessToken  len={len(tok_body)}  prefix={tok_body[:20]}...")
print(f"  sessionToken len={len(sess_body)}  prefix={sess_body[:20]}...")

# Decode both JWT-ish payloads if possible
for name, t in [("accessToken", tok_body), ("sessionToken", sess_body)]:
    try:
        parts_j = t.split(".")
        if len(parts_j) >= 2:
            pad = "=" * (-len(parts_j[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts_j[1] + pad).decode())
            exp = payload.get("exp")
            plan = (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
            print(f"  {name} payload: exp={exp}  plan={plan}  keys={list(payload.keys())[:6]}")
    except Exception as e:
        print(f"  {name} decode: {e}")

# Now what does patched _exchange_session return?
tok2 = ChatGPTHttpClient._exchange_session(raw, api_base="https://chatgpt.com/backend-api")
print(f"\npatched _exchange_session returned token len={len(tok2)}  prefix={tok2[:20]}...")
print(f"  == accessToken from raw?  {tok2 == tok_body}")
print(f"  == sessionToken from raw? {tok2 == sess_body}")

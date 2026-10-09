"""Verify the patched client:
  1. imports cleanly
  2. exchanges session -> bearer A
  3. explicitly refreshes -> bearer B (different access token, same session)
  4. makes a backend-api call that succeeds with the refreshed token
"""
import os, sys
from pathlib import Path
os.environ["LITOPS_SKIP_DOTENV"] = "1"
os.environ["LITOPS_CHATGPT_THROTTLE_SECONDS"] = "1"
os.environ["LITOPS_CHATGPT_SESSION_TOKEN"] = Path(
    r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt"
).read_text(encoding="utf-8").strip()

sys.path.insert(0, r"C:\projects\conceptarticle")
from litops.acquisition.providers.chatgpt import ChatGPTHttpClient

c = ChatGPTHttpClient.from_env()
print(f"session_token stored on client: {bool(c._session_token)}")
tok_a = c._token
print(f"initial access_token len: {len(tok_a)}")

# force a refresh
ok = c._refresh_access_token()
print(f"_refresh_access_token returned: {ok}")
tok_b = c._token
print(f"refreshed access_token len: {len(tok_b)}")
print(f"token changed: {tok_a != tok_b}")

# make a backend-api call to confirm the refreshed bearer is valid
page = c.list_conversations(offset=0, limit=1, archived=False, order="updated")
print(f"list_conversations after refresh: items={len(page.get('items', []))} total={page.get('total')}")

"""Reproduce what the worker does at PASS 1 discover, with tracebacks."""
import os, sys, traceback
os.environ["LITOPS_CHATGPT_THROTTLE_SECONDS"] = "1"
from pathlib import Path
os.environ["LITOPS_CHATGPT_SESSION_TOKEN"] = Path(r"C:\Users\Homee\Downloads\chatgpt-archive-launcher\token.txt").read_text(encoding="utf-8").strip()

sys.path.insert(0, r"C:\projects\conceptarticle")
from litops.acquisition.providers.chatgpt import (
    ChatGPTHttpClient, ChatGPTAuthError, ChatGPTTransportError
)

try:
    c = ChatGPTHttpClient.from_env()
    print("client OK, account_id set:", bool(c._account_id))
    print("token len:", len(c._token))
    print("first list_conversations...")
    page = c.list_conversations(offset=0, limit=5, archived=False, order="updated")
    items = page.get("items", [])
    print(f"items: {len(items)}  total: {page.get('total')}  has_more: {page.get('has_more')}")
    for it in items[:3]:
        print(" -", it.get("id"), "update_time:", it.get("update_time"))

    print("\nsecond: list_projects...")
    projs = c.list_projects()
    print(f"projects: {len(projs)}")
except ChatGPTAuthError as e:
    print("AUTH:", e)
    traceback.print_exc()
except ChatGPTTransportError as e:
    print("TRANSPORT:", e)
    traceback.print_exc()
except Exception as e:
    print("OTHER:", type(e).__name__, e)
    traceback.print_exc()

# Nisaba

Named after the Sumerian goddess of writing and grain accounting — Nisaba
makes a **full, resumable, offline-readable cold archive** of a ChatGPT
account: every conversation (JSON + Markdown), every project (manifest +
files + instructions), every attachment that OpenAI still has, plus
indexes and a signed completeness report.

Rescues both personal archives and research corpora when OpenAI's own
export (the "we'll email you a link" one) silently fails or stalls, as it
did for the author across August–October 2026. Nisaba has been running
against a Plus-plan account with 1329 conversations, 71 projects,
~13 000 attachments and ~12 GB of material.

> Nisaba is pronounced **Ni-SAH-ba**. She measures and records. She does
> not interpret, delete, or re-order the record.

## Status

- **Transport** — session-cookie auth (not Bearer), auto-refresh on 401/403,
  Cloudflare TLS fingerprint via `curl_cffi` for the exchange endpoint only.
- **Verified** — 12.4 GB production archive, `full_archive_complete: True`,
  all 12 invariants PASS.
- **Platform** — primary target Windows + PowerShell (where the operator
  lives). Core Python is portable; the `scripts/*.ps1` launcher wraps it
  for Windows scheduled tasks. Linux/macOS runs via `python -m nisaba`
  directly.
- **Python** — 3.11+. Tested on 3.14.

## What it produces

```
<output>/
├── conversations/<uuid>/
│   ├── conversation.json          # authoritative raw body (OpenAI shape)
│   ├── conversation.md            # rendered markdown with YAML frontmatter
│   ├── metadata.json              # our metadata (sha256, bytes, acquired_at)
│   └── files/                     # attachments from this chat
├── projects/<pid>__<slug>/
│   ├── project.json               # project meta
│   ├── conversations.json         # list of this project's conversations
│   ├── instructions.txt           # system prompt
│   └── files/                     # project files
├── conversations.jsonl            # 1 line per conversation with project_id + memberships
├── projects.json                  # all projects + conversation counts
├── discovery.json
├── account_manifest.json          # oldest/newest timestamps, counts, invariants
├── files.jsonl                    # 1 line per file (ok and failed)
├── errors.jsonl
├── progress.json                  # resume checkpoint
└── reports/
    ├── conversation_index.json, project_index.json, file_index.json,
    │   failed_index.json
    └── final_report.md            # written only when the full run completes
```

The `conversation.json` is **source-faithful** — exactly what the OpenAI
backend returns, with the raw `mapping` tree (not flattened). The
`.md` is a convenience render.

## Install

```powershell
# Windows (PowerShell)
python -m pip install --user curl_cffi
# (that is the only non-stdlib dep)
```

```bash
# Linux / macOS
pip install curl_cffi
```

No `setup.py` yet; use `PYTHONPATH=.../nisaba/src` or `pip install -e .`
once pyproject lands.

## Run

**Step 1 — session cookie.** See [`docs/BRAVE_COOKIE.md`](docs/BRAVE_COOKIE.md)
for extracting `__Secure-next-auth.session-token` from Chrome/Brave DevTools
(2 clicks). The cookie lasts weeks.

**Step 2 — set env + run.**

```powershell
# PowerShell
$env:LITOPS_CHATGPT_SESSION_TOKEN = (Get-Content token.txt -Raw).Trim()
$env:LITOPS_SKIP_DOTENV = "1"        # avoid a dotenv override gotcha (see TROUBLESHOOTING)
$env:PYTHONPATH = "src"
python -m nisaba --output D:\archives\chatgpt\primary --resume --include-archived --include-projects --include-files
```

```bash
# bash
export LITOPS_CHATGPT_SESSION_TOKEN="$(cat token.txt)"
export LITOPS_SKIP_DOTENV=1
export PYTHONPATH=src
python -m nisaba --output ~/archives/chatgpt/primary --resume --include-archived --include-projects --include-files
```

**Step 3 — pre-canned Windows launcher.** See [`scripts/run-archive.ps1`](scripts/run-archive.ps1)
which registers a Windows Scheduled Task (`ChatGPTFullArchive`) so the
worker outlives your shell. `scripts/watchdog.ps1` sends a toast on
finish or crash and auto-restarts on worker death.

**Monitoring:** `python -m nisaba --output <...> --verify-only --json-status`
re-reads `progress.json` without API calls and prints invariants.

Expect ~4–8 hours of wall-clock on a Plus-plan account with 1000+ chats
(1 sec throttle × ~13 000 API calls, with retries for transient 500s).
Resume is cheap — new runs skip anything already on disk.

## Status flags, briefly

- `conversations_complete` / `conversations_pending` / `conversations_failed`
  — JSON fetch status per chat.
- `files_complete` / `files_failed` — includes both project files and
  attachments. Expect ~60 % failure on legacy attachments: OpenAI returns
  500 on many old files, 404 on deleted ones, 403 on expired pre-signed
  URLs. See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).
- `attachment_discovery_status` — `ATTACHMENT_DISCOVERY_COMPLETE` after
  the first full pass, else `_PARTIAL`.
- `full_archive_complete` in `final_report.md` — only `True` when all 12
  invariants pass.

## For AI agents using this tool

Read [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) **first**. It describes:

- exactly what to ask the human operator (and what *not* to ask),
- how to pick an output directory,
- how to recover from `BLOCKED_AUTH` without re-doing the ACQUIRE,
- how to classify terminal statuses (`DELETED_REMOTE`, `INACCESSIBLE`,
  `FAILED_FINAL`).

## Troubleshooting

[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — ten non-obvious
failure modes discovered during the Oct 2026 bring-up (Cyrillic paths,
em-dash parsing in PS 5.1, dotenv override, Cloudflare TLS, bearer
expiry on 403, legacy-file 500s, …).

## Known limits

- One account per run (output dir is per-account).
- No automatic daily cron included — pair with Windows Task Scheduler or
  cron. `scripts/run-archive.ps1` is a template.
- No concurrent download — single worker + 1 s throttle (deliberately
  conservative; OpenAI is quick to Cloudflare-challenge heavy fingerprints).
- Markdown render is lossy by design (full fidelity lives in
  `conversation.json`).

## Credits

- Protocol lineage: `brianjlacy/export-chatgpt` (MIT).
- Session-cookie → bearer exchange: `curl_cffi` with Chrome 131 TLS
  impersonation.
- Build / debugging history: see each repo file's history and
  `patches/0001-session-refresh-and-curl-cffi.diff`.

## License

MIT. See [`LICENSE`](LICENSE).

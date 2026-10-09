# Troubleshooting

Ten non-obvious failure modes discovered while bringing Nisaba up on a
production Windows + Plus-plan account across 2026-09-30 → 2026-10-04.
If something stalls, scan this list first — the fix is almost always
here.

## 1. Cyrillic (or any non-ASCII) in the archiver's working path

**Symptom.** Worker crashes immediately with `[Errno 22] Invalid
argument`. Appears when the launcher wrote `worker_launch.cmd` under a
path containing non-ASCII characters (e.g. a Russian folder name under
`Documents/`).

**Why.** `cmd.exe` on Windows reads `.cmd` scripts in the OEM code page
(cp866/cp1251 on Russian systems). When the launcher wrote the file with
`-Encoding ASCII`, every non-ASCII character became `?`, and the
subsequent Python invocation received a nonexistent path.

**Fix.** Keep **the archiver's own repo path** pure ASCII. The archive
*output* path may contain anything (Python handles UTF-16 Win32 APIs
fine). Nisaba's default install path `~/code/nisaba` is ASCII-safe.

## 2. em-dash in `.ps1` under Windows PowerShell 5.1

**Symptom.** `powershell -File run-archive.ps1` fails to parse: "The
string is missing the terminator", with squiggles under a `)`.

**Why.** Windows PowerShell 5.1 reads scripts in Windows-1252 unless the
file has a UTF-8 BOM. A single em-dash (`—`, U+2014) in a comment
decodes to two Latin-1 characters, one of which happens to be a quote,
and the parser derails for the rest of the file.

**Fix.** Either (a) save `.ps1` files as **UTF-8 with BOM**, or (b)
restrict all `.ps1` content to pure ASCII (safer across editors). Nisaba
scripts are ASCII-only on purpose.

## 3. `/api/auth/session` returns `403 cf-mitigated: challenge`

**Symptom.** `_exchange_session` fails with "session token rejected
(403, cf-mitigated=challenge)". The same cookie works fine in the
browser.

**Why.** Cloudflare fingerprints the TLS handshake. `urllib`'s
fingerprint is unmistakable Python; CF rejects.

**Fix.** Nisaba uses `curl_cffi` with `impersonate="chrome131"` for this
one endpoint only. Install: `pip install --user curl_cffi`. The
backend-api itself (`/backend-api/...`) accepts `urllib` + Bearer
without issue, so the rest of the client is pure stdlib.

If `curl_cffi` is not installed, Nisaba raises `ChatGPTAuthError` with
"`curl_cffi not installed; run: python -m pip install --user curl_cffi`".

## 4. `.env.local` silently overrides the process environment

**Symptom.** You set `LITOPS_CHATGPT_SESSION_TOKEN` in your shell, run
the worker, and it uses a *different* (stale) cookie. Instant
`BLOCKED_AUTH`.

**Why.** An older variant of this code called `ensure_dotenv_loaded()`
on import. That function loaded `.env` with `override=False` **but
`.env.local` with `override=True`** — meaning a stray `.env.local` in
the current directory wins over your shell. If `.env.local` contains an
expired `LITOPS_CHATGPT_SESSION_TOKEN=` from months ago, every run uses
that one.

**Fix.** Nisaba does **not** call `ensure_dotenv_loaded` any more. The
only env source is the actual process environment. Pin
`LITOPS_SKIP_DOTENV=1` anyway for belt-and-braces (future reorganizers
may add dotenv back).

## 5. OpenAI bearer lives ~1 hour; our archiver runs for 10+

**Symptom.** `BLOCKED_AUTH` after about an hour of successful work.

**Why.** `_exchange_session` is called once in `from_env()`. The bearer
is used until exhausted. OpenAI expires it after ~3600 s.

**Fix (built in).** `ChatGPTHttpClient` stores the session cookie; on
HTTP **401 or 403** in `_request_json`, it transparently re-exchanges
and retries once. If still 403 after refresh, the error propagates
normally. See `patches/0001-session-refresh-and-curl-cffi.diff`.

## 6. Only `401` is "bearer expired" — wrong, `403` happens too

**Symptom.** Variant of #5. Your refresh patch only handles 401, so you
still crash periodically.

**Why.** Depending on OpenAI's internal state, an expired bearer
sometimes returns `403` with the normal `Content-Type: application/json`
body (not a Cloudflare HTML challenge). We cannot distinguish from
"forbidden" semantically without reading the body.

**Fix.** Nisaba treats both `401` and `403` as refresh triggers (one
retry each), and only raises `ChatGPTAuthError` if the refresh itself
fails or the retried call still returns one of those codes.

## 7. Legacy project files 500 from OpenAI backend

**Symptom.** Many entries in `errors.jsonl` with `class: FILE_DOWNLOAD`,
`message: file download HTTP 500` on specific `project_file` records.

**Why.** OpenAI's project file storage returns 500 on files that are
"too old" or "migrated away" — ~30 % of 2024-era project files on tested
accounts. These are permanent server-side; retry does not help.

**Classification.** Nisaba records them, keeps running. They show up in
`final_report.md` as `files_failed`. The `failed_index.json` lists them
with disposition `FAILED_FINAL` if retries are exhausted.

## 8. Attachment pre-signed URLs expire fast

**Symptom.** Mid-run, many attachments start returning 403 (not through
our client's bearer path — through a signed-URL path).

**Why.** OpenAI gives us a time-limited AWS/CloudFront signed URL after
we call `/files/download/<id>`. If the archiver is throttled and the
attachment list is huge, the URL may expire before the download starts.

**Fix (built in).** Nisaba fetches the metadata URL and downloads
*immediately* in the same tick, within the same `_request_json` →
`urlopen` pair, no queueing. If a 403 still happens, the record is
retried on the next full pass (archiver does three passes).

## 9. `files.jsonl` `FAILED` records do not keep `original_name`

**Symptom.** You want to tell the owner "we lost these files" and show
names, but failed rows only have the opaque `file_id`.

**Why.** The OpenAI metadata endpoint only returns a human-readable
`name` on 200. On 404/403/500 we have nothing but the id we started
with.

**Workaround.** The names can be reconstructed from either:

- `projects.json` → `projects[].files[].name` (for failed *project* files), or
- `conversations/<uuid>/conversation.json` → `mapping.*.message.metadata.attachments[]` and `content.parts[]`
  (for failed *attachments* within chats).

A reconstruction script (parse all `conversation.json`, join with
`files.jsonl` by `file_id`) is on the roadmap.

## 10. Scheduled-task restart storms when the worker fails fast

**Symptom.** `watchdog.ps1` restarts the worker every 2 minutes, 10
times in a row, then gives up.

**Why.** If the worker dies on `import` (e.g. the whole archiver repo
was moved by the operator mid-run, breaking `PYTHONPATH`), each restart
dies in < 10 seconds. Watchdog's "worker dead" check fires on the
default `CheckEveryS=120` interval, so the restart budget evaporates in
under 30 minutes.

**Fix.** `watchdog.ps1` now checks for the specific `BLOCKED_AUTH`
output marker and exits early rather than restart-looping. If you see
`restart budget exhausted (10)` in `watchdog.log`, look at
`worker.err.log` and `worker.out.log` for the actual Python traceback —
it's never a transient fault at that count.

---

See also: `patches/0001-session-refresh-and-curl-cffi.diff` for the
exact modifications over the baseline provider, and
`docs/AGENT_GUIDE.md` for the operator-side protocol.

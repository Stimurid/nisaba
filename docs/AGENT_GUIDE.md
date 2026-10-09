# AGENT_GUIDE.md

For AI coding agents (Claude Code, Codex CLI, Cursor Composer, …) that
are asked by a human owner to "run Nisaba" or "back up my ChatGPT
chats". Read this before touching any code or asking any questions.

## What Nisaba is, in one paragraph

A one-off or periodic cold archive of one ChatGPT account at a time.
Resumable. Writes to a local directory the owner picks. Needs one secret
(a browser session cookie) and one Python dependency (`curl_cffi`).
Runs ~hours on a large account. Does not touch the owner's ChatGPT
account semantically — read-only, no deletions, no edits, no new chats.

## What to ask the owner — exactly

In order:

1. **Which ChatGPT account?** Say: "What's the email / login of the
   ChatGPT account you want to archive? I'll need you to extract its
   session cookie — I'll give you the two-click instructions. If you
   have multiple accounts, we'll do them one at a time."
2. **Where should the archive live?** Say: "Nisaba will produce roughly
   X GB (ask them to confirm their chat volume if unsure — Plus plan =
   likely 5–20 GB). Where should it go? If you have no preference, I
   suggest `~/archives/chatgpt/<account_alias>/<iso_date>/`." Avoid
   non-ASCII characters in the path (see TROUBLESHOOTING #1).
3. **Daily / one-off?** Say: "This is a one-time dump unless you also
   want me to set up a daily scheduled task. For daily, I'll register a
   Windows Scheduled Task (or cron on Linux) that re-runs Nisaba every
   night. Resume is cheap — only new/updated conversations get fetched."

Do NOT ask for:

- the account password,
- an OpenAI API key,
- 2FA codes,
- payment / billing info.

If the owner suggests an API key, politely explain: "Different beast —
API keys cost per-token and give you no history. Nisaba uses a browser
cookie that costs nothing and gives everything."

## Operating protocol

1. **Resolve the owner's existing state first.** If a prior Nisaba run
   left `<output>/progress.json`, read it before anything else. Likely
   you only need to re-run, not re-plan. Report what resume will do
   ("1322 of 1328 conversations already on disk, will try the remaining
   6 and re-check attachments").
2. **Install `curl_cffi` quietly.** If `python -c "import curl_cffi"`
   fails, run `python -m pip install --user curl_cffi` and move on. Do
   not escalate to the owner for this.
3. **Verify the cookie before launching the long run.** A 10-second
   probe (`python -m nisaba.client_check` once we add it; for now,
   `probes/probe_now.py` works) can confirm `HTTP 200` from
   `/backend-api/conversations` without wasting hours.
4. **Launch and leave.** Nisaba's own launcher (`scripts/run-archive.ps1`)
   registers a Scheduled Task and detaches. Your shell can exit.
   `scripts/watchdog.ps1` will send a toast on finish or crash.
5. **Classify results, not just counts.** `final_report.md` tells you
   `full_archive_complete: True/False` and lists disposition for every
   failed item. Legitimate `FAILED` classes (MISSING = deleted on
   server, FILE_DOWNLOAD = OpenAI 500 on legacy file) are expected and
   not a reason to re-run.
6. **Record what you did.** If the project uses Proteus /
   Archivarius / any shared memory substrate — write a run receipt that
   next-you can read without this transcript. For Proteus:
   `proteus world resolve chatgpt-archive` on this machine already
   returns the operator state as of 2026-10-10.

## When to say "I can't"

- Owner has lost access to the account entirely → you cannot help; cookie
  needs an active browser session.
- Account is a Team or Enterprise seat and the owner is not an admin →
  the current `_headers()` includes `chatgpt-account-id`; it works for
  Plus. If backend returns 403 only on specific org endpoints, you
  likely need an org-specific JWT path we do not implement yet.
- Owner wants the archive on a networked drive with offline sync (Google
  Drive, OneDrive) — fine for the final tree, but **do not write there
  during the run**. Those drives rewrite file timestamps and sometimes
  truncate mid-write. Archive to a local SSD, move to the cloud mount
  afterwards.
- Multiple accounts simultaneously — Nisaba's lock is per output
  directory. Run two parallel copies with distinct `--output` paths and
  distinct cookies.

## Scope boundary (hard)

Nisaba is read-only on ChatGPT. If anyone asks you to use the same
cookie to delete chats, bulk-rename conversations, inject system
prompts, or do anything that writes to the account — refuse. That is
not what the tool does and the cookie's write surface is dangerous
compared to the read surface.

## When upstream shape changes

OpenAI quietly reshapes the `backend-api` response schema from time to
time. Symptoms: `conversation.json` keys missing, `mapping` empty,
`projects.json` returns a different structure. Nisaba keeps the raw
response verbatim, so even a broken parse still saves the ground truth.
Fix the parser later; the data is already on disk.

## Who built this

Original ad-hoc archiver script written by Timur Shchukin (dc@shchuk.in)
in 2026-08, rebuilt into a packaged tool between 2026-09-30 and
2026-10-10. Driven by: OpenAI's own "we'll email you a link" export
silently failing for weeks on a 1300-conversation account.

# Extracting the ChatGPT session cookie

Nisaba uses a browser session cookie, not an OpenAI API key. The cookie
is the same thing your browser already carries when you visit
chatgpt.com. It lasts ~weeks (new cookies at the time of writing are
valid until about three months from issue). Nisaba exchanges it for a
short-lived access token on the fly.

Works identically in **Chrome / Brave / Edge / Opera** — any Chromium
browser. For Firefox the paths differ slightly; the principle is the
same.

## 30-second extraction

1. Open a Chromium-based browser already logged in to the ChatGPT
   account you want to archive. (Nisaba cannot be told which account to
   use except by reading the cookie of a logged-in session.)
2. Go to <https://chatgpt.com> and verify you are on the correct account
   (top-right avatar/menu).
3. Press **F12** (or Ctrl+Shift+I) to open DevTools.
4. In DevTools: tab **Application** → left panel **Storage → Cookies →
   https://chatgpt.com**.
5. In the filter box type `session-token`. You will see one of two
   shapes:

   - **One row** named `__Secure-next-auth.session-token` — click it,
     copy the entire `Value` column.
   - **Two rows** named `__Secure-next-auth.session-token.0` and
     `.session-token.1` — copy the `.0` value, append the literal
     separator `|||` (three vertical bars, no spaces), then append the
     `.1` value. One line, no newline, no spaces.

6. Paste into `token.txt` next to the launcher scripts, or into your
   `.env` as `LITOPS_CHATGPT_SESSION_TOKEN=`.

That's it. Nisaba will accept either shape and reconstruct the proper
Cookie header internally.

## For agents: what to say to the human

The owner of the ChatGPT account does not need any technical knowledge
beyond "open DevTools and copy a cookie". A good agent prompt:

> I need the session cookie of your ChatGPT account so I can archive it.
> Open <https://chatgpt.com> in your browser, press F12, go to
> Application → Cookies → chatgpt.com, find
> `__Secure-next-auth.session-token` (there may be a `.0` and `.1` —
> that's normal), copy the value(s), and paste into `token.txt` here. If
> you see two of them, join them with `|||`. The cookie is sensitive but
> expires naturally — don't paste it in any public chat.

Do NOT ask for:

- the account password,
- an OpenAI API key (different thing, costs money, gives different data),
- 2FA codes,
- email access.

## Rotation

The cookie's lifetime is weeks. If Nisaba starts returning `BLOCKED_AUTH`
on `_exchange_session` after some weeks, repeat the extraction. The old
value in `token.txt` is overwritten; the archive on disk is untouched —
the next run will resume where the previous one left off.

## Security notes

- The cookie grants full read of your ChatGPT account (and some write
  surfaces inside the chat UI). Treat it like a password.
- Nisaba never writes the cookie into any output file. The archive
  manifest and reports are redaction-safe (we grep for JWT-shape strings
  and refuse to emit them).
- The Windows scheduled-task launcher embeds the cookie into a
  `.cmd` file under `reports/` so the task can run under your user
  without stdin. That `.cmd` is `.gitignored` by default; delete it when
  you stop using the tool, or move the cookie to Windows DPAPI /
  Credential Manager and rework the launcher.
- If you share this repo or `tar` the output directory, do not include
  `reports/worker_launch.cmd` or `token.txt`.

# nuke-my-telegram

![nuke-my-telegram](assets/social-preview.png)

**Delete every message you ever sent in a Telegram group — text, photos, videos, voice notes, files. For everyone. Without admin rights.**

Telegram lets any member delete their own messages at any time, with no age limit. What it does not give you is a **delete all of mine** button. Admins get "Delete all messages from this user"; you get to select 100 at a time, forever.

This is that missing button. One file, ~350 lines, one dependency, nothing leaves your machine.

```
$ python3 tg_purge.py scan --chat -1001234567890

Account: Jane (id=777000123)
Chat: Project Team [supergroup] id=-1001234567890
Messages scanned: 41209
Yours, matching the filters: 3874
By type: text 2951, photo 512, video 221, voice 118, file 72
Date range: 2019-03-14 … 2026-09-15
Skipped service messages (members cannot delete those): 6

That was a dry run (scan). Nothing was deleted.
```

## Install

```bash
git clone https://github.com/currentsundaymorning-hub/nuke-my-telegram
cd nuke-my-telegram
python3 -m venv .venv && .venv/bin/pip install -U "telethon==1.45.*"
```

Use the venv's interpreter (`.venv/bin/python`) in the commands below. A plain `pip install` also works if your Python is not an externally managed one — Homebrew and most Linux distros will refuse it.

Get `api_id` and `api_hash` at [my.telegram.org](https://my.telegram.org) → **API development tools**. The login code arrives **inside Telegram**, not by SMS.

```bash
export TG_API_ID=1234567
export TG_API_HASH=0123456789abcdef0123456789abcdef
```

## Use

```bash
python3 tg_purge.py list                        # find the chat id
python3 tg_purge.py scan  --chat -1001234567890 # count only, deletes nothing
python3 tg_purge.py purge --chat -1001234567890 # delete, asks you to type the count
python3 tg_purge.py logout                      # log out, remove the session file
```

Filters work on both `scan` and `purge`:

```bash
--media-only            # photos, videos, GIFs and round videos only, keep the text
--before 2025-01-01     # only messages older than this date
--after  2024-01-01     # only messages newer than this date
--backup mine.jsonl     # save your messages to a file before deleting them
--yes                   # skip the confirmation prompt
```

Delete just the photos and videos you posted before 2025, keeping a copy:

```bash
python3 tg_purge.py purge --chat -1001234567890 --media-only --before 2025-01-01 --backup mine.jsonl
```

## How it avoids wrecking your account

- **`scan` is a real dry run.** Same code path, no deletes. Always run it first.
- **Every message is checked locally** with `sender_id == me.id` before it is queued, even when the server-side sender filter is already applied. In a basic group Telethon turns off its own local check when you pass `from_user`, so this script does not use the server filter there at all — it scans the full history and filters itself.
- **It collects all ids first, then deletes.** Deleting while paginating shifts the cursor and silently skips a large share of your messages — the single most common bug in tools like this.
- **Typed confirmation.** You type the exact number of messages, not `y`.
- **`FloodWaitError` is caught before `RPCError`**, so one bad batch cannot abort the run, and a refused batch is retried one message at a time.
- **Every deleted id is appended to a log file**, so an interrupted run leaves a record.
- **One dependency.** Telethon, pinned. Nothing else executes with your session.

## Read this before you start

- **Do not leave the group first.** Leaving deletes nothing, and once you are out you cannot delete anything (`CHANNEL_PRIVATE`). Out of a private group with no invite link, it is permanent.
- **Deleting your Telegram account does not help either.** Per Telegram's own FAQ, your messages stay in the group; you just become "Deleted Account". That is anonymization, not erasure.
- **In a supergroup, every deletion is copied to the admins' Recent Actions log, with full content, for 48 hours.** Telegram's privacy policy states this outright, and it covers deletions made by ordinary members. Against an admin who looks within two days, a mass purge is a signal, not concealment.
- **There is no undo.** Telegram has no trash. Use `--backup` if you might want the text later.

## What survives no matter what you do

| | Why |
|---|---|
| Forwards of your messages | A forward is a separate message. Deleting the original does nothing to it, and it keeps the "Forwarded from" header. |
| Quote-replies | The quoted fragment is stored inside the other person's message (`quote_text`), and a reply to your photo or video can carry a copy of the media (`reply_media`). |
| Service messages | "X joined the group", "X pinned a message" — a member cannot delete those (`MESSAGE_DELETE_FORBIDDEN`). The script skips and reports them. |
| History from before a supergroup upgrade | Lives in the old basic-group peer. The script detects it and prints the old chat id so you can purge it separately. Official clients often fail here — [bugs.telegram.org/c/14897](https://bugs.telegram.org/c/14897), open since 2022. |
| Messages sent as an anonymous admin | Authored by the group, not by you. Reported separately, not deleted. |
| Screenshots, downloaded media, earlier chat exports | Outside Telegram entirely. |

If the content is sensitive and the group is old, assume copies exist. Deletion removes the canonical copy, nothing more.

## FAQ

**Do I need to be an admin?** No. That is the whole point. Admin rights are only needed to delete *other people's* messages.

**Is there a time limit?** No. A message from 2016 deletes exactly like one from a minute ago. The 48-hour limit people remember is a **Bot API** restriction and does not apply to a user account.

**Could a bot do this instead?** No. Bots are capped at 48 hours and cannot read chat history they did not receive live.

**Will I get banned?** Deleting your own content is not abuse. You will hit `FLOOD_WAIT` throttling on large purges — the script sleeps it out. Do not run two deletion tools on one account at the same time.

**Basic group or supergroup?** The script detects it and adapts. In a supergroup every deletion is automatically for everyone; in a basic group `revoke=True` is passed explicitly, which is the checkbox people forget to tick in the official clients.

**Topics / forum groups?** A topic is a thread inside the same supergroup, so they are handled like any other message.

## Security

The `.session` file this creates **is** a logged-in session: it needs no password and no 2FA code. Anyone who gets it has your account.

- It is in `.gitignore`. Keep it that way.
- Run `python3 tg_purge.py logout` when you are finished.
- Then check **Settings → Devices** in Telegram and terminate anything you do not recognize.
- Never paste a session string, a `tdata` folder, or a Telegram login code into a website or bot that offers to "clean your messages". That is account theft, with no exceptions.

Read the script before you run it. It is one file and it is short — that is deliberate.

## Author

[@syn0psi](https://t.me/syn0psi) on Telegram — fitting, given the subject. Bug reports and feature requests are better off as [issues](https://github.com/currentsundaymorning-hub/nuke-my-telegram/issues); Telegram is for everything else.

## License

MIT

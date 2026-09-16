#!/usr/bin/env python3
"""
nuke-my-telegram — delete every message you ever sent in one Telegram chat:
text, photos, videos, voice notes, files. For everyone, not just for you.

You do NOT need to be an admin. Telegram lets any member delete their own
messages at any time, with no age limit. What it does not give you is a
"delete all of mine" button — that is what this script is.

One dependency:  python3 -m pip install -U "telethon==1.45.*"

Commands:
    python3 tg_purge.py list                      list your chats and their ids
    python3 tg_purge.py scan  --chat <id|@name>   count only, delete nothing
    python3 tg_purge.py purge --chat <id|@name>   delete (asks for confirmation)
    python3 tg_purge.py logout                    log out and remove the session file

Flags for scan/purge:
    --media-only          photos, videos, GIFs and round videos only (keep text)
    --before 2025-01-01   only messages older than this date
    --after  2024-01-01   only messages newer than this date
    --backup out.jsonl    save your messages to a file BEFORE deleting them
    --yes                 skip the confirmation prompt (non-interactive use)

Get api_id / api_hash at https://my.telegram.org -> "API development tools".
Set them as TG_API_ID and TG_API_HASH, or the script will ask.

The session file (./tg_purge.session by default) is equivalent to being logged
into your account. Never share or commit it; run `tg_purge.py logout` when done.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

try:
    from telethon import TelegramClient, errors, utils
    from telethon.tl.types import (Channel, Chat, MessageService, MessageMediaPhoto,
                                   MessageMediaDocument, DocumentAttributeVideo,
                                   DocumentAttributeAudio, DocumentAttributeSticker,
                                   DocumentAttributeAnimated)
    from telethon.tl.functions.channels import GetFullChannelRequest
except ImportError:                 # keep --help working without the dependency installed
    TelegramClient = None
    MISSING_TELETHON = ('Telethon is missing. Install it:  '
                        'python3 -m pip install -U "telethon==1.45.*"')
else:
    MISSING_TELETHON = None

SESSION_NAME = os.environ.get('TG_SESSION', 'tg_purge')
CHUNK = 100          # Telegram accepts at most 100 ids per delete request
CHUNK_SLEEP = 1.0    # seconds between delete requests
MEDIA_KINDS = {'photo', 'video', 'gif', 'video_note'}


# ───────────────────────────────── helpers ─────────────────────────────────

def credentials():
    api_id = os.environ.get('TG_API_ID')
    api_hash = os.environ.get('TG_API_HASH')
    if not api_id:
        api_id = input('api_id (from my.telegram.org): ').strip()
    if not api_hash:
        api_hash = input('api_hash: ').strip()
    try:
        return int(api_id), api_hash
    except ValueError:
        sys.exit('api_id must be a number')


def kind_of(msg):
    """Rough message classification, used for the report and for --media-only."""
    media = msg.media
    if media is None:
        return 'text'
    if isinstance(media, MessageMediaPhoto):
        return 'photo'
    if isinstance(media, MessageMediaDocument):
        attrs = getattr(getattr(media, 'document', None), 'attributes', None) or []
        video = next((a for a in attrs if isinstance(a, DocumentAttributeVideo)), None)
        audio = next((a for a in attrs if isinstance(a, DocumentAttributeAudio)), None)
        if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
            return 'sticker'
        if video is not None:
            if getattr(video, 'round_message', False):
                return 'video_note'
            return 'gif' if any(isinstance(a, DocumentAttributeAnimated) for a in attrs) else 'video'
        if audio is not None:
            return 'voice' if getattr(audio, 'voice', False) else 'audio'
        return 'file'
    return type(media).__name__.replace('MessageMedia', '').lower()


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f'Dates must look like YYYY-MM-DD, got: {value}')


async def resolve(client, target):
    """Resolve @name, t.me link or numeric id. Returns (entity, human-readable kind)."""
    try:
        entity = await client.get_entity(target)
    except ValueError:
        # private group with no @username: warm up the dialog cache and retry
        print('Not found directly — loading your dialog list...')
        async for _ in client.iter_dialogs():
            pass
        entity = await client.get_entity(target)

    if isinstance(entity, Chat):
        if getattr(entity, 'migrated_to', None):
            entity = await client.get_entity(entity.migrated_to)
            return entity, 'supergroup (this group was upgraded from a basic group)'
        return entity, 'BASIC GROUP (legacy)'
    if isinstance(entity, Channel):
        return entity, 'supergroup' if entity.megagroup else 'BROADCAST CHANNEL'
    sys.exit('Not a group or channel — this script is for group chats.')


async def migrated_from(client, entity):
    """For a supergroup, return the id of the basic group it was upgraded from, if any.

    That pre-migration history is the classic trap: official clients often refuse to
    delete it (bugs.telegram.org/c/14897), but addressing the old chat directly works."""
    if not isinstance(entity, Channel) or not entity.megagroup:
        return None
    try:
        full = await client(GetFullChannelRequest(entity))
        return getattr(full.full_chat, 'migrated_from_chat_id', None)
    except Exception:
        return None


# ────────────────────────────────── collect ─────────────────────────────────

async def collect(client, entity, me, args):
    """Collect the ids of all your own messages.

    Collect everything first, delete afterwards: deleting while paginating shifts the
    cursor under you and silently skips a large share of your messages."""
    before, after = parse_date(args.before), parse_date(args.after)

    # The server-side from_user filter is only trustworthy in supergroups and channels.
    # In a basic group Telethon also switches off its own local sender check, so we scan
    # the full history and filter locally instead.
    server_filter = isinstance(entity, Channel)
    kwargs = {'from_user': 'me'} if server_filter else {}
    if not server_filter:
        print('Basic group: the server-side sender filter is unreliable here — '
              'scanning the whole history.')

    items, scanned, skipped_service, skipped_anon = [], 0, 0, 0
    while True:
        try:
            async for msg in client.iter_messages(entity, **kwargs):
                scanned += 1
                if scanned % 2000 == 0:
                    print(f'  scanned {scanned}, mine so far {len(items)}', flush=True)

                if msg.sender_id != me.id:          # safety net, even with the server filter
                    if msg.out:
                        skipped_anon += 1           # sent as the group (anonymous admin)
                    continue
                if isinstance(msg, MessageService):  # "X joined the group" — members cannot delete
                    skipped_service += 1
                    continue
                if msg.id == 1:                      # the chat's very first message is protected
                    continue
                if before and msg.date >= before:
                    continue
                if after and msg.date <= after:
                    continue

                kind = kind_of(msg)
                if args.media_only and kind not in MEDIA_KINDS:
                    continue
                items.append({'id': msg.id, 'date': msg.date.isoformat(),
                              'kind': kind, 'text': (msg.message or '')})
            break
        except errors.FloodWaitError as e:
            print(f'[flood] waiting {e.seconds + 5}s, then restarting the scan', flush=True)
            await asyncio.sleep(e.seconds + 5)
            items, scanned, skipped_service, skipped_anon = [], 0, 0, 0

    items.sort(key=lambda x: x['id'])
    return items, scanned, skipped_service, skipped_anon


def report(items, scanned, skipped_service, skipped_anon):
    print(f'\nMessages scanned: {scanned}')
    print(f'Yours, matching the filters: {len(items)}')
    if not items:
        return
    counts = {}
    for it in items:
        counts[it['kind']] = counts.get(it['kind'], 0) + 1
    print('By type: ' + ', '.join(f'{k} {v}'
                                  for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    print(f"Date range: {items[0]['date'][:10]} … {items[-1]['date'][:10]}")
    if skipped_service:
        print(f'Skipped service messages (members cannot delete those): {skipped_service}')
    if skipped_anon:
        print(f'Skipped messages you sent as the group (anonymous admin): {skipped_anon}')


# ─────────────────────────────────── delete ─────────────────────────────────

async def delete_all(client, entity, items, logfile):
    ids = [it['id'] for it in items]
    done, failed = 0, []
    with open(logfile, 'a', encoding='utf-8') as log:
        for i in range(0, len(ids), CHUNK):
            batch = ids[i:i + CHUNK]
            while True:
                try:
                    await client.delete_messages(entity, batch, revoke=True)
                    done += len(batch)
                    log.write('\n'.join(str(x) for x in batch) + '\n')
                    log.flush()
                    print(f'  deleted {done}/{len(ids)}', flush=True)
                    break
                except errors.FloodWaitError as e:       # must be caught before RPCError
                    print(f'[flood] waiting {e.seconds + 5}s', flush=True)
                    await asyncio.sleep(e.seconds + 5)
                except errors.RPCError as e:
                    print(f'  batch refused ({e.__class__.__name__}) — retrying one by one',
                          flush=True)
                    for mid in batch:
                        while True:
                            try:
                                await client.delete_messages(entity, [mid], revoke=True)
                                done += 1
                                log.write(f'{mid}\n')
                                break
                            except errors.FloodWaitError as e2:
                                await asyncio.sleep(e2.seconds + 5)
                            except errors.RPCError as e2:
                                failed.append((mid, e2.__class__.__name__))
                                break
                    log.flush()
                    break
            await asyncio.sleep(CHUNK_SLEEP)
    return done, failed


# ───────────────────────────────── commands ─────────────────────────────────

async def cmd_list(client):
    print(f'{"id":>16}  type         name')
    async for d in client.iter_dialogs():
        e = d.entity
        if isinstance(e, Chat):
            kind = 'group'
        elif isinstance(e, Channel):
            kind = 'supergroup' if e.megagroup else 'channel'
        else:
            continue
        print(f'{utils.get_peer_id(e):>16}  {kind:<11}  {d.name}')
    print('\nSupergroup ids start with -100; basic group ids are just negative.')


async def cmd_scan_or_purge(client, args, do_delete):
    me = await client.get_me()
    print(f'Account: {utils.get_display_name(me)} (id={me.id})')

    entity, kind = await resolve(client, args.chat)
    chat_id = utils.get_peer_id(entity)
    print(f'Chat: {utils.get_display_name(entity)} [{kind}] id={chat_id}')
    if kind == 'BROADCAST CHANNEL':
        print('Note: this is a channel, not a group. Only admins post there.')

    old = await migrated_from(client, entity)
    if old:
        print(f'\n[!] This group has history from before it became a supergroup (old id {old}).')
        print(f'    Purge it separately: python3 {os.path.basename(__file__)} '
              f'{"purge" if do_delete else "scan"} --chat {old}')

    items, scanned, sk_srv, sk_anon = await collect(client, entity, me, args)
    report(items, scanned, sk_srv, sk_anon)
    if not items:
        return

    if args.backup:
        with open(args.backup, 'w', encoding='utf-8') as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + '\n')
        print(f'Backup of your messages written to {args.backup}')

    if not do_delete:
        print('\nThat was a dry run (scan). Nothing was deleted.')
        return

    print('\nDeletion is permanent: Telegram has no trash and no undo.')
    if not args.yes:
        want = str(len(items))
        got = input(f'Type {want} to confirm deletion: ').strip()
        if got != want:
            sys.exit('Cancelled.')

    logfile = f'tg_purge_deleted_{chat_id}.log'
    done, failed = await delete_all(client, entity, items, logfile)
    print(f'\nDeleted {done} of {len(items)}. Ids written to {logfile}')
    if failed:
        print(f'Could not delete {len(failed)}:')
        for mid, err in failed[:20]:
            print(f'  {mid}: {err}')
        if len(failed) > 20:
            print(f'  ...and {len(failed) - 20} more')
        print('MESSAGE_DELETE_FORBIDDEN means a service message — members cannot delete those.')
    print('\nVerify: run scan again, it should report 0.')
    print('When you are done: python3 tg_purge.py logout, then check '
          'Telegram Settings -> Devices for stray sessions.')


async def main():
    p = argparse.ArgumentParser(
        description='Delete all of your own messages from a Telegram chat.')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='list your chats and their ids')
    sub.add_parser('logout', help='log out and remove the session file')

    for name, help_text in (('scan', 'count what would be deleted, change nothing'),
                            ('purge', 'delete your messages')):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument('--chat', required=True, help='id, @name or t.me link')
        sp.add_argument('--media-only', action='store_true',
                        help='photos, videos, GIFs and round videos only')
        sp.add_argument('--before', help='only messages older than YYYY-MM-DD')
        sp.add_argument('--after', help='only messages newer than YYYY-MM-DD')
        sp.add_argument('--backup', help='.jsonl file to save your messages into first')
        sp.add_argument('--yes', action='store_true', help='skip the confirmation prompt')

    args = p.parse_args()
    if MISSING_TELETHON:
        sys.exit(MISSING_TELETHON)
    api_id, api_hash = credentials()

    async with TelegramClient(SESSION_NAME, api_id, api_hash,
                              flood_sleep_threshold=60) as client:
        if args.cmd == 'list':
            await cmd_list(client)
        elif args.cmd == 'logout':
            await client.log_out()
            print('Logged out. Telethon removed the session file.')
        else:
            await cmd_scan_or_purge(client, args, do_delete=(args.cmd == 'purge'))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nInterrupted. What was deleted stays deleted; the rest is untouched — '
              'run scan to see what is left.')

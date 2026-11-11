#!/usr/bin/env python3
"""Resumable queue for learning many videos: a YouTube channel/playlist, a MEGA course folder, a link list.

`enqueue` builds the queue (one entry per video) in the knowledge library (.queue.json). The agent then
loops `next` -> watch + save a note -> `done`, so a stopped run resumes where it left off.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
from knowledge import resolve_dir  # noqa: E402
from runtime import configure_stdio, diagnostic, run_text  # noqa: E402
import sources  # noqa: E402

QUEUE = '.queue.json'


def cache_dir(url: str) -> Path:
    return Path.home() / '.cache' / 'watch' / 'courses' / hashlib.sha256(url.encode()).hexdigest()[:12]


def load(root: Path) -> dict:
    try:
        data = json.loads((root / QUEUE).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('items'), list) else {'items': []}
    except (OSError, ValueError):
        return {'items': []}


def save(root: Path, queue: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / QUEUE).write_text(json.dumps(queue, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def enumerate_urls(url: str, limit: int | None) -> list[dict]:
    cmd = ['yt-dlp', '--flat-playlist', '--ignore-errors', '--print', '%(webpage_url)s\t%(title)s']
    if limit:
        cmd += ['--playlist-end', str(limit)]
    result = run_text([*cmd, '--', url])
    items = []
    for line in result.stdout.splitlines():
        link, _, title = line.partition('\t')
        if link.startswith(('http://', 'https://')) and link not in {i['source'] for i in items}:
            items.append({'source': link, 'title': title.strip() or link})
    if not items:
        raise SystemExit('No videos found at that link: ' + diagnostic(result.stderr, 600)
                         + '\nFor Instagram profiles, enqueue a list of reel links with --links-file instead.')
    return items


def build_items(args) -> list[dict]:
    exclude = args.exclude or []
    if args.links_file:
        text = Path(args.links_file).expanduser().read_text(encoding='utf-8')
        return [{'source': l.strip(), 'title': l.strip()} for l in text.splitlines()
                if l.strip().startswith(('http://', 'https://'))]
    if sources.is_mega_folder(args.source):
        dest = cache_dir(args.source)
        return [{'source': str(p), 'title': re.sub(r'\.[^.]+$', '', p.name), 'origin': args.source}
                for p in sources.fetch_mega_folder(args.source, dest, exclude)]
    items = enumerate_urls(args.source, args.limit)
    return [i for i in items if not any(e.lower() in (i['title'] + i['source']).lower() for e in exclude if e.strip())]


def summary(queue: dict) -> dict:
    counts: dict[str, int] = {}
    for item in queue['items']:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    return {'total': len(queue['items']), **counts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Resumable queue for learning many videos.')
    ap.add_argument('--dir', default=None, help='Knowledge library folder (same default as knowledge.py)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('enqueue', help='Build the queue from a channel/playlist/MEGA folder/links file')
    p.add_argument('source', nargs='?', default=None)
    p.add_argument('--links-file', default=None, help='Text file with one video URL per line')
    p.add_argument('--exclude', action='append', help='Skip videos/files whose title or path contains this text (repeatable)')
    p.add_argument('--limit', type=int, default=None, help='Only the first N videos (channels/playlists)')
    p.add_argument('--append', action='store_true', help='Keep the existing queue and add new items')
    sub.add_parser('status', help='Counts and a list of items')
    sub.add_parser('next', help='Print the next pending item (marks nothing)')
    p = sub.add_parser('done', help='Mark an item done')
    p.add_argument('id', type=int)
    p = sub.add_parser('skip', help='Mark an item skipped, with a reason')
    p.add_argument('id', type=int)
    p.add_argument('--reason', default='')
    sub.add_parser('reset', help='Empty the queue')
    args = ap.parse_args(argv)
    root = resolve_dir(args.dir)
    queue = load(root)

    if args.cmd == 'enqueue':
        if not args.source and not args.links_file:
            ap.error('give a source link or --links-file')
        known = {i['source'] for i in queue['items']} if args.append else set()
        items = [i for i in build_items(args) if i['source'] not in known]
        base = queue['items'] if args.append else []
        for item in items:
            item['status'] = 'pending'
        merged = base + items
        for n, item in enumerate(merged, 1):
            item['id'] = n
        queue = {'created_from': args.source or args.links_file, 'items': merged}
        save(root, queue)
        out = {**summary(queue), 'added': len(items), 'queue': str(root / QUEUE),
               'titles': [f"{i['id']}. {i['title']}" for i in merged[:50]]}
    elif args.cmd == 'status':
        out = {**summary(queue), 'items': [{k: i.get(k) for k in ('id', 'title', 'status', 'reason')} for i in queue['items']]}
    elif args.cmd == 'next':
        item = next((i for i in queue['items'] if i['status'] == 'pending'), None)
        out = {'next': item, **summary(queue)}
    elif args.cmd in ('done', 'skip'):
        item = next((i for i in queue['items'] if i['id'] == args.id), None)
        if not item:
            raise SystemExit(f'No queue item {args.id}.')
        item['status'] = 'done' if args.cmd == 'done' else 'skipped'
        if args.cmd == 'skip':
            item['reason'] = args.reason
        save(root, queue)
        out = summary(queue)
    else:
        save(root, {'items': []})
        out = {'total': 0}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    configure_stdio()
    raise SystemExit(main())

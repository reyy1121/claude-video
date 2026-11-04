#!/usr/bin/env python3
"""Cloud-drive sources yt-dlp cannot handle well: Google Drive and MEGA share links.

Both are fetched into the run's own directory and then treated as a local file, so
the rest of the pipeline (frames, transcript, Gemini upload) is unchanged.
"""
from __future__ import annotations

import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from download import VIDEO_EXTS
from runtime import diagnostic, run_text

DRIVE_HOSTS = {'drive.google.com', 'docs.google.com', 'drive.usercontent.google.com'}
MEGA_HOSTS = {'mega.nz', 'mega.co.nz', 'mega.io'}
CHUNK = 1 << 20


def _host(source: str) -> str:
    try:
        parsed = urllib.parse.urlparse(source)
    except ValueError:
        return ''
    if parsed.scheme not in ('http', 'https'):
        return ''
    return (parsed.hostname or '').lower().removeprefix('www.')


def cloud_kind(source: str) -> str | None:
    """'gdrive', 'mega', or None for anything else (YouTube, Instagram, local paths...)."""
    host = _host(source)
    if host in DRIVE_HOSTS:
        return 'gdrive'
    if host in MEGA_HOSTS:
        return 'mega'
    return None


def drive_file_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if '/folders/' in parsed.path or '/drive/folders' in parsed.path:
        raise SystemExit('Google Drive folder links are not supported; open the folder and share one video file link at a time.')
    match = re.search(r'/(?:file/)?d/([A-Za-z0-9_-]{10,})', parsed.path)
    if match:
        return match.group(1)
    ids = urllib.parse.parse_qs(parsed.query).get('id')
    if ids and re.fullmatch(r'[A-Za-z0-9_-]{10,}', ids[0]):
        return ids[0]
    raise SystemExit('Could not find a file id in that Google Drive link; use the "Share → Copy link" URL of a single file.')


def _filename_from_headers(headers, fallback: str) -> str:
    disposition = headers.get('Content-Disposition') or ''
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(r'filename="?([^";]+)"?', disposition)
    name = urllib.parse.unquote(match.group(1)) if match else fallback
    name = Path(name.replace('\\', '/')).name.strip() or fallback
    return re.sub(r'[^\w.\- ()\[\]]+', '_', name)[:150]


def fetch_gdrive(url: str, run_dir: Path, *, opener=None) -> dict:
    """Download a publicly shared ("Anyone with the link") Drive file."""
    file_id = drive_file_id(url)
    endpoint = ('https://drive.usercontent.google.com/download?'
                + urllib.parse.urlencode({'id': file_id, 'export': 'download', 'confirm': 't'}))
    opener = opener or urllib.request.build_opener()
    request = urllib.request.Request(endpoint, headers={'User-Agent': 'watch-skill'})
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as exc:
        hint = ' Set sharing to "Anyone with the link", or download it and pass the local path.' if exc.code in (401, 403, 404) else ''
        raise SystemExit(f'Google Drive download failed (HTTP {exc.code}).{hint}') from None
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f'Google Drive download failed: {type(exc).__name__}: {getattr(exc, "reason", exc)}') from None
    with response:
        ctype = (response.headers.get('Content-Type') or '').lower()
        if ctype.startswith('text/html'):
            raise SystemExit('Google Drive returned a web page instead of the file: it is private, over its '
                             'download quota, or needs sign-in. Share it as "Anyone with the link", or '
                             'download it yourself and pass the local path.')
        name = _filename_from_headers(response.headers, f'gdrive-{file_id}.mp4')
        run_dir.mkdir(parents=True, exist_ok=True)
        target = (run_dir / name).resolve()
        if not target.is_relative_to(run_dir.resolve()):
            target = run_dir.resolve() / f'gdrive-{file_id}.mp4'
        size = 0
        with open(target, 'wb') as out:
            while chunk := response.read(CHUNK):
                out.write(chunk)
                size += len(chunk)
    if not size:
        target.unlink(missing_ok=True)
        raise SystemExit('Google Drive returned an empty file.')
    return {'video_path': str(target), 'title': name}


def _newest_media(directory: Path) -> Path | None:
    files = [p for p in directory.rglob('*') if p.is_file() and p.stat().st_size > 0]
    videos = [p for p in files if p.suffix.lower() in VIDEO_EXTS] or files
    return max(videos, key=lambda p: p.stat().st_size, default=None)


def mega_command(url: str, run_dir: Path) -> list[str]:
    if shutil.which('megadl'):  # megatools
        return ['megadl', '--no-progress', '--path', str(run_dir), url]
    if shutil.which('mega-get'):  # MEGAcmd
        return ['mega-get', url, str(run_dir)]
    raise SystemExit('MEGA links need megatools (`megadl`) or MEGAcmd (`mega-get`). Install one '
                     '(macOS: `brew install megatools`; Debian/Ubuntu: `sudo apt install megatools`; '
                     'Windows: MEGAcmd from https://mega.io/cmd), or download the file and pass the local path.')


def fetch_mega(url: str, run_dir: Path) -> dict:
    if '/folder/' in url or '#F!' in url:
        raise SystemExit('MEGA folder links are not supported; share one video file link at a time.')
    run_dir.mkdir(parents=True, exist_ok=True)
    result = run_text(mega_command(url, run_dir))
    if result.returncode != 0:
        raise SystemExit(f'MEGA download failed (exit {result.returncode}): {diagnostic(result.stderr or result.stdout, 800)}')
    media = _newest_media(run_dir)
    if not media:
        raise SystemExit('MEGA download finished but produced no file.')
    return {'video_path': str(media.resolve()), 'title': media.name}


def fetch_cloud(source: str, run_dir: Path) -> dict:
    """Fetch a Drive/MEGA link into run_dir; return a download.resolve_local-shaped record."""
    kind = cloud_kind(source)
    print(f"[watch] fetching from {'Google Drive' if kind == 'gdrive' else 'MEGA'}…", file=sys.stderr)
    fetched = fetch_gdrive(source, run_dir) if kind == 'gdrive' else fetch_mega(source, run_dir)
    return {'video_path': fetched['video_path'], 'subtitle_path': None, 'downloaded': True,
            'info': {'title': fetched['title'], 'url': source}, 'run_dir': str(run_dir)}

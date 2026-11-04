"""Google Drive / MEGA source handling and the knowledge-library advisor skill (no network)."""
import io
import json
import re
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest

import knowledge
import sources

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('url, kind', [
    ('https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing', 'gdrive'),
    ('https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp', 'gdrive'),
    ('https://mega.nz/file/AbCd1234#keykeykey', 'mega'),
    ('https://www.youtube.com/watch?v=abc', None),
    ('https://www.instagram.com/reel/Cxyz/', None),
    ('/home/me/drive.google.com.mp4', None),
])
def test_cloud_kind(url, kind):
    assert sources.cloud_kind(url) == kind


def test_drive_file_id_forms_and_folder_rejected():
    assert sources.drive_file_id('https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view') == '1AbCdEfGhIjKlMnOp'
    assert sources.drive_file_id('https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download') == '1AbCdEfGhIjKlMnOp'
    with pytest.raises(SystemExit, match='folder'):
        sources.drive_file_id('https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp')


class _Response(io.BytesIO):
    def __init__(self, data, headers):
        super().__init__(data)
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value


class _Opener:
    def __init__(self, response):
        self.response, self.urls = response, []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return self.response


def test_fetch_gdrive_writes_inside_run_dir(tmp_path):
    opener = _Opener(_Response(b'\x00' * 100, {'Content-Type': 'video/mp4',
                                               'Content-Disposition': 'attachment; filename="../../evil talk.mp4"'}))
    out = sources.fetch_gdrive('https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view', tmp_path / 'run', opener=opener)
    path = Path(out['video_path'])
    assert path.parent == (tmp_path / 'run').resolve() and path.name == 'evil talk.mp4' and path.stat().st_size == 100
    assert 'id=1AbCdEfGhIjKlMnOp' in opener.urls[0] and 'confirm=t' in opener.urls[0]


def test_fetch_gdrive_html_means_private(tmp_path):
    opener = _Opener(_Response(b'<html>sign in</html>', {'Content-Type': 'text/html; charset=utf-8'}))
    with pytest.raises(SystemExit, match='Anyone with the link'):
        sources.fetch_gdrive('https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view', tmp_path, opener=opener)


def test_mega_requires_a_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.shutil, 'which', lambda name: None)
    with pytest.raises(SystemExit, match='megatools'):
        sources.fetch_mega('https://mega.nz/file/AbCd#key', tmp_path)
    monkeypatch.setattr(sources.shutil, 'which', lambda name: '/usr/bin/megadl' if name == 'megadl' else None)
    assert sources.mega_command('https://mega.nz/file/AbCd#key', tmp_path)[0] == 'megadl'
    with pytest.raises(SystemExit, match='folder'):
        sources.fetch_mega('https://mega.nz/folder/AbCd#key', tmp_path)


def test_fetch_mega_picks_downloaded_video(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        (Path(cmd[cmd.index('--path') + 1]) / 'talk.mp4').write_bytes(b'x' * 10)
        return subprocess.CompletedProcess(cmd, 0, '', '')
    monkeypatch.setattr(sources.shutil, 'which', lambda name: '/usr/bin/megadl' if name == 'megadl' else None)
    monkeypatch.setattr(sources, 'run_text', fake_run)
    out = sources.fetch_cloud('https://mega.nz/file/AbCd#key', tmp_path / 'dl')
    assert Path(out['video_path']).name == 'talk.mp4' and out['info']['url'].startswith('https://mega.nz')


def test_watch_processes_cloud_file_as_local(monkeypatch, tmp_path, static_clip, capsys):
    import watch
    def fake_fetch(source, run_dir):
        run_dir.mkdir(parents=True)
        target = run_dir / 'clip.mp4'
        target.write_bytes(Path(static_clip).read_bytes())
        return {'video_path': str(target), 'title': 'clip.mp4'}
    monkeypatch.setattr(sources, 'fetch_gdrive', fake_fetch)
    url = 'https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view'
    monkeypatch.setattr(sys, 'argv', ['watch.py', url, '--no-whisper', '--engine', 'local', '--out-dir', str(tmp_path / 'o')])
    assert watch.main() == 0
    report = capsys.readouterr().out
    assert f'**Source:** {url}' in report and '## Frames' in report and '.jpg' in report


def _note(tmp_path, text='## Summary\nCharge for outcomes.\n\n## Decision rules\n- If leads are cold, then lead with a case study. [01:05]\n'):
    path = tmp_path / 'note.md'
    path.write_text(text, encoding='utf-8')
    return str(path)


def test_library_is_a_valid_skill(tmp_path, capsys):
    lib = tmp_path / 'skills' / 'my-advisor'
    assert knowledge.main(['--dir', str(lib), 'init', '--goals', 'Grow my agency']) == 0
    assert knowledge.main(['--dir', str(lib), 'add', '--title', 'Sales calls', '--source', 'https://youtu.be/x1',
                           '--topics', 'Sales, Lead Gen', '--creator', 'Ann', '--note', _note(tmp_path)]) == 0
    capsys.readouterr()
    skill = (lib / 'SKILL.md').read_text(encoding='utf-8')
    front = skill.split('---')[1]
    keys = {line.split(':')[0] for line in front.splitlines() if line and not line.startswith(' ')}
    assert keys <= {'name', 'description', 'license', 'allowed-tools', 'metadata', 'compatibility'}
    assert re.search(r'^name: my-advisor$', front, re.M) and 'lead-gen' in front and 'Grow my agency' in skill
    assert json.loads(front.split('description: ', 1)[1].splitlines()[0])  # valid quoted string
    index = (lib / 'INDEX.md').read_text(encoding='utf-8')
    assert '## lead-gen' in index and 'Charge for outcomes.' in index and 'YouTube' in index
    assert (lib / 'PLAYBOOK.md').is_file() and (lib / 'decisions').is_dir()


def test_add_dedupes_by_source_and_replace(tmp_path, capsys):
    lib = tmp_path / 'lib'
    args = ['--dir', str(lib), 'add', '--title', 'T', '--source', 'https://youtu.be/x1', '--note', _note(tmp_path)]
    knowledge.main(args)
    with pytest.raises(SystemExit, match='Already learned'):
        knowledge.main(args)
    knowledge.main(args[:-2] + ['--note', _note(tmp_path, '## Summary\nNew take.\n'), '--replace'])
    capsys.readouterr()
    notes = knowledge.load_notes(lib)
    assert len(notes) == 1 and notes[0]['summary'] == 'New take.'


def test_playbook_is_never_overwritten_and_search_ranks(tmp_path, capsys):
    lib = tmp_path / 'lib'
    knowledge.main(['--dir', str(lib), 'add', '--title', 'Cold outreach', '--source', 'a', '--topics', 'sales',
                    '--note', _note(tmp_path)])
    knowledge.main(['--dir', str(lib), 'add', '--title', 'Gardening', '--source', 'b',
                    '--note', _note(tmp_path, '## Summary\nWater tomatoes daily.\n')])
    (lib / 'PLAYBOOK.md').write_text('# mine\n', encoding='utf-8')
    knowledge.main(['--dir', str(lib), 'rebuild'])
    assert (lib / 'PLAYBOOK.md').read_text(encoding='utf-8') == '# mine\n'
    capsys.readouterr()
    knowledge.main(['--dir', str(lib), 'search', 'should I use a case study for cold leads?'])
    hits = json.loads(capsys.readouterr().out)['hits']
    assert [h['title'] for h in hits] == ['Cold outreach'] and hits[0]['matches']


def test_default_dir_and_env_override(tmp_path, monkeypatch):
    assert knowledge.default_dir(tmp_path) == tmp_path / '.agents' / 'skills' / 'video-advisor'
    (tmp_path / '.claude').mkdir()
    assert knowledge.default_dir(tmp_path) == tmp_path / '.claude' / 'skills' / 'video-advisor'
    monkeypatch.setenv('WATCH_KNOWLEDGE_DIR', str(tmp_path / 'x'))
    assert knowledge.resolve_dir() == (tmp_path / 'x').resolve()


def test_skill_documents_learn_mode_and_sources():
    skill = (ROOT / 'skills/watch/SKILL.md').read_text(encoding='utf-8')
    for needle in ('knowledge.py', 'Learn mode', 'PLAYBOOK.md', 'megadl', 'Google Drive', 'Instagram', '--flat-playlist'):
        assert needle in skill, needle

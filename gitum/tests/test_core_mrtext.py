"""Merge request texts with images: set, import from a forge, render for another forge."""

import http.server
import json
import threading

import pytest

from gitum.cli import main
from gitum.mrtext import mark_internal, remote_project, strip_internal
from gitum.repo import Repo as GitumRepo

from core_helpers import INIT_ARGS, make_stack

PNG = b'\x89PNG\r\n\x1a\nfake-image'
SHOT = b'\x89PNG\r\n\x1a\nscreenshot'


def gitum(work, capsys, *args, expect=0):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	assert code == expect, out
	return json.loads(out) if out.strip() else None


def journal(work):
	return GitumRepo(work.path).journal()


class FakeGitLab(http.server.BaseHTTPRequestHandler):
	"""A public GitLab: merge requests by API and uploads by their web path. Any credentials are refused."""
	mrs = {}		# (project path, iid) -> {title, description}
	files = {}		# '/<project>/uploads/<secret>/<name>' -> bytes
	requests = []

	def _send(self, code, data, ctype='application/json'):
		self.send_response(code)
		self.send_header('Content-Type', ctype)
		self.end_headers()
		self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

	def do_GET(self):
		from urllib.parse import unquote
		self.requests.append((self.command, self.path))
		if self.headers.get('Authorization') or self.headers.get('Private-Token'):
			return self._send(400, {'message': 'gitum must not send credentials'})
		if self.path.startswith('/api/v4/projects/'):
			project, _, rest = self.path[len('/api/v4/projects/'):].partition('/')
			data = self.mrs.get((unquote(project), int(rest.split('/')[1])))
			return self._send(200 if data else 404, data or {})
		if self.path in self.files:
			return self._send(200, self.files[self.path], 'image/png')
		if '/uploads/' in self.path:
			return self._send(200, b'<html>sign in</html>', 'text/html')
		return self._send(404, {})

	def do_POST(self):
		self.requests.append((self.command, self.path))
		self._send(405, {})

	def log_message(self, *args):
		pass


@pytest.fixture
def world(tmp_path, capsys, monkeypatch):
	server = http.server.HTTPServer(('127.0.0.1', 0), FakeGitLab)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	FakeGitLab.mrs, FakeGitLab.files, FakeGitLab.requests = {}, {}, []
	url = 'http://127.0.0.1:%d' % server.server_port
	monkeypatch.setenv('GITLAB_TOKEN', 'secret')		# must not be used
	up, work = make_stack(tmp_path)
	assert main(['--repo', str(work.path), *INIT_ARGS]) == 0
	cfg = tmp_path / 'cfg.toml'
	cfg.write_text(journal(work).config_text +
		       '\n[forge.internal]\ntype = "gitlab"\nurl = "%s"\n'
		       '[forge.upstream]\ntype = "gitlab"\nurl = "%s"\nproject = "wine/wine"\n'
		       '[forge.plain]\ntype = "gitlab"\nurl = "%s"\n'
		       '[forge.other]\ntype = "github"\nurl = "https://github.com"\nproject = "a/b"\n'
		       '[series]\ninternal-mr-forge = "internal"\npr-forge = "upstream"\nmr-internal-separator = "---"\n'
		       % (url, url, url))
	assert main(['--repo', str(work.path), 'config', 'load', str(cfg)]) == 0
	work.run('checkout', '-q', '-b', 'series/fix', 'v1.0')
	work.commit('ntdll: Fix it.', {'ntdll': 'fix\n'})
	work.run('checkout', '-q', '--detach')
	assert main(['--repo', str(work.path), 'series', 'add', 'fix', '--base', 'upstream:winehq']) == 0
	capsys.readouterr()
	yield {'work': work, 'url': url, 'tmp': tmp_path}
	server.shutdown()


def test_strip_internal():
	text = 'a\n<!-- internal -->\nsecret\n<!-- /internal -->\nb\n<!-- internal -->\ntail\n'
	assert strip_internal(text) == 'a\nb\n'


def test_mark_internal_and_remote_project():
	assert mark_internal('up\n---\nru\n---\nmore\n', '---') == 'up\n<!-- internal -->\nru\n---\nmore\n'
	assert mark_internal('a\n<!-- internal -->\nb\n---\n', '---') == 'a\n<!-- internal -->\nb\n---\n'
	assert remote_project('git@gitlab.winehq.org:wine/wine.git', 'https://gitlab.winehq.org') == 'wine/wine'
	assert remote_project('https://gitlab.winehq.org/wine/wine.git', 'https://gitlab.winehq.org/') == 'wine/wine'
	assert remote_project('ssh://git@gitlab.winehq.org:2222/wine/wine', 'https://gitlab.winehq.org') == 'wine/wine'
	assert remote_project('https://github.com/wine-mirror/wine.git', 'https://gitlab.winehq.org') is None
	assert remote_project('/srv/git/wine.git', 'https://gitlab.winehq.org') is None


def test_set_render_record(world, capsys):
	work, tmp = world['work'], world['tmp']
	(tmp / 'img').mkdir()
	(tmp / 'img' / 'before.png').write_bytes(PNG)
	md = tmp / 'mr.md'
	md.write_text('# ntdll: Fix it\n\nBefore:\n\n![before](img/before.png)\n\n'
		      '<!-- internal -->\nInternal note, see ![shot](https://example.org/x.png)\n<!-- /internal -->\n'
		      'Done.\n')
	data = gitum(work, capsys, 'mr', 'set', 'fix', str(md))
	assert data['assets'] == ['before.png'] and data['title'] == 'ntdll: Fix it'
	j = journal(work)
	assert j.read('series/fix/assets/before.png') == PNG
	assert '](assets/before.png)' in j.read_text('series/fix/mr.md')

	# saving the series again keeps the text and the images
	assert main(['--repo', str(work.path), 'series', 'save', 'fix']) == 0
	assert journal(work).read('series/fix/assets/before.png') == PNG

	# upstream rendering: no internal part, a placeholder for the image; nothing goes to the forge
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'upstream')
	assert data['title'] == 'ntdll: Fix it' and data['project'] == 'wine/wine'
	assert 'Internal note' not in data['description'] and 'Done.' in data['description']
	assert '](' + '{{gitum-asset:before.png}})' in data['description'] and not FakeGitLab.requests

	# the URL of the uploaded image is used from now on; only the new image is left to upload
	gitum(work, capsys, 'mr', 'record', 'fix', 'before.png=/uploads/1/before.png', '--forge', 'upstream')
	(tmp / 'img' / 'after.png').write_bytes(SHOT)
	md.write_text(md.read_text() + '\nAfter:\n\n<img src="img/after.png" width="300">\n')
	gitum(work, capsys, 'mr', 'set', 'fix', str(md))
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'upstream')
	assert data['reused'] == ['before.png'] and [p['file'] for p in data['pending']] == ['after.png']
	assert '](/uploads/1/before.png)' in data['description']
	assert '<img src="{{gitum-asset:after.png}}"' in data['description']

	# internal rendering keeps the internal part
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--internal', '--forge', 'upstream')
	assert 'Internal note' in data['description']


def test_import_public_mr(world, capsys):
	work = world['work']
	FakeGitLab.mrs[('wine/wine', 7)] = {
		'title': 'ntdll: Fix it',
		'description': 'See ![s](/uploads/abc123/shot.png), again ![s](/uploads/abc123/shot.png).'}
	FakeGitLab.files['/wine/wine/uploads/abc123/shot.png'] = SHOT
	data = gitum(work, capsys, 'mr', 'import', 'fix', 'upstream:wine/wine!7')
	assert data['assets'] == ['shot.png'] and data['title'] == 'ntdll: Fix it'
	text = journal(work).read_text('series/fix/mr.md')
	assert text.startswith('# ntdll: Fix it\n') and text.count('](assets/shot.png)') == 2
	assert journal(work).read('series/fix/assets/shot.png') == SHOT
	assert all(m == 'GET' for m, _ in FakeGitLab.requests)
	# a private upload answers with a login page: refused, not stored as an image
	FakeGitLab.mrs[('wine/wine', 8)] = {'title': 'x', 'description': '![p](/uploads/def/private.png)'}
	err = gitum(work, capsys, 'mr', 'import', 'fix', 'upstream:wine/wine!8', expect=1)
	assert 'not an image' in err['error']


def test_separator_assets_dir_and_upstream_project(world, capsys):
	work, tmp = world['work'], world['tmp']
	(tmp / 'a.png').write_bytes(PNG)
	md = tmp / 'mr.md'
	md.write_text('# Title\n\nPublic ![a](a.png).\n\n---\n\nВнутреннее.\n')
	assert gitum(work, capsys, 'mr', 'set', 'fix', str(md))['internal']
	assert '<!-- internal -->' in journal(work).read_text('series/fix/mr.md')
	# no project for the forge, no links: taken from the remote of the series' upstream
	work.run('remote', 'set-url', 'winehq', 'git@127.0.0.1:wine/wine.git')
	out = tmp / 'lavtomate-in'
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'plain', '--assets-dir', str(out))
	assert data['project'] == 'wine/wine' and 'Внутреннее' not in data['description']
	assert data['pending'][0]['path'] == str(out / 'fix' / 'a.png') and (out / 'fix' / 'a.png').read_bytes() == PNG

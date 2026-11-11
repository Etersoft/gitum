"""Merge request texts with images: set, import from a forge, render for another forge."""

import http.server
import json
import threading

import pytest

from gitum.cli import main
from gitum.mrtext import strip_internal
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
	"""Merge requests, uploads by secret, and the upload API of one GitLab."""
	mrs = {}		# (project path, iid) -> {title, description}
	files = {}		# '/uploads/<secret>/<name>' -> bytes (per project, simplified)
	uploads = []		# (project path, file name, bytes)

	def _send(self, code, data, ctype='application/json'):
		self.send_response(code)
		self.send_header('Content-Type', ctype)
		self.end_headers()
		self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

	def do_GET(self):
		from urllib.parse import unquote
		path = self.path
		if self.headers.get('Authorization') != 'Bearer secret':
			return self._send(401, {})
		if path.startswith('/api/v4/projects/'):
			project, _, rest = path[len('/api/v4/projects/'):].partition('/')
			project = unquote(project)
			if rest.startswith('merge_requests/'):
				data = self.mrs.get((project, int(rest.split('/')[1])))
				return self._send(200 if data else 404, data or {})
			if rest.startswith('uploads/'):
				data = self.files.get('/' + rest)
				return self._send(200 if data else 404, data or b'', 'image/png')
		return self._send(404, {})

	def do_POST(self):
		from urllib.parse import unquote
		body = self.rfile.read(int(self.headers['Content-Length']))
		project = unquote(self.path[len('/api/v4/projects/'):].split('/')[0])
		name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
		data = body.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
		self.uploads.append((project, name, data))
		self._send(201, {'url': '/uploads/%032x/%s' % (len(self.uploads), name)})

	def log_message(self, *args):
		pass


@pytest.fixture
def world(tmp_path, capsys, monkeypatch):
	server = http.server.HTTPServer(('127.0.0.1', 0), FakeGitLab)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	FakeGitLab.mrs, FakeGitLab.files, FakeGitLab.uploads = {}, {}, []
	url = 'http://127.0.0.1:%d' % server.server_port
	monkeypatch.setenv('GITLAB_TOKEN', 'secret')
	up, work = make_stack(tmp_path)
	assert main(['--repo', str(work.path), *INIT_ARGS]) == 0
	cfg = tmp_path / 'cfg.toml'
	cfg.write_text(journal(work).config_text +
		       '\n[forge.internal]\ntype = "gitlab"\nurl = "%s"\n'
		       '[forge.upstream]\ntype = "gitlab"\nurl = "%s"\nproject = "wine/wine"\n'
		       '[forge.other]\ntype = "github"\nurl = "https://github.com"\nproject = "a/b"\n'
		       '[series]\ninternal-mr-forge = "internal"\npr-forge = "upstream"\n' % (url, url))
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


def test_set_render_upload_once(world, capsys):
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

	# upstream rendering: no internal part, the image uploaded to the upstream project
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'upstream')
	assert data['title'] == 'ntdll: Fix it' and data['project'] == 'wine/wine'
	assert 'Internal note' not in data['description'] and 'Done.' in data['description']
	assert FakeGitLab.uploads == [('wine/wine', 'before.png', PNG)]
	assert '](%s)' % data['uploaded'][0]['url'] in data['description']

	# a new version with the same image and a new one: only the new one is uploaded
	(tmp / 'img' / 'after.png').write_bytes(SHOT)
	md.write_text(md.read_text() + '\nAfter:\n\n<img src="img/after.png" width="300">\n')
	gitum(work, capsys, 'mr', 'set', 'fix', str(md))
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'upstream')
	assert [u[1] for u in FakeGitLab.uploads] == ['before.png', 'after.png']
	assert data['reused'] == ['before.png'] and '<img src="/uploads/' in data['description']

	# internal rendering keeps the internal part
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--internal', '--forge', 'upstream')
	assert 'Internal note' in data['description']


def test_placeholders_and_record(world, capsys):
	work, tmp = world['work'], world['tmp']
	(tmp / 'a.png').write_bytes(PNG)
	md = tmp / 'mr.md'
	md.write_text('# Title\n\n![a](a.png)\n')
	gitum(work, capsys, 'mr', 'set', 'fix', str(md))
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'other')
	assert not data['uploaded'] and data['pending'][0]['placeholder'] == '{{gitum-asset:a.png}}'
	assert open(data['pending'][0]['path'], 'rb').read() == PNG
	assert '](' + '{{gitum-asset:a.png}})' in data['description']
	# the external tool uploaded it: next time the URL is used
	gitum(work, capsys, 'mr', 'record', 'fix', 'a.png=https://github.com/u/a.png', '--forge', 'other')
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'other')
	assert not data['pending'] and '](https://github.com/u/a.png)' in data['description']


def test_import_from_internal_mr(world, capsys):
	work = world['work']
	FakeGitLab.mrs[('etersoft/wine', 7)] = {
		'title': 'ntdll: Fix it',
		'description': 'See ![s](/uploads/abc123/shot.png) and ![x](https://example.org/x.png).'}
	FakeGitLab.files['/uploads/abc123/shot.png'] = SHOT
	data = gitum(work, capsys, 'mr', 'import', 'fix', 'internal:etersoft/wine!7')
	assert data['assets'] == ['shot.png'] and data['title'] == 'ntdll: Fix it'
	text = journal(work).read_text('series/fix/mr.md')
	assert text.startswith('# ntdll: Fix it\n') and '](assets/shot.png)' in text
	assert '](https://example.org/x.png)' in text
	assert journal(work).read('series/fix/assets/shot.png') == SHOT
	data = gitum(work, capsys, 'mr', 'render', 'fix', '--forge', 'upstream', '--no-upload')
	assert data['pending'][0]['file'] == 'shot.png' and not FakeGitLab.uploads

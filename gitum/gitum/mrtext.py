"""Merge request texts with images, kept with a series and carried between forges.

The text lives in the journal as series/<name>/mr.md; its first line "# Title"
is the title. Images are series/<name>/assets/<file>, referenced as
assets/<file>. A part between the lines <!-- internal --> and <!-- /internal -->
(or from <!-- internal --> to the end) is for internal review only and is not
rendered for upstream.

Rendering for a forge uploads the images where the forge API allows it
(GitLab project uploads) and remembers the URLs, so that a new version of
the merge request uploads only new or changed images. Otherwise the text
gets placeholders and the images are written out for an external tool.
"""

import hashlib
import json
import os
import re
import secrets
import urllib.parse
import urllib.request
from pathlib import Path

from . import series as series_mod
from .errors import GitumError
from .git import RefTransaction
from .journal import JOURNAL_REF, JournalWriter

INTERNAL_START = '<!-- internal -->'
INTERNAL_END = '<!-- /internal -->'
IMAGE_RE = re.compile(r'(!\[[^\]]*\]\()([^)\s]+)((?:\s+"[^"]*")?\))|(<img\b[^>]*?\bsrc=")([^"]+)(")')
PLACEHOLDER = '{{gitum-asset:%s}}'


def text_path(name):
	return 'series/%s/mr.md' % name


def asset_path(name, file):
	return 'series/%s/assets/%s' % (name, file)


def strip_internal(text):
	"""Text without the internal parts."""
	out, skipping = [], False
	for line in text.split('\n'):
		if line.strip() == INTERNAL_START:
			skipping = True
			continue
		if line.strip() == INTERNAL_END:
			skipping = False
			continue
		if not skipping:
			out.append(line)
	return '\n'.join(out).strip('\n') + '\n'


def mark_internal(text, separator):
	"""Turn the first line equal to the separator (e.g. ---) into the internal marker."""
	if not separator or INTERNAL_START in text:
		return text
	lines = text.split('\n')
	for i, line in enumerate(lines):
		if line.strip() == separator:
			lines[i] = INTERNAL_START
			break
	return '\n'.join(lines)


def split_title(text):
	lines = text.lstrip('\n').split('\n')
	if lines and lines[0].startswith('# '):
		return lines[0][2:].strip(), '\n'.join(lines[1:]).strip('\n') + '\n'
	return '', text


def replace_images(text, fn):
	"""Replace every image link (markdown or <img src>) by fn(link) unless it returns None."""
	def sub(m):
		if m.group(1):
			new = fn(m.group(2))
			return m.group(0) if new is None else m.group(1) + new + m.group(3)
		new = fn(m.group(5))
		return m.group(0) if new is None else m.group(4) + new + m.group(6)
	return IMAGE_RE.sub(sub, text)


def unique_name(name, taken):
	base, ext = os.path.splitext(re.sub(r'[^\w.+-]+', '_', name) or 'image')
	candidate, i = base + ext, 1
	while candidate in taken:
		i += 1
		candidate = '%s-%d%s' % (base, i, ext)
	taken.add(candidate)
	return candidate


def _store(repo, name, text, assets, message):
	"""Write mr.md and assets of a series (assets: {file: bytes}) in one journal commit."""
	with repo.lock():
		series_mod.check_claims(repo, [name])
		journal = repo.journal()
		meta = series_mod.load(journal, name)
		writer = JournalWriter(repo.git, journal.sha)
		text = mark_internal(text, journal.config.series.get('mr-internal-separator'))
		writer.remove_dir('series/%s/assets' % name)
		writer.put(text_path(name), text)
		for file, data in assets.items():
			writer.put(asset_path(name, file), data)
		meta['mr'] = {'assets': {f: hashlib.sha256(d).hexdigest() for f, d in assets.items()},
			      'uploads': (meta.get('mr') or {}).get('uploads', {}), 'title': split_title(text)[0]}
		series_mod.touch(meta, event='mr text')
		writer.put_json(series_mod.meta_path(name), meta)
		writer.put('series/%s/definition' % name, series_mod.definition_text(meta, journal.config))
		new = writer.write('gitum: series %s: %s' % (name, message))
		tr = RefTransaction(repo.git)
		tr.update(JOURNAL_REF, new, journal.sha)
		tr.commit()
	return {'series': name, 'assets': sorted(assets), 'title': split_title(text)[0],
		'internal': INTERNAL_START in text}


def set_from_file(repo, name, path):
	"""Take the text from a markdown file; local images it refers to become assets."""
	path = Path(path)
	text = path.read_text()
	assets, taken = {}, set()

	def take(link):
		if re.match(r'^[a-z][a-z0-9+.-]*:', link) or link.startswith('/'):
			return None
		local = (path.parent / urllib.parse.unquote(link)).resolve()
		if not local.is_file():
			raise GitumError('%s: image %s does not exist' % (path, link))
		file = unique_name(local.name, taken)
		assets[file] = local.read_bytes()
		return 'assets/' + file

	text = replace_images(text, take)
	return _store(repo, name, text, assets, 'merge request text from %s' % path.name)


# forges

def _forge(cfg, forge_name):
	conf = cfg.forges.get(forge_name)
	if not conf or not conf.get('url'):
		raise GitumError('no [forge.%s] url in gitum.toml' % forge_name)
	from .forge import TOKEN_ENV
	kind = conf.get('type', 'gitlab')
	base = conf['url'].rstrip('/')
	api = conf.get('api') or {'gitlab': base + '/api/v4', 'forgejo': base + '/api/v1',
				  'github': 'https://api.github.com'}[kind]
	token = os.environ.get(conf.get('token-env') or TOKEN_ENV[kind], '')
	return kind, base, api, token, conf


def _headers(kind, token):
	headers = {'User-Agent': 'gitum'}
	if token:
		headers['Authorization'] = ('token %s' if kind == 'forgejo' else 'Bearer %s') % token
	return headers


def _http(url, kind, token, data=None, content_type=None):
	headers = _headers(kind, token)
	if content_type:
		headers['Content-Type'] = content_type
	req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
	try:
		with urllib.request.urlopen(req, timeout=60) as resp:
			return resp.read()
	except OSError as e:
		raise GitumError('%s: %s' % (url, e)) from e


def import_mr(repo, name, link):
	"""Take the title, description and images of an existing merge request."""
	cfg = repo.journal().config
	url = series_mod.normalize_link(cfg, 'internal_mr', link) if '://' not in link else link
	from .forge import TOKEN_ENV, parse_url
	kind, api, project, num, _conf = parse_url(url, cfg.forges)
	forge_name = next((n for n, c in cfg.forges.items() if url.startswith(c.get('url', '').rstrip('/') + '/')), None)
	token = _forge(cfg, forge_name)[3] if forge_name else os.environ.get(TOKEN_ENV[kind], '')
	if kind == 'gitlab':
		data = json.loads(_http('%s/projects/%s/merge_requests/%d' % (api, urllib.parse.quote(project, safe=''), num),
				       kind, token))
		title, body = data.get('title', ''), data.get('description') or ''
	else:
		data = json.loads(_http('%s/repos/%s/pulls/%d' % (api, project, num), kind, token))
		title, body = data.get('title', ''), data.get('body') or ''
	site = url.split('://', 1)[0] + '://' + url.split('://', 1)[1].split('/', 1)[0]
	base = url[:url.index('/' + project + '/') + len(project) + 1]
	assets, taken = {}, set()

	def take(link):
		m = re.match(r'^/uploads/([0-9a-f]+)/([^/?#]+)$', link)
		if kind == 'gitlab' and m:
			# GitLab: relative to the project; the API serves it with a token, the web only with a session
			file = unique_name(urllib.parse.unquote(m.group(2)), taken)
			try:
				assets[file] = _http('%s/projects/%s/uploads/%s/%s' % (
					api, urllib.parse.quote(project, safe=''), m.group(1), m.group(2)), kind, token)
			except GitumError:
				assets[file] = _http(base + link, kind, token)
			return 'assets/' + file
		if link.startswith('/uploads/'):
			src = base + link
		elif link.startswith('/'):
			src = site + link
		elif link.startswith(site + '/'):
			src = link
		else:
			return None		# an image somewhere else stays a link
		file = unique_name(urllib.parse.unquote(src.rsplit('/', 1)[-1].split('?')[0]), taken)
		assets[file] = _http(src, kind, token)
		return 'assets/' + file

	text = ('# %s\n\n' % title if title else '') + replace_images(body, take).strip('\n') + '\n'
	return _store(repo, name, text, assets, 'merge request text from %s' % url)


def _multipart(field, filename, data):
	boundary = 'gitum' + secrets.token_hex(12)
	body = (b'--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
		b'Content-Type: application/octet-stream\r\n\r\n' % (boundary.encode(), field.encode(), filename.encode())
		+ data + b'\r\n--%s--\r\n' % boundary.encode())
	return body, 'multipart/form-data; boundary=' + boundary


def remote_project(remote_url, forge_url):
	"""group/project of a remote URL on the forge's host (https, ssh:// or scp-like), or None."""
	host = urllib.parse.urlsplit(forge_url).hostname
	m = (re.match(r'^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$', remote_url)
	     or re.match(r'^(?:[^@/]+@)?([^/:]+):(?!/)(.+?)(?:\.git)?/?$', remote_url))
	return m.group(2) if m and host and m.group(1) == host else None


def _upstream_project(git, cfg, meta, conf):
	"""The project of the series' upstream (or of any upstream) when its remote is on this forge."""
	if not conf.get('url'):
		return None
	kind, _, target = (meta.get('base') or '').partition(':')
	names = ([target] if kind == 'upstream' and target in cfg.upstreams else []) + list(cfg.upstreams)
	for n in names:
		up = cfg.upstreams[n]
		ref = up.ref.removeprefix('refs/remotes/')
		remote = up.remote or (ref.split('/', 1)[0] if '/' in ref and not ref.startswith('refs/') else '')
		url = up.url or (git.out('remote', 'get-url', remote, check=False) if remote else '')
		found = url and remote_project(url.strip(), conf['url'])
		if found:
			return found
	return None


def _project_for(git, cfg, meta, forge_name, project):
	if project:
		return project
	conf = cfg.forges.get(forge_name, {})
	for key in ('pr', 'internal_mr'):
		url = meta.get(key, '')
		if url and url.startswith(conf.get('url', '').rstrip('/') + '/'):
			from .forge import parse_url
			return parse_url(url, cfg.forges)[2]
	if conf.get('project'):
		return conf['project']
	found = _upstream_project(git, cfg, meta, conf)
	if found:
		return found
	raise GitumError('which project of %s? use --project or set [forge.%s] project' % (forge_name, forge_name))


def render(repo, name, forge_name=None, project=None, internal=False, upload=True, assets_dir=None):
	"""The title and description for a forge; images are uploaded there or left as placeholders."""
	journal = repo.journal()
	cfg = journal.config
	meta = series_mod.load(journal, name)
	text = journal.read_text(text_path(name))
	if text is None:
		raise GitumError('series %s has no merge request text: gitum mr set or gitum mr import' % name)
	if not internal:
		text = strip_internal(text)
	title, description = split_title(text)
	assets = (meta.get('mr') or {}).get('assets', {})
	key = '%s:%s' % (forge_name, _project_for(repo.git, cfg, meta, forge_name, project)) if forge_name else None
	known = dict(((meta.get('mr') or {}).get('uploads') or {}).get(key, {})) if key else {}
	kind, api, token = (None, None, '')
	if forge_name:
		kind, _base, api, token, _conf = _forge(cfg, forge_name)
	uploaded, pending = [], []
	assets_dir = assets_dir or cfg.series.get('mr-assets-dir')
	out_dir = (Path(assets_dir).expanduser() if assets_dir else repo.gitum_dir / 'mr') / name

	def place(link):
		if not link.startswith('assets/'):
			return None
		file = link[len('assets/'):]
		digest = assets.get(file)
		if digest is None:
			raise GitumError('series %s: %s is not among its assets' % (name, link))
		if digest in known:
			return known[digest]
		if forge_name and upload and kind == 'gitlab' and token:
			body, ctype = _multipart('file', file, journal.read(asset_path(name, file)))
			proj = urllib.parse.quote(key.split(':', 1)[1], safe='')
			data = json.loads(_http('%s/projects/%s/uploads' % (api, proj), kind, token, body, ctype))
			known[digest] = data['url']
			uploaded.append({'file': file, 'url': data['url']})
			return data['url']
		out_dir.mkdir(parents=True, exist_ok=True)
		(out_dir / file).write_bytes(journal.read(asset_path(name, file)))
		pending.append({'file': file, 'path': str(out_dir / file), 'placeholder': PLACEHOLDER % file, 'sha256': digest})
		return PLACEHOLDER % file

	description = replace_images(description, place)
	if uploaded:
		_remember(repo, name, key, {d: u for d, u in known.items()})
	return {'series': name, 'forge': forge_name, 'project': key.split(':', 1)[1] if key else None, 'title': title,
		'description': description, 'uploaded': uploaded, 'pending': pending,
		'reused': sorted(f for f, d in assets.items() if d in known and f not in {u['file'] for u in uploaded})}


def _remember(repo, name, key, urls):
	with repo.lock():
		journal = repo.journal()
		meta = series_mod.load(journal, name)
		mr = meta.setdefault('mr', {'assets': {}})
		mr.setdefault('uploads', {})[key] = urls
		writer = JournalWriter(repo.git, journal.sha)
		writer.put_json(series_mod.meta_path(name), meta)
		new = writer.write('gitum: series %s: images uploaded to %s' % (name, key))
		tr = RefTransaction(repo.git)
		tr.update(JOURNAL_REF, new, journal.sha)
		tr.commit()


def record_uploads(repo, name, forge_name, project, pairs):
	"""Remember URLs of images uploaded by an external tool: pairs {file: url}."""
	journal = repo.journal()
	meta = series_mod.load(journal, name)
	assets = (meta.get('mr') or {}).get('assets', {})
	key = '%s:%s' % (forge_name, _project_for(repo.git, journal.config, meta, forge_name, project))
	urls = dict(((meta.get('mr') or {}).get('uploads') or {}).get(key, {}))
	for file, url in pairs.items():
		if file not in assets:
			raise GitumError('series %s has no asset %s' % (name, file))
		urls[assets[file]] = url
	_remember(repo, name, key, urls)
	return {'series': name, 'forge': key, 'recorded': sorted(pairs)}

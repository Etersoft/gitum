"""Merge request texts with images, kept with a series and carried between forges.

The text lives in the journal as series/<name>/mr.md; its first line "# Title"
is the title. Images are series/<name>/assets/<file>, referenced as
assets/<file>. A part between the lines <!-- internal --> and <!-- /internal -->
(or from <!-- internal --> to the end) is for internal review only and is not
rendered for upstream.

gitum never uploads anything and uses no tokens. Rendering puts placeholders
in place of the images and writes the images out; whoever opens the merge
request uploads them and tells gitum the URLs (gitum mr record), so that the
next version of the text uses them and only new or changed images are left
to upload. Importing reads public merge requests only.
"""

import hashlib
import os
import re
import urllib.parse
from pathlib import Path

from . import series as series_mod
from .errors import GitumError
from .journal import JournalWriter

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
		writer.commit('gitum: series %s: %s' % (name, message))
	return {'series': name, 'assets': sorted(assets), 'title': split_title(text)[0],
		'internal': INTERNAL_START in text}


def set_from_file(repo, name, path):
	"""Take the text from a markdown file; local images it refers to become assets."""
	path = Path(path)
	text = path.read_text()
	assets, taken, by_path = {}, set(), {}

	def take(link):
		if re.match(r'^[a-z][a-z0-9+.-]*:', link) or link.startswith('/'):
			return None
		local = (path.parent / urllib.parse.unquote(link)).resolve()
		if not local.is_file():
			raise GitumError('%s: image %s does not exist' % (path, link))
		if local not in by_path:
			by_path[local] = unique_name(local.name, taken)
			assets[by_path[local]] = local.read_bytes()
		return 'assets/' + by_path[local]

	text = replace_images(text, take)
	return _store(repo, name, text, assets, 'merge request text from %s' % path.name)


# public merge requests

def _get_image(url):
	from .forge import http_get
	data, ctype = http_get(url, 'image/*')
	if not (ctype.startswith('image/') or ctype in ('application/octet-stream', 'binary/octet-stream')):
		raise GitumError('%s: got %s, not an image (is the project public?)' % (url, ctype))
	return data


def import_mr(repo, name, link):
	"""Take the title, description and images of a public merge request."""
	cfg = repo.journal().config
	url = series_mod.normalize_link(cfg, 'internal_mr', link) if '://' not in link else link
	from .forge import get_json as _get_json, parse_url
	kind, api, project, num, _conf = parse_url(url, cfg.forges)
	if kind == 'gitlab':
		data = _get_json('%s/projects/%s/merge_requests/%d' % (api, urllib.parse.quote(project, safe=''), num))
		title, body = data.get('title', ''), data.get('description') or ''
	else:
		data = _get_json('%s/repos/%s/pulls/%d' % (api, project, num))
		title, body = data.get('title', ''), data.get('body') or ''
	site = url.split('://', 1)[0] + '://' + url.split('://', 1)[1].split('/', 1)[0]
	base = url[:url.index('/' + project + '/') + len(project) + 1]
	assets, taken, by_src = {}, set(), {}

	def take(link):
		if link.startswith('/uploads/'):
			src = base + link		# GitLab: relative to the project
		elif link.startswith('/'):
			src = site + link
		elif link.startswith(('https://', 'http://')):
			src = link
		else:
			return None
		if src not in by_src:
			file = unique_name(urllib.parse.unquote(src.rsplit('/', 1)[-1].split('?')[0]), taken)
			assets[file] = _get_image(src)
			by_src[src] = file
		return 'assets/' + by_src[src]

	text = ('# %s\n\n' % title if title else '') + replace_images(body, take).strip('\n') + '\n'
	return _store(repo, name, text, assets, 'merge request text from %s' % url)


# rendering for a forge

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
		if url and conf.get('url') and url.startswith(conf['url'].rstrip('/') + '/'):
			from .forge import parse_url
			return parse_url(url, cfg.forges)[2]
	if conf.get('project'):
		return conf['project']
	found = _upstream_project(git, cfg, meta, conf)
	if found:
		return found
	raise GitumError('which project of %s? use --project or set [forge.%s] project' % (forge_name, forge_name))


def _key(git, cfg, meta, forge_name, project):
	return '%s:%s' % (forge_name, _project_for(git, cfg, meta, forge_name, project))


def render(repo, name, forge_name=None, project=None, internal=False, assets_dir=None):
	"""The title and description for a forge: known image URLs there, placeholders for the rest."""
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
	key = _key(repo.git, cfg, meta, forge_name, project) if forge_name else None
	known = ((meta.get('mr') or {}).get('uploads') or {}).get(key, {}) if key else {}
	assets_dir = assets_dir or cfg.series.get('mr-assets-dir')
	out_dir = (Path(assets_dir).expanduser() if assets_dir else repo.gitum_dir / 'mr') / name
	pending, reused = [], []

	def place(link):
		if not link.startswith('assets/'):
			return None
		file = link[len('assets/'):]
		digest = assets.get(file)
		if digest is None:
			raise GitumError('series %s: %s is not among its assets' % (name, link))
		if digest in known:
			if file not in reused:
				reused.append(file)
			return known[digest]
		if file not in {p['file'] for p in pending}:
			out_dir.mkdir(parents=True, exist_ok=True)
			(out_dir / file).write_bytes(journal.read(asset_path(name, file)))
			pending.append({'file': file, 'path': str(out_dir / file), 'placeholder': PLACEHOLDER % file,
					'sha256': digest})
		return PLACEHOLDER % file

	description = replace_images(description, place)
	return {'series': name, 'forge': forge_name, 'project': key.split(':', 1)[1] if key else None, 'title': title,
		'description': description, 'pending': pending, 'reused': sorted(reused)}


def record_uploads(repo, name, forge_name, project, pairs):
	"""Remember URLs of images uploaded outside gitum: pairs {file: url}."""
	with repo.lock():
		journal = repo.journal()
		meta = series_mod.load(journal, name)
		assets = (meta.get('mr') or {}).get('assets', {})
		key = _key(repo.git, journal.config, meta, forge_name, project)
		for file in pairs:
			if file not in assets:
				raise GitumError('series %s has no asset %s' % (name, file))
		mr = meta.setdefault('mr', {'assets': {}})
		urls = mr.setdefault('uploads', {}).setdefault(key, {})
		urls.update({assets[f]: u for f, u in pairs.items()})
		writer = JournalWriter(repo.git, journal.sha)
		writer.put_json(series_mod.meta_path(name), meta)
		writer.commit('gitum: series %s: image URLs on %s' % (name, key))
	return {'series': name, 'forge': key, 'recorded': sorted(pairs)}

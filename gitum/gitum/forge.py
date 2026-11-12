"""Forge states of pull/merge requests: GitHub, GitLab, Forgejo/Gitea, or JSON from outside.

A change or a series refers to its request by URL (meta 'pr', 'internal_mr').
`refresh` asks the forge APIs about public requests, without any credentials; `import_json` takes the same data prepared by
somebody else (for example an agent with Lavtomate MCP), `export_json` gives
everything gitum knows.
"""

import json
import re
import urllib.parse
import urllib.request

from . import bugs as bugs_mod
from . import changes as changes_mod
from . import series as series_mod
from .errors import GitumError
from .journal import JournalWriter

GITHUB_RE = re.compile(r'^https?://(?P<host>github\.com)/(?P<path>[^/]+/[^/]+)/pull/(?P<num>\d+)')
GITLAB_RE = re.compile(r'^(?P<scheme>https?)://(?P<host>[^/]+)/(?P<path>.+?)/(?:-/)?merge_requests/(?P<num>\d+)')
FORGEJO_RE = re.compile(r'^(?P<scheme>https?)://(?P<host>[^/]+)/(?P<path>[^/]+/[^/]+)/pulls/(?P<num>\d+)')


def parse_url(url, forges=None):
	"""(kind, api base, project path, number) of a request URL."""
	for name, conf in (forges or {}).items():
		base = conf.get('url', '').rstrip('/')
		if base and url.startswith(base + '/'):
			kind = conf.get('type', 'gitlab')
			rest = url[len(base) + 1:]
			m = {'gitlab': re.match(r'(?P<path>.+?)/(?:-/)?merge_requests/(?P<num>\d+)', rest),
			     'forgejo': re.match(r'(?P<path>[^/]+/[^/]+)/pulls/(?P<num>\d+)', rest),
			     'github': re.match(r'(?P<path>[^/]+/[^/]+)/pull/(?P<num>\d+)', rest)}.get(kind)
			if m:
				api = conf.get('api') or {'gitlab': base + '/api/v4', 'forgejo': base + '/api/v1',
							  'github': 'https://api.github.com'}[kind]
				return kind, api, m['path'], int(m['num']), conf
	m = GITHUB_RE.match(url)
	if m:
		return 'github', 'https://api.github.com', m['path'], int(m['num']), {}
	m = GITLAB_RE.match(url)
	if m:
		return 'gitlab', '%s://%s/api/v4' % (m['scheme'], m['host']), m['path'], int(m['num']), {}
	m = FORGEJO_RE.match(url)
	if m:
		return 'forgejo', '%s://%s/api/v1' % (m['scheme'], m['host']), m['path'], int(m['num']), {}
	raise GitumError('unknown request URL %s' % url)


def http_get(url, accept='application/json'):
	"""GET a public URL without credentials: (body, content type)."""
	req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'gitum'})
	try:
		with urllib.request.urlopen(req, timeout=60) as resp:
			return resp.read(), resp.headers.get_content_type()
	except OSError as e:
		raise GitumError('%s: %s' % (url, e)) from e


def get_json(url):
	data, _ctype = http_get(url)
	try:
		return json.loads(data)
	except ValueError as e:
		raise GitumError('%s: not a JSON answer (is the request public?)' % url) from e


def _approved(url, kind):
	"""Whether an open request is approved by a reviewer; None if that is unknown."""
	try:
		data = get_json(url)
	except GitumError:
		return None
	if kind == 'gitlab':
		return bool(data.get('approved') or data.get('approved_by'))
	latest = {}
	for r in data if isinstance(data, list) else []:
		if isinstance(r, dict) and r.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
			latest[(r.get('user') or {}).get('login')] = r['state']
	return bool(latest) and 'APPROVED' in latest.values() and 'CHANGES_REQUESTED' not in latest.values()


def request_state(url, forges=None):
	"""State of a pull/merge request: open, approved, merged or closed; merge commit and head commit."""
	kind, api, path, num, conf = parse_url(url, forges)
	if kind == 'gitlab':
		base = '%s/projects/%s/merge_requests/%d' % (api, urllib.parse.quote(path, safe=''), num)
		data = get_json(base)
		state = {'opened': 'open', 'merged': 'merged', 'closed': 'closed', 'locked': 'open'}.get(data.get('state'), 'open')
		commit = (data.get('merge_commit_sha') or data.get('squash_commit_sha') or '') if state == 'merged' else ''
		head = data.get('sha') or ''
		merged_at = data.get('merged_at') or ''
		reviews = base + '/approvals'
	else:
		base = '%s/repos/%s/pulls/%d' % (api, path, num)
		data = get_json(base)
		state = 'merged' if data.get('merged') else data.get('state', 'open')
		commit = (data.get('merge_commit_sha') or '') if data.get('merged') else ''
		head = (data.get('head') or {}).get('sha', '')
		merged_at = data.get('merged_at') or ''
		reviews = base + '/reviews'
	result = {'state': state, 'merge_commit': commit or '', 'head': head, 'merged_at': merged_at, 'url': url}
	if state == 'open':
		approved = _approved(reviews, kind)
		if approved:
			result['state'] = 'approved'
		elif approved is None:
			result['approval_unknown'] = True
	return result


CHANGE_FATE = {'open': 'sent', 'approved': 'sent', 'merged': 'merged', 'closed': 'rejected'}
SERIES_STATE = {'open': 'sent', 'approved': 'approved', 'merged': 'merged', 'closed': 'rejected'}


SERIES_ITEM_KEYS = {'series', 'state', 'pr', 'internal_mr', 'upstream_commit', 'version', 'reviewer', 'note',
		    'merged_at', 'merge_commit', 'head', 'approval_unknown', 'url', 'bugs', 'title'}


def _apply(cfg, tracker, all_series, item):
	"""Apply one state record; return a short description of what changed or None."""
	if item.get('series'):
		meta = all_series.get(item['series'])
		if meta is None:
			raise GitumError('no series %s' % item['series'])
		unknown = set(item) - SERIES_ITEM_KEYS
		if unknown:
			raise GitumError('series %s: unknown keys %s' % (item['series'], ', '.join(sorted(unknown))))
		before = json.dumps(meta, sort_keys=True)
		old_state = meta.get('state')
		state = item.get('state')
		if state in SERIES_STATE:
			state = SERIES_STATE[state]
		if item.get('approval_unknown') and state == 'sent' and old_state == 'approved':
			state = 'approved'		# the approval could not be read: keep what is known
		if state:
			if state not in series_mod.STATES:
				raise GitumError('unknown series state %s' % state)
			meta['state'] = state
		for key in ('pr', 'internal_mr', 'upstream_commit', 'version', 'reviewer', 'note', 'merged_at', 'title'):
			if item.get(key):
				meta[key] = series_mod.normalize_link(cfg, key, item[key]) if key in ('pr', 'internal_mr') \
					else item[key]
		if item.get('bugs'):
			meta['bugs'] = bugs_mod.normalize_list(cfg, list(meta.get('bugs') or []) + list(item['bugs']))
		if item.get('merge_commit'):
			meta['upstream_commit'] = item['merge_commit']
		if item.get('head'):
			meta['mr_head'] = item['head']
		if json.dumps(meta, sort_keys=True) == before:
			return None
		if meta.get('state') != old_state:
			series_mod.touch(meta, state=meta['state'], previous=old_state, source='forge')
		else:
			series_mod.touch(meta)
		return 'series ' + item['series']
	rec = tracker.get(item['id'])
	before = json.dumps(rec, sort_keys=True)
	fate = item.get('fate') or CHANGE_FATE.get(item.get('state'))
	meta = {k: item[k] for k in changes_mod.META_KEYS if item.get(k) and k != 'series'}
	if item.get('merge_commit'):
		meta['upstream_commit'] = item['merge_commit']
	tracker.set(rec['id'], fate, **meta)
	return ('change ' + rec['id']) if json.dumps(rec, sort_keys=True) != before else None


def _write(repo, journal, tracker, all_series, touched_series, message):
	writer = JournalWriter(repo.git, journal.sha)
	tracker.write(writer)
	for name in touched_series:
		writer.put_json(series_mod.meta_path(name), all_series[name])
	writer.commit(message)


def import_json(repo, items):
	"""Apply a list of records: {"id": change, ...} or {"series": name, ...}."""
	if isinstance(items, dict):
		items = items.get('changes', []) + items.get('series', [])
	with repo.lock():
		journal = repo.journal()
		tracker = changes_mod.Tracker(journal)
		all_series = series_mod.load_all(journal)
		states_before = {n: m.get('state') for n, m in all_series.items()}
		updated = [u for u in (_apply(journal.config, tracker, all_series, i) for i in items) if u]
		touched = sorted({u.split(' ', 1)[1] for u in updated if u.startswith('series ')})
		if updated:
			_write(repo, journal, tracker, all_series, touched, 'gitum: forge import (%d updates)' % len(updated))
		for name in touched:
			if all_series[name].get('state') != states_before.get(name):
				series_mod.state_hook(repo, journal.config, all_series[name], states_before.get(name), 'forge')
		return {'updated': updated}


def export_json(repo):
	journal = repo.journal()
	return {'changes': sorted(changes_mod.load_all(journal).values(), key=lambda c: c['id']),
		'series': sorted(series_mod.load_all(journal).values(), key=lambda s: s['name'])}


def refresh(repo, fetcher=request_state):
	"""Ask the forges about every change and series with a request link that is still open."""
	journal = repo.journal()
	forges = journal.config.forges
	items, errors = [], []
	for rec in changes_mod.load_all(journal).values():
		url = rec.get('meta', {}).get('pr')
		if url and rec.get('fate') in ('sent', 'review', 'queued', 'draft', 'local'):
			try:
				items.append({'id': rec['id'], **fetcher(url, forges)})
			except GitumError as e:
				errors.append(str(e))
	outdated = []
	for meta in series_mod.load_all(journal).values():
		url = meta.get('pr')
		if url and meta.get('state') not in series_mod.DONE_STATES + ('disabled',):
			try:
				state = fetcher(url, forges)
			except GitumError as e:
				errors.append(str(e))
				continue
			items.append({'series': meta['name'], **state})
			prepared = (meta.get('prepared') or {}).get('tip')
			if state.get('head') and prepared and state['head'] != prepared and state['state'] != 'merged':
				outdated.append({'series': meta['name'], 'head': state['head'], 'prepared': prepared})
	for i in items:
		i.pop('url', None)
	result = import_json(repo, items) if items else {'updated': []}
	result['errors'] = errors
	result['checked'] = len(items)
	result['outdated'] = outdated
	return result

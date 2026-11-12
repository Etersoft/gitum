"""Series: named sets of changes with their own base, metadata and patches.

In the journal a series is a directory compatible with wine-staging:
    series/<name>/definition     Fixes:, Depends:, Disabled: (+ our keys)
    series/<name>/0001-*.patch   format-patch from the series base
    series/<name>/meta.json      base, branch, state, links, changes
Work on a series happens in its git branch; `gitum series save` records it.
"""

import json
import os
import re
import socket
import tempfile
from pathlib import Path

from . import bugs as bugs_mod
from . import changes as changes_mod
from .errors import Busy, GitumError, NeedsHuman
from .git import RefTransaction
from .journal import JOURNAL_REF, JournalWriter, LayerState, describe_changes
from .util import now

# draft -> review (internal) -> queued -> sent (upstream, version vN) -> approved (by an upstream
# reviewer) -> merged | rejected | superseded (upstream solved it another way); disabled: not applied
STATES = ('draft', 'review', 'queued', 'sent', 'approved', 'merged', 'rejected', 'superseded', 'disabled')
DONE_STATES = ('merged', 'rejected', 'superseded')


def state_rank(state):
	"""Progress of a state: finished states rank highest, disabled lowest."""
	if state in DONE_STATES:
		return len(STATES)
	return STATES.index(state) if state in STATES and state != 'disabled' else -1
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]*$')
SET_KEYS = {'state', 'pr', 'internal_mr', 'bugs', 'depends', 'disabled', 'title', 'version', 'base', 'branch',
	    'note', 'upstream_commit', 'fixes', 'upstream', 'export_dir', 'target', 'reviewer', 'position',
	    'merged_at', 'release'}
LINK_FIELDS = ('pr', 'internal_mr')
LINK_RE = re.compile(r'^(?:([A-Za-z0-9][\w.-]*):)?([\w.-]+(?:/[\w.-]+)+)!(\d+)$')
MR_URL = {'gitlab': '{url}/{project}/-/merge_requests/{n}', 'forgejo': '{url}/{project}/pulls/{n}',
	  'github': '{url}/{project}/pull/{n}'}


def normalize_link(cfg, field, value):
	"""A merge request link as a URL; [forge:]group/project!N is resolved through [forge.NAME].

	Without a forge name the default of the field is used: [series] pr-forge or
	internal-mr-forge.
	"""
	if not value or not isinstance(value, str) or value.startswith(('http://', 'https://')):
		return value
	m = LINK_RE.match(value.strip())
	if not m:
		raise GitumError('%s: %r is neither a URL nor [forge:]group/project!N' % (field, value))
	name = m.group(1) or cfg.series.get(field.replace('_', '-') + '-forge')
	conf = cfg.forges.get(name) if name else None
	if not conf or not conf.get('url'):
		raise GitumError('%s: %r needs a forge: write NAME:%s with [forge.NAME] url in gitum.toml, or set '
				 '[series] %s-forge' % (field, value, value, field.replace('_', '-')))
	template = conf.get('mr-url') or MR_URL[conf.get('type', 'gitlab')]
	return template.format(url=conf['url'].rstrip('/'), project=m.group(2), n=m.group(3))
# files of a series directory that gitum writes; everything else there belongs to people
MANAGED = ('definition',)


def is_managed(path):
	return path.is_file() and (path.name in MANAGED or path.name.endswith('.patch'))


def touch(meta, **event):
	"""Mark a series as updated; state changes go to its history."""
	meta['updated'] = now()
	if event:
		meta.setdefault('history', []).append({'date': meta['updated'], **event})


def check_claims(repo, names):
	"""Stop if another session holds one of the series (see claim)."""
	me = session_id()
	for name in names:
		owner = claim_owner(repo, name)
		if owner and owner.get('session') != me:
			raise Busy('series %s is claimed by %s (since %s); gitum series release %s --force to take it'
				   % (name, owner.get('session'), owner.get('since'), name), owner=owner)


def session_id():
	return os.environ.get('GITUM_SESSION') or '%s@%s' % (os.environ.get('USER', ''), socket.gethostname())


def claims_dir(repo):
	return repo.gitum_dir / 'claims'


def claim_owner(repo, name):
	path = claims_dir(repo) / ('%s.json' % name)
	return json.loads(path.read_text()) if path.exists() else None


def claim(repo, name, reason='', force=False):
	"""Mark a series as being worked on by this session (shared by all work trees of the repository)."""
	with repo.lock():
		load(repo.journal(), name)
		owner = claim_owner(repo, name)
		if owner and owner.get('session') != session_id() and not force:
			raise Busy('series %s is claimed by %s since %s' % (name, owner.get('session'), owner.get('since')),
				   owner=owner)
		data = {'series': name, 'session': session_id(), 'pid': os.getpid(), 'host': socket.gethostname(),
			'since': now(), 'reason': reason}
		repo.write_json('claims/%s.json' % name, data)
	return data


def release(repo, name, force=False):
	with repo.lock():
		owner = claim_owner(repo, name)
		if not owner:
			return None
		if owner.get('session') != session_id() and not force:
			raise Busy('series %s is claimed by %s; use --force' % (name, owner.get('session')), owner=owner)
		(claims_dir(repo) / ('%s.json' % name)).unlink()
	return owner


def export_dir(meta):
	"""Directory of a series relative to an export root: export_dir or the series name."""
	rel = meta.get('export_dir') or meta['name']
	parts = Path(rel).parts
	if Path(rel).is_absolute() or '..' in parts or not parts:
		raise GitumError('series %s: bad export_dir %r' % (meta['name'], rel))
	return Path(*parts)


def meta_path(name):
	return 'series/%s/meta.json' % name


def check_name(name):
	if not NAME_RE.match(name):
		raise GitumError('bad series name %r' % name)


def load_all(journal):
	result = {}
	for path, data in journal.read_files('series', '/meta.json').items():
		parts = path.split('/')
		if len(parts) == 3 and data:
			result[parts[1]] = json.loads(data)
	return result


def load(journal, name):
	meta = journal.read_json(meta_path(name))
	if not meta:
		raise GitumError('no series %s' % name)
	return meta


def base_ref(cfg, spec):
	"""A git ref for a series base: upstream:<name>, layer:<name> or any ref."""
	kind, _, target = spec.partition(':')
	if kind == 'upstream' and target in cfg.upstreams:
		return cfg.upstreams[target].ref
	if kind == 'layer' and target in cfg.layers:
		return 'refs/heads/' + cfg.layers[target].branch
	if kind == 'series':
		return 'refs/heads/series/' + target
	return spec


def definition_text(meta, cfg=None):
	"""A wine-staging definition file; with [series] fixes-tracker its bugs are Fixes: lines."""
	lines = []
	for fix in bugs_mod.fixes_lines(cfg, meta) if cfg else meta.get('fixes', []):
		lines.append('Fixes: %s' % fix)
	for dep in meta.get('depends', []):
		lines.append('Depends: %s' % dep)
	if meta.get('disabled'):
		reason = meta['disabled'] if meta['disabled'] is not True else ''
		for line in str(reason).splitlines():
			lines.append('# %s' % line)
		lines.append('Disabled: true')
	return '\n'.join(lines) + ('\n' if lines else '')


def parse_definition(text):
	meta = {'fixes': [], 'depends': [], 'disabled': ''}
	comments = []
	for line in text.splitlines():
		stripped = line.strip()
		if stripped.startswith('#'):
			comments.append(stripped.lstrip('#').strip())
			continue
		key, sep, value = stripped.partition(':')
		if not sep:
			continue
		key, value = key.strip().lower(), value.strip()
		if key == 'fixes':
			meta['fixes'].append(value)
		elif key == 'depends':
			meta['depends'].append(value)
		elif key == 'disabled' and value.lower() == 'true':
			meta['disabled'] = '\n'.join(comments) or True
		elif key == 'helps':
			meta.setdefault('helps', []).append(value)
	return meta


def order(all_series, names):
	"""Names with their dependencies first."""
	result, seen = [], set()

	def visit(name, stack=()):
		if name in seen:
			return
		if name in stack:
			raise GitumError('series dependency cycle: %s' % ' -> '.join(stack + (name,)))
		if name not in all_series:
			raise GitumError('unknown series %s' % name)
		for dep in all_series[name].get('depends', []):
			visit(dep, stack + (name,))
		seen.add(name)
		result.append(name)

	for name in names:
		visit(name)
	return result


def _format_patches(git, shas, start=1):
	"""format-patch files of commits (oldest first) as {filename: bytes}, numbered from start."""
	if not shas:
		return {}
	with tempfile.TemporaryDirectory() as tmp:
		# one process; format-patch numbers the listed commits in the reverse of the given order
		# (a single revision would mean "since <rev>", so one commit is given as -1)
		revs = ['-1', shas[0]] if len(shas) == 1 else ['--no-walk=unsorted', *reversed(shas)]
		git.run('format-patch', '--quiet', '--no-numbered', '--no-signature', '--zero-commit', '--start-number', str(start),
			'-o', tmp, *revs)
		return {p.name: p.read_bytes() for p in sorted(Path(tmp).iterdir())}


def dependencies_of(git, journal, all_series, meta):
	"""What the dependencies of a series contribute: their tips, their patch-ids and,
	for those exported into the same directory, the number of their patches."""
	tips, pids, offset = [], set(), 0
	if not meta.get('depends'):
		return tips, pids, offset
	own_dir = export_dir(meta)
	for n in order({**all_series, meta['name']: meta}, [meta['name']])[:-1]:
		d = all_series[n]
		branch_tip = git.rev('refs/heads/' + d['branch']) if d.get('branch') else None
		tips += [t for t in (branch_tip, d.get('tip')) if t]
		pids |= {c['patch_id'] for c in (d.get('changes') or []) if c.get('patch_id')}
		if export_dir(d) == own_dir:
			# the recorded changes, already updated when the dependency was saved in the same run
			offset += len(d['changes']) if d.get('changes') is not None else \
				len([f for f in journal.list('series/' + n) if f.endswith('.patch')])
	return tips, pids, offset


def drop_patches(journal, writer, name):
	"""Remove the patch files of a series from the journal; its other files (mr.md, assets) stay."""
	for f in journal.list('series/' + name):
		if f.endswith('.patch'):
			writer.remove('series/%s/%s' % (name, f))


_UPSTREAM_COMMITTERS = {}
_USER_EMAIL = {}


def upstream_looking(git, ref, base, tip):
	"""Commits base..tip committed by someone who commits to the upstream, but not by us.

	Such commits in a series usually mean that the series branch stands on a
	newer upstream than the local upstream ref: the ref needs a fetch.
	"""
	ref_sha = git.rev(ref)
	if ref_sha not in _UPSTREAM_COMMITTERS:
		_UPSTREAM_COMMITTERS[ref_sha] = set(git.lines('log', '-n', '500', '--no-merges', '--format=%ce', ref_sha))
	ups = _UPSTREAM_COMMITTERS[ref_sha]
	key = str(git.cwd)
	if key not in _USER_EMAIL:
		_USER_EMAIL[key] = git.out('config', 'user.email', check=False)
	me = _USER_EMAIL[key]
	found = []
	for line in git.lines('log', '--reverse', '--no-merges', '--format=%H %ce', '%s..%s' % (base, tip)):
		sha, _, email = line.partition(' ')
		if email in ups and email != me:
			found.append(sha)
	return found


def suspect_warning(meta):
	"""A warning for a series whose branch looks newer than its upstream ref, or ''."""
	found = meta.get('upstream_suspects')
	if not found:
		return ''
	return ('series %s: %d of its commits look like upstream ones (committed by an upstream committer): '
		'the branch base is probably not in %s yet; fetch the upstream and run gitum series save %s'
		% (meta['name'], len(found), meta.get('upstream') or meta['base'], meta['name']))


def _record(repo, journal, writer, tracker, name, meta, branch_tip, context, all_series=None):
	"""Recompute changes and patches of a series from its branch.

	A series branch may contain the commits of the series it depends on: the
	base then is the tip of the dependency, and changes with the patch-id of a
	dependency change are not part of the series.
	"""
	git = repo.git
	cfg = journal.config
	ref = base_ref(cfg, meta['base'])
	if not git.rev(ref):
		raise GitumError('series %s: base %s does not exist' % (name, ref))
	base = git.merge_base(ref, branch_tip)
	if not base:
		raise GitumError('series %s: %s has no common history with %s' % (name, meta['branch'], ref))
	dep_tips, dep_pids, offset = dependencies_of(git, journal, all_series if all_series is not None
						     else load_all(journal), meta)
	for t in dep_tips:
		if t != branch_tip and git.is_ancestor(t, branch_tip) and git.is_ancestor(base, t):
			base = t
	old = LayerState('series:' + name, {'base': meta.get('base_sha', base), 'tip': meta.get('tip', branch_tip),
					    'changes': meta.get('changes', [])}) if meta.get('changes') is not None else None
	new_changes = [c for c in describe_changes(git, base, branch_tip) if c.patch_id not in dep_pids]
	tracker.track_layer('series:' + name, old, new_changes, context=context)
	for c in new_changes:
		rec = tracker.changes[c.gitum_id]
		if rec['meta'].get('series') != name:
			tracker.set(c.gitum_id, series=name)
	meta.update(base_sha=base, tip=branch_tip, changes=[c.to_json() for c in new_changes])
	suspects = upstream_looking(git, ref, base, branch_tip) if meta['base'].startswith('upstream:') else []
	if suspects:
		meta['upstream_suspects'] = suspects
	else:
		meta.pop('upstream_suspects', None)
	drop_patches(journal, writer, name)
	writer.put_json(meta_path(name), meta)
	writer.put('series/%s/definition' % name, definition_text(meta, cfg))
	patches = _format_patches(git, [c.sha for c in new_changes], offset + 1)
	# every patch file name the series ever had: export removes only those of them that are stale
	meta['patch_files'] = sorted(set(meta.get('patch_files', [])) | set(patches))
	writer.put_json(meta_path(name), meta)
	for fname, data in patches.items():
		writer.put('series/%s/%s' % (name, fname), data)


def add(repo, name, branch, base, depends=(), state='draft', **meta):
	check_name(name)
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		if journal.read_json(meta_path(name)):
			raise GitumError('series %s already exists' % name)
		tip = git.rev('refs/heads/' + branch)
		if not tip:
			raise GitumError('branch %s does not exist' % branch)
		known = load_all(journal)
		for dep in depends:
			if dep not in known:
				raise GitumError('unknown dependency %s' % dep)
		data = {'name': name, 'branch': branch, 'base': base, 'depends': list(depends), 'state': state,
			'fixes': [], 'changes': None, **{k: v for k, v in meta.items() if v}}
		touch(data, state=state)
		writer = JournalWriter(git, journal.sha)
		tracker = changes_mod.Tracker(journal)
		_record(repo, journal, writer, tracker, name, data, tip, 'series add', known)
		tracker.write(writer)
		writer.commit('gitum: series add %s' % name, [tip])
		if data.get('state') != 'draft':
			state_hook(repo, journal.config, data, None, 'add')
		data['warnings'] = [w for w in [suspect_warning(data)] if w]
		return data


def save(repo, names=None, message=''):
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		all_series = load_all(journal)
		writer = JournalWriter(git, journal.sha)
		tracker = changes_mod.Tracker(journal)
		saved, tips, claimed = [], [], []
		if names:
			for name in names:
				if name not in all_series:
					raise GitumError('no series %s' % name)
			check_claims(repo, names)
		named = set(names or [])
		# dependencies first: a dependent series numbers its patches after theirs
		for name in order(all_series, names or sorted(all_series)):
			meta = all_series[name]
			if not meta.get('branch'):
				continue
			tip = git.rev('refs/heads/' + meta['branch'])
			if not tip:
				if name in named:
					raise GitumError('series %s: branch %s does not exist' % (name, meta['branch']))
				continue
			renumber = any(d in saved and export_dir(all_series[d]) == export_dir(meta)
				       for d in order(all_series, [name])[:-1])
			if tip == meta.get('tip') and name not in named and not renumber:
				continue		# named series are always recounted (rules or dependencies may have changed)
			if names and name not in named and not renumber:
				continue		# a dependency of a named series is saved only by its own name
			owner = claim_owner(repo, name)
			if owner and owner.get('session') != session_id():
				claimed.append(name)
				continue
			_record(repo, journal, writer, tracker, name, meta, tip, 'series save', all_series)
			touch(meta, event='saved', tip=tip)
			writer.put_json(meta_path(name), meta)
			saved.append(name)
			tips.append(tip)
		if not saved:
			return {'changed': False, 'saved': [], 'claimed': claimed}
		tracker.write(writer)
		writer.commit('gitum: series save %s%s' % (', '.join(saved), ('\n\n' + message) if message else ''), tips)
		warnings = [w for w in (suspect_warning(all_series[n]) for n in saved) if w]
		return {'changed': True, 'saved': saved, 'warnings': warnings, 'claimed': claimed}


def set_(repo, name, values):
	with repo.lock():
		check_claims(repo, [name])
		journal = repo.journal()
		meta = load(journal, name)
		old_state = meta.get('state')
		for key, value in values.items():
			if key not in SET_KEYS:
				raise GitumError('unknown key %s (known: %s)' % (key, ', '.join(sorted(SET_KEYS))))
			if key == 'state' and value not in STATES:
				raise GitumError('unknown state %s (known: %s)' % (value, ', '.join(STATES)))
			if key in ('depends', 'bugs', 'fixes'):
				value = [v for v in value.split(',') if v] if isinstance(value, str) else list(value)
				if key == 'depends':
					known = load_all(journal)
					for dep in value:
						if dep not in known:
							raise GitumError('unknown dependency %s' % dep)
					order({**known, name: {**meta, 'depends': value}}, [name])
			if key == 'bugs':
				value = bugs_mod.normalize_list(journal.config, value)
			if key == 'position' and value not in ('', None):
				value = int(value)
			if key in LINK_FIELDS:
				value = normalize_link(journal.config, key, value)
			if value in ('', None, []):
				meta.pop(key, None)
			else:
				meta[key] = value
		# the state and the Disabled: line of the definition are one thing
		if 'disabled' in values and 'state' not in values:
			if meta.get('disabled'):
				meta['state'] = 'disabled'
			elif meta.get('state') == 'disabled':
				meta['state'] = 'draft'
		elif 'state' in values:
			if meta.get('state') == 'disabled' and not meta.get('disabled'):
				meta['disabled'] = True
			elif meta.get('state') != 'disabled':
				meta.pop('disabled', None)
		if 'fixes' in values:
			found = bugs_mod.from_fixes(journal.config, meta.get('fixes'))
			if found:
				meta['bugs'] = bugs_mod.normalize_list(journal.config, list(meta.get('bugs') or []) + found)
		if meta.get('state') != old_state:
			touch(meta, state=meta.get('state'), previous=old_state)
		else:
			touch(meta)
		writer = JournalWriter(repo.git, journal.sha)
		tip = repo.git.rev('refs/heads/' + meta['branch']) if meta.get('branch') else None
		if tip and {'export_dir', 'depends', 'base', 'branch'} & set(values) and \
				(meta.get('changes') is not None or 'branch' in values):
			# the series boundary or the numbering of its patches may change
			tracker = changes_mod.Tracker(journal)
			_record(repo, journal, writer, tracker, name, meta, tip, 'series set')
			tracker.write(writer)
		else:
			writer.put_json(meta_path(name), meta)
		writer.put('series/%s/definition' % name, definition_text(meta, journal.config))
		writer.commit('gitum: series %s: %s' % (name, ', '.join('%s=%s' % kv for kv in values.items())))
		if meta.get('state') != old_state:
			state_hook(repo, journal.config, meta, old_state)
		return meta


def state_hook(repo, cfg, meta, previous, source=''):
	"""[hooks] series gets an event when the state of a series changes (to comment in its bugs and so on)."""
	if not cfg.hooks.get('series'):
		return []
	from . import hooks
	event = {'event': 'series-state', 'series': meta['name'], 'state': meta.get('state'), 'previous': previous,
		 'version': meta.get('version', ''), 'pr': meta.get('pr', ''), 'internal_mr': meta.get('internal_mr', ''),
		 'upstream_commit': meta.get('upstream_commit', ''), 'release': meta.get('release', ''),
		 'bugs': bugs_mod.of_series(cfg, meta), 'source': source}
	return hooks.run_event(repo, 'series', event, {'series': meta['name'], 'state': meta.get('state'),
							'previous_state': previous})


def remove(repo, name):
	with repo.lock():
		check_claims(repo, [name])
		journal = repo.journal()
		load(journal, name)
		for other, meta in load_all(journal).items():
			if name in meta.get('depends', []):
				raise GitumError('series %s depends on %s' % (other, name))
		writer = JournalWriter(repo.git, journal.sha)
		writer.remove_dir('series/%s' % name)
		writer.commit('gitum: series remove %s' % name)
		(claims_dir(repo) / ('%s.json' % name)).unlink(missing_ok=True)


def export(repo, names, directory, check=False):
	"""Write series in the wine-staging layout: <dir>/<export_dir>/definition + patches.

	Only the files gitum manages (definition and *.patch) are written, compared
	and removed when stale; other files and subdirectories are never touched.
	With check, compare instead and return the names whose copy is stale.
	"""
	journal = repo.journal()
	all_series = load_all(journal)
	names = names or sorted(all_series)
	directory = Path(directory)
	stale = []
	for name in names:
		if name not in all_series:
			raise GitumError('no series %s' % name)
		files = {p.rsplit('/', 1)[1]: data for p, data in journal.read_files('series/' + name, '.patch').items()
			 if p.count('/') == 2}
		files['definition'] = shared_definition(all_series, all_series[name], journal.config).encode()
		target = directory / export_dir(all_series[name])
		# files of this series: its current files and every patch name it ever had
		ours = set(files) | set(all_series[name].get('patch_files', []))
		current = {p.name: p.read_bytes() for p in target.iterdir() if is_managed(p) and p.name in ours} \
			if target.is_dir() else {}
		if current != files:
			stale.append(name)
			if not check:
				target.mkdir(parents=True, exist_ok=True)
				for f in set(current) - set(files):
					(target / f).unlink()
				for f, data in files.items():
					(target / f).write_bytes(data)
	return {'series': names, 'stale': stale, 'written': [] if check else stale}


def shared_definition(all_series, meta, cfg=None):
	"""The definition for the export directory of a series, combined for series sharing it.

	Fixes of all of them in dependency order, Depends only on series outside the
	directory, Disabled only when all of them are disabled.
	"""
	here = export_dir(meta)
	members = sorted(n for n, m in all_series.items() if export_dir(m) == here)
	group = [n for n in order(all_series, members) if n in members]
	if len(group) == 1:
		return definition_text(meta, cfg)
	fixes, depends, reasons = [], [], []
	for n in group:
		m = all_series[n]
		fixes += [f for f in (bugs_mod.fixes_lines(cfg, m) if cfg else m.get('fixes', [])) if f not in fixes]
		depends += [d for d in m.get('depends', []) if d not in group and d not in depends]
		if m.get('disabled'):
			reasons.append('%s: %s' % (n, m['disabled'] if m['disabled'] is not True else 'disabled'))
	disabled = '\n'.join(reasons) if len(reasons) == len(group) else ''
	return definition_text({'fixes': fixes, 'depends': depends, 'disabled': disabled})


def series_dirs(directory):
	"""Directories with a definition or patches, at any depth: [(relative path, path)]."""
	result = []
	for d in sorted(p for p in [directory, *directory.rglob('*')] if p.is_dir()):
		if any(is_managed(p) for p in d.iterdir()):
			result.append((d.relative_to(directory), d))
	return result


def import_staging(repo, directory, base, names=None):
	"""Import wine-staging style series: directories with a definition and *.patch files.

	A series may sit at any depth; its name is the relative path with '/'
	replaced by '-', and a nested path is kept as export_dir. Re-importing a
	known series updates its definition and patches and keeps the rest of its
	metadata (state, links, branch). Branches are not created; gitum series
	checkout builds one.
	"""
	directory = Path(directory)
	with repo.lock():
		journal = repo.journal()
		known = load_all(journal)
		writer = JournalWriter(repo.git, journal.sha)
		imported, changed_state = [], {}
		for rel, d in series_dirs(directory):
			if rel == Path('.'):
				continue
			name = '-'.join(rel.parts)
			if names and name not in names and str(rel) not in names:
				continue
			check_name(name)
			if name in known:
				check_claims(repo, [name])
			meta = dict(known.get(name) or {'name': name, 'base': base, 'branch': '', 'state': 'draft',
							 'changes': None})
			previous = known[name].get('state') if name in known else None
			definition = parse_definition((d / 'definition').read_text(errors='replace')) \
				if (d / 'definition').exists() else {'fixes': [], 'depends': [], 'disabled': ''}
			meta.update(fixes=definition['fixes'], depends=definition['depends'],
				    disabled=definition.get('disabled', ''))
			if definition.get('helps'):
				meta['helps'] = definition['helps']
			found = bugs_mod.from_fixes(journal.config, meta['fixes'])
			if found or meta.get('bugs'):
				meta['bugs'] = bugs_mod.normalize_list(journal.config, list(meta.get('bugs') or []) + found)
			if meta['disabled']:
				meta['state'] = 'disabled'
			elif meta.get('state') == 'disabled':
				meta['state'] = 'draft'
			if len(rel.parts) > 1:
				meta['export_dir'] = str(rel)
			drop_patches(journal, writer, name)
			writer.put_json(meta_path(name), meta)
			writer.put('series/%s/definition' % name, definition_text(meta, journal.config))
			names_here = []
			for p in sorted(d.iterdir()):
				if p.is_file() and p.name.endswith('.patch'):
					writer.put('series/%s/%s' % (name, p.name), p.read_bytes())
					names_here.append(p.name)
			meta['patch_files'] = sorted(set(meta.get('patch_files', [])) | set(names_here))
			if meta.get('state') != previous and (previous is not None or meta['state'] != 'draft'):
				touch(meta, state=meta['state'], previous=previous, source='import-staging')
				changed_state[name] = previous
			writer.put_json(meta_path(name), meta)
			imported.append((name, meta))
		if not imported:
			raise GitumError('no series found in %s' % directory)
		writer.commit('gitum: import %d series from %s' % (len(imported), directory.name))
		for name, meta in imported:
			if name in changed_state:
				state_hook(repo, journal.config, meta, changed_state[name], 'import-staging')
		return {'imported': [n for n, _m in imported]}


def checkout(repo, name, onto=None, branch=None, with_depends=True, force=False):
	"""Build a branch from the patches of a series (and of its dependencies) in the journal.

	An existing branch with commits the journal does not have is kept unless force.
	"""
	git = repo.git
	with repo.lock():
		check_claims(repo, [name])
		journal = repo.journal()
		all_series = load_all(journal)
		meta = load(journal, name)
		start = git.rev(onto or base_ref(journal.config, meta['base']))
		if not start:
			raise GitumError('base %s does not exist' % (onto or meta['base']))
		branch = branch or meta.get('branch') or 'series/' + name
		ref = 'refs/heads/' + branch
		old = git.rev(ref)
		if old and old != meta.get('tip') and not force:
			raise NeedsHuman('%s has commits that are not saved in the series (%s): gitum series save %s, '
					 'or checkout --force to throw them away' % (branch, old[:12], name), branch=branch)
		names = order(all_series, [name]) if with_depends else [name]
		wt = repo.add_worktree(start, 'checkout-' + name)
		applied = []
		try:
			for n in names:
				files = journal.read_files('series/' + n, '.patch')
				for path in sorted(p for p in files if p.count('/') == 2):
					f, data = path.rsplit('/', 1)[1], files[path]
					res = wt.run('am', '--keep-cr', '-3', input=data, check=False)
					if res.returncode != 0:
						wt.run('am', '--abort', check=False)
						raise NeedsHuman('series %s: %s/%s does not apply on %s' % (name, n, f, start[:12]),
								 series=n, patch=f)
					applied.append('%s/%s' % (n, f))
			tip = wt.rev('HEAD')
		finally:
			repo.remove_worktree(wt.cwd)
		tr = RefTransaction(git)
		tr.update(ref, tip, old or None)
		if not meta.get('branch'):
			meta['branch'] = branch
			writer = JournalWriter(git, journal.sha)
			writer.put_json(meta_path(name), meta)
			tr.update(JOURNAL_REF, writer.write('gitum: series %s: branch %s' % (name, branch)), journal.sha)
		with repo.committing(tr, [(branch, old, tip)], kind='branch'):
			pass
		return {'branch': branch, 'tip': tip, 'applied': applied, 'base': start}


IMPORT_KEYS = SET_KEYS | {'name', 'branch', 'base', 'depends', 'state', 'updated', 'history'}


def export_json(repo, names=None):
	"""Series records with the keys import_json takes (and their dates), for another repository."""
	all_series = load_all(repo.journal())
	for name in names or []:
		if name not in all_series:
			raise GitumError('no series %s' % name)
	return [{k: v for k, v in all_series[n].items() if k in IMPORT_KEYS}
		for n in order(all_series, names or sorted(all_series))]


def import_json(repo, items, force=False):
	"""Create or update series from records (for an initial import of existing work).

	A record needs name and, for a new series, base. With an existing branch
	the series gets its changes from it; without one only the metadata is
	recorded.
	"""
	created, updated, skipped = [], [], []
	journal = repo.journal()
	known = load_all(journal)
	names = {i['name'] for i in items}
	for item in sorted(items, key=lambda i: len(i.get('depends', []))):
		unknown = set(item) - IMPORT_KEYS
		if unknown:
			raise GitumError('series %s: unknown keys %s' % (item.get('name'), ', '.join(sorted(unknown))))
		for dep in item.get('depends', []):
			if dep not in known and dep not in names:
				raise GitumError('series %s: unknown dependency %s' % (item['name'], dep))
	pending = list(items)
	while pending:
		progress = False
		for item in list(pending):
			known = load_all(repo.journal())
			if any(dep not in known for dep in item.get('depends', [])):
				continue
			name = item['name']
			rest = {k: v for k, v in item.items()
				if k not in ('name', 'branch', 'base', 'depends', 'state', 'updated', 'history')}
			if name not in known:
				if not item.get('base'):
					raise GitumError('series %s: base is required' % name)
				branch = item.get('branch') or ''
				if branch and repo.git.rev('refs/heads/' + branch):
					add(repo, name, branch, item['base'], item.get('depends', []), item.get('state', 'draft'))
				else:
					check_name(name)
					with repo.lock():
						j = repo.journal()
						meta = {'name': name, 'base': item['base'], 'branch': branch,
							'depends': item.get('depends', []), 'state': item.get('state', 'draft'),
							'fixes': [], 'changes': None}
						touch(meta, state=meta['state'])
						writer = JournalWriter(repo.git, j.sha)
						writer.put_json(meta_path(name), meta)
						writer.put('series/%s/definition' % name, definition_text(meta, j.config))
						writer.commit('gitum: series import %s' % name)
				created.append(name)
			else:
				updated.append(name)
			values = dict(rest)
			if name in updated:
				values.update({k: item[k] for k in ('state', 'depends', 'base', 'branch') if k in item})
				current = known[name]
				newer = current.get('updated', '') > (item.get('updated') or '')
				if 'state' in values and newer and not force and \
						state_rank(values['state']) < state_rank(current.get('state')):
					state = values.pop('state')
					skipped.append({'series': name, 'state': state, 'kept': current.get('state'),
							'reason': 'the series changed after this record (%s)' % current.get('updated'),
							'command': 'gitum series set %s state=%s' % (name, state)})
				values = {k: v for k, v in values.items() if current.get(k) != v}
			if values:
				set_(repo, name, values)
			if item.get('updated') or item.get('history'):
				_put_dates(repo, name, item.get('updated'), item.get('history'))
			pending.remove(item)
			progress = True
		if not progress:
			raise GitumError('series dependency cycle among %s' % ', '.join(i['name'] for i in pending))
	final = load_all(repo.journal())
	warnings = [w for w in (suspect_warning(final[n]) for n in created + updated if n in final) if w]
	return {'created': created, 'updated': updated, 'skipped': skipped, 'warnings': warnings}


def _put_dates(repo, name, updated, history):
	"""Keep the real dates of imported work instead of the date of the import."""
	with repo.lock():
		journal = repo.journal()
		meta = load(journal, name)
		if history:
			meta['history'] = list(history)
		meta['updated'] = updated or (meta.get('history') or [{}])[-1].get('date') or meta.get('updated')
		writer = JournalWriter(repo.git, journal.sha)
		writer.put_json(meta_path(name), meta)
		writer.commit('gitum: series %s: dates' % name)

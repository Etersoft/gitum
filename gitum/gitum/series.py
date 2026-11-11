"""Series: named sets of changes with their own base, metadata and patches.

In the journal a series is a directory compatible with wine-staging:
    series/<name>/definition     Fixes:, Depends:, Disabled: (+ our keys)
    series/<name>/0001-*.patch   format-patch from the series base
    series/<name>/meta.json      base, branch, state, links, changes
Work on a series happens in its git branch; `gitum series save` records it.
"""

import re
import tempfile
from pathlib import Path

from . import changes as changes_mod
from .errors import GitumError, NeedsHuman
from .git import RefTransaction
from .journal import JOURNAL_REF, JournalWriter, LayerState, describe_changes

STATES = ('draft', 'review', 'queued', 'sent', 'merged', 'rejected', 'disabled')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]*$')
SET_KEYS = {'state', 'pr', 'internal_mr', 'bugs', 'depends', 'disabled', 'title', 'version', 'base', 'branch',
	    'note', 'upstream_commit', 'fixes', 'upstream', 'export_dir'}
# files of a series directory that gitum writes; everything else there belongs to people
MANAGED = ('definition',)


def is_managed(path):
	return path.is_file() and (path.name in MANAGED or path.name.endswith('.patch'))


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
	for name in journal.list('series'):
		meta = journal.read_json(meta_path(name))
		if meta:
			result[name] = meta
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


def definition_text(meta):
	"""A wine-staging definition file."""
	lines = []
	for fix in meta.get('fixes', []):
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


def _format_patches(git, base, tip):
	"""format-patch files of base..tip as {filename: bytes}."""
	with tempfile.TemporaryDirectory() as tmp:
		if git.rev_list('%s..%s' % (base, tip)):
			git.run('format-patch', '--quiet', '--no-signature', '--zero-commit', '-o', tmp, '%s..%s' % (base, tip))
		return {p.name: p.read_bytes() for p in sorted(Path(tmp).iterdir())}


def _commit(repo, journal, writer, message, extra_parents=()):
	new = writer.write(message, extra_parents)
	tr = RefTransaction(repo.git)
	tr.update(JOURNAL_REF, new, journal.sha)
	tr.commit()
	return new


def _record(repo, journal, writer, tracker, name, meta, branch_tip, context):
	"""Recompute changes and patches of a series from its branch."""
	git = repo.git
	cfg = journal.config
	ref = base_ref(cfg, meta['base'])
	if not git.rev(ref):
		raise GitumError('series %s: base %s does not exist' % (name, ref))
	base = git.merge_base(ref, branch_tip)
	if not base:
		raise GitumError('series %s: %s has no common history with %s' % (name, meta['branch'], ref))
	old = LayerState('series:' + name, {'base': meta.get('base_sha', base), 'tip': meta.get('tip', branch_tip),
					    'changes': meta.get('changes', [])}) if meta.get('changes') is not None else None
	new_changes = describe_changes(git, base, branch_tip)
	tracker.track_layer('series:' + name, old, new_changes, context=context)
	for c in new_changes:
		rec = tracker.changes[c.gitum_id]
		if rec['meta'].get('series') != name:
			tracker.set(c.gitum_id, series=name)
	meta.update(base_sha=base, tip=branch_tip, changes=[c.to_json() for c in new_changes])
	writer.remove_dir('series/%s' % name)
	writer.put_json(meta_path(name), meta)
	writer.put('series/%s/definition' % name, definition_text(meta))
	for fname, data in _format_patches(git, base, branch_tip).items():
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
		writer = JournalWriter(git, journal.sha)
		tracker = changes_mod.Tracker(journal)
		_record(repo, journal, writer, tracker, name, data, tip, 'series add')
		tracker.write(writer)
		_commit(repo, journal, writer, 'gitum: series add %s' % name, [tip])
		return data


def save(repo, names=None):
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		all_series = load_all(journal)
		writer = JournalWriter(git, journal.sha)
		tracker = changes_mod.Tracker(journal)
		saved, tips = [], []
		for name in names or sorted(all_series):
			if name not in all_series:
				raise GitumError('no series %s' % name)
			meta = all_series[name]
			if not meta.get('branch'):
				continue
			tip = git.rev('refs/heads/' + meta['branch'])
			if not tip:
				if names:
					raise GitumError('series %s: branch %s does not exist' % (name, meta['branch']))
				continue
			if tip == meta.get('tip'):
				continue
			_record(repo, journal, writer, tracker, name, meta, tip, 'series save')
			saved.append(name)
			tips.append(tip)
		if not saved:
			return {'changed': False, 'saved': []}
		tracker.write(writer)
		_commit(repo, journal, writer, 'gitum: series save %s' % ', '.join(saved), tips)
		return {'changed': True, 'saved': saved}


def set_(repo, name, values):
	with repo.lock():
		journal = repo.journal()
		meta = load(journal, name)
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
			if value in ('', None, []):
				meta.pop(key, None)
			else:
				meta[key] = value
		writer = JournalWriter(repo.git, journal.sha)
		writer.put_json(meta_path(name), meta)
		writer.put('series/%s/definition' % name, definition_text(meta))
		_commit(repo, journal, writer, 'gitum: series %s: %s' % (name, ', '.join('%s=%s' % kv for kv in values.items())))
		return meta


def remove(repo, name):
	with repo.lock():
		journal = repo.journal()
		load(journal, name)
		for other, meta in load_all(journal).items():
			if name in meta.get('depends', []):
				raise GitumError('series %s depends on %s' % (other, name))
		writer = JournalWriter(repo.git, journal.sha)
		writer.remove_dir('series/%s' % name)
		_commit(repo, journal, writer, 'gitum: series remove %s' % name)


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
		files = {f: journal.read('series/%s/%s' % (name, f)) for f in journal.list('series/' + name)
			 if f != 'meta.json'}
		target = directory / export_dir(all_series[name])
		current = {p.name: p.read_bytes() for p in target.iterdir() if is_managed(p)} if target.is_dir() else {}
		if current != files:
			stale.append(name)
			if not check:
				target.mkdir(parents=True, exist_ok=True)
				for f in set(current) - set(files):
					(target / f).unlink()
				for f, data in files.items():
					(target / f).write_bytes(data)
	return {'series': names, 'stale': stale, 'written': [] if check else stale}


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
		imported = []
		for rel, d in series_dirs(directory):
			if rel == Path('.'):
				continue
			name = '-'.join(rel.parts)
			if names and name not in names and str(rel) not in names:
				continue
			check_name(name)
			meta = dict(known.get(name) or {'name': name, 'base': base, 'branch': '', 'state': 'draft',
							 'changes': None})
			definition = parse_definition((d / 'definition').read_text(errors='replace')) \
				if (d / 'definition').exists() else {'fixes': [], 'depends': [], 'disabled': ''}
			meta.update(fixes=definition['fixes'], depends=definition['depends'],
				    disabled=definition.get('disabled', ''))
			if definition.get('helps'):
				meta['helps'] = definition['helps']
			if meta['disabled']:
				meta['state'] = 'disabled'
			elif meta.get('state') == 'disabled':
				meta['state'] = 'draft'
			if len(rel.parts) > 1:
				meta['export_dir'] = str(rel)
			writer.remove_dir('series/%s' % name)
			writer.put_json(meta_path(name), meta)
			writer.put('series/%s/definition' % name, definition_text(meta))
			for p in sorted(d.iterdir()):
				if p.is_file() and p.name.endswith('.patch'):
					writer.put('series/%s/%s' % (name, p.name), p.read_bytes())
			imported.append(name)
		if not imported:
			raise GitumError('no series found in %s' % directory)
		_commit(repo, journal, writer, 'gitum: import %d series from %s' % (len(imported), directory.name))
		return {'imported': imported}


def checkout(repo, name, onto=None, branch=None, with_depends=True):
	"""Build a branch from the patches of a series (and of its dependencies) in the journal."""
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		all_series = load_all(journal)
		meta = load(journal, name)
		start = git.rev(onto or base_ref(journal.config, meta['base']))
		if not start:
			raise GitumError('base %s does not exist' % (onto or meta['base']))
		branch = branch or meta.get('branch') or 'series/' + name
		ref = 'refs/heads/' + branch
		old = git.rev(ref)
		found = repo.check_checkouts([branch]) if old else {}
		names = order(all_series, [name]) if with_depends else [name]
		wt = repo.add_worktree(start, 'checkout-' + name)
		applied = []
		try:
			for n in names:
				for f in sorted(journal.list('series/' + n)):
					if not f.endswith('.patch'):
						continue
					data = journal.read('series/%s/%s' % (n, f))
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
		tr.commit()
		repo.follow_checkouts([(branch, old, tip)], found)
		if not meta.get('branch'):
			meta['branch'] = branch
			writer = JournalWriter(git, journal.sha)
			writer.put_json(meta_path(name), meta)
			_commit(repo, journal, writer, 'gitum: series %s: branch %s' % (name, branch))
		return {'branch': branch, 'tip': tip, 'applied': applied, 'base': start}

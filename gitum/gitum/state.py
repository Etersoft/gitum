"""Creating the journal and recording branch states: init, save, status."""


from . import changes as changes_mod
from . import config as config_mod
from .errors import ConfigError, GitumError, NeedsHuman
from .git import RefTransaction
from .repo import CANDIDATE_FILE, COMMIT_FILE
from .journal import (
	JOURNAL_REF,
	JournalWriter,
	LayerState,
	describe_changes,
	history_commit,
	history_ref,
	make_root,
)


def upstream_tag(git, upstream, sha):
	"""Release tag pointing exactly at sha, if any."""
	if not upstream.release_tags:
		return ''
	res = git.run('describe', '--tags', '--exact-match', '--match', upstream.release_tags, sha, check=False)
	return res.stdout.decode().strip() if res.returncode == 0 else ''


def upstream_json(git, upstream, sha):
	return {'sha': sha, 'ref': upstream.ref, 'tag': upstream_tag(git, upstream, sha)}


def find_base(git, cfg, layer, tip, new_tips):
	"""Base of a layer branch for init: its START tag, the parent tip or the merge base."""
	if layer.compat_start_tag:
		sha = git.rev('refs/tags/' + layer.compat_start_tag)
		if sha and git.is_ancestor(sha, tip):
			return sha, layer.compat_start_tag
	if layer.base_kind == 'layer':
		parent_tip = new_tips[layer.base_name]
		if git.is_ancestor(parent_tip, tip):
			return parent_tip, ''
		base = git.merge_base(parent_tip, tip)
	else:
		up = cfg.upstreams[layer.base_name]
		ref = git.rev(up.ref)
		if not ref:
			raise GitumError('upstream %s: %s does not exist' % (up.name, up.ref))
		base = git.merge_base(ref, tip)
	if not base:
		raise GitumError('layer %s: %s has no common history with its base' % (layer.name, layer.branch))
	return base, ''


def write_initial_journal(repo, cfg_text, layer_states, upstream_states, message='init'):
	git = repo.git
	cfg = config_mod.parse(cfg_text)
	root = make_root(git)
	writer = JournalWriter(git, root)
	writer.put(config_mod.CONFIG_FILE, cfg_text)
	for name, data in upstream_states.items():
		writer.put_json('upstreams/%s.json' % name, data)
	tracker = changes_mod.Tracker()
	for state in layer_states.values():
		tracker.track_layer(state.name, None, state.changes, context=message)
		writer.put_layer(state)
	tracker.write(writer)
	tips = [s.tip for s in layer_states.values()]
	journal = writer.write('gitum: %s' % message, tips)
	tr = RefTransaction(git)
	tr.update(JOURNAL_REF, journal, None)
	for name, state in layer_states.items():
		msg = '%s: start of history at %s\n\nbase %s\ntip %s\n' % (name, state.tip[:12], state.base, state.tip)
		tr.update(history_ref(name), history_commit(git, state.tip, None, msg), None)
	tr.commit()
	return {'journal': journal, 'layers': {n: s.to_json() | {'changes': len(s.changes)} for n, s in layer_states.items()},
		'upstreams': upstream_states, 'layer_order': [l.name for l in cfg.ordered_layers()]}


def init(repo, upstreams=(), layers=(), config_text=None):
	"""Create the journal from existing branches.

	upstreams: dicts with name, ref and optionally remote, release-tags.
	layers: dicts with name, branch and base (an upstream or layer name).
	"""
	git = repo.git
	if repo.has_journal():
		raise GitumError('the gitum journal already exists')
	if config_text is None:
		up_names = {u['name'] for u in upstreams}
		layer_names = {l['name'] for l in layers}
		if not upstreams:
			raise ConfigError('init needs at least one upstream (or --config); layers are optional')
		layer_dicts = []
		for l in layers:
			base = l.get('base') or (next(iter(up_names)) if len(up_names) == 1 else '')
			if base in layer_names:
				base = 'layer:' + base
			elif base in up_names:
				base = 'upstream:' + base
			else:
				raise ConfigError('layer %s: unknown base %r' % (l['name'], base))
			layer_dicts.append({**l, 'base': base})
		for u in upstreams:
			if not u.get('remote') and '/' in u['ref'] and git.rev('refs/remotes/' + u['ref']):
				u['remote'] = u['ref'].split('/', 1)[0]
		cfg = config_mod.parse(config_mod.render(upstreams, layer_dicts))
		# remember START tags of the existing workflow
		for d in layer_dicts:
			tag = d['branch'] + '-START'
			if not d.get('compat-start-tag') and git.rev('refs/tags/' + tag):
				d['compat-start-tag'] = tag
		config_text = config_mod.render(upstreams, layer_dicts)
	cfg = config_mod.parse(config_text)
	with repo.lock():
		states, tips = {}, {}
		for layer in cfg.ordered_layers():
			tip = git.rev('refs/heads/' + layer.branch)
			if not tip:
				raise GitumError('layer %s: branch %s does not exist' % (layer.name, layer.branch))
			base, _tag = find_base(git, cfg, layer, tip, tips)
			tips[layer.name] = tip
			up = cfg.root_upstream(layer)
			states[layer.name] = LayerState(layer.name, {
				'base': base, 'tip': tip, 'branch': layer.branch, 'upstream_ref': up.ref,
				'changes': [c.to_json() for c in describe_changes(git, base, tip)],
			})
		up_states = {}
		for layer in cfg.ordered_layers():
			if layer.base_kind == 'upstream' and layer.base_name not in up_states:
				up = cfg.upstreams[layer.base_name]
				up_states[up.name] = upstream_json(git, up, states[layer.name].base)
		return write_initial_journal(repo, config_text, states, up_states)


def save(repo, layer_names=None, message=''):
	"""Record the current layer branches in the journal (after work done with plain git)."""
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		cfg = journal.config
		old_states = journal.layers()
		new_tips = {}
		changed = {}
		for layer in cfg.ordered_layers():
			old = old_states.get(layer.name)
			tip = git.rev('refs/heads/' + layer.branch)
			if not tip:
				raise GitumError('layer %s: branch %s does not exist' % (layer.name, layer.branch))
			new_tips[layer.name] = tip
			if layer_names and layer.name not in layer_names:
				continue
			if old and old.tip == tip:
				continue
			candidates = [old.base] if old else []
			if layer.base_kind == 'layer':
				parent = changed.get(layer.base_name) or old_states.get(layer.base_name)
				if parent:
					candidates.append(parent.tip)
			else:
				up = journal.upstream(layer.base_name)
				if up.get('sha'):
					candidates.append(up['sha'])
			base = next((c for c in candidates if git.is_ancestor(c, tip)), None)
			if not base:
				raise NeedsHuman('layer %s: %s was rebased outside gitum onto an unknown base; '
						 'rebase it back or run gitum sync' % (layer.name, layer.branch))
			state = LayerState(layer.name, {
				'base': base, 'tip': tip, 'branch': layer.branch,
				'upstream_ref': cfg.root_upstream(layer).ref,
				'changes': [c.to_json() for c in describe_changes(git, base, tip)],
			})
			changed[layer.name] = state
		if not changed:
			return {'changed': False, 'layers': {}}
		writer = JournalWriter(git, journal.sha)
		tr = RefTransaction(git)
		summary = {}
		tracker = changes_mod.Tracker(journal)
		for name, state in changed.items():
			tracker.track_layer(name, old_states.get(name), state.changes, context=message or 'save')
			writer.put_layer(state)
			old = old_states.get(name)
			before = {c.patch_id or c.sha for c in old.changes} if old else set()
			after = {c.patch_id or c.sha for c in state.changes}
			added = [c.subject for c in state.changes if (c.patch_id or c.sha) not in before]
			removed = [c.subject for c in (old.changes if old else []) if (c.patch_id or c.sha) not in after]
			body = ''.join('+ %s\n' % s for s in added) + ''.join('- %s\n' % s for s in removed)
			hist_old = git.rev(history_ref(name))
			msg = '%s: %s\n\n%s\ntip %s\n' % (name, message or 'save', body, state.tip)
			tr.update(history_ref(name), history_commit(git, state.tip, hist_old, msg), hist_old or None)
			summary[name] = {'added': added, 'removed': removed, 'tip': state.tip, 'changes': len(state.changes)}
		tracker.write(writer)
		new_journal = writer.write('gitum: save %s\n\n%s' % (', '.join(changed), message),
					   [s.tip for s in changed.values()])
		tr.update(JOURNAL_REF, new_journal, journal.sha)
		tr.commit()
		return {'changed': True, 'journal': new_journal, 'layers': summary}


def status(repo, changes=False, all_changes=False):
	"""Where everything stands; with changes, also the change table (all_changes: not only those to act on)."""
	git = repo.git
	journal = repo.journal()
	cfg = journal.config
	states = journal.layers()
	result = {'journal': journal.sha, 'upstreams': {}, 'layers': {}, 'operation': None, 'candidate': None,
		  'lock': repo.lock_owner()}
	for up in cfg.upstreams.values():
		data = journal.upstream(up.name)
		ref_sha = git.rev(up.ref)
		entry = {'ref': up.ref, 'sha': data.get('sha'), 'tag': data.get('tag', ''), 'ref_sha': ref_sha}
		if ref_sha and data.get('sha') and not up.rewritten and git.is_ancestor(data['sha'], ref_sha):
			entry['behind'] = git.count('%s..%s' % (data['sha'], ref_sha))
		elif ref_sha and data.get('sha') and ref_sha != data['sha']:
			entry['moved'] = True
		result['upstreams'][up.name] = entry
	for layer in cfg.ordered_layers():
		st = states.get(layer.name)
		tip = git.rev('refs/heads/' + layer.branch)
		entry = {'branch': layer.branch, 'base': layer.base, 'exists': bool(tip)}
		if st:
			entry.update({'base_sha': st.base, 'tip': st.tip, 'changes': len(st.changes)})
			if tip and tip != st.tip:
				if git.is_ancestor(st.tip, tip):
					entry['unsaved'] = git.count('%s..%s' % (st.tip, tip))
				else:
					entry['rewritten'] = True
			if layer.base_kind == 'layer' and layer.base_name in states:
				parent_tip = states[layer.base_name].tip
				if parent_tip != st.base:
					entry['parent_moved'] = True
			elif layer.base_kind == 'upstream':
				up = result['upstreams'][layer.base_name]
				if up.get('behind') and st.base != up['ref_sha']:
					entry['upstream_behind'] = git.count('%s..%s' % (st.base, up['ref_sha']))
				elif up.get('moved'):
					entry['upstream_moved'] = True
		where = repo.checked_out(layer.branch)
		if where:
			entry['checked_out'] = where
		result['layers'][layer.name] = entry
	op = repo.load_op()
	if op:
		result['operation'] = {k: op.get(k) for k in ('kind', 'run', 'stopped_at', 'worktree', 'message')}
		result['operation']['layer'] = (op.get('current') or {}).get('layer')
		if not op.get('stopped_at'):
			result['operation']['message'] = 'running' if result['lock'] else \
				'interrupted; gitum continue resumes it, gitum abort drops it'
	if (repo.gitum_dir / COMMIT_FILE).exists():
		result['interrupted_commit'] = repo.read_json(COMMIT_FILE).get('kind')
	result['candidate'] = repo.read_json(CANDIDATE_FILE)
	records = changes_mod.load_all(journal)
	result['fates'] = {}
	for rec in records.values():
		if not rec.get('gone'):
			result['fates'][rec.get('fate', 'local')] = result['fates'].get(rec.get('fate', 'local'), 0) + 1
	from . import series as series_mod
	all_series = series_mod.load_all(journal)
	result['series'] = {}
	for meta in all_series.values():
		result['series'][meta.get('state', '')] = result['series'].get(meta.get('state', ''), 0) + 1
	warnings = [w for w in (series_mod.suspect_warning(m) for _, m in sorted(all_series.items())) if w]
	if warnings:
		result['warnings'] = warnings
	if changes:
		result['changes'] = [r for r in changes_mod.rows(records) if all_changes or r['action']]
	return result


def move_layer(repo, name, new_tip, message, adjust=None, keep=()):
	"""Move a layer branch to new_tip and record it in the journal in one transaction.

	The new tip must stand on the recorded base of the layer. adjust(tracker,
	state) may set fates of the changes before they are written. keep: more
	commits to keep reachable from the journal (e.g. the commits of others
	that were put on top, as they were pushed).
	"""
	git = repo.git
	journal = repo.journal()
	cfg = journal.config
	layer = cfg.layers[name]
	old = journal.layer(name)
	ref = 'refs/heads/' + layer.branch
	current = git.rev(ref)
	if not git.is_ancestor(old.base, new_tip):
		raise GitumError('layer %s: %s is not based on %s' % (name, new_tip[:12], old.base[:12]))
	state = LayerState(name, {'base': old.base, 'tip': new_tip, 'branch': layer.branch,
				  'upstream_ref': cfg.root_upstream(layer).ref,
				  'changes': [c.to_json() for c in describe_changes(git, old.base, new_tip)]})
	tracker = changes_mod.Tracker(journal)
	tracker.track_layer(name, old, state.changes, context=message)
	if adjust:
		adjust(tracker, state)
	writer = JournalWriter(git, journal.sha)
	writer.put_layer(state)
	tracker.write(writer)
	tr = RefTransaction(git)
	tr.update(ref, new_tip, current or None)
	hist_old = git.rev(history_ref(name))
	tr.update(history_ref(name), history_commit(git, new_tip, hist_old, '%s: %s\n\ntip %s\n' % (
		name, message, new_tip)), hist_old or None)
	tr.update(JOURNAL_REF, writer.write('gitum: %s: %s' % (name, message), [new_tip, *keep]), journal.sha)
	with repo.committing(tr, [(layer.branch, current, new_tip)], kind='layer'):
		pass
	return state



def restore(repo, rev):
	"""Bring the whole stack back to an earlier journal state; recorded as a new state, nothing is lost.

	The layer branches, their bases (and compat START tags) and the upstream
	sync points become those of the state rev; the history branches and the
	journal only grow, so the restore itself can be undone the same way.
	"""
	from .sync import check_idle
	git = repo.git
	with repo.lock():
		check_idle(repo)
		journal = repo.journal()
		sha = git.rev(rev + '^{commit}') if rev else None
		if not sha or sha not in journal.history():
			raise GitumError('%s is not a state of the journal (gitum log lists them)' % rev)
		old = repo.journal(sha)
		cfg = journal.config
		writer = JournalWriter(git, journal.sha)
		tracker = changes_mod.Tracker(journal)
		tr = RefTransaction(git)
		moves, restored = [], []
		for name, data in old.read_files('upstreams', '.json').items():
			writer.put(name, data)
		for layer in cfg.ordered_layers():
			then, now_ = old.layer(layer.name), journal.layer(layer.name)
			if not then:
				raise GitumError('layer %s did not exist in %s' % (layer.name, rev))
			ref = 'refs/heads/' + layer.branch
			current = git.rev(ref)
			if now_ and (then.base, then.tip) == (now_.base, now_.tip) and current == then.tip:
				continue
			state = LayerState(layer.name, {**then.to_json(), 'branch': layer.branch,
							'upstream_ref': cfg.root_upstream(layer).ref})
			tracker.track_layer(layer.name, now_, state.changes, context='restore %s' % sha[:12])
			writer.put_layer(state)
			if current != then.tip:
				tr.update(ref, then.tip, current or None)
				moves.append((layer.branch, current, then.tip))
			hist_old = git.rev(history_ref(layer.name))
			tr.update(history_ref(layer.name), history_commit(git, then.tip, hist_old, '%s: restore %s\n\ntip %s\n' % (
				layer.name, sha[:12], then.tip)), hist_old or None)
			if layer.compat_start_tag:
				tr.update('refs/tags/' + layer.compat_start_tag, then.base, '')
			restored.append(layer.name)
		if not restored:
			return {'changed': False, 'state': sha, 'layers': []}
		tracker.write(writer)
		tr.update(JOURNAL_REF, writer.write('gitum: restore the state %s' % sha[:12],
						     [m[2] for m in moves]), journal.sha)
		with repo.committing(tr, moves, kind='restore'):
			pass
		return {'changed': True, 'state': sha, 'layers': restored}

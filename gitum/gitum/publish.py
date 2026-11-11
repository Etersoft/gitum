"""Publishing to and pulling from the shared remote.

Layer branches are rewritten by sync and pushed with a lease. The journal and
the history branches only grow and are pushed without force. Commits that
somebody else pushed onto a layer branch since our last publish are put on
top of our result instead of being overwritten.
"""

from . import hooks, state
from .packaging import tag_version
from .errors import GitumError, NeedsHuman
from .git import RefTransaction
from .journal import HISTORY_PREFIX, JOURNAL_BRANCH, JOURNAL_REF, history_ref


def published_ref(remote, branch):
	return 'refs/gitum/published/%s/%s' % (remote, branch)


def remote_ref(remote, branch):
	return 'refs/remotes/%s/%s' % (remote, branch)


def _remote(repo, remote):
	if not remote and repo.has_journal():
		remote = repo.journal().config.publish_remote
	if not remote and not repo.has_journal():
		remote = 'origin'
	if not remote:
		raise GitumError('no remote: set [publish] remote in gitum.toml or pass one')
	return remote


def fetch(repo, remote):
	repo.git.run('fetch', '--quiet', remote,
		     '+refs/heads/*:refs/remotes/%s/*' % remote, '+refs/tags/*:refs/tags/*')


def _integrate(repo, branch, published, remote_tip, local_tip):
	"""Put commits published..remote_tip on top of local_tip; return the new tip."""
	git = repo.git
	commits = git.rev_list('--reverse', '--no-merges', '%s..%s' % (published, remote_tip))
	wt = repo.add_worktree(local_tip, 'integrate-' + branch.replace('/', '-'))
	try:
		for sha in commits:
			if not wt.ok('-c', 'rerere.enabled=true', 'cherry-pick', '--allow-empty', '--empty=drop', sha):
				raise NeedsHuman('commit %s pushed to %s by somebody else conflicts with the new %s; '
						 'gitum pull, then sync again' % (sha[:12], branch, branch), commit=sha)
		return wt.rev('HEAD'), commits
	finally:
		repo.remove_worktree(wt.cwd)


def publish(repo, remote=None):
	git = repo.git
	with repo.lock():
		remote = _remote(repo, remote)
		fetch(repo, remote)
		cfg = repo.journal().config
		leases, integrated = {}, {}
		their_journal_sha = git.rev(remote_ref(remote, JOURNAL_BRANCH))
		their_journal = repo.journal(their_journal_sha) if their_journal_sha else None
		for layer in cfg.ordered_layers():
			branch = layer.branch
			local = git.rev('refs/heads/' + branch)
			theirs = git.rev(remote_ref(remote, branch))
			published = git.rev(published_ref(remote, branch))
			if not published and their_journal and their_journal.layer(layer.name):
				# no local marker (a fresh clone): the tip recorded in the remote journal was published
				published = their_journal.layer(layer.name).tip
			if not theirs or theirs == local or git.is_ancestor(theirs, local) or theirs == published:
				leases[branch] = theirs or ''
			elif published and git.is_ancestor(published, theirs):
				new_tip, commits = _integrate(repo, branch, published, theirs, local)
				state.move_layer(repo, layer.name, new_tip, 'integrate commits from %s' % remote, keep=[theirs])
				integrated[layer.name] = [git.commit_info(c)['subject'] for c in commits]
				leases[branch] = theirs
			elif git.is_ancestor(local, theirs):
				raise NeedsHuman('%s/%s is ahead of the local %s: gitum pull %s first' % (remote, branch, branch, remote))
			else:
				raise NeedsHuman('%s/%s and the local %s have diverged and %s/%s is not a state gitum published: '
						 'gitum pull %s first, or look at what was pushed there with plain git'
						 % (remote, branch, branch, remote, branch, remote))
		journal = git.rev(JOURNAL_REF)
		their_journal = git.rev(remote_ref(remote, JOURNAL_BRANCH))
		if their_journal and not git.is_ancestor(their_journal, journal):
			raise NeedsHuman('the journal on %s has states we do not have: gitum pull first' % remote)
		args = ['push', '--quiet', '--atomic', remote]
		refspecs = ['%s:%s' % (JOURNAL_REF, JOURNAL_REF)]
		for layer in cfg.ordered_layers():
			args.append('--force-with-lease=refs/heads/%s:%s' % (layer.branch, leases[layer.branch]))
			refspecs.append('+refs/heads/%s:refs/heads/%s' % (layer.branch, layer.branch))
			refspecs.append('%s:%s' % (history_ref(layer.name), history_ref(layer.name)))
			if layer.compat_start_tag and git.ref('refs/tags/' + layer.compat_start_tag):
				refspecs.append('+refs/tags/%s:refs/tags/%s' % (layer.compat_start_tag, layer.compat_start_tag))
		git.run(*args, *refspecs)
		tr = RefTransaction(git)
		for layer in cfg.ordered_layers():
			tr.update(published_ref(remote, layer.branch), git.rev('refs/heads/' + layer.branch), '')
		tr.commit()
		pending = repo.read_json('candidate-on-remote.json')
		if pending and pending['remote'] == remote:
			git.run('push', '--quiet', remote, *[':' + r for r in pending['refs']], check=False)
			(repo.gitum_dir / 'candidate-on-remote.json').unlink(missing_ok=True)
		fetch(repo, remote)
		event = publish_event(repo, remote, leases, integrated)
		hooks.run_event(repo, 'publish', event, event_values(event))
		return {'remote': remote, 'integrated': integrated, 'journal': event['journal'], 'event': event}


def publish_event(repo, remote, leases, integrated):
	"""The publish event: upstream versions and old/new tips of every layer on the remote."""
	git = repo.git
	journal = repo.journal()
	cfg = journal.config
	upstreams = {}
	for up in cfg.upstreams.values():
		data = journal.upstream(up.name)
		upstreams[up.name] = {'ref': up.ref, 'sha': data.get('sha'), 'tag': data.get('tag', ''),
				      'version': tag_version(up.release_tags, data.get('tag', ''))}
	layers = {}
	for layer in cfg.ordered_layers():
		st = journal.layer(layer.name)
		layers[layer.name] = {'branch': layer.branch, 'old': leases.get(layer.branch) or None,
				      'new': git.rev('refs/heads/' + layer.branch), 'base': st.base if st else None,
				      'start_tag': layer.compat_start_tag or None}
	return {'event': 'publish', 'remote': remote, 'journal': journal.sha, 'upstreams': upstreams,
		'layers': layers, 'integrated': integrated}


def event_values(event):
	for name, up in event['upstreams'].items():
		return {'upstream': name, 'target': up['sha'], 'target_tag': up['tag'], 'version': up['version']}
	return {}


def pull(repo, remote=None):
	"""Take the published state: journal, history and layer branches."""
	git = repo.git
	with repo.lock():
		remote = _remote(repo, remote)
		fetch(repo, remote)
		local_journal = git.rev(JOURNAL_REF)
		theirs = git.rev(remote_ref(remote, JOURNAL_BRANCH))
		if not theirs or theirs == local_journal or (local_journal and git.is_ancestor(theirs, local_journal)):
			return {'changed': False, 'remote': remote}
		target = theirs
		if local_journal and not git.is_ancestor(local_journal, theirs):
			target = merge_journals(repo, local_journal, theirs, remote)
		old_states = repo.journal().layers() if local_journal else {}
		new = repo.journal(target)
		tr = RefTransaction(git)
		moved = []
		for layer in new.config.ordered_layers():
			st = new.layer(layer.name)
			ref = 'refs/heads/' + layer.branch
			local = git.rev(ref)
			old = old_states.get(layer.name)
			if local and old and local != old.tip and local != st.tip and \
					not git.is_ancestor(local, target) and not contained(git, old.tip, local, st.changes):
				raise NeedsHuman('%s has commits that are neither saved in the journal nor in the pulled '
						 'state: gitum save, then pull' % layer.branch)
			if local != st.tip:
				tr.update(ref, st.tip, local or None)
				moved.append((layer.branch, local, st.tip))
			hist = git.rev(remote_ref(remote, HISTORY_PREFIX + layer.name))
			local_hist = git.rev(history_ref(layer.name))
			if hist and (not local_hist or git.is_ancestor(local_hist, hist)):
				tr.update(history_ref(layer.name), hist, '')
			tr.update(published_ref(remote, layer.branch), git.rev(remote_ref(remote, layer.branch)) or st.tip, '')
		tr.update(JOURNAL_REF, target, local_journal or None)
		with repo.committing(tr, moved, kind='pull'):
			pass
		return {'changed': True, 'remote': remote, 'journal': target, 'merged': target != theirs,
			'moved': [m[0] for m in moved]}


def contained(git, old_tip, local, changes):
	"""Whether the local commits after old_tip are all in the pulled layer (pushed and taken in)."""
	if not git.is_ancestor(old_tip, local):
		return False
	pids = {c.patch_id for c in changes}
	return all(pid in pids for pid in git.patch_ids(git.rev_list('%s..%s' % (old_tip, local))).values())


def merge_journals(repo, ours, theirs, remote):
	"""Three-way merge of two journal states that changed different files (series, changes)."""
	git = repo.git
	res = git.run('merge-tree', '--write-tree', '--name-only', '--no-messages', ours, theirs, check=False)
	lines = res.stdout.decode().splitlines()
	if res.returncode != 0:
		conflicts = [l for l in lines[1:] if l]
		raise NeedsHuman('the local journal and %s/%s changed the same files: %s'
				 % (remote, JOURNAL_BRANCH, ', '.join(conflicts)), conflicts=conflicts)
	return git.commit_tree(lines[0], [ours, theirs], 'gitum: merge journal from %s' % remote)

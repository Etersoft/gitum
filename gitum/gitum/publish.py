"""Publishing to and pulling from the shared remote.

Layer branches are rewritten by sync and pushed with a lease. The journal and
the history branches only grow and are pushed without force. Commits that
somebody else pushed onto a layer branch since our last publish are put on
top of our result instead of being overwritten.
"""

from . import state
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
		for layer in cfg.ordered_layers():
			branch = layer.branch
			local = git.rev('refs/heads/' + branch)
			theirs = git.rev(remote_ref(remote, branch))
			published = git.rev(published_ref(remote, branch))
			if not theirs or theirs == local or git.is_ancestor(theirs, local) or theirs == published:
				leases[branch] = theirs or ''
			elif published and git.is_ancestor(published, theirs):
				new_tip, commits = _integrate(repo, branch, published, theirs, local)
				tr = RefTransaction(git)
				tr.update('refs/heads/' + branch, new_tip, local)
				tr.commit()
				integrated[layer.name] = [git.commit_info(c)['subject'] for c in commits]
				leases[branch] = theirs
			elif git.is_ancestor(local, theirs):
				raise NeedsHuman('%s/%s is ahead of the local %s: gitum pull first' % (remote, branch, branch))
			else:
				raise NeedsHuman('%s/%s and the local %s have diverged' % (remote, branch, branch))
		if integrated:
			state.save(repo, list(integrated), 'integrate commits from %s' % remote)
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
		fetch(repo, remote)
		return {'remote': remote, 'integrated': integrated, 'journal': git.rev(JOURNAL_REF)}


def pull(repo, remote=None, update_checked_out=False):
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
			if local and old and local != old.tip and local != st.tip:
				raise NeedsHuman('%s has commits that are not saved in the journal: gitum save, then pull'
						 % layer.branch)
			where = repo.checked_out(layer.branch)
			if where and local != st.tip:
				if not update_checked_out:
					raise NeedsHuman('%s is checked out in %s: use --update-checked-out' % (layer.branch, where))
				if not git.at(where).is_clean():
					raise NeedsHuman('%s is checked out in %s with local changes' % (layer.branch, where))
			if local != st.tip:
				tr.update(ref, st.tip, local or None)
				moved.append((layer.branch, local, st.tip, where))
			hist = git.rev(remote_ref(remote, HISTORY_PREFIX + layer.name))
			local_hist = git.rev(history_ref(layer.name))
			if hist and (not local_hist or git.is_ancestor(local_hist, hist)):
				tr.update(history_ref(layer.name), hist, '')
			tr.update(published_ref(remote, layer.branch), git.rev(remote_ref(remote, layer.branch)) or st.tip, '')
		tr.update(JOURNAL_REF, target, local_journal or None)
		tr.commit()
		for branch, old, new_tip, where in moved:
			if where and old:
				git.at(where).run('read-tree', '-m', '-u', old, new_tip)
		return {'changed': True, 'remote': remote, 'journal': target, 'merged': target != theirs,
			'moved': [m[0] for m in moved]}


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

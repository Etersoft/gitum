"""gitum sync: move the layer stack onto a new upstream in one pass.

The work happens in a private work tree. No branch changes until the whole
stack is done; then all refs move in one transaction. A conflict that no
resolver handles stops the run (exit 2); `gitum continue`, `skip` or `abort`
resume it from the state saved in <common dir>/gitum/operation.json.
"""

import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import changes as changes_mod
from . import hooks, packaging, rrcache, state
from .errors import GitumError, NeedsHuman
from .git import RefTransaction
from .journal import (
	JOURNAL_REF,
	JournalWriter,
	LayerState,
	describe_changes,
	dump_json,
	history_commit,
	history_ref,
)

SHA_RE = re.compile(r'\b[0-9a-f]{40}\b')
ORIGIN_RE = re.compile(r'^(?:Origin:.*|\(cherry picked from commit .*\))$', re.M)
REVERT_RE = re.compile(r'^Revert "(.*)"$')

# results of a change in a sync
PICKED = 'picked'			# applied cleanly
RESOLVED = 'resolved'			# applied after a conflict resolution
DROPPED_UPSTREAM = 'dropped-upstream'	# the same patch is in the new base
DROPPED_BACKPORT = 'dropped-backport'	# its Origin commit is in the new base
ANNIHILATED = 'annihilated'		# a patch + revert pair
EMPTIED = 'emptied'			# nothing left after applying: probably accepted with changes
SKIPPED = 'skipped'			# dropped by a person (gitum skip)
DROPPED_MARKED = 'dropped-marked'	# its fate says so (merged, fixed differently, disabled)
TRUSTED_RESOLVERS = {'rerere', 'manual'}


def utcnow():
	return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def next_release(git, upstream, current):
	"""The first release tag after the current base, by version order."""
	if not upstream.release_tags:
		raise GitumError('upstream %s has no release-tags pattern for --next-release' % upstream.name)
	for tag in git.lines('tag', '-l', upstream.release_tags, '--sort=v:refname'):
		sha = git.rev('refs/tags/' + tag)
		if sha and sha != current and git.is_ancestor(current, sha):
			return tag, sha
	return None, None


def origin_commit(git, message, new_base):
	"""The upstream commit a backport came from, if it is in the new base."""
	for line in ORIGIN_RE.findall(message):
		for sha in SHA_RE.findall(line):
			if git.rev(sha) and git.is_ancestor(sha, new_base):
				return sha
	return None


def revert_pairs(git, commits):
	"""Commits forming 'patch' + 'Revert "patch"' pairs (exact subject match)."""
	infos = git.commit_infos(commits)
	subjects = [(sha, infos[sha]['subject']) for sha in commits]
	drop = {}
	for i, (sha, subject) in enumerate(subjects):
		m = REVERT_RE.match(subject)
		if not m:
			continue
		for j in range(i - 1, -1, -1):
			osha, osubject = subjects[j]
			if osha not in drop and osubject == m.group(1):
				drop[osha] = sha
				drop[sha] = osha
				break
	return drop


class Sync:
	"""A sync run. Its whole state lives in self.op so that it can be resumed."""

	def __init__(self, repo, op=None):
		self.repo = repo
		self.git = repo.git
		self.op = op

	# public entry points

	op_allow_rewrite = False

	def start(self, to=None, next_release_=False, candidate='auto', publish=False, fetch=True,
		  find_culprit=False, allow_rewrite=False):
		repo, git = self.repo, self.git
		self.op_allow_rewrite = allow_rewrite
		if repo.load_op():
			raise NeedsHuman('an operation is in progress: gitum continue, skip or abort')
		if (repo.gitum_dir / 'candidate.json').exists():
			raise NeedsHuman('a candidate is waiting for review: gitum accept or gitum drop-candidate')
		journal = repo.journal()
		cfg = journal.config
		if fetch:
			remotes = {u.remote for u in cfg.upstreams.values() if u.remote}
			for remote in sorted(remotes):
				git.run('fetch', '--quiet', '--tags', remote)
		# record work done with plain git before moving anything
		state.save(repo, message='before sync')
		journal = repo.journal()
		rrcache.import_from(repo, journal)
		targets, target_desc = self._targets(journal, to, next_release_)
		run_id, run_dir = repo.new_run_dir('sync')
		self.op = {
			'kind': 'sync', 'run': run_id, 'run_dir': str(run_dir), 'started': utcnow(),
			'journal': journal.sha, 'targets': targets, 'target_desc': target_desc,
			'options': {'candidate': candidate, 'publish': publish, 'find_culprit': find_culprit},
			'snapshot': self._snapshot(journal), 'layers': {}, 'pending': [l.name for l in cfg.ordered_layers()],
			'current': None, 'worktree': None, 'conflicts': 0,
		}
		self._check_checkouts()
		if all(targets[u]['sha'] == journal.upstream(u).get('sha') for u in targets):
			if not any(self._layer_moves(journal, l) for l in cfg.ordered_layers()):
				shutil.rmtree(run_dir, ignore_errors=True)
				return {'changed': False, 'message': 'everything is up to date', 'targets': targets}
		repo.save_op(self.op)		# from here on gitum continue can resume after a crash
		return self._run()

	def resume(self, action):
		"""action: continue, skip or abort."""
		op = self.op
		cur = op.get('current')
		wt = self.git.at(op['worktree']) if op.get('worktree') else None
		if action == 'abort':
			if wt:
				wt.run('cherry-pick', '--abort', check=False)
				wt.run('am', '--abort', check=False)
				head = wt.rev('HEAD')
				if head and cur:
					self.git.run('update-ref', 'refs/gitum/failed/%s/%s' % (op['run'], cur['layer']), head)
				self.repo.remove_worktree(op['worktree'])
			self.repo.clear_op()
			return {'changed': False, 'aborted': True, 'run': op['run'],
				'message': 'sync aborted; branches are unchanged'}
		if action == 'continue' and not op.get('stopped_at'):
			# not stopped on a conflict: the previous gitum was killed; redo the interrupted step
			self._recover()
			return self._run()
		if not cur or not wt:
			raise GitumError('nothing to %s' % action)
		if cur.get('gen'):
			self._resume_generation(wt, action)
			return self._run()
		res = next(r for r in cur['results'] if r['sha'] == op.get('stopped_at'))
		if action == 'skip':
			wt.run('cherry-pick', '--skip', check=False)
			wt.run('reset', '-q', '--hard', 'HEAD')
			res.update(result=SKIPPED, resolver='manual')
		else:
			unmerged = wt.unmerged_files()
			if unmerged:
				raise NeedsHuman('unresolved files in %s: %s' % (op['worktree'], ', '.join(unmerged)),
						 files=unmerged)
			self._save_resolution(wt, res)
			before = cur.get('head_before') or wt.rev('HEAD')
			if wt.ok('rev-parse', '-q', '--verify', 'CHERRY_PICK_HEAD'):
				if not wt.ok('-c', 'core.editor=true', 'cherry-pick', '--continue'):
					wt.run('cherry-pick', '--skip', check=False)
			after = wt.rev('HEAD')
			res.update(result=RESOLVED if after != before else EMPTIED, resolver='manual')
			if after != before:
				res['new_sha'] = after
		res.pop('stopped', None)
		op.pop('stopped_at', None)
		op.pop('message', None)
		cur.pop('head_before', None)
		self.repo.save_op(op)
		return self._run()

	# planning

	def _targets(self, journal, to, next_release_):
		cfg = journal.config
		git = self.git
		targets, desc = {}, []
		explicit = dict(to or {})
		for up in cfg.upstreams.values():
			current = journal.upstream(up.name).get('sha')
			if up.name in explicit or (None in explicit and len(cfg.upstreams) == 1):
				ref = explicit.get(up.name) or explicit.get(None)
				sha = git.rev(ref)
				if not sha:
					raise GitumError('upstream %s: %s does not exist' % (up.name, ref))
				tag = state.upstream_tag(git, up, sha)
			elif next_release_:
				tag, sha = next_release(git, up, current)
				if not sha:
					sha, tag = current, journal.upstream(up.name).get('tag', '')
			else:
				sha = git.rev(up.ref)
				if not sha:
					raise GitumError('upstream %s: %s does not exist' % (up.name, up.ref))
				tag = state.upstream_tag(git, up, sha)
			if current and sha != current and not git.is_ancestor(current, sha) \
					and not (up.rewritten or self.op_allow_rewrite):
				raise GitumError('upstream %s: %s is not newer than the current base %s'
						 % (up.name, tag or sha[:12], current[:12]))
			targets[up.name] = {'sha': sha, 'tag': tag, 'old': current}
			desc.append('%s %s' % (up.name, tag or sha[:12]))
		if None in explicit and len(cfg.upstreams) != 1:
			raise GitumError('several upstreams: use --to NAME=REF')
		return targets, ', '.join(desc)

	def _layer_moves(self, journal, layer):
		st = journal.layer(layer.name)
		if layer.base_kind == 'upstream':
			return st.base != self.op['targets'][layer.base_name]['sha']
		return st.base != journal.layer(layer.base_name).tip

	def _snapshot(self, journal):
		cfg = journal.config
		snap = {JOURNAL_REF: journal.sha}
		for layer in cfg.layers.values():
			snap['refs/heads/' + layer.branch] = self.git.rev('refs/heads/' + layer.branch)
			snap[history_ref(layer.name)] = self.git.rev(history_ref(layer.name))
			if layer.compat_start_tag:
				snap['refs/tags/' + layer.compat_start_tag] = self.git.ref('refs/tags/' + layer.compat_start_tag)
		return snap

	def _recover(self):
		"""Bring the private work tree back to the state before the interrupted step."""
		op = self.op
		cur = op.get('current')
		path = op.get('worktree')
		if path and not Path(path).is_dir():
			# the work tree is gone: start the current layer again
			self.repo.git.run('worktree', 'prune', check=False)
			op['worktree'] = None
			if cur:
				op['pending'].insert(0, cur['layer'])
				op['current'] = None
			self.repo.save_op(op)
			return
		if not path:
			return
		wt = self.git.at(path)
		wt.run('cherry-pick', '--abort', check=False)
		wt.run('am', '--abort', check=False)
		if cur and cur.get('picking'):
			wt.run('reset', '-q', '--hard', cur['head'])
			cur.pop('picking')
		gen = (cur or {}).get('gen')
		if gen and gen.get('applying') is not None:
			wt.run('reset', '-q', '--hard', gen['head'])
			gen.pop('applying')
		self.repo.save_op(op)

	def _check_checkouts(self):
		"""Layer branches checked out in clean user work trees follow the sync; dirty ones stop it."""
		cfg = self.repo.journal(self.op['journal']).config
		self.repo.check_checkouts([l.branch for l in cfg.layers.values()])

	def _env(self, layer_name=None, **extra):
		"""GITUM_* environment for the commands run on behalf of a layer."""
		op = self.op
		cfg = self.repo.journal(op['journal']).config
		up = cfg.root_upstream(cfg.layers[layer_name]) if layer_name else \
			(next(iter(cfg.upstreams.values())) if len(cfg.upstreams) == 1 else None)
		target = op['targets'].get(up.name, {}) if up else {}
		return self.repo.hook_env(
			worktree=op.get('worktree'), layer=layer_name, run=op['run'], run_dir=op['run_dir'],
			upstream=up.name if up else None, target=target.get('sha'), target_tag=target.get('tag', ''),
			version=packaging.tag_version(up.release_tags, target.get('tag', '')) if up else '', **extra)

	# the main loop

	def _run(self):
		op = self.op
		journal = self.repo.journal(op['journal'])
		cfg = journal.config
		while op['current'] or op['pending']:
			if not op['current']:
				name = op['pending'].pop(0)
				layer = cfg.layers[name]
				if not self._start_layer(journal, layer):
					continue
			elif op['current'].get('gen'):
				self._continue_generation()		# resumed in the middle of a generated layer
			self._continue_layer()
			self._finish_layer()
		return self._finish()

	def _start_layer(self, journal, layer):
		op, git = self.op, self.git
		old = journal.layer(layer.name)
		if layer.base_kind == 'upstream':
			new_base = op['targets'][layer.base_name]['sha']
		else:
			new_base = op['layers'][layer.base_name]['new_tip']
		if new_base == old.base:
			op['layers'][layer.name] = {'old_base': old.base, 'old_tip': old.tip, 'new_base': old.base,
						    'new_tip': old.tip, 'results': [], 'unchanged': True}
			self.repo.save_op(op)
			return False
		if layer.generator:
			return self._generate_layer(journal, layer, old, new_base)
		if git.rev_list('--merges', '%s..%s' % (old.base, old.tip)):
			raise GitumError('layer %s has merge commits; gitum layers must be linear' % layer.name)
		commits = git.rev_list('--reverse', '%s..%s' % (old.base, old.tip))
		results, todo = [], []
		pairs = revert_pairs(git, commits) if layer.annihilate_reverts else {}
		known = changes_mod.load_all(journal)
		fate_of = {c.sha: known.get(c.gitum_id) for c in old.changes if c.gitum_id in known}
		cherry = {}
		for line in git.lines('cherry', new_base, old.tip, old.base):
			sign, sha = line.split()
			cherry[sha] = sign
		infos = git.commit_infos(commits)
		for sha in commits:
			info = infos[sha]
			res = {'sha': sha, 'subject': info['subject']}
			gid = re.findall(r'^Gitum-Id:\s*(\S+)', info['message'], re.M)
			if gid:
				res['gitum_id'] = gid[-1]
			origin = origin_commit(git, info['message'], new_base)
			marked = changes_mod.should_drop(git, fate_of[sha], new_base) if fate_of.get(sha) else None
			if sha in pairs:
				res.update(result=ANNIHILATED, pair=pairs[sha])
			elif marked:
				res.update(result=DROPPED_MARKED, reason=marked)
			elif origin:
				res.update(result=DROPPED_BACKPORT, origin=origin)
			elif cherry.get(sha) == '-':
				res['result'] = DROPPED_UPSTREAM
			else:
				todo.append(sha)
			results.append(res)
		self._worktree(new_base)
		op['current'] = {'layer': layer.name, 'old_base': old.base, 'old_tip': old.tip,
				 'new_base': new_base, 'todo': todo, 'results': results}
		self.repo.save_op(op)
		return True

	def _generate_layer(self, journal, layer, old, new_base):
		"""Rebuild a layer by its generator (wine-staging, a command) on the new base."""
		op = self.op
		cfg = journal.config
		self._worktree(new_base)
		up = cfg.root_upstream(layer)
		target_tag = op['targets'].get(up.name, {}).get('tag', '')
		cur = {'layer': layer.name, 'old_base': old.base, 'old_tip': old.tip, 'new_base': new_base,
		       'todo': [], 'results': []}
		try:
			if layer.generator == 'staging':
				info, plan = packaging.staging_plan(self.repo, layer.options, target_tag, up.release_tags,
								    op['run_dir'], layer.name)
				cur['gen'] = {'plan': plan, 'pos': 0, 'info': info, 'resolved': [], 'skipped': []}
			else:
				wt = self.git.at(op['worktree'])
				info = packaging.generate_command(self.repo, wt, layer.options, self._env(layer.name, base=new_base),
								  op['run_dir'], layer.name)
				cur['gen'] = {'plan': [], 'pos': 0, 'info': info, 'resolved': [], 'skipped': []}
		except NeedsHuman:
			self._cleanup()
			raise
		op['current'] = cur
		self.repo.save_op(op)
		self._continue_generation()
		return True

	def _continue_generation(self):
		"""Apply the rest of the generator plan; stop on a patch that does not apply."""
		op = self.op
		cur = op['current']
		gen = cur['gen']
		wt = self.git.at(op['worktree'])
		layer = self.repo.journal(op['journal']).config.layers[cur['layer']]
		while gen['pos'] < len(gen['plan']):
			item = gen['plan'][gen['pos']]
			gen['applying'], gen['head'] = gen['pos'], wt.rev('HEAD')
			self.repo.save_op(op)
			res = packaging.apply_plan_item(wt, item)
			if res.returncode != 0:
				files = wt.unmerged_files()
				op['stopped_at'] = '%s/%s' % (item['series'], item['patch'])
				op['message'] = 'wine-staging %s does not apply' % op['stopped_at']
				(Path(op['run_dir']) / ('staging-failed-%s.txt' % cur['layer'])).write_bytes(res.stdout + res.stderr)
				self.repo.save_op(op)
				raise NeedsHuman(
					'%s: wine-staging patch %s does not apply%s.\nResolve it in %s (git am is in progress: '
					'fix the files, git add them), then run gitum continue; gitum skip drops the patch, '
					'gitum abort stops the sync.' % (cur['layer'], op['stopped_at'],
									 ' (%s)' % ', '.join(files) if files else '', op['worktree']),
					worktree=op['worktree'], series=item['series'], patch=item['patch'], files=files, run=op['run'])
			gen['pos'] += 1
			gen.pop('applying', None)
			self.repo.save_op(op)
		post = packaging.post_command(self.repo, wt, layer.options, self._env(layer.name), op['run_dir'], layer.name)
		if post:
			gen['info']['post_log'] = post
			wt.run('clean', '-q', '-fdx')		# backup and cache files of autoreconf and the like
		self._generation_results()

	def _resume_generation(self, wt, action):
		op = self.op
		gen = op['current']['gen']
		item = gen['plan'][gen['pos']]
		if action == 'skip':
			if not wt.ok('am', '--skip'):
				wt.run('am', '--abort', check=False)
			gen['skipped'].append({'series': item['series'], 'patch': item['patch'],
					       'subject': packaging.patch_subject(item['path'])})
		else:
			unmerged = wt.unmerged_files()
			if unmerged:
				raise NeedsHuman('unresolved files in %s: %s' % (op['worktree'], ', '.join(unmerged)),
						 files=unmerged)
			if (Path(wt.git_dir()) / 'rebase-apply').exists():
				res = wt.run('am', '--continue', check=False)
				if res.returncode != 0:
					raise NeedsHuman('git am --continue failed in %s: %s' % (
						op['worktree'], res.stderr.decode(errors='replace').strip()))
			gen['resolved'].append(wt.rev('HEAD'))
		gen['pos'] += 1
		op.pop('stopped_at', None)
		op.pop('message', None)
		self.repo.save_op(op)
		self._continue_generation()

	def _generation_results(self):
		op, git = self.op, self.git
		cur = op['current']
		gen = cur['gen']
		new_tip = git.at(op['worktree']).rev('HEAD')
		generated = git.rev_list('--reverse', '%s..%s' % (cur['new_base'], new_tip))
		new_pids = git.patch_ids(generated)
		infos = git.commit_infos(generated)
		new_subjects = {infos[s]['subject'] for s in generated}
		results = []
		for sha in generated:
			r = {'sha': sha, 'new_sha': sha, 'subject': infos[sha]['subject'], 'result': 'generated'}
			if sha in gen['resolved']:
				r.update(result=RESOLVED, resolver='manual')
			results.append(r)
		for sk in gen['skipped']:
			results.append({'sha': '', 'subject': sk['subject'], 'result': SKIPPED, 'resolver': 'manual',
					'series': sk['series']})
		old = self.repo.journal(op['journal']).layer(cur['layer'])
		for c in old.changes:
			if c.patch_id not in new_pids.values() and c.subject not in new_subjects:
				results.append({'sha': c.sha, 'subject': c.subject, 'result': 'dropped-by-generator'})
		cur['results'] = results
		cur['generator'] = gen['info']
		del cur['gen']
		self.repo.save_op(op)

	def _worktree(self, rev):
		op = self.op
		if not op['worktree']:
			wt = self.repo.add_worktree(rev, 'sync-' + op['run'])
			op['worktree'] = str(wt.cwd)
		else:
			wt = self.git.at(op['worktree'])
			wt.run('clean', '-q', '-fdx')		# leftovers of the previous layer (generated files)
			wt.run('checkout', '-q', '--detach', rev)
		return wt

	def _continue_layer(self):
		op = self.op
		cur = op['current']
		wt = self.git.at(op['worktree'])
		by_sha = {r['sha']: r for r in cur['results']}
		while cur['todo']:
			sha = cur['todo'][0]
			res = by_sha[sha]
			if res.get('result'):
				cur['todo'].pop(0)
				continue
			before = wt.rev('HEAD')
			# a step marker: after a crash the work tree goes back to `head` and the step is redone
			cur['picking'], cur['head'] = sha, before
			self.repo.save_op(op)
			pick = wt.run('-c', 'rerere.enabled=true', '-c', 'rerere.autoUpdate=true', 'cherry-pick',
				      '--allow-empty', '--empty=drop', sha, check=False)
			if pick.returncode == 0:
				after = wt.rev('HEAD')
				res['result'] = PICKED if after != before else EMPTIED
				if after != before:
					res['new_sha'] = after
			else:
				self._resolve(wt, res, pick)
			cur['todo'].pop(0)
			cur.pop('picking', None)
			self.repo.save_op(op)

	def _resolve(self, wt, res, pick):
		op, cfg = self.op, self.repo.journal(self.op['journal']).config
		files = wt.unmerged_files()
		if not files and not wt.ok('rev-parse', '-q', '--verify', 'CHERRY_PICK_HEAD'):
			raise GitumError('cherry-pick of %s failed: %s' % (res['sha'][:12], pick.stderr.decode().strip()))
		res['files'] = files or res.get('files', [])
		op['conflicts'] += 1
		res['conflict'] = op['conflicts']
		cdir = Path(op['run_dir']) / 'conflicts' / ('%03d-%s' % (op['conflicts'], res['sha'][:12]))
		cdir.mkdir(parents=True, exist_ok=True)
		(cdir / 'conflict.diff').write_bytes(wt.run('diff', check=False).stdout)
		resolver = None
		if not files:
			resolver = 'rerere'
		else:
			for name in cfg.resolve_chain:
				if name == 'rerere':
					continue
				cmd = cfg.resolve_rules if name == 'rules' else cfg.resolve_agent
				if not cmd:
					continue
				env = self._env(op['current']['layer'], commit=res['sha'], subject=res['subject'],
						files='\n'.join(files), conflict_dir=cdir, resolver=name)
				log = (cdir / ('%s.log' % name)).open('wb')
				proc = subprocess.run(['sh', '-c', cmd], cwd=wt.cwd, env=env,
						      stdout=log, stderr=subprocess.STDOUT)
				log.close()
				if proc.returncode == 0 and not wt.unmerged_files() and not self._has_markers(wt, files):
					resolver = name
					break
		if not resolver:
			if op['options'].get('find_culprit'):
				res['culprit'] = self._find_culprit(res['sha'])
			res['stopped'] = True
			op['stopped_at'] = res['sha']
			op['current']['head_before'] = wt.rev('HEAD')
			op['message'] = 'conflict in %s (%s)' % (res['subject'], ', '.join(files))
			self.repo.save_op(op)
			raise NeedsHuman(
				'%s: conflict in "%s" (%s).\nResolve it in %s, git add the files, then run '
				'gitum continue (or gitum skip / gitum abort).'
				% (op['current']['layer'], res['subject'], ', '.join(files), op['worktree']),
				worktree=op['worktree'], commit=res['sha'], files=files, run=op['run'])
		self._save_resolution(wt, res)
		before = wt.rev('HEAD')
		wt.run('add', '-u', check=False)
		if not wt.ok('-c', 'core.editor=true', 'cherry-pick', '--continue'):
			wt.run('cherry-pick', '--skip', check=False)
		after = wt.rev('HEAD')
		res.update(result=RESOLVED if after != before else EMPTIED, resolver=resolver)
		if after != before:
			res['new_sha'] = after

	@staticmethod
	def _has_markers(wt, files):
		for f in files:
			path = Path(wt.cwd) / f
			if path.is_file() and re.search(rb'^(<{7}|>{7})( |$)', path.read_bytes(), re.M):
				return True
		return False

	def _save_resolution(self, wt, res):
		cdir = Path(self.op['run_dir']) / 'conflicts' / ('%03d-%s' % (res.get('conflict', 0), res['sha'][:12]))
		cdir.mkdir(parents=True, exist_ok=True)
		(cdir / 'resolution.diff').write_bytes(wt.run('diff', '--cached', 'HEAD', check=False).stdout)

	def _find_culprit(self, sha):
		"""First upstream commit after which the patch no longer merges (merge-tree bisection)."""
		cur = self.op['current']
		git = self.git
		candidates = git.rev_list('--reverse', '--first-parent', '%s..%s' % (cur['old_base'], cur['new_base']))
		if not candidates:
			return None

		def conflicts(base):
			return git.run('merge-tree', '--write-tree', '--merge-base=%s^' % sha, base, sha,
				       check=False).returncode == 1

		if not conflicts(candidates[-1]):
			return None
		lo, hi = 0, len(candidates) - 1
		while lo < hi:
			mid = (lo + hi) // 2
			if conflicts(candidates[mid]):
				hi = mid
			else:
				lo = mid + 1
		info = git.commit_info(candidates[lo])
		return {'sha': candidates[lo], 'subject': info['subject']}

	def _finish_layer(self):
		op = self.op
		cur = op['current']
		wt = self.git.at(op['worktree'])
		new_tip = wt.rev('HEAD')
		range_diff = self.git.run('range-diff', '--no-color', '%s..%s' % (cur['old_base'], cur['old_tip']),
					  '%s..%s' % (cur['new_base'], new_tip), check=False).stdout
		(Path(op['run_dir']) / ('range-diff-%s.txt' % cur['layer'])).write_bytes(range_diff)
		op['layers'][cur['layer']] = {'old_base': cur['old_base'], 'old_tip': cur['old_tip'],
					      'new_base': cur['new_base'], 'new_tip': new_tip, 'results': cur['results']}
		if cur.get('generator'):
			op['layers'][cur['layer']]['generator'] = cur['generator']
		op['current'] = None
		self.repo.save_op(op)

	# finishing

	def _report(self):
		op = self.op
		layers = {}
		for name, l in op['layers'].items():
			counts = {}
			for r in l['results']:
				counts[r['result']] = counts.get(r['result'], 0) + 1
			layers[name] = {**l, 'counts': counts}
		return {'run': op['run'], 'started': op['started'], 'finished': utcnow(), 'targets': op['targets'],
			'target': op['target_desc'], 'layers': layers}

	def _needs_review(self, report):
		mode = self.op['options']['candidate']
		if mode == 'always':
			return True
		if mode == 'never':
			return False
		cfg = self.repo.journal(self.op['journal']).config
		trusted = set(cfg.review_unless) | TRUSTED_RESOLVERS
		return any(r.get('resolver') and r['resolver'] not in trusted
			   for l in report['layers'].values() for r in l['results'])

	def _run_check(self, report):
		cfg = self.repo.journal(self.op['journal']).config
		if not cfg.check:
			return
		wt = self.git.at(self.op['worktree'])
		leaves = [l for l in cfg.ordered_layers() if not cfg.children(l.name)]
		for layer in leaves:
			tip = self.op['layers'][layer.name]['new_tip']
			wt.run('checkout', '-q', '--detach', tip)
			log = Path(self.op['run_dir']) / ('check-%s.log' % layer.name)
			with log.open('wb') as f:
				proc = subprocess.run(['sh', '-c', cfg.check], cwd=wt.cwd, stdout=f, stderr=subprocess.STDOUT,
						      env=self._env(layer.name, tip=tip))
			if proc.returncode != 0:
				for name, l in self.op['layers'].items():
					self.git.run('update-ref', 'refs/gitum/failed/%s/%s' % (self.op['run'], name), l['new_tip'])
				self._cleanup()
				raise NeedsHuman('check failed on layer %s (see %s); branches are unchanged, the result is '
						 'kept in refs/gitum/failed/%s/' % (layer.name, log, self.op['run']),
						 log=str(log), run=self.op['run'])

	def _cleanup(self):
		if self.op.get('worktree'):
			self.repo.remove_worktree(self.op['worktree'])
		self.repo.clear_op()

	def _finish(self):
		op = self.op
		report = self._report()
		Path(op['run_dir'], 'report.json').write_bytes(dump_json(report))
		self._run_check(report)
		if self._needs_review(report):
			result = save_candidate(self.repo, op, report)
			self._cleanup()
			hooks.run_hooks(self.repo, report, candidate=True)
			raise NeedsHuman('the update needs a review: %s; then gitum accept' % result['message'],
					 report=report, candidate=result)
		tr, moves = commit_states(self.repo, op, report)
		with self.repo.committing(tr, moves, kind='sync', run=op['run'],
					  hooks_report='runs/%s/report.json' % op['run']):
			self._cleanup()
			hooks.run_hooks(self.repo, report)
		result = {'changed': True, 'report': report, 'run_dir': op['run_dir']}
		if op['options']['publish']:
			from . import publish
			result['published'] = publish.publish(self.repo)
		return result


def new_states(repo, op, tips=None):
	"""Layer states and upstream states after a run (tips: layer -> tip override)."""
	git = repo.git
	journal = repo.journal(op['journal'])
	cfg = journal.config
	states, new_tips = {}, {}
	for layer in cfg.ordered_layers():
		l = op['layers'][layer.name]
		tip = (tips or {}).get(layer.name, l['new_tip'])
		base = l['new_base'] if layer.base_kind == 'upstream' else new_tips[layer.base_name]
		if not git.is_ancestor(base, tip):
			raise GitumError('layer %s: %s is not based on %s' % (layer.name, tip[:12], base[:12]))
		new_tips[layer.name] = tip
		states[layer.name] = LayerState(layer.name, {
			'base': base, 'tip': tip, 'branch': layer.branch, 'upstream_ref': cfg.root_upstream(layer).ref,
			'changes': [c.to_json() for c in describe_changes(git, base, tip)],
		})
	ups = {}
	for name, t in op['targets'].items():
		ups[name] = {'sha': t['sha'], 'ref': cfg.upstreams[name].ref, 'tag': t['tag']}
	return states, ups


def commit_states(repo, op, report, tips=None):
	"""The transaction moving branches, history, START tags and the journal: (tr, moves)."""
	git = repo.git
	journal = repo.journal(op['journal'])
	cfg = journal.config
	states, ups = new_states(repo, op, tips)
	current_journal = git.rev(JOURNAL_REF)
	if current_journal != op['journal']:
		raise NeedsHuman('the journal changed during the run (%s); gitum abort and sync again' % current_journal[:12])
	writer = JournalWriter(git, journal.sha)
	tracker = changes_mod.Tracker(journal)
	for name, st in states.items():
		results = {r['sha']: r for r in op['layers'][name]['results']}
		tracker.track_layer(name, journal.layer(name), st.changes, results, context=op['target_desc'])
		writer.put_layer(st)
	tracker.write(writer)
	for name, data in ups.items():
		writer.put_json('upstreams/%s.json' % name, data)
	run_path = 'runs/%s' % op['run']
	writer.put_json(run_path + '/report.json', report)
	for f in Path(op['run_dir']).glob('range-diff-*.txt'):
		writer.put('%s/%s' % (run_path, f.name), f.read_bytes())
	rrcache.export_to(repo, writer, journal)
	tr = RefTransaction(git)
	moved = []
	for layer in cfg.ordered_layers():
		st = states[layer.name]
		ref = 'refs/heads/' + layer.branch
		old = op['snapshot'].get(ref)
		if st.tip != old:
			tr.update(ref, st.tip, old or None)
			moved.append((layer.branch, old, st.tip))
		hist_old = op['snapshot'].get(history_ref(layer.name))
		msg = '%s: sync to %s\n\nbase %s\ntip %s\nrun %s\n' % (layer.name, op['target_desc'], st.base, st.tip, op['run'])
		tr.update(history_ref(layer.name), history_commit(git, layer.name, st.tip, hist_old, msg), hist_old or None)
		if layer.compat_start_tag:
			tr.update('refs/tags/' + layer.compat_start_tag, st.base, '')
	message = 'gitum: sync to %s\n\n%s' % (op['target_desc'], summary_text(report))
	new_journal = writer.write(message, [s.tip for s in states.values()])
	tr.update(JOURNAL_REF, new_journal, journal.sha)
	return tr, moved


def save_candidate(repo, op, report):
	"""Put the result into candidate/* branches instead of the layer branches."""
	git = repo.git
	cfg = repo.journal(op['journal']).config
	tr = RefTransaction(git)
	refs = {}
	for layer in cfg.ordered_layers():
		ref = 'refs/heads/%s%s' % (cfg.candidate_prefix, layer.branch)
		tr.update(ref, op['layers'][layer.name]['new_tip'], '')
		refs[layer.name] = ref
	tr.commit()
	pushed = False
	if cfg.publish_remote:
		git.run('push', '--quiet', '--force', cfg.publish_remote,
			*['%s:%s' % (r, r) for r in refs.values()])
		pushed = True
	data = {'run': op['run'], 'op': {k: v for k, v in op.items() if k not in ('current', 'pending')},
		'refs': refs, 'pushed': pushed, 'report': report}
	repo.write_json('candidate.json', data)
	where = ' and %s' % cfg.publish_remote if pushed else ''
	return {'refs': refs, 'pushed': pushed,
		'message': 'the result is in %s*%s' % (cfg.candidate_prefix, where)}


def summary_text(report):
	lines = []
	for name, l in report['layers'].items():
		if l.get('unchanged'):
			continue
		counts = ', '.join('%s %d' % (k, v) for k, v in sorted(l['counts'].items()))
		lines.append('%s: %s' % (name, counts or 'no changes'))
		for r in l['results']:
			if r['result'] != PICKED:
				extra = ' [%s]' % r['resolver'] if r.get('resolver') else ''
				lines.append('  %s%s: %s' % (r['result'], extra, r['subject']))
	return '\n'.join(lines) + '\n'


def accept(repo, publish_=False):
	"""Publish a reviewed candidate (the candidate branches may have been edited)."""
	git = repo.git
	with repo.lock():
		path = repo.gitum_dir / 'candidate.json'
		if not path.exists():
			raise GitumError('no candidate to accept')
		cand = json.loads(path.read_text())
		op = cand['op']
		cfg = repo.journal(op['journal']).config
		if cand['pushed'] and cfg.publish_remote:
			git.run('fetch', '--quiet', cfg.publish_remote,
				*['+%s:%s' % (r, r) for r in cand['refs'].values()])
		tips = {name: git.rev(ref) for name, ref in cand['refs'].items()}
		tr, moves = commit_states(repo, op, cand['report'], tips)
		for ref in cand['refs'].values():
			tr.delete(ref)
		with repo.committing(tr, moves, kind='accept', run=op['run'], delete_refs=list(cand['refs'].values()),
				     hooks_report='runs/%s/report.json' % op['run']):
			if cand['pushed'] and cfg.publish_remote:
				# the remote keeps the candidate until the accepted state is published there
				repo.write_json('candidate-on-remote.json', {'remote': cfg.publish_remote,
									     'refs': list(cand['refs'].values())})
			path.unlink()
			hooks.run_hooks(repo, cand['report'])
	result = {'changed': True, 'report': cand['report'], 'accepted': cand['run']}
	if publish_:
		from . import publish
		result['published'] = publish.publish(repo)
	return result


def drop_candidate(repo):
	git = repo.git
	path = repo.gitum_dir / 'candidate.json'
	if not path.exists():
		raise GitumError('no candidate')
	cand = json.loads(path.read_text())
	cfg = repo.journal(cand['op']['journal']).config
	tr = RefTransaction(git)
	for ref in cand['refs'].values():
		if git.ref(ref):
			tr.delete(ref)
	tr.commit()
	if cand['pushed'] and cfg.publish_remote:
		git.run('push', '--quiet', cfg.publish_remote, *[':' + r for r in cand['refs'].values()], check=False)
	path.unlink()
	return {'dropped': cand['run']}


def step_targets(repo, to=None, next_release_=False):
	"""First-parent upstream commits from the current base to the target (one upstream only)."""
	journal = repo.journal()
	cfg = journal.config
	if len(cfg.upstreams) != 1:
		raise GitumError('--step works with one upstream')
	(name, up), = cfg.upstreams.items()
	current = journal.upstream(name).get('sha')
	if to:
		target = repo.git.rev(next(iter(to.values())))
	elif next_release_:
		target = next_release(repo.git, up, current)[1]
	else:
		target = repo.git.rev(up.ref)
	if not target or target == current:
		return []
	return repo.git.rev_list('--reverse', '--first-parent', '%s..%s' % (current, target))


def sync(repo, step=False, **kwargs):
	with repo.lock(wait=kwargs.pop('wait', False)):
		cfg = repo.journal().config
		if kwargs.get('fetch', True) and cfg.publish_remote:
			from . import publish
			publish.pull(repo)
		if not step:
			return Sync(repo).start(**kwargs)
		if kwargs.get('fetch', True):
			for remote in sorted({u.remote for u in cfg.upstreams.values() if u.remote}):
				repo.git.run('fetch', '--quiet', '--tags', remote)
		commits = step_targets(repo, kwargs.pop('to', None), kwargs.pop('next_release_', False))
		kwargs['fetch'] = False
		steps = []
		for sha in commits:
			result = Sync(repo).start(to={None: sha}, **kwargs)
			if result.get('changed'):
				steps.append(result['report'])
		if not steps:
			return {'changed': False, 'message': 'everything is up to date'}
		return {'changed': True, 'report': steps[-1], 'steps': steps}


def resume(repo, action):
	with repo.lock():
		op = repo.load_op()
		if not op:
			if repo.recovered:
				return {'changed': True, 'recovered': repo.recovered,
					'message': 'recovered: %s' % '; '.join(repo.recovered)}
			raise GitumError('no operation in progress')
		if op['kind'] != 'sync':
			raise GitumError('unknown operation %s' % op['kind'])
		return Sync(repo, op).resume(action)

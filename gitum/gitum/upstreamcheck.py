"""Finding our changes in upstream, also when they were accepted with edits.

For every change: exact patch-id match first; then candidates among the
upstream commits touching the same files, scored by subject and diff
similarity. A similar commit counts as "merged-modified" only when the
content is verified: our change applied onto upstream changes nothing.
Everything else similar is "suspect" and needs a person. The first release
tag containing the found commit is reported.
"""

import difflib
import os
import re
import tempfile

from . import changes as changes_mod
from . import series as series_mod
from .errors import GitumError
from .journal import JOURNAL_REF, JournalWriter
from .git import RefTransaction

SUSPECT_SCORE = 0.5
MAX_CANDIDATES = 30
SUBJECT_NOISE = re.compile(r'\(?(?:eter)?bug\s*#?\s*\d+\)?|\[[^\]]*\]|\s+', re.I)


def norm_subject(subject):
	return SUBJECT_NOISE.sub(' ', subject).strip().rstrip('.').lower()


MAX_DIFF_LINES = 4000		# difflib is quadratic: compare at most this many changed lines


def parse_diff(patch):
	"""Changed lines (without context) and touched files of a patch."""
	lines, files = [], set()
	for line in patch.decode('utf-8', errors='replace').splitlines():
		if line.startswith('+++ b/'):
			files.add(line[6:])
		elif line.startswith('--- a/'):
			files.add(line[6:])
		elif line[:1] and line[0] in '+-' and not line.startswith(('+++', '---')):
			lines.append(line[0] + line[1:].strip())
	return lines[:MAX_DIFF_LINES], files


def diff_lines(git, shas, relative=''):
	"""parse_diff() of many commits in one git process: {sha: (lines, files)}."""
	options = ['--unified=0'] + (['--relative=' + relative] if relative else [])
	return {sha: parse_diff(patch) for sha, patch in git.diffs(shas, *options).items()}


def relative_patch_ids(git, shas, relative):
	"""Patch-ids of the changes of commits inside a vendored subtree."""
	patches = git.diffs(shas, '--relative=' + relative)
	text = b''.join(b'commit %s\n%s' % (sha.encode(), patch) for sha, patch in patches.items())
	out = git.run('patch-id', '--stable', input=text).stdout.decode()
	return {sha: pid for pid, sha in (line.split() for line in out.splitlines())}


def content_in(git, sha, ref, relative='', ref_tree=None):
	"""Whether the change of commit sha is already contained in ref.

	A three-way apply of the commit onto ref must leave the tree of ref
	unchanged. For a vendored tree (relative) the reverse patch must apply.
	"""
	if relative:
		diff = git.run('show', '--format=', '--relative=' + relative, sha).stdout
		with tempfile.TemporaryDirectory() as tmp:
			g = git.with_env(GIT_INDEX_FILE=os.path.join(tmp, 'index'))
			g.run('read-tree', ref)
			return bool(diff.strip()) and g.ok('apply', '--cached', '-R', '--check', input=diff)
	res = git.run('merge-tree', '--write-tree', '--merge-base=%s^' % sha, ref, sha, check=False)
	return res.returncode == 0 and res.stdout.decode().split()[:1] == [ref_tree or git.tree(ref)]


class UpstreamIndex:
	"""Upstream commits of a range with their files and patch-ids."""

	def __init__(self, git, base, ref):
		self.git = git
		self.ref = ref
		self.ref_tree = git.tree(ref)
		self.commits = []
		raw = git.run('log', '--no-merges', '--format=%x01%H%x00%ae%x00%an%x00%s', '--name-only',
			      '%s..%s' % (base, ref)).stdout
		for block in raw.decode('utf-8', errors='replace').split('\x01')[1:]:
			head, _, names = block.partition('\n')
			sha, email, name, subject = head.split('\x00', 3)
			self.commits.append({'sha': sha, 'email': email.lower(), 'name': name.strip().lower(), 'subject': subject,
					     'files': {n for n in names.splitlines() if n}})
		self.by_file = {}
		for c in self.commits:
			for f in c['files']:
				self.by_file.setdefault(f, []).append(c)
		self.pids = git.patch_ids([c['sha'] for c in self.commits]) if self.commits else {}
		self.by_pid = {pid: sha for sha, pid in self.pids.items()}
		self._lines = {}

	def prefetch(self, shas):
		missing = [s for s in dict.fromkeys(shas) if s not in self._lines]
		for sha, (lines, _files) in diff_lines(self.git, missing).items():
			self._lines[sha] = lines

	def lines(self, sha):
		if sha not in self._lines:
			self.prefetch([sha])
		return self._lines.get(sha, [])

	def candidates(self, files, subject, author):
		"""Upstream commits touching the same files, best first, with their subject similarity.

		author: the set of identities (lowercase emails and names) of our author.
		"""
		found = {}
		for f in files:
			for c in self.by_file.get(f, []):
				found[c['sha']] = c
		scored = []
		subj = norm_subject(subject)
		for c in found.values():
			overlap = len(files & c['files']) / max(len(files | c['files']), 1)
			s_ratio = difflib.SequenceMatcher(None, subj, norm_subject(c['subject'])).ratio()
			same_author = c['email'] in author or c['name'] in author
			same_subject = norm_subject(c['subject']) == subj
			scored.append((overlap + s_ratio + (0.2 if same_author else 0), c, s_ratio, same_author, same_subject))
		scored.sort(key=lambda t: -t[0])
		return [t[1:] for t in scored[:MAX_CANDIDATES]]

	def find(self, sha, patch_id, ours, files, candidates, relative=''):
		if patch_id and patch_id in self.by_pid:
			return {'status': 'merged', 'upstream_commit': self.by_pid[patch_id], 'similarity': 1.0}
		if not candidates:
			return {'status': 'not-found'}		# upstream did not touch our files
		best = None
		for c, s_ratio, same_author, same_subject in candidates:
			d_ratio = difflib.SequenceMatcher(None, ours, self.lines(c['sha']), autojunk=False).ratio() if ours else 0
			score = round(0.35 * s_ratio + 0.65 * d_ratio, 3)
			if not best or score > best['similarity']:
				best = {'upstream_commit': c['sha'], 'upstream_subject': c['subject'], 'similarity': score,
					'same_author': same_author, 'same_subject': same_subject}
		present = content_in(self.git, sha, self.ref, relative, self.ref_tree)
		if not best or best['similarity'] < SUSPECT_SCORE:
			if present:
				return {'status': 'suspect', 'content_in_upstream': True,
					'note': 'the content is in upstream, the commit is unknown'}
			return {'status': 'not-found'}
		best['content_in_upstream'] = present
		if present:
			best['status'] = 'merged-modified'
		else:
			best['status'] = 'suspect'
			best['note'] = 'similar upstream commit, but our change is not contained in %s' % self.ref
		return best


def identities(cfg, name, email):
	"""An author as a set of lowercase names and emails, extended by [identity] aliases."""
	ids = {name.strip().lower(), email.lower()}
	for group in cfg.raw.get('identity', {}).get('aliases', []):
		group = {g.strip().lower() for g in group}
		if ids & group:
			ids |= group
	return ids


def one_to_one(found):
	"""Give each upstream commit to its best match; others keep it only with verified content."""
	by_commit = {}
	for e in found:
		if e.get('upstream_commit') and e['status'] != 'merged':
			by_commit.setdefault(e['upstream_commit'], []).append(e)
	for commit, entries in by_commit.items():
		if len(entries) < 2:
			continue
		entries.sort(key=lambda e: -e.get('similarity', 0))
		ids = [e.get('id') or e['sha'][:12] for e in entries]
		for e in entries:
			e['shared_with'] = [i for i in ids if i != (e.get('id') or e['sha'][:12])]
		for e in entries[1:]:
			if e['status'] == 'merged-modified':
				e['note'] = 'upstream commit %s also matches %s (several changes merged into one?)' % (
					commit[:12], ', '.join(e['shared_with']))
			else:
				e['status'] = 'suspect'
				e['note'] = 'upstream commit %s matches %s better' % (commit[:12], entries[0].get('id') or '')


def merged_series(tracker, all_series, containers, git=None):
	"""Series whose changes are all accepted upstream become merged (source check-upstream).

	Returns ({name: previous state} of the series that became merged, [names of merged
	series whose release was filled in]); the metadata in all_series is updated.
	"""
	merged, filled = {}, []
	for spec in sorted(containers):
		kind, _, name = spec.partition(':')
		meta = all_series.get(name) if kind == 'series' else None
		if not meta or not meta.get('changes'):
			continue
		recs = [tracker.changes.get(c.get('gitum_id')) for c in meta['changes']]
		if not all(r and r.get('fate') in ('merged', 'merged-modified') for r in recs):
			continue
		if meta.get('state') in series_mod.DONE_STATES:
			# merged already (by a forge): fill in what the forge could not know
			if meta.get('state') == 'merged' and not meta.get('release'):
				meta['release'] = max((r['meta'].get('release', '') for r in recs), key=_version_key)
				if meta['release']:
					filled.append(name)
			continue
		last = recs[-1]['meta']
		previous = meta.get('state')
		meta.update(state='merged', upstream_commit=last.get('upstream_commit', ''),
			    release=max((r['meta'].get('release', '') for r in recs), key=_version_key))
		if git and meta['upstream_commit'] and not meta.get('merged_at'):
			meta['merged_at'] = git.out('show', '-s', '--format=%cI', meta['upstream_commit'], check=False)
		series_mod.touch(meta, state='merged', previous=previous, source='check-upstream')
		merged[name] = previous
	return merged, filled


def _version_key(tag):
	return [int(p) if p.isdigit() else p for p in re.split(r'(\d+)', tag or '')]


_RELEASES = {}


def first_release(git, upstream, sha):
	"""The first release tag that contains sha (cached per upstream and commit)."""
	if not upstream or not upstream.release_tags or not sha:
		return ''
	key = (str(git.cwd), upstream.release_tags, sha)
	if key not in _RELEASES:
		tags = git.lines('tag', '--contains', sha, '--list', upstream.release_tags, '--sort=v:refname')
		_RELEASES[key] = tags[0] if tags else ''
	return _RELEASES[key]


def containers(journal, what):
	"""(name, search start, tip, changes, upstream) for 'layer:X', 'series:Y' or all.

	The search starts at the upstream sync point for layers (their own base
	for a layer on a layer would leave nothing to search) and at the base of
	a series.
	"""
	cfg = journal.config
	result = []
	layers = journal.layers()
	all_series = series_mod.load_all(journal)
	for spec in what or ['layer:' + n for n in layers] + ['series:' + n for n in all_series]:
		kind, _, name = spec.partition(':')
		if kind == 'layer' and name in layers:
			st = layers[name]
			up = cfg.root_upstream(cfg.layers[name])
			start = journal.upstream(up.name).get('sha') or st.base
			result.append((spec, start, st.tip, st.changes, up))
		elif kind == 'series' and name in all_series:
			meta = all_series[name]
			if meta.get('changes') is None:
				continue
			from .journal import Change
			up = None
			base_kind, _, base_name = meta['base'].partition(':')
			if meta.get('upstream'):
				up = cfg.upstreams.get(meta['upstream'])
			elif base_kind == 'upstream':
				up = cfg.upstreams.get(base_name)
			elif base_kind == 'layer' and base_name in cfg.layers:
				up = cfg.root_upstream(cfg.layers[base_name])
			result.append((spec, meta['base_sha'], meta['tip'], [Change(**c) for c in meta['changes']], up))
		else:
			raise GitumError('unknown %s (use layer:NAME or series:NAME)' % spec)
	return result


def check(repo, what=None, ref=None, apply=False, since=None):
	git = repo.git
	journal = repo.journal()
	found = []
	indexes = {}
	if since and not git.rev(since):
		raise GitumError('%s does not exist' % since)
	for spec, base, tip, chs, upstream in containers(journal, what):
		base = git.rev(since) if since else base
		target = ref or (upstream.ref if upstream else None)
		if not target or not git.rev(target):
			raise GitumError('%s: no upstream ref to search (use --ref)' % spec)
		key = (base, target)
		if key not in indexes:
			indexes[key] = UpstreamIndex(git, base, target)
		index = indexes[key]
		prefix = upstream.prefix if upstream else ''
		shas = [c.sha for c in chs]
		infos = git.commit_infos(shas)
		ours = diff_lines(git, shas, prefix)
		pids = relative_patch_ids(git, shas, prefix) if prefix else {c.sha: c.patch_id for c in chs}
		cands = {c.sha: index.candidates(ours.get(c.sha, ([], set()))[1], c.subject,
						 identities(journal.config, infos[c.sha]['author'], infos[c.sha]['email']))
			 for c in chs}
		index.prefetch(t[0]['sha'] for lst in cands.values() for t in lst)
		for c in chs:
			lines, files = ours.get(c.sha, ([], set()))
			if prefix and not files:
				continue		# does not touch the vendored tree
			res = index.find(c.sha, pids.get(c.sha), lines, files, cands[c.sha], prefix)
			entry = {'container': spec, 'id': c.gitum_id, 'sha': c.sha, 'subject': c.subject, **res}
			if res.get('upstream_commit'):
				entry['release'] = first_release(git, upstream, res['upstream_commit'])
			found.append(entry)
	one_to_one(found)
	applied, series_merged, filled = [], [], []
	if apply:
		with repo.lock():
			journal = repo.journal()
			tracker = changes_mod.Tracker(journal)
			all_series = series_mod.load_all(journal)
			for e in found:
				rec = tracker.changes.get(e.get('id'))
				if not rec:
					continue
				if e['status'] in ('merged', 'merged-modified'):
					if rec.get('fate') in ('merged', 'merged-modified') and \
							rec['meta'].get('upstream_commit') == e['upstream_commit']:
						continue
					tracker.set(e['id'], e['status'], upstream_commit=e['upstream_commit'],
						    similarity=str(e['similarity']), release=e.get('release', ''), candidate='')
					applied.append(e['id'])
				elif e['status'] == 'suspect' and e.get('upstream_commit') and \
						rec['meta'].get('candidate') != e['upstream_commit']:
					# remembered for gitum change confirm
					tracker.set(e['id'], candidate=e['upstream_commit'])
			merged, filled = merged_series(tracker, all_series, {e['container'] for e in found}, git)
			series_merged = sorted(merged)
			if tracker.dirty or merged or filled:
				writer = JournalWriter(git, journal.sha)
				tracker.write(writer)
				for name in list(merged) + filled:
					writer.put_json(series_mod.meta_path(name), all_series[name])
				new = writer.write('gitum: check-upstream: %d changes found upstream%s' % (
					len(applied), '; merged: ' + ', '.join(series_merged) if series_merged else ''))
				tr = RefTransaction(git)
				tr.update(JOURNAL_REF, new, journal.sha)
				tr.commit()
				for name, previous in merged.items():
					series_mod.state_hook(repo, journal.config, all_series[name], previous, 'check-upstream')
	return {'results': found, 'applied': applied, 'series_merged': series_merged, 'release_filled': filled,
		'suspect': [e for e in found if e['status'] == 'suspect']}

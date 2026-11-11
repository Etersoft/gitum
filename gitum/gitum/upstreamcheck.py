"""Finding our changes in upstream, also when they were accepted with edits.

For every change: exact patch-id match first; then candidates among the
upstream commits touching the same files, scored by subject and diff
similarity. The first release tag containing the found commit is reported.
"""

import difflib
import re

from . import changes as changes_mod
from . import series as series_mod
from .errors import GitumError
from .journal import JOURNAL_REF, JournalWriter
from .git import RefTransaction

MERGED_SCORE = 0.75
SUSPECT_SCORE = 0.5
MAX_CANDIDATES = 30
SUBJECT_NOISE = re.compile(r'\(?(?:eter)?bug\s*#?\s*\d+\)?|\[[^\]]*\]|\s+', re.I)


def norm_subject(subject):
	return SUBJECT_NOISE.sub(' ', subject).strip().rstrip('.').lower()


def diff_lines(git, sha):
	"""Changed lines of a commit (without context) and the files it touches."""
	out = git.run('show', '--format=', '--unified=0', '--no-color', sha).stdout.decode('utf-8', errors='replace')
	lines, files = [], set()
	for line in out.splitlines():
		if line.startswith('+++ b/'):
			files.add(line[6:])
		elif line.startswith('--- a/'):
			files.add(line[6:])
		elif line[:1] in '+-' and not line.startswith(('+++', '---')):
			lines.append(line[0] + line[1:].strip())
	return lines, files


class UpstreamIndex:
	"""Upstream commits of a range with their files and patch-ids."""

	def __init__(self, git, base, ref):
		self.git = git
		self.commits = []
		raw = git.run('log', '--no-merges', '--format=%x01%H%x00%ae%x00%s', '--name-only', '%s..%s' % (base, ref)).stdout
		for block in raw.decode('utf-8', errors='replace').split('\x01')[1:]:
			head, _, names = block.partition('\n')
			sha, email, subject = head.split('\x00', 2)
			self.commits.append({'sha': sha, 'email': email, 'subject': subject,
					     'files': {n for n in names.splitlines() if n}})
		self.by_file = {}
		for c in self.commits:
			for f in c['files']:
				self.by_file.setdefault(f, []).append(c)
		self.pids = git.patch_ids([c['sha'] for c in self.commits]) if self.commits else {}
		self.by_pid = {pid: sha for sha, pid in self.pids.items()}
		self._lines = {}

	def lines(self, sha):
		if sha not in self._lines:
			self._lines[sha] = diff_lines(self.git, sha)[0]
		return self._lines[sha]

	def find(self, sha, patch_id, subject, email):
		if patch_id and patch_id in self.by_pid:
			return {'status': 'merged', 'upstream_commit': self.by_pid[patch_id], 'similarity': 1.0}
		ours, files = diff_lines(self.git, sha)
		candidates = {}
		for f in files:
			for c in self.by_file.get(f, []):
				candidates[c['sha']] = c
		scored = []
		subj = norm_subject(subject)
		for c in candidates.values():
			overlap = len(files & c['files']) / max(len(files | c['files']), 1)
			s_ratio = difflib.SequenceMatcher(None, subj, norm_subject(c['subject'])).ratio()
			scored.append((overlap + s_ratio + (0.2 if c['email'] == email else 0), c, s_ratio))
		scored.sort(key=lambda t: -t[0])
		best = None
		for _, c, s_ratio in scored[:MAX_CANDIDATES]:
			d_ratio = difflib.SequenceMatcher(None, ours, self.lines(c['sha']), autojunk=False).ratio() if ours else 0
			score = round(0.35 * s_ratio + 0.65 * d_ratio, 3)
			if not best or score > best['similarity']:
				best = {'upstream_commit': c['sha'], 'upstream_subject': c['subject'], 'similarity': score}
		if not best or best['similarity'] < SUSPECT_SCORE:
			return {'status': 'not-found'}
		best['status'] = 'merged-modified' if best['similarity'] >= MERGED_SCORE else 'suspect'
		return best


def first_release(git, upstream, sha):
	if not upstream or not upstream.release_tags:
		return ''
	tags = git.lines('tag', '--contains', sha, '--list', upstream.release_tags, '--sort=v:refname')
	return tags[0] if tags else ''


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
			if base_kind == 'upstream':
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
		for c in chs:
			info = git.commit_info(c.sha)
			res = index.find(c.sha, c.patch_id, c.subject, info['email'])
			entry = {'container': spec, 'id': c.gitum_id, 'sha': c.sha, 'subject': c.subject, **res}
			if res.get('upstream_commit'):
				entry['release'] = first_release(git, upstream, res['upstream_commit'])
			found.append(entry)
	applied = []
	if apply:
		with repo.lock():
			journal = repo.journal()
			tracker = changes_mod.Tracker(journal)
			for e in found:
				if e['status'] in ('merged', 'merged-modified') and e.get('id') in tracker.changes:
					rec = tracker.changes[e['id']]
					if rec.get('fate') in ('merged', 'merged-modified') and \
							rec['meta'].get('upstream_commit') == e['upstream_commit']:
						continue
					tracker.set(e['id'], e['status'], upstream_commit=e['upstream_commit'],
						    similarity=str(e['similarity']), release=e.get('release', ''))
					applied.append(e['id'])
			if applied:
				writer = JournalWriter(git, journal.sha)
				tracker.write(writer)
				new = writer.write('gitum: check-upstream: %d changes found upstream' % len(applied))
				tr = RefTransaction(git)
				tr.update(JOURNAL_REF, new, journal.sha)
				tr.commit()
	return {'results': found, 'applied': applied,
		'suspect': [e for e in found if e['status'] == 'suspect']}

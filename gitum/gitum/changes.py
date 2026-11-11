"""Changes: persistent identity and fate of every local change.

A change is identified without rewriting commits: the id lives in the
journal (changes/<id>.json) and follows the change through syncs by the
Gitum-Id trailer if present, the sha mapping of a sync, the patch-id and,
last, the subject. Each change has a fate: what is known about it upstream.
"""

import datetime
import re
import secrets

from .errors import GitumError

FATES = {
	'local': 'only ours, no decision yet',
	'draft': 'being prepared for upstream',
	'review': 'internal review',
	'queued': 'waiting in the upstream queue',
	'sent': 'sent upstream (pr)',
	'merged': 'accepted upstream (upstream_commit)',
	'merged-modified': 'accepted upstream with changes (upstream_commit)',
	'rejected': 'rejected upstream',
	'fixed-differently': 'upstream fixed it another way (link)',
	'distro-only': 'not for upstream (reason)',
	'backport': 'taken from upstream (origin), drop when the base has it',
	'disabled': 'kept but not applied (reason)',
	'dropped': 'removed from the layer',
}
# fates after which a change is not applied any more once the condition holds
DROP_FATES = {'merged', 'merged-modified', 'fixed-differently', 'disabled'}
META_KEYS = {'pr', 'internal_mr', 'bugs', 'upstream_commit', 'release', 'link', 'reason', 'origin',
	     'drop_on', 'similarity', 'series', 'note'}
LIST_KEYS = {'bugs'}

# sync result -> new fate of a change that left its layer
RESULT_FATES = {
	'dropped-upstream': 'merged',
	'dropped-backport': 'merged',
	'emptied': 'merged-modified',
	'annihilated': 'dropped',
	'skipped': 'dropped',
	'dropped-marked': None,		# keeps its fate
	'dropped-by-generator': 'dropped',
}
BUG_RE = re.compile(r'(?:eterbug|bug)\s*#?\s*(\d+)', re.I)


def now():
	return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def new_id(existing):
	while True:
		cid = secrets.token_hex(4)
		if cid not in existing:
			return cid


def path(cid):
	return 'changes/%s.json' % cid


def load_all(journal):
	result = {}
	for name in journal.list('changes'):
		if name.endswith('.json'):
			data = journal.read_json('changes/' + name)
			if data:
				result[data['id']] = data
	return result


def action(change):
	"""What a person should do about a change, by its fate."""
	fate = change.get('fate', 'local')
	meta = change.get('meta', {})
	if change.get('gone'):
		return ''
	if fate == 'local':
		return 'decide: send upstream or mark distro-only'
	if fate == 'draft':
		return 'finish and send upstream'
	if fate in ('review', 'queued'):
		return 'wait for %s' % fate
	if fate == 'sent':
		return 'wait for upstream (%s)' % meta.get('pr', 'no pr link')
	if fate in ('merged', 'merged-modified'):
		if meta.get('upstream_commit'):
			return 'will be dropped when the base has %s' % meta['upstream_commit'][:12]
		return 'confirm the upstream commit (gitum check-upstream)'
	if fate == 'rejected':
		return 'rework or mark distro-only'
	if fate == 'fixed-differently':
		return 'will be dropped%s' % (' at ' + meta['drop_on'] if meta.get('drop_on') else ' at next sync')
	if fate == 'backport':
		return 'will be dropped when the base has %s' % (meta.get('origin') or '?')[:12]
	return ''


def should_drop(git, change, new_base):
	"""Whether a change must not be applied onto new_base because of its fate."""
	fate = change.get('fate')
	meta = change.get('meta', {})
	if fate == 'disabled':
		return 'disabled'
	if fate in ('merged', 'merged-modified') and meta.get('upstream_commit'):
		sha = git.rev(meta['upstream_commit'])
		if sha and git.is_ancestor(sha, new_base):
			return 'upstream has %s' % meta['upstream_commit'][:12]
	if fate == 'fixed-differently':
		drop_on = meta.get('drop_on')
		if not drop_on:
			return 'fixed differently upstream'
		sha = git.rev(drop_on)
		if sha and git.is_ancestor(sha, new_base):
			return 'fixed differently upstream in %s' % drop_on
	return None


class Tracker:
	"""Matches the changes of new layer states to known change ids and records events."""

	def __init__(self, journal=None):
		self.journal = journal
		self.changes = load_all(journal) if journal else {}
		self.dirty = set()
		self.by_sha = {c['sha']: cid for cid, c in self.changes.items() if c.get('sha')}
		self.by_pid = {}
		for cid, c in self.changes.items():
			if c.get('patch_id') and not c.get('gone'):
				self.by_pid.setdefault(c['patch_id'], cid)

	def get(self, ref):
		"""A change by id, id prefix, sha or sha prefix."""
		if ref in self.changes:
			return self.changes[ref]
		found = [c for cid, c in self.changes.items()
			 if cid.startswith(ref) or (len(ref) >= 7 and c.get('sha', '').startswith(ref))]
		if len(found) == 1:
			return found[0]
		if not found:
			raise GitumError('no change %s' % ref)
		raise GitumError('change %s is ambiguous: %s' % (ref, ', '.join(c['id'] for c in found)))

	def _event(self, change, event, **data):
		change.setdefault('events', []).append({'date': now(), 'event': event,
							**{k: v for k, v in data.items() if v}})
		self.dirty.add(change['id'])

	def track_layer(self, layer, old_state, new_changes, results=None, context=''):
		"""Assign ids to new_changes (journal.Change objects) and record what happened.

		results: sync results of the layer (sha -> result dict) or None for a save.
		"""
		old_ids = {c.sha: c.gitum_id for c in (old_state.changes if old_state else []) if c.gitum_id}
		old_by_pid = {c.patch_id: c.gitum_id for c in (old_state.changes if old_state else [])
			      if c.gitum_id and c.patch_id}
		old_by_subject = {}
		for c in (old_state.changes if old_state else []):
			if c.gitum_id:
				old_by_subject.setdefault(c.subject, c.gitum_id)
		new_sha_to_old = {}
		for r in (results or {}).values():
			if r.get('new_sha'):
				new_sha_to_old[r['new_sha']] = r['sha']
		seen = set()
		for c in new_changes:
			cid = None
			if c.gitum_id and c.gitum_id in self.changes:
				cid = c.gitum_id
			elif c.sha in new_sha_to_old and new_sha_to_old[c.sha] in old_ids:
				cid = old_ids[new_sha_to_old[c.sha]]
			elif c.sha in old_ids:
				cid = old_ids[c.sha]
			elif c.patch_id and c.patch_id in old_by_pid:
				cid = old_by_pid[c.patch_id]
			elif c.patch_id and c.patch_id in self.by_pid:
				cid = self.by_pid[c.patch_id]
			elif c.subject in old_by_subject:
				cid = old_by_subject[c.subject]
			if cid in seen:
				cid = None
			if cid is None or cid not in self.changes:
				cid = cid or c.gitum_id or new_id(self.changes)
				self.changes[cid] = {'id': cid, 'fate': 'local', 'meta': {}, 'created': now()}
				bugs = sorted(set(BUG_RE.findall(c.subject)))
				if bugs:
					self.changes[cid]['meta']['bugs'] = bugs
				self._event(self.changes[cid], 'added', layer=layer, sha=c.sha, context=context)
			seen.add(cid)
			c.gitum_id = cid
			rec = self.changes[cid]
			moved = rec.get('layer') and rec.get('layer') != layer
			if (rec.get('sha'), rec.get('subject'), rec.get('patch_id'), rec.get('layer')) != \
					(c.sha, c.subject, c.patch_id, layer) or rec.get('gone'):
				rec.update(sha=c.sha, subject=c.subject, patch_id=c.patch_id, layer=layer)
				rec.pop('gone', None)
				self.dirty.add(cid)
			if moved:
				self._event(rec, 'moved', layer=layer)
			r = (results or {}).get(new_sha_to_old.get(c.sha, c.sha))
			if r and r.get('result') == 'resolved':
				self._event(rec, 'resolved', resolver=r.get('resolver'), context=context, sha=c.sha,
					    culprit=(r.get('culprit') or {}).get('sha'))
		# changes that left the layer
		for old in (old_state.changes if old_state else []):
			if not old.gitum_id or old.gitum_id in seen or old.gitum_id not in self.changes:
				continue
			rec = self.changes[old.gitum_id]
			if rec.get('layer') != layer:
				continue
			r = (results or {}).get(old.sha)
			result = r['result'] if r else 'removed'
			rec['gone'] = True
			fate = RESULT_FATES.get(result, 'dropped') if r else 'dropped'
			if fate and rec.get('fate') not in DROP_FATES | {'backport'} | ({'merged'} if fate == 'merged-modified' else set()):
				rec['fate'] = fate
			if r and r.get('origin'):
				rec['meta']['upstream_commit'] = r['origin']
			self._event(rec, result, layer=layer, context=context, reason=(r or {}).get('reason'))
		return new_changes

	def set(self, ref, fate=None, **meta):
		rec = self.get(ref)
		if fate:
			if fate not in FATES:
				raise GitumError('unknown fate %s (known: %s)' % (fate, ', '.join(FATES)))
			if rec.get('fate') != fate:
				self._event(rec, 'fate', fate=fate, previous=rec.get('fate'))
			rec['fate'] = fate
		for key, value in meta.items():
			if key not in META_KEYS:
				raise GitumError('unknown key %s (known: %s)' % (key, ', '.join(sorted(META_KEYS))))
			if value in (None, ''):
				rec['meta'].pop(key, None)
			elif key in LIST_KEYS:
				values = value if isinstance(value, list) else [v for v in str(value).split(',') if v]
				rec['meta'][key] = sorted(set(rec['meta'].get(key, [])) | set(values))
			else:
				rec['meta'][key] = value
		self.dirty.add(rec['id'])
		return rec

	def write(self, writer):
		for cid in sorted(self.dirty):
			writer.put_json(path(cid), self.changes[cid])
		return len(self.dirty)

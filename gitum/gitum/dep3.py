"""DEP-3 patch headers (https://dep-team.pages.debian.net/deps/dep3/) from and to the fate of a change.

    Forwarded: <url> | no | not-needed     sent upstream (pr) / only ours / not for upstream
    Applied-Upstream: <version|commit|url>  merged
    Origin: upstream|backport, <commit|url> taken from upstream
    Bug: <url> / Bug-<Tracker>: <url>       bugs: the fixes tracker is the upstream one
"""

import re

from . import bugs as bugs_mod

FIELD_RE = re.compile(r'^(Forwarded|Applied-Upstream|Origin|Bug(?:-[\w.-]+)?):[ \t]*(\S.*?)\s*$', re.M)
SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')


def fields(cfg, rec):
	"""DEP-3 fields of a change record, in a stable order."""
	if not rec:
		return []
	fate, meta = rec.get('fate', 'local'), rec.get('meta', {})
	result = []
	if fate == 'backport' or meta.get('origin'):
		result.append(('Origin', 'upstream, %s' % meta['origin'] if meta.get('origin') else 'upstream'))
	if fate in ('merged', 'merged-modified'):
		result.append(('Applied-Upstream', meta.get('release') or meta.get('upstream_commit') or 'yes'))
	elif meta.get('pr'):
		result.append(('Forwarded', meta['pr']))
	elif fate == 'distro-only':
		result.append(('Forwarded', 'not-needed'))
	elif fate != 'backport':
		result.append(('Forwarded', 'no'))
	upstream_tracker = cfg.series.get('fixes-tracker') if cfg else None
	for b in meta.get('bugs') or []:
		info = bugs_mod.info(cfg, b)
		if not info['url']:
			continue
		key = 'Bug' if info['tracker'] == upstream_tracker else 'Bug-%s' % (info['tracker'] or 'Vendor').capitalize()
		result.append((key, info['url']))
	return result


def add_to_patch(text, pairs):
	"""Put the fields at the end of the description of a format-patch file (before ---)."""
	if not pairs:
		return text
	block = ''.join('%s: %s\n' % kv for kv in pairs)
	head, sep, tail = text.partition('\n---\n')
	if not sep:
		return text
	last = head.rstrip('\n').rsplit('\n\n', 1)[-1]
	trailers = all(re.match(r'^[\w-]+: ', line) for line in last.splitlines() if line)
	return head.rstrip('\n') + ('\n' if trailers and '\n\n' in head else '\n\n') + block.rstrip('\n') + sep + tail


def parse(text):
	"""DEP-3 fields of a patch (its header or description, before the diff): {field: value}."""
	head = re.split(r'^(?:---\s|diff --git |Index: )', text, maxsplit=1, flags=re.M)[0]
	return {m.group(1): m.group(2) for m in FIELD_RE.finditer(head)}


def fate_of(found):
	"""(fate, meta) for a change from parsed DEP-3 fields; (None, {}) when they say nothing."""
	if 'Applied-Upstream' in found:
		value = found['Applied-Upstream']
		meta = {'upstream_commit': value} if SHA_RE.match(value) else \
			{'link': value} if '://' in value else {'release': value}
		return 'merged', meta
	origin = found.get('Origin', '')
	kind, _, where = origin.partition(',')
	if kind.strip() in ('upstream', 'backport') and where.strip():
		return 'backport', {'origin': where.strip()}
	forwarded = found.get('Forwarded', '')
	if forwarded == 'not-needed':
		return 'distro-only', {'reason': 'Forwarded: not-needed'}
	if '://' in forwarded:
		return 'sent', {'pr': forwarded}
	return None, {}

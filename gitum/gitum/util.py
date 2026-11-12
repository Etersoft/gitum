"""Small helpers shared by the core and the commands: times, dates, tags, links."""

import datetime
import re
from fnmatch import fnmatch

from .errors import GitumError


def now():
	"""The current UTC time as stored in the journal (ISO, seconds)."""
	return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def short(sha):
	return sha[:12] if sha else '-'


def tag_version(pattern, tag):
	"""The version part of a release tag: wine-* and wine-11.20 give 11.20."""
	if not pattern or not tag or not fnmatch(tag, pattern):
		return ''
	prefix, _, suffix = pattern.partition('*')
	return tag[len(prefix):len(tag) - len(suffix) if suffix else None]


def upstream_values(name, release_tags, sha, tag):
	"""GITUM_UPSTREAM, _TARGET, _TARGET_TAG and _VERSION of a target commit of an upstream."""
	return {'upstream': name, 'target': sha, 'target_tag': tag or '', 'version': tag_version(release_tags, tag or '')}


def md_link(url):
	"""A merge request URL as a short markdown link."""
	m = re.match(r'^https?://[^/]+/(.+?)(?:/-/merge_requests/|/merge_requests/|/pulls?/)(\d+)', url or '')
	return '[%s!%s](%s)' % (m.group(1), m.group(2), url) if m else (url or '')


def local_date(value, tz=None):
	"""The date of an ISO timestamp in the local time zone, or in tz ("Europe/Moscow", "+03:00")."""
	if not value:
		return ''
	try:
		t = datetime.datetime.fromisoformat(value)
	except ValueError:
		return value[:10]
	if t.tzinfo is None:
		return t.date().isoformat()
	if tz:
		m = re.match(r'^([+-])(\d\d):?(\d\d)$', tz)
		if m:
			delta = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
			zone = datetime.timezone(delta if m.group(1) == '+' else -delta)
		else:
			import zoneinfo
			try:
				zone = zoneinfo.ZoneInfo(tz)
			except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
				raise GitumError('unknown time zone %r: %s' % (tz, e)) from e
		return t.astimezone(zone).date().isoformat()
	return t.astimezone().date().isoformat()

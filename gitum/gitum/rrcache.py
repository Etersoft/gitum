"""A git rerere cache shared through the journal.

Resolutions recorded anywhere (a person, the robot, another session) are
kept in rr-cache/ of the journal; before a sync they are copied into the
repository rr-cache, after it new ones go back into the journal.
"""

from pathlib import Path

RR_FILES = ('preimage', 'postimage')


def local_dir(repo):
	return Path(repo.common_dir) / 'rr-cache'


def import_from(repo, journal):
	"""Copy resolutions from the journal into the repository rr-cache (missing ones only)."""
	target = local_dir(repo)
	count = 0
	for entry in journal.list('rr-cache'):
		for name in RR_FILES:
			dst = target / entry / name
			if dst.exists():
				continue
			data = journal.read('rr-cache/%s/%s' % (entry, name))
			if data is not None:
				dst.parent.mkdir(parents=True, exist_ok=True)
				dst.write_bytes(data)
				count += 1
	return count


def export_to(repo, writer, journal):
	"""Add resolved entries of the repository rr-cache that the journal does not have yet."""
	source = local_dir(repo)
	if not source.is_dir():
		return 0
	known = set(journal.list('rr-cache'))
	count = 0
	for entry in sorted(p for p in source.iterdir() if p.is_dir()):
		if entry.name in known or not (entry / 'postimage').exists():
			continue
		for name in RR_FILES:
			if (entry / name).exists():
				writer.put('rr-cache/%s/%s' % (entry.name, name), (entry / name).read_bytes())
		count += 1
	return count

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
	for path, data in journal.read_files('rr-cache').items():
		dst = target / path[len('rr-cache/'):]
		if data is None or dst.exists() or not is_rr_file(dst.name):
			continue
		dst.parent.mkdir(parents=True, exist_ok=True)
		dst.write_bytes(data)
		count += 1
	return count


def is_rr_file(name):
	"""preimage, postimage and the numbered variants rerere keeps for several resolutions."""
	return name.split('.', 1)[0] in RR_FILES


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
		for f in sorted(entry.iterdir()):
			if f.is_file() and is_rr_file(f.name):
				writer.put('rr-cache/%s/%s' % (entry.name, f.name), f.read_bytes())
		count += 1
	return count

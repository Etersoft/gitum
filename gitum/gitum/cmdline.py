"""Shared pieces of the command line: the parser, KEY=VALUE pairs, files, exit codes."""

import argparse
import json
import sys
from pathlib import Path

from .errors import EXIT_OK, EXIT_UPDATED, GitumError


class UsageError(GitumError):
	"""A mistake on the command line (exit code 1, not 2: nothing waits for a person)."""


class Parser(argparse.ArgumentParser):
	"""argparse that reports usage mistakes as a GitumError instead of exiting with 2."""

	def error(self, message):
		raise UsageError('%s: %s' % (self.prog, message), usage=self.format_usage().strip())


def pairs(items, what='KEY=VALUE'):
	"""KEY=VALUE arguments as a dict; anything without '=' is a usage error."""
	result = {}
	for item in items:
		key, sep, value = item.partition('=')
		if not sep or not key:
			raise UsageError('%r is not %s' % (item, what))
		result[key] = value
	return result


def read_json_input(path):
	"""JSON from a file, or from stdin for '-' or no file."""
	text = sys.stdin.read() if path in (None, '-') else Path(path).read_text()
	return json.loads(text)


def write_output(path, text):
	"""Write a generated text to a file with a final newline; the message to print instead of it."""
	Path(path).write_text(text if text.endswith('\n') else text + '\n')
	return 'Written %s' % path


def done(args, changed):
	"""Exit code of a command that may change something: 10 for a change with --detailed-exitcode."""
	return EXIT_UPDATED if changed and getattr(args, 'detailed_exitcode', False) else EXIT_OK


def only_with(args, needed, *options):
	"""Refuse options that mean something only together with another one (instead of ignoring them)."""
	given = [o for o in options if getattr(args, o.lstrip('-').replace('-', '_'), None) not in (None, False, [])]
	if given and not getattr(args, needed.lstrip('-').replace('-', '_'), None):
		raise UsageError('%s only make%s sense with %s' % (', '.join(given), 's' if len(given) == 1 else '', needed))

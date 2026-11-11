"""Errors and exit codes."""

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_HUMAN = 2
EXIT_UPDATED = 10
EXIT_BUSY = 75


class GitumError(Exception):
	"""An error reported to the user; exit_code is the process exit status."""
	exit_code = EXIT_ERROR

	def __init__(self, message, **details):
		super().__init__(message)
		self.message = message
		self.details = details


class NeedsHuman(GitumError):
	"""The operation stopped and waits for a person (conflict, review, ...)."""
	exit_code = EXIT_NEEDS_HUMAN


class Busy(GitumError):
	exit_code = EXIT_BUSY


class NotGitumRepo(GitumError):
	pass


class ConfigError(GitumError):
	pass


class GitError(GitumError):
	def __init__(self, args, returncode, stdout, stderr):
		self.args_ = args
		self.returncode = returncode
		self.stdout = stdout.decode('utf-8', errors='replace') if isinstance(stdout, bytes) else stdout
		self.stderr = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else stderr
		super().__init__('git %s failed (%d): %s' % (' '.join(args), returncode, self.stderr.strip()))

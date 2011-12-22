class GitUmException(Exception):
	pass

class NotSupported(GitUmException):
	pass

class RepoIsDirty(GitUmException):
	pass

class NotUptodate(GitUmException):
	pass

class PatchFailed(GitUmException):
	pass

class RebaseFailed(GitUmException):
	pass

class CherryPickFailed(GitUmException):
	pass

class NoStateFile(GitUmException):
	pass

class NoConfigFile(GitUmException):
	pass

class BrokenRepo(GitUmException):
	pass

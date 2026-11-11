"""Project configuration: gitum.toml stored in the journal."""

import tomllib
from dataclasses import dataclass, field

from .errors import ConfigError

CONFIG_FILE = 'gitum.toml'
CONFIG_VERSION = 2


@dataclass
class Upstream:
	name: str
	ref: str				# what to follow, e.g. winehq/master or a local branch
	remote: str = ''			# remote to fetch before sync
	url: str = ''
	release_tags: str = ''			# glob for release tags, e.g. wine-*
	prefix: str = ''			# vendored upstream inside the tree
	rewritten: bool = False			# history is regenerated (e.g. a branch built by patchinstall)


@dataclass
class Layer:
	name: str
	branch: str
	base: str				# upstream:<name> or layer:<name>
	compat_start_tag: str = ''
	annihilate_reverts: bool = False

	@property
	def base_kind(self):
		return self.base.split(':', 1)[0]

	@property
	def base_name(self):
		return self.base.split(':', 1)[1]


@dataclass
class Config:
	upstreams: dict = field(default_factory=dict)
	layers: dict = field(default_factory=dict)
	resolve_chain: list = field(default_factory=lambda: ['rerere'])
	resolve_rules: str = ''
	resolve_agent: str = ''
	review_unless: list = field(default_factory=lambda: ['rerere'])
	publish_remote: str = ''
	candidate_prefix: str = 'candidate/'
	check: str = ''
	hooks: dict = field(default_factory=dict)
	series: dict = field(default_factory=dict)
	queue: dict = field(default_factory=dict)
	forges: dict = field(default_factory=dict)
	raw: dict = field(default_factory=dict)

	def ordered_layers(self):
		"""Layers so that every layer comes after the layer it is based on."""
		result, seen = [], set()

		def visit(layer, stack=()):
			if layer.name in seen:
				return
			if layer.name in stack:
				raise ConfigError('layer cycle: %s' % ' -> '.join(stack + (layer.name,)))
			if layer.base_kind == 'layer':
				visit(self.layers[layer.base_name], stack + (layer.name,))
			seen.add(layer.name)
			result.append(layer)

		for layer in self.layers.values():
			visit(layer)
		return result

	def root_upstream(self, layer):
		"""The upstream a layer finally stands on."""
		while layer.base_kind == 'layer':
			layer = self.layers[layer.base_name]
		return self.upstreams[layer.base_name]

	def children(self, name):
		return [l for l in self.layers.values() if l.base == 'layer:' + name]

	def layer_by_branch(self, branch):
		for layer in self.layers.values():
			if layer.branch == branch:
				return layer
		return None


def _get(table, key, typ, default, where):
	value = table.get(key, default)
	if not isinstance(value, typ):
		raise ConfigError('%s: %s must be %s' % (where, key, typ.__name__))
	return value


def parse(text):
	try:
		raw = tomllib.loads(text)
	except tomllib.TOMLDecodeError as e:
		raise ConfigError('%s: %s' % (CONFIG_FILE, e)) from e
	version = raw.get('version', CONFIG_VERSION)
	if version != CONFIG_VERSION:
		raise ConfigError('%s: unsupported version %s' % (CONFIG_FILE, version))
	cfg = Config(raw=raw)
	for name, t in raw.get('upstream', {}).items():
		where = 'upstream.' + name
		cfg.upstreams[name] = Upstream(
			name=name,
			ref=_get(t, 'ref', str, '', where),
			remote=_get(t, 'remote', str, '', where),
			url=_get(t, 'url', str, '', where),
			release_tags=_get(t, 'release-tags', str, '', where),
			prefix=_get(t, 'prefix', str, '', where),
			rewritten=_get(t, 'rewritten', bool, False, where),
		)
		if not cfg.upstreams[name].ref:
			raise ConfigError('%s: ref is required' % where)
	for name, t in raw.get('layer', {}).items():
		where = 'layer.' + name
		cfg.layers[name] = Layer(
			name=name,
			branch=_get(t, 'branch', str, name, where),
			base=_get(t, 'base', str, '', where),
			compat_start_tag=_get(t, 'compat-start-tag', str, '', where),
			annihilate_reverts=_get(t, 'annihilate-reverts', bool, False, where),
		)
	for layer in cfg.layers.values():
		kind, _, target = layer.base.partition(':')
		if kind == 'upstream' and target in cfg.upstreams:
			continue
		if kind == 'layer' and target in cfg.layers and target != layer.name:
			continue
		raise ConfigError('layer.%s: base must be upstream:<name> or layer:<name> of a known one, not %r'
				  % (layer.name, layer.base))
	resolve = raw.get('resolve', {})
	cfg.resolve_chain = _get(resolve, 'chain', list, ['rerere'], 'resolve')
	unknown = set(cfg.resolve_chain) - {'rerere', 'rules', 'agent'}
	if unknown:
		raise ConfigError('resolve.chain: unknown resolvers %s' % ', '.join(sorted(unknown)))
	cfg.resolve_rules = _get(resolve, 'rules', str, '', 'resolve')
	cfg.resolve_agent = _get(resolve, 'agent', str, '', 'resolve')
	cfg.review_unless = _get(resolve, 'review-unless', list, ['rerere'], 'resolve')
	publish = raw.get('publish', {})
	cfg.publish_remote = _get(publish, 'remote', str, '', 'publish')
	cfg.candidate_prefix = _get(publish, 'candidate-prefix', str, 'candidate/', 'publish')
	cfg.check = _get(publish, 'check', str, '', 'publish')
	cfg.hooks = _get(raw, 'hooks', dict, {}, 'hooks')
	cfg.series = _get(raw, 'series', dict, {}, 'series')
	cfg.queue = _get(raw, 'queue', dict, {}, 'queue')
	cfg.forges = _get(raw, 'forge', dict, {}, 'forge')
	cfg.ordered_layers()
	return cfg


def _quote(value):
	if isinstance(value, bool):
		return 'true' if value else 'false'
	if isinstance(value, list):
		return '[' + ', '.join(_quote(v) for v in value) + ']'
	return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def render(upstreams, layers, extra=''):
	"""Render a minimal gitum.toml from upstream and layer definitions (lists of dicts)."""
	lines = ['version = %d' % CONFIG_VERSION, '']
	for up in upstreams:
		lines.append('[upstream.%s]' % up['name'])
		for key in ('remote', 'url', 'ref', 'release-tags', 'prefix', 'rewritten'):
			if up.get(key):
				lines.append('%s = %s' % (key, _quote(up[key])))
		lines.append('')
	for layer in layers:
		lines.append('[layer.%s]' % layer['name'])
		for key in ('base', 'branch', 'compat-start-tag', 'annihilate-reverts'):
			if layer.get(key) not in (None, '', False):
				lines.append('%s = %s' % (key, _quote(layer[key])))
		lines.append('')
	if extra:
		lines.append(extra.strip())
		lines.append('')
	return '\n'.join(lines)

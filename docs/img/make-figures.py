#!/usr/bin/python3
"""Draw the commit pictures of the documentation as SVG (in the style of the 2012 presentation).

Run from this directory: python3 make-figures.py
Every figure is written in English and Russian: NAME.en.svg, NAME.ru.svg.
"""

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent

# kind of a commit -> fill, stroke
STYLE = {
	'up': ('#3d6fc4', '#1f3f80'),		# upstream commit
	'new': ('#2fae7f', '#16704d'),		# new upstream commit
	'our': ('#f2cf4a', '#a8891b'),		# our change
	'edit': ('#f08a3c', '#a6531b'),		# our change, edited
	'took': ('#2fae7f', '#a8891b'),		# upstream commit that is our accepted change
	'gone': ('#ffffff', '#b4b4b4'),		# dropped
}
LEGEND = {
	'en': [('up', 'upstream commit'), ('new', 'new upstream commit'), ('our', 'our change'),
	       ('edit', 'our change, edited'), ('took', 'our change, accepted upstream')],
	'ru': [('up', 'коммит апстрима'), ('new', 'новый коммит апстрима'), ('our', 'наше изменение'),
	       ('edit', 'наше изменение, исправленное'), ('took', 'наше изменение, принятое апстримом')],
}
STEP = 34		# vertical distance of commits
COL = 170		# horizontal distance of columns
TOP = 96		# where branch titles end; branch lines start below


class Figure:
	def __init__(self, width, height):
		self.w, self.h = width, height
		self.items = []

	def add(self, text):
		self.items.append(text)

	def text(self, x, y, s, size=15, anchor='middle', weight='normal', color='#222', italic=False):
		style = ' font-style="italic"' if italic else ''
		self.add('<text x="%d" y="%d" font-size="%d" text-anchor="%s" font-weight="%s" fill="%s"%s>%s</text>'
			 % (x, y, size, anchor, weight, color, style, escape(s)))

	def commit(self, x, y, kind, label=''):
		fill, stroke = STYLE[kind]
		dash = ' stroke-dasharray="3 2"' if kind == 'gone' else ''
		width = 3 if kind == 'took' else 1.6
		self.add('<circle cx="%d" cy="%d" r="11" fill="%s" stroke="%s" stroke-width="%s"%s/>'
			 % (x, y, fill, stroke, width, dash))
		if label:
			self.text(x + 18, y + 5, label, 13, 'start', color='#555' if kind != 'gone' else '#999',
				  italic=kind == 'gone')

	def branch(self, x, title, commits, bottom, note=''):
		"""A branch line with commits from the bottom up: [(kind, label), ...]."""
		top = max(bottom - STEP * (len(commits) + 0.6), TOP)
		self.add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#555" stroke-width="1.5"/>' % (x, bottom + 14, x, top))
		self.text(x, TOP - 40, title, 15, weight='bold')
		if note:
			self.text(x, TOP - 22, note, 12, color='#666')
		for i, (kind, label) in enumerate(commits):
			self.commit(x, bottom - STEP * i, kind, label)

	def states(self, x, title, states, bottom, note=''):
		"""A history branch: one square per recorded state, from the bottom up."""
		top = max(bottom - STEP * (len(states) + 0.6), TOP)
		self.add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#555" stroke-width="1.5"/>' % (x, bottom + 14, x, top))
		self.text(x, TOP - 40, title, 15, weight='bold')
		if note:
			self.text(x, TOP - 22, note, 12, color='#666')
		for i, label in enumerate(states):
			y = bottom - STEP * i
			self.add('<rect x="%d" y="%d" width="20" height="20" rx="3" fill="#8f7cc9" stroke="#55448c" '
				 'stroke-width="1.6"/>' % (x - 10, y - 10))
			self.text(x + 18, y + 5, label, 13, 'start', color='#555')

	def arrow(self, x1, x2, y, label=''):
		self.add('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#888" stroke-width="2" marker-end="url(#a)"/>'
			 % (x1, y, x2 - 6, y))
		if label:
			self.text((x1 + x2) // 2, y - 10, label, 13, color='#555')

	def legend(self, lang, kinds, y):
		x = 20
		for kind, label in LEGEND[lang]:
			if kind not in kinds:
				continue
			self.commit(x + 11, y, kind)
			self.text(x + 28, y + 5, label, 13, 'start', color='#444')
			x += 46 + (8.3 if any(ord(c) > 127 for c in label) else 7.4) * len(label)

	def svg(self):
		return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
			'font-family="DejaVu Sans, Verdana, sans-serif">\n'
			'<defs><marker id="a" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">'
			'<path d="M0,0 L10,5 L0,10 z" fill="#888"/></marker></defs>\n'
			'<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="#fdfdfb" stroke="#ddd"/>\n%s\n</svg>\n'
			% (self.w, self.h, self.w, self.h, self.w - 1, self.h - 1, '\n'.join(self.items)))


def T(lang, en, ru):
	return en if lang == 'en' else ru


def problem(lang):
	"""merge buries our changes, rebase loses their history, gitum keeps both."""
	f = Figure(5 * COL + 20, 470)
	b = 380
	up = [('up', ''), ('up', ''), ('up', '')]
	f.branch(90, 'upstream', up + [('new', ''), ('new', ''), ('new', '')], b)
	f.branch(90 + COL, 'merge', up + [('our', 'A'), ('new', ''), ('our', 'B'), ('new', ''), ('new', '')], b,
		 T(lang, 'ours get buried', 'наши тонут'))
	f.branch(90 + 2 * COL, 'rebase', up + [('new', ''), ('new', ''), ('new', ''), ('our', "A'"), ('our', "B'")], b,
		 T(lang, 'history is lost', 'история потеряна'))
	f.branch(90 + 3 * COL - 20, 'gitum', up + [('new', ''), ('new', ''), ('new', ''), ('our', "A'"), ('our', "B'")], b,
		 T(lang, 'layer on top', 'слой сверху'))
	f.states(90 + 4 * COL - 70, 'history', [T(lang, 'state 1', 'состояние 1'), 'sync'], b,
		 T(lang, 'kept', 'сохранена'))
	f.legend(lang, {'up', 'new', 'our'}, 445)
	return f


def sync(lang):
	"""A new upstream release: gitum sync moves our changes on top and records a new state."""
	f = Figure(6 * COL + 20, 440)
	b = 340
	up = [('up', ''), ('up', ''), ('up', '')]
	ours = [('our', 'A'), ('our', 'B')]
	f.text(70 + COL, 24, T(lang, 'before', 'до'), 14, color='#888', italic=True)
	f.text(70 + 4 * COL + 45, 24, T(lang, 'after gitum sync', 'после gitum sync'), 14, color='#888', italic=True)
	f.branch(70, 'upstream', up + [('new', 'U4'), ('new', 'U5')], b, T(lang, 'new release', 'новый релиз'))
	f.branch(70 + COL, T(lang, 'your layer', 'ваш слой'), up + ours, b)
	f.states(70 + 2 * COL, 'history', [T(lang, 'state 1', 'состояние 1')], b)
	f.arrow(70 + 2 * COL + 90, 70 + 3 * COL + 60, b - 2 * STEP, 'gitum sync')
	f.branch(70 + 4 * COL - 40, T(lang, 'your layer', 'ваш слой'), up + [('new', ''), ('new', ''), ('our', "A'"), ('our', "B'")], b,
		 T(lang, 'ours on top again', 'наши снова сверху'))
	f.states(70 + 5 * COL - 30, 'history', [T(lang, 'state 1', 'состояние 1'), 'sync v1.1'], b,
		 T(lang, 'a new state', 'новое состояние'))
	f.legend(lang, {'up', 'new', 'our'}, 415)
	return f


def save(lang):
	"""Your own new commit and an edited old one: gitum save records the new state."""
	f = Figure(5 * COL + 60, 440)
	b = 340
	up = [('up', ''), ('up', ''), ('up', '')]
	f.branch(80, T(lang, 'your layer', 'ваш слой'), up + [('our', 'A'), ('our', 'B')], b)
	f.states(80 + COL - 20, 'history', [T(lang, 'state 1', 'состояние 1')], b)
	f.arrow(80 + COL + 70, 80 + 2 * COL + 30, b - 2 * STEP, T(lang, 'commit, amend', 'commit, amend'))
	f.branch(80 + 3 * COL - 50, T(lang, 'your layer', 'ваш слой'), up + [('edit', "A*"), ('our', "B'"), ('our', 'C')], b,
		 T(lang, 'A edited, C added', 'A исправлено, C новое'))
	f.states(80 + 4 * COL - 30, 'history', [T(lang, 'state 1', 'состояние 1'), 'save'], b,
		 T(lang, 'git log -p: what changed', 'git log -p: что изменилось'))
	f.text(80 + 4 * COL - 30, b + 40, T(lang, 'the old A stays in the journal', 'прежнее A остаётся в журнале'), 12, color='#666')
	f.legend(lang, {'up', 'our', 'edit'}, 415)
	return f


def accepted(lang):
	"""Upstream took one of our changes: the next sync drops it from the layer by itself."""
	f = Figure(5 * COL + 60, 440)
	b = 340
	up = [('up', ''), ('up', ''), ('up', '')]
	f.branch(80, 'upstream', up + [('new', ''), ('took', 'A'), ('new', '')], b, T(lang, 'took our A', 'приняли наше A'))
	f.branch(80 + COL, T(lang, 'your layer', 'ваш слой'), up + [('our', 'A'), ('our', 'B')], b)
	f.arrow(80 + COL + 60, 80 + 2 * COL + 70, b - 2 * STEP, 'gitum sync')
	f.branch(80 + 3 * COL - 30, T(lang, 'your layer', 'ваш слой'),
		 up + [('new', ''), ('took', 'A'), ('new', ''), ('our', "B'")], b, T(lang, 'A is gone from the layer', 'A ушло из слоя'))
	f.add('<rect x="%d" y="%d" width="%d" height="96" rx="8" fill="#f4f1e6" stroke="#d8cfa8"/>'
	      % (80 + 4 * COL - 70, b - 180, 210))
	f.text(80 + 4 * COL + 35, b - 155, T(lang, 'journal: change A', 'журнал: изменение A'), 13, weight='bold', color='#444')
	f.text(80 + 4 * COL + 35, b - 132, T(lang, 'fate: merged', 'судьба: принято'), 13, color='#444')
	f.text(80 + 4 * COL + 35, b - 112, T(lang, 'as upstream commit …', 'как коммит апстрима …'), 12, color='#666')
	f.text(80 + 4 * COL + 35, b - 94, T(lang, 'in release v1.1', 'в релизе v1.1'), 12, color='#666')
	f.legend(lang, {'up', 'new', 'our', 'took'}, 415)
	return f


FIGURES = {'problem': problem, 'sync': sync, 'save': save, 'accepted': accepted}

if __name__ == '__main__':
	for name, draw in FIGURES.items():
		for lang in ('en', 'ru'):
			(OUT / ('%s.%s.svg' % (name, lang))).write_text(draw(lang).svg())
	print('written to', OUT)

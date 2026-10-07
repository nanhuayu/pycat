"""Small native source highlighter; no parser, document copies or extra editor."""

from PyQt6.QtCore import QRegularExpression
from PyQt6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat

from pycat.gui.utils.theme import resolve_accent, resolve_theme, theme_tokens

LANGUAGES = {
    '.py': 'python', '.pyw': 'python', '.pyi': 'python',
    '.js': 'code', '.jsx': 'code', '.mjs': 'code', '.cjs': 'code', '.ts': 'code', '.tsx': 'code',
    '.c': 'code', '.h': 'code', '.cpp': 'code', '.hpp': 'code', '.cs': 'code', '.java': 'code',
    '.go': 'code', '.rs': 'code', '.json': 'json', '.jsonl': 'json',
    '.html': 'markup', '.htm': 'markup', '.xhtml': 'markup', '.xml': 'markup', '.svg': 'markup',
    '.css': 'css', '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'config', '.ini': 'config',
    '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell', '.ps1': 'shell', '.sql': 'sql',
    '.md': 'markdown', '.markdown': 'markdown',
}
KEYWORDS = {
    'python': 'False None True and as assert async await break class continue def del elif else except finally '
              'for from global if import in is lambda nonlocal not or pass raise return try while with yield match case',
    'code': 'abstract as async await bool break case catch char class const continue default delete do double else '
            'enum export extends false float fn for function if implements import in int interface let namespace '
            'new null nullptr package private protected public return static string struct super switch this throw '
            'throws true try type typeof var virtual void while yield',
    'json': 'true false null', 'yaml': 'true false null yes no', 'config': 'true false',
    'shell': 'if then else elif fi for while do done case esac function return in foreach param begin process end',
    'sql': 'select from where join left right inner outer on as and or not null true false insert into values update '
           'set delete create table alter drop index group by order having limit offset union distinct case when then end',
}
STRINGS = r'''"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?'''
NUMBER = r'\b(?:0[xX][\da-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b'


def _expression(language):
    """One left-to-right token scan keeps comment markers inside strings inert."""
    delimiters = {
        'python': [('"""', '"""', 'string'), ("'''", "'''", 'string')],
        'code': [('/*', '*/', 'comment'), ('`', '`', 'string')],
        'css': [('/*', '*/', 'comment')], 'sql': [('/*', '*/', 'comment')],
        'markup': [('<!--', '-->', 'comment')],
    }.get(language, [])
    patterns = []
    if delimiters:
        patterns.append('(?<open>' + '|'.join(QRegularExpression.escape(start) for start, _, _ in delimiters) + ')')
    comment = {'python': '#.*', 'code': '//.*', 'yaml': '#.*', 'config': '[#;].*', 'shell': '#.*', 'sql': '--.*'}.get(language)
    if comment:
        patterns.append(f'(?<comment>{comment})')
    key = {
        'json': r'"(?:\\.|[^"\\])*"(?=\s*:)',
        'yaml': r'^\s*[\w.-]+(?=\s*:)', 'config': r'^\s*(?:[\w.-]+(?=\s*=)|\[[^\]]+\])',
        'markup': r'</?[\w:-]+|/?>|[\w:-]+(?=\s*=)',
        'css': r'[\w-]+(?=\s*:)', 'shell': r'\$\{?[\w]+',
        'markdown': r'^\s*(?:[-*+]\s+|\d+\.\s+|>\s?)|\[[^\]]+\]\([^)]*\)',
    }.get(language)
    if key:
        patterns.append(f'(?<key>{key})')
    patterns.append(f'(?<string>{STRINGS if language != "markdown" else r"`[^`]+`"})')
    keywords = KEYWORDS.get(language, '').split()
    keyword = r'\b(?:' + '|'.join(keywords) + r')\b' if keywords else ''
    if language in {'sql', 'shell', 'yaml', 'config'}:
        keyword = '(?i:' + keyword + ')'
    if language == 'markdown':
        keyword = r'^#{1,6}\s+.*|\*\*[^*]+\*\*'
    if keyword:
        patterns.append(f'(?<keyword>{keyword})')
    if language != 'markdown':
        patterns.append(f'(?<number>{NUMBER})')
    expression = QRegularExpression('|'.join(patterns))
    expression.setPatternOptions(QRegularExpression.PatternOption.UseUnicodePropertiesOption)
    expression.optimize()
    return expression, [(start, QRegularExpression(QRegularExpression.escape(end)), role)
                        for start, end, role in delimiters]


class DocumentHighlighter(QSyntaxHighlighter):
    MAX_DOCUMENT_CHARS = 512 * 1024  # QTextDocument UTF-16 units, checked without copying its text.
    MAX_BLOCKS = 10000
    MAX_LINE_CHARS = 4096

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.language = ''
        self._expression, self._delimiters = None, []
        self._colors, self._formats = {}, {}
        self.refresh_theme()
        editor.document().contentsChange.connect(self.sync_document)

    def configure(self, suffix):
        self.setDocument(None)
        self.language = LANGUAGES.get(str(suffix).lower(), '')
        self._expression, self._delimiters = _expression(self.language) if self.language else (None, [])
        self.sync_document()

    def sync_document(self, *_):
        document = self.editor.document()
        bounded = (document.characterCount() - 1 <= self.MAX_DOCUMENT_CHARS and document.blockCount() <= self.MAX_BLOCKS)
        target = document if self.language and bounded else None
        if self.document() is not target:
            self.setDocument(target)

    def refresh_theme(self):
        tokens = theme_tokens(resolve_theme(self.editor), resolve_accent(self.editor))
        colors = {role: tokens.color('syntax_' + role) for role in ('keyword', 'key', 'string', 'number', 'comment')}
        if self._colors == colors:
            return
        self._colors = colors
        for role, color in colors.items():
            format_ = QTextCharFormat()
            format_.setForeground(QColor(color))
            self._formats[role] = format_
        if self.document() is not None:
            self.rehighlight()

    def highlightBlock(self, text):
        self.setCurrentBlockState(0)
        length = self.currentBlock().length() - 1
        if length > self.MAX_LINE_CHARS:
            return
        if self.language == 'markdown':
            marker = text.lstrip()
            fence = 1 if marker.startswith('```') else 2 if marker.startswith('~~~') else 0
            previous = max(0, self.previousBlockState())
            if previous or fence:
                self.setFormat(0, length, self._formats['string'])
                self.setCurrentBlockState(0 if previous and fence == previous else previous or fence)
                return
        offset = 0
        state = self.previousBlockState()
        if 0 < state <= len(self._delimiters):
            _, closing, role = self._delimiters[state - 1]
            match = closing.match(text)
            offset = match.capturedEnd() if match.hasMatch() else length
            self.setFormat(0, offset, self._formats[role])
            if not match.hasMatch():
                self.setCurrentBlockState(state)
                return
        matches = self._expression.globalMatch(text, offset)
        while matches.hasNext():
            match = matches.next()
            opening = match.captured('open')
            if opening:
                state = next(i for i, (start, _, _) in enumerate(self._delimiters, 1) if start == opening)
                _, closing, role = self._delimiters[state - 1]
                end = closing.match(text, match.capturedEnd())
                stop = end.capturedEnd() if end.hasMatch() else length
                self.setFormat(match.capturedStart(), stop - match.capturedStart(), self._formats[role])
                if not end.hasMatch():
                    self.setCurrentBlockState(state)
                    return
                matches = self._expression.globalMatch(text, stop)
            else:
                for role, format_ in self._formats.items():
                    if match.capturedStart(role) >= 0:
                        self.setFormat(match.capturedStart(), match.capturedLength(), format_)
                        break

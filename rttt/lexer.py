import re
from typing import Callable
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.document import Document
from prompt_toolkit.styles.named_colors import NAMED_COLORS
from prompt_toolkit.formatted_text.base import StyleAndTextTuples


DEF_LOG_LEXER_COLOR_TABLE = {
    'default': '#eeeeee',
    'X': NAMED_COLORS['Blue'],
    'D': NAMED_COLORS['Magenta'],
    'I': NAMED_COLORS['Green'],
    'W': NAMED_COLORS['Yellow'],
    'E': NAMED_COLORS['Red'],
    'dbg': NAMED_COLORS['Magenta'],
    'inf': NAMED_COLORS['Green'],
    'wrn': NAMED_COLORS['Yellow'],
    'err': NAMED_COLORS['Red'],
}

LOG_PATERN = r'^(\[.*?\].*?<(\w+)\>)(.*)'
OLD_LOG_PATERN = r'^(#.*?\d(?:\.\d+)? <(\w)\>)(.*)'


class LogLexer(Lexer):
    def __init__(self, colors: dict | None = DEF_LOG_LEXER_COLOR_TABLE) -> None:
        super().__init__()
        self.re = re.compile(LOG_PATERN)
        self.re_old = re.compile(OLD_LOG_PATERN)
        self.colors = colors or DEF_LOG_LEXER_COLOR_TABLE
        self.def_color = self.colors.get('default', '#eeeeee')

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        lines = document.lines

        def get_line(lineno: int) -> StyleAndTextTuples:
            line = lines[lineno]
            g = self.re.match(line)
            if not g:
                g = self.re_old.match(line)
            if g:
                color = self.colors.get(g.group(2), self.def_color)
                return [(color, g.group(1)), (self.def_color, g.group(3))]

            return [(self.def_color, line)]

        return get_line

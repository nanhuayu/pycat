"""A passive text/table projection of bounded HTML; original source stays intact."""
from html import escape
from html.parser import HTMLParser


class _StaticHtml(HTMLParser):
    tags = set('html body p div span a b strong i em u s sub sup code pre blockquote '
               'h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr td th br hr font'.split())
    omitted = {'head', 'script', 'style', 'iframe', 'object', 'canvas', 'svg', 'form', 'audio', 'video'}
    attributes = set('style align dir color bgcolor size face border cellspacing cellpadding width height '
                     'colspan rowspan valign type start name id title'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.omit_tag, self.omit_depth = [], '', 0

    def handle_starttag(self, tag, attrs):
        if tag == 'body' and self.omit_tag == 'head':
            self.omit_depth = 0
        if self.omit_depth:
            if tag == self.omit_tag:
                self.omit_depth += 1
            return
        if tag in self.omitted:
            self.omit_tag, self.omit_depth = tag, 1
        elif tag == 'img':
            alt = dict(attrs).get('alt', '')
            self.parts.append(escape('[' + alt + ']') if alt else '')
        elif tag in self.tags:
            values = [(key, value) for key, value in attrs if value is not None and
                      (key in self.attributes or key == 'href' and value.startswith('#'))]
            self.parts.append('<' + tag + ''.join(f' {key}="{escape(value, quote=True)}"' for key, value in values) + '>')

    def handle_endtag(self, tag):
        if self.omit_depth:
            if tag == self.omit_tag:
                self.omit_depth -= 1
        elif tag in self.tags:
            self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.omit_depth:
            self.parts.append(escape(data))


def static_html(source: str) -> str:
    parser = _StaticHtml()
    parser.feed(source)
    parser.close()
    return ''.join(parser.parts)

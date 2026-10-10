from __future__ import annotations

import re

#: An admonition GitHub alert syntax made, with the bold line it opens with - its title.
TITLED_CALLOUT = re.compile(
    r'(<div class="admonition (?:note|tip|important|warning|caution)">\s*)'
    r'<p class="admonition-title">[^<]*</p>\s*<p><strong>(?P<title>.*?)</strong></p>',
    re.S,
)
#: The title of an admonition of no title of its own.
UNTITLED_CALLOUT = re.compile(r'<p class="admonition-title">(Note|Tip|Important|Warning|Caution)</p>')
#: The title of an untitled admonition on a Russian page, by its type.
RUSSIAN_CALLOUT_TITLES = {
    "Note": "Примечание",
    "Tip": "Совет",
    "Important": "Важно",
    "Warning": "Внимание",
    "Caution": "Осторожно",
}
#: A heading whose anchor an inline ``<a id="...">`` names - the form GitHub reads.
HTML_ANCHORED_HEADING = re.compile(r'^(?P<level>#{1,6}) <a id="(?P<anchor>[A-Za-z0-9_-]+)"></a>(?P<title>.*)$', re.M)
#: A fenced code block, its fences included - left as it is.
FENCED_CODE = re.compile(r"(^[ \t>]*```.*?^[ \t>]*```)", re.M | re.S)

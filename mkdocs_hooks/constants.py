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
#: The suffix of a Russian page - its English sibling has the same name without ``.ru``.
RUSSIAN_PAGE_SUFFIX = ".ru.md"
#: The language of the site build the pages are checked in - the i18n plugin builds the site once per
#: language, and the pages of both are checked together.
CHECKED_SITE_LANGUAGE = "en"
#: The page every reader starts from - no other page needs to link it.
HOME_PAGE_NAME = "index.md"
#: The pages ``benchmarks/bench.py`` writes - only what doesn't depend on their text is checked.
GENERATED_PAGE_PREFIXES = ("benchmarks/",)
#: The page comparing hare with Django - a link to it may name Django.
OTHER_ORM_PAGE_NAME = "coming-from-django"
#: The pages that may compare hare with other ORMs.
OTHER_ORM_PAGE_PREFIXES = ("benchmarks/", f"getting-started/{OTHER_ORM_PAGE_NAME}")
#: The databases a page or a heading may be about, by the folder of their pages under ``dialects/``.
DATABASE_NAMES_BY_FOLDER = {"postgresql": "PostgreSQL", "sqlite": "SQLite", "clickhouse": "ClickHouse"}
#: The longest line of prose - a link that can't be broken may go past it.
MAX_PROSE_LINE_LENGTH = 110
#: The longest table cell - more goes into a section the cell links to.
MAX_TABLE_CELL_LENGTH = 400

#: A line opening or closing a fenced code block, inside a list or a quote too: its indent and the
#: language the opening fence names.
CODE_FENCE = re.compile(r"^(?P<indent>[ \t]*(?:>[ \t]?)?[ \t]*)```(?P<language>[\w+-]*)\s*$")
#: A Markdown heading: its level and its text.
MARKDOWN_HEADING = re.compile(r"^(?P<level>#{1,6}) (?P<title>.*)$")
#: The inline anchor opening a heading's text.
HEADING_ANCHOR = re.compile(r'^<a id="(?P<anchor>[A-Za-z0-9_-]+)"></a>')
#: A Markdown link: its target.
MARKDOWN_LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
#: Inline code - left out of the checks of prose.
INLINE_CODE = re.compile(r"`[^`]*`")
#: A link whose text is empty - ``[](target)``, an image ``![](...)`` left out.
EMPTY_LINK_TEXT = re.compile(r"(?<!!)\[\]\(")
#: What separates the cells of a table row - a pipe not escaped as ``\|``.
TABLE_CELL_SEPARATOR = re.compile(r"(?<!\\)\|")
#: The target of a link, left out of the length of a line: a long one can't be broken.
LINK_TARGET = re.compile(r"\]\([^)]*\)")
#: A version named as the oldest one a feature needs: ``SQLite 3.37+``, ``PostgreSQL 17 or newer``.
VERSION_REQUIREMENT = re.compile(
    r"\b(?P<database>PostgreSQL|SQLite|ClickHouse) (?P<version>\d+(?:\.\d+)*)(?:\+| or (?:newer|later))"
)
#: A heading about one database in a form the standard replaces by ``Topic (Database)``.
DATABASE_HEADING_FORM = re.compile(
    r"^(?:PostgreSQL|SQLite|ClickHouse): "
    r"|\((?:PostgreSQL|SQLite|ClickHouse) only\)"
    r"|(?:PostgreSQL|SQLite|ClickHouse)-only"
)
#: A comment of a code block - Python's, SQL's, a shell's, an INI file's - to the end of its line.
CODE_COMMENT = re.compile(r"\s*(?:#|--|//|;).*$")
#: A docstring on one line - the text of an example, translated with the page.
ONE_LINE_DOCSTRING = re.compile(r'^\s*""".*"""$')
#: A hyphen standing for a dash between words of prose.
HYPHEN_AS_DASH = re.compile(r"\w - \w")
#: Straight quotes around Russian words, where the standard has «ёлочки».
STRAIGHT_QUOTED_RUSSIAN = re.compile(r'"[А-Яа-яЁё][^"]*"')
#: Comparisons with other ORMs in English prose - allowed on ``OTHER_ORM_PAGE_PREFIXES`` only.
OTHER_ORM_COMPARISONS = re.compile(r"\bDjango\b|\btortoise", re.IGNORECASE)
#: Words and phrases English prose replaces -> what the standard says instead.
ENGLISH_REPLACED_WORDING = {
    # DB-API is the name of Python's database interface (PEP 249).
    re.compile(r"\bDB\b(?!-API)"): 'say "database"',
    re.compile(r"\bconnection name\b|\bconnection names\b(?! (?:its|the|a|an) )"): 'say "connection alias"',
    re.compile(r"\binitialis"): 'American spelling: "initialize"',
    re.compile(r"\bbehaviour"): 'American spelling: "behavior"',
    re.compile(r"\bpreviously\b|\bin (?:earlier|older) versions\b|\bused to be\b|\banymore\b"): (
        "describe what the code does now, not its history"
    ),
}
#: Words Russian prose replaces -> what the glossary says instead.
RUSSIAN_REPLACED_WORDING = {
    re.compile(r"\b[Сс]толб(?:ец|ц)"): 'глоссарий: "колонка"',
    re.compile(r"\b[Кк]еш"): 'глоссарий: "кэш"',
    re.compile(r"\b[Тт]енант"): 'глоссарий: "арендатор"',
    re.compile(r"\b[Лл]укап|\b[Ll]ookups?\b"): 'глоссарий: "оператор фильтра"',
    re.compile(r"\b[Пп]равил\w* сортировки|\b[Кк]олляци|\b[Сс]опоставлени\w* строк"): 'глоссарий: "правило сравнения"',
    re.compile(r"\b[Сс]ыр\w* SQL"): 'глоссарий: "готовый SQL"',
}
#: The page GitHub Pages serves for every missing path, and the language it is written in - each
#: language's build writes it to the site's root, so only this one's is kept.
NOT_FOUND_TEMPLATE_NAME = "404.html"
NOT_FOUND_PAGE_LANGUAGE = "en"
#: The search index of a build, under the site's root - and under a translation's own folder.
SEARCH_INDEX_PATH = "search/search_index.json"
#: The base path in a page's theme configuration - the search index and the search results are read
#: from it.
PAGE_CONFIG_BASE = re.compile(r'("base": ")([^"]*)(")')

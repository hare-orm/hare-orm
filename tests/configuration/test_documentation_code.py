from __future__ import annotations

import pytest

from mkdocs_hooks.docs_standard_checks import DocsStandardChecks

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("value", ["https://example.test/a#b", "value; -- tail", "left // right", "# literal"])
def test_translation_keeps_comment_characters_inside_strings(value):
    original = f"value = {value!r} # explanation\n"
    translated = f"value = {value!r} # translated explanation\n"
    changed = f"value = {(value + ' changed')!r} # explanation\n"
    assert DocsStandardChecks.get_code_without_comments(original) == DocsStandardChecks.get_code_without_comments(
        translated
    )
    assert DocsStandardChecks.get_code_without_comments(original) != DocsStandardChecks.get_code_without_comments(
        changed
    )


def test_only_actual_docstrings_are_translatable():
    original = 'class Example:\n    """Explanation\n    continued."""\n    value = "payload"\n'
    translated = original.replace("Explanation", "Translated").replace("continued", "more")
    changed = translated.replace("payload", "different")
    assert DocsStandardChecks.get_code_without_comments(original) == DocsStandardChecks.get_code_without_comments(
        translated
    )
    assert DocsStandardChecks.get_code_without_comments(original) != DocsStandardChecks.get_code_without_comments(
        changed
    )
    assert DocsStandardChecks.get_code_without_comments(
        '"doc"; value = "one"'
    ) != DocsStandardChecks.get_code_without_comments('"doc"; value = "two"')


@pytest.mark.parametrize("language", ["sql", "bash", "ini"])
def test_other_languages_are_not_parsed_as_python_comments(language):
    original = "value = 'https://example.test/#one'; -- suffix"
    changed = original.replace("one", "two")
    assert DocsStandardChecks.get_code_without_comments(
        original, language
    ) != DocsStandardChecks.get_code_without_comments(changed, language)


@pytest.mark.parametrize(
    "language,source",
    [
        ("bash", 'echo "# literal" word#fragment # explanation'),
        ("bash", "# explanation\ncat <<'EOF'\n# literal\nEOF\n"),
        ("bash", 'echo "first\n# literal\nlast" # explanation\n'),
        ("ini", "[section]\n; explanation\nvalue = https://example.test/#literal\n"),
    ],
)
def test_language_comments_can_be_translated_without_hiding_literals(language, source):
    translated = source.replace("explanation", "translated explanation")
    changed = translated.replace("literal", "different value")
    normalize = DocsStandardChecks.get_code_without_comments
    assert normalize(source, language) == normalize(translated, language)
    assert normalize(source, language) != normalize(changed, language)


def test_shell_interpreter_is_part_of_the_example():
    normalize = DocsStandardChecks.get_code_without_comments
    assert normalize("#!/bin/bash\necho one\n", "bash") != normalize("#!/bin/sh\necho one\n", "bash")

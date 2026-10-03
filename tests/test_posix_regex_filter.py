import os
import uuid
from decimal import Decimal

import pytest

from hare.contrib import test
from hare.contrib.test.helpers import hare_test_context
from hare.dialects.sqlite.functions.regex import SqlitePosixRegex
from hare.exceptions import OperationalError, UnSupportedError
from tests import testmodels
from tests.typed_row_models import TypedRow
from tests.utils.database_under_test import DatabaseUnderTest


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter(db):
    author = await testmodels.Author.objects.create(name="Johann Wolfgang von Goethe")
    assert set(
        await testmodels.Author.objects.filter(name__posix_regex="^Johann [a-zA-Z]+ von Goethe$").values_list(
            "name", flat=True
        )
    ) == {author.name}


@test.requires_features(supports_posix_regex=False)
@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["posix_regex", "iposix_regex"])
async def test_regex_filter_without_regex_support_raises_before_the_query_runs(db, lookup):
    with pytest.raises(UnSupportedError, match="features.supports_posix_regex"):
        await testmodels.Author.objects.filter(**{f"name__{lookup}": "^J"}).count()


@test.requires_features(dialect="postgresql", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_works_with_null_field_postgres(db):
    await testmodels.Tournament.objects.create(name="Test")
    print(testmodels.Tournament.objects.filter(desc__posix_regex="^test$").sql())
    assert (
        set(await testmodels.Tournament.objects.filter(desc__posix_regex="^test$").values_list("name", flat=True))
        == set()
    )


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_works_with_null_field_sqlite(db):
    await testmodels.Tournament.objects.create(name="Test")
    print(testmodels.Tournament.objects.filter(desc__posix_regex="^test$").sql())
    assert (
        set(await testmodels.Tournament.objects.filter(desc__posix_regex="^test$").values_list("name", flat=True))
        == set()
    )


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_matches_empty_string_sqlite(db):
    """sqlite's regexp() UDF used `if not expr or not item: return False`, treating a genuine
    empty-string column value exactly like NULL - always non-matching, before re.search() ever
    ran, even for a pattern like ".*" that's specifically meant to match an empty string."""
    author = await testmodels.Author.objects.create(name="")
    assert set(await testmodels.Author.objects.filter(name__posix_regex=".*").values_list("name", flat=True)) == {
        author.name
    }


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_case_insensitive_regex_filter_works_with_null_field_sqlite(db):
    """sqlite's iregexp() UDF (backing MATCH/iposix_regex) had no None-guard at all, unlike
    regexp() - crashed re.search() with a TypeError on a NULL column value instead of just not
    matching, where the case-sensitive posix_regex path (test_regex_filter_works_with_null_field_
    sqlite above) already worked correctly for the exact same NULL scenario."""
    await testmodels.Tournament.objects.create(name="Test")
    assert (
        set(await testmodels.Tournament.objects.filter(desc__iposix_regex="^test$").values_list("name", flat=True))
        == set()
    )


@test.requires_features(dialect="postgresql", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_case_insensitive_regex_filter_postgres(db):
    author = await testmodels.Author.objects.create(name="Johann Wolfgang von Goethe")
    assert set(
        await testmodels.Author.objects.filter(name__iposix_regex="^johann [a-zA-Z]+ Von goethe$").values_list(
            "name", flat=True
        )
    ) == {author.name}


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_case_insensitive_regex_filter_sqlite(db):
    author = await testmodels.Author.objects.create(name="Johann Wolfgang von Goethe")
    assert set(
        await testmodels.Author.objects.filter(name__iposix_regex="^johann [a-zA-Z]+ Von goethe$").values_list(
            "name", flat=True
        )
    ) == {author.name}


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_rejects_absurdly_long_pattern_sqlite(db):
    """A cheap sanity ceiling (MAX_REGEX_PATTERN_LENGTH) against typo/abuse-scale garbage
    patterns - NOT a fix for catastrophic backtracking (ReDoS) in general, see
    install_regexp_functions's own docstring for why a short pattern can still hang re.search()
    regardless of this limit."""
    await testmodels.Author.objects.create(name="Johann Wolfgang von Goethe")
    too_long_pattern = "a" * 1001

    with pytest.raises(OperationalError):
        await testmodels.Author.objects.filter(name__posix_regex=too_long_pattern).values_list("name", flat=True)


@test.requires_features(dialect="sqlite", supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_accepts_pattern_at_the_length_limit_sqlite(db):
    author = await testmodels.Author.objects.create(name="test")
    # "test" (4 chars) followed by 249 zero-width non-capturing groups (4 chars each) - a
    # trivially valid, cheap-to-evaluate pattern padded to exactly the 1000-char limit, still
    # matching the stored name (unlike name itself, a regex pattern's length is unrelated to
    # what it can match).
    at_limit_pattern = "test" + "(?:)" * 249
    assert len(at_limit_pattern) == 1000

    assert set(
        await testmodels.Author.objects.filter(name__posix_regex=at_limit_pattern).values_list("name", flat=True)
    ) == {author.name}


# ---------------------------------------------------------------------------
# POSIX named character classes ([:digit:], [:alpha:], [:space:], ...) - Postgres's ~/~* natively
# understand these; SQLite matches via Python's own re.search(), which treats "[:digit:]" as a
# literal (non-POSIX) character class and silently matches nothing instead of erroring. Both
# dialects must return the SAME rows for the SAME pattern.
# ---------------------------------------------------------------------------


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [".*", "^$", "x*", "^", "$"])
async def test_regex_filter_never_matches_null_field_for_any_pattern(db, pattern):
    """A NULL column value used to be coalesced to "" before matching - a pattern that matches an
    empty string (".*", "^$", "x*", ...) then wrongly matched the NULL row too. NULL must never
    match, regardless of the pattern."""
    await testmodels.Tournament.objects.create(name="Test")
    assert (
        set(await testmodels.Tournament.objects.filter(desc__posix_regex=pattern).values_list("name", flat=True))
        == set()
    )
    assert (
        set(await testmodels.Tournament.objects.filter(desc__iposix_regex=pattern).values_list("name", flat=True))
        == set()
    )


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_regex_filter_exclude_null_field_keeps_null_row_django_semantics(db):
    """exclude()/~ on a direct (no relation crossed) nullable column now matches Django's own
    documented exclude() semantics: a row stays unless the excluded condition is definitely TRUE
    - a NULL `desc` makes `desc__posix_regex`/`desc__iposix_regex`/`desc__contains` UNKNOWN, not
    TRUE, so the row must stay, the same way it already stays for exclude() across a relation
    with no related row at all (Q._negate_across_joins). This used to assert the opposite (the
    NULL row silently dropped from exclude() too, same as it's dropped from the equivalent
    filter()) - see Q._finalize_modifier()/QueryModifier.__invert__() for the fix."""
    tournament = await testmodels.Tournament.objects.create(name="Test")

    assert await testmodels.Tournament.objects.exclude(desc__posix_regex="^a").values_list("name", flat=True) == [
        "Test"
    ]
    assert await testmodels.Tournament.objects.exclude(desc__iposix_regex="^a").values_list("name", flat=True) == [
        "Test"
    ]
    assert await testmodels.Tournament.objects.exclude(desc__contains="a").values_list("name", flat=True) == ["Test"]

    tournament.desc = "banana"
    await tournament.save()
    assert await testmodels.Tournament.objects.exclude(desc__posix_regex="^a").values_list("name", flat=True) == [
        "Test"
    ]

    tournament.desc = "apple"
    await tournament.save()
    assert await testmodels.Tournament.objects.exclude(desc__posix_regex="^a").values_list("name", flat=True) == []


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_digit_character_class(db):
    await testmodels.Author.objects.create(name="hello123")
    await testmodels.Author.objects.create(name="HELLO123")
    await testmodels.Author.objects.create(name="no-digits-here")

    assert set(
        await testmodels.Author.objects.filter(name__posix_regex="[[:digit:]]+").values_list("name", flat=True)
    ) == {
        "hello123",
        "HELLO123",
    }


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_alpha_character_class(db):
    await testmodels.Author.objects.create(name="hello")
    await testmodels.Author.objects.create(name="hello123")
    await testmodels.Author.objects.create(name="12345")

    assert set(
        await testmodels.Author.objects.filter(name__posix_regex="^[[:alpha:]]+$").values_list("name", flat=True)
    ) == {"hello"}


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_space_character_class(db):
    await testmodels.Author.objects.create(name="has a space")
    await testmodels.Author.objects.create(name="nospace")

    assert set(
        await testmodels.Author.objects.filter(name__posix_regex="[[:space:]]").values_list("name", flat=True)
    ) == {"has a space"}


async def get_author_names(**filter_kwargs) -> set[str]:
    return set(await testmodels.Author.objects.filter(**filter_kwargs).values_list("name", flat=True))


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_dot_matches_newline(db):
    """`.` matches a newline, as in a Postgres ARE - SQLite's `re` needed DOTALL."""
    await testmodels.Author.objects.create(name="x\ny")
    await testmodels.Author.objects.create(name="xy")

    assert await get_author_names(name__posix_regex="x.y") == {"x\ny"}


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_dollar_matches_only_text_end(db):
    """`$` matches only the end of the text, not the position before a trailing newline."""
    await testmodels.Author.objects.create(name="a\n")
    await testmodels.Author.objects.create(name="b")

    assert await get_author_names(name__posix_regex="^[ab]$") == {"b"}


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_posix_regex_character_classes_cover_unicode(db):
    for name in ("Привет", "İx", "ascii", "ÉTÉ", "ß", "12"):
        await testmodels.Author.objects.create(name=name)

    assert await get_author_names(name__posix_regex="[[:upper:]]") == {"Привет", "İx", "ÉTÉ"}
    assert await get_author_names(name__posix_regex="^[[:lower:]]+$") == {"ascii", "ß"}
    assert await get_author_names(name__posix_regex="^[[:alpha:]]+$") == {"Привет", "İx", "ascii", "ÉTÉ", "ß"}
    assert await get_author_names(name__posix_regex="^[[:alnum:]]+$") == {"Привет", "İx", "ascii", "ÉTÉ", "ß", "12"}


@test.requires_features(supports_posix_regex=True)
@pytest.mark.asyncio
async def test_case_insensitive_regex_uses_simple_case_variants(db):
    """A pattern character matches itself and its simple upper/lowercase forms only: `i` doesn't
    match the Turkish dotted `İ`, but `İ` matches `i`."""
    for name in ("İx", "ix", "Ix", "привет", "ПРИВЕТ"):
        await testmodels.Author.objects.create(name=name)

    assert await get_author_names(name__iposix_regex="^i") == {"ix", "Ix"}
    assert await get_author_names(name__iposix_regex="^İ") == {"İx", "ix"}
    assert await get_author_names(name__iposix_regex="^[а-я]+$") == {"привет", "ПРИВЕТ"}
    assert await get_author_names(name__iposix_regex="^[^I]x") == {"İx"}
    # [[:upper:]] and [[:lower:]] widen to [[:alpha:]] in a case-insensitive match.
    assert await get_author_names(name__iposix_regex="^[[:upper:]]+$") == {"İx", "ix", "Ix", "привет", "ПРИВЕТ"}


def test_sqlite_regex_translation_matches_postgres_semantics():
    """The SQLite pattern translation, without a database - the default SQLite test database
    doesn't install the regex functions."""
    assert SqlitePosixRegex.search("x.y", "x\ny", case_insensitive=False) is True
    assert SqlitePosixRegex.search("^a$", "a\n", case_insensitive=False) is False
    assert SqlitePosixRegex.search("[[:upper:]]", "Привет", case_insensitive=False) is True
    assert SqlitePosixRegex.search("[[:lower:]]", "Привет", case_insensitive=False) is True
    assert SqlitePosixRegex.search("^[[:digit:]]$", "٣", case_insensitive=False) is False
    assert SqlitePosixRegex.search("[[:space:]]", "\u00a0", case_insensitive=False) is False
    assert SqlitePosixRegex.search("[[:punct:]]", "«", case_insensitive=False) is True
    assert SqlitePosixRegex.search("^i", "İx", case_insensitive=True) is False
    assert SqlitePosixRegex.search("^İ", "ix", case_insensitive=True) is True
    assert SqlitePosixRegex.search("STRASSE", "straße", case_insensitive=True) is False
    assert SqlitePosixRegex.search("[a-c]", "B", case_insensitive=True) is True
    assert SqlitePosixRegex.search("[^a]", "A", case_insensitive=True) is False
    assert SqlitePosixRegex.search("(ab)+$", "xABab", case_insensitive=True) is True
    assert SqlitePosixRegex.search("^x", None, case_insensitive=False) is None


@pytest.mark.asyncio
async def test_regex_reads_a_number_as_postgres_writes_it():
    """A float/decimal column's text for a regex is Postgres's own, as for ``__contains`` -
    SQLite would write ``2.0`` for a float Postgres writes as ``2``."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    db_url = db_url.format(uuid.uuid4().hex) if "{}" in db_url else db_url
    if DatabaseUnderTest.is_file_database(db_url):
        db_url += ("&" if "?" in db_url else "?") + "install_regexp_functions=True"
    async with hare_test_context(["tests.typed_row_models"], db_url=db_url) as context:
        if not context.db().features.supports_posix_regex:
            pytest.skip("The database has no POSIX regular expressions")
        await TypedRow.objects.create(id=1, ratio=2.0, amount=Decimal("1.20"))
        await TypedRow.objects.create(id=2, ratio=1e20, amount=Decimal("100"))
        await TypedRow.objects.create(id=3, ratio=0.1 + 0.2)

        async def matching(**lookups):
            return sorted(await TypedRow.objects.filter(**lookups).values_list("id", flat=True))

        assert await matching(ratio__posix_regex="^2$") == [1]
        assert await matching(ratio__posix_regex=r"^0\.30000000000000004$") == [3]
        assert await matching(ratio__iposix_regex=r"E\+20") == [2]
        assert await matching(amount__posix_regex=r"^1\.20$") == [1]
        assert await matching(amount__posix_regex=r"^100\.00$") == [2]

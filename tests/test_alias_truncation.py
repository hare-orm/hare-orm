import pytest

from hare.dialects.constants import IDENTIFIER_LENGTH_LIMIT
from hare.dialects.identifiers import Identifiers
from tests.testmodels import (
    AliasSourceModelWithAVeryLongClassNameForTruncation,
    AliasTargetModelWithAVeryLongClassNameForTruncation,
)

# ============================================================================
# Unit tests for LookupPaths._safe_alias
# ============================================================================


def test_safe_alias_leaves_short_names_untouched():
    assert Identifiers.get_within_limit("events") == "events"


def test_safe_alias_leaves_exactly_max_bytes_untouched():
    name = "a" * IDENTIFIER_LENGTH_LIMIT
    assert Identifiers.get_within_limit(name) == name


def test_safe_alias_truncates_names_over_max_bytes():
    name = "a" * (IDENTIFIER_LENGTH_LIMIT + 1)
    result = Identifiers.get_within_limit(name)
    assert len(result.encode()) <= IDENTIFIER_LENGTH_LIMIT
    assert result != name


def test_safe_alias_avoids_collision_between_names_sharing_a_prefix():
    """Two different long names that share the same first 63 bytes must not produce the same
    alias - this is the exact DuplicateAliasError scenario found in backend's RawSQL workaround."""
    prefix = "a" * (IDENTIFIER_LENGTH_LIMIT + 5)
    name_1 = f"{prefix}_one"
    name_2 = f"{prefix}_two"

    alias_1 = Identifiers.get_within_limit(name_1)
    alias_2 = Identifiers.get_within_limit(name_2)

    assert alias_1 != alias_2
    assert len(alias_1.encode()) <= IDENTIFIER_LENGTH_LIMIT
    assert len(alias_2.encode()) <= IDENTIFIER_LENGTH_LIMIT


def test_safe_alias_is_deterministic():
    name = "b" * (IDENTIFIER_LENGTH_LIMIT + 10)
    assert Identifiers.get_within_limit(name) == Identifiers.get_within_limit(name)


def test_safe_alias_handles_multibyte_truncation_boundary():
    """A long name whose byte-length truncation point falls inside a multi-byte UTF-8 character
    must not raise - not a realistic table/field name, but a table name could plausibly contain
    non-ASCII characters (e.g. a schema-qualified name from a non-English project)."""
    name = "café" * 20
    result = Identifiers.get_within_limit(name)
    assert len(result.encode()) <= IDENTIFIER_LENGTH_LIMIT


# ============================================================================
# End-to-end: a genuinely long relation-lookup path still resolves correctly
# ============================================================================


@pytest.mark.asyncio
async def test_long_relation_name_join_resolves_correctly(db):
    source = await AliasSourceModelWithAVeryLongClassNameForTruncation.objects.create(name="Source")
    await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.create(
        name="Target A", source_with_a_very_long_relation_field_name_for_truncation=source
    )
    await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.create(
        name="Target B", source_with_a_very_long_relation_field_name_for_truncation=source
    )

    matches = await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.filter(
        source_with_a_very_long_relation_field_name_for_truncation__name="Source"
    ).order_by("name")
    assert [m.name for m in matches] == ["Target A", "Target B"]

    reverse = await AliasSourceModelWithAVeryLongClassNameForTruncation.objects.filter(
        targets__name="Target A"
    ).first()
    assert reverse.name == "Source"


@pytest.mark.asyncio
async def test_long_relation_name_select_related_resolves_correctly(db):
    source = await AliasSourceModelWithAVeryLongClassNameForTruncation.objects.create(name="Source")
    await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.create(
        name="Target", source_with_a_very_long_relation_field_name_for_truncation=source
    )

    target = (
        await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.filter(name="Target")
        .select_related("source_with_a_very_long_relation_field_name_for_truncation")
        .first()
    )
    assert target.source_with_a_very_long_relation_field_name_for_truncation.name == "Source"


@pytest.mark.asyncio
async def test_long_relation_name_only_resolves_correctly(db):
    """.only("relation__field") for a genuinely long relation name - the one existing branch of
    safe_select_label added for _get_only() this session had no direct regression test with a
    name actually long enough to hit truncation."""
    source = await AliasSourceModelWithAVeryLongClassNameForTruncation.objects.create(name="Source")
    await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.create(
        name="Target", source_with_a_very_long_relation_field_name_for_truncation=source
    )

    target = (
        await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.filter(name="Target")
        .only("name", "source_with_a_very_long_relation_field_name_for_truncation__name")
        .first()
    )
    assert target.source_with_a_very_long_relation_field_name_for_truncation.name == "Source"


@pytest.mark.asyncio
async def test_long_relation_name_defer_resolves_correctly(db):
    """.defer() only takes direct (non-nested) field names (see its own docstring) - this
    exercises the long-relation-name JOIN alias path via .select_related() alongside a .defer()
    on the base model's own column, not a nested defer on the relation itself."""
    source = await AliasSourceModelWithAVeryLongClassNameForTruncation.objects.create(name="Source")
    await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.create(
        name="Target", source_with_a_very_long_relation_field_name_for_truncation=source
    )

    target = (
        await AliasTargetModelWithAVeryLongClassNameForTruncation.objects.filter(name="Target")
        .select_related("source_with_a_very_long_relation_field_name_for_truncation")
        .defer("name")
        .first()
    )
    assert "name" not in target.__dict__
    assert target.source_with_a_very_long_relation_field_name_for_truncation.name == "Source"

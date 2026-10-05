import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.ddl import RawSQLTerm
from hare.exceptions import OperationalError, QueryError, UnSupportedError
from hare.query.expressions import Q
from tests.testmodels import Tournament

PARTIAL_INDEX_NAME = "tournament_name_active_partial_uniq"


async def _add_partial_unique_index() -> None:
    """Tournament has no unique index on name by default - adds a real PARTIAL one via raw DDL
    (transactional on Postgres, rolled back by the `db` fixture's own rollback) so that
    ON CONFLICT (name) needs its own WHERE clause repeated to match it - the same "real DDL, not
    a hare-generated schema" approach test_bulk_on_conflict_constraint.py uses for a named
    constraint."""
    conn = Connections.get("models")
    await conn.execute_script(
        f'CREATE UNIQUE INDEX {PARTIAL_INDEX_NAME} ON tournament ("name") WHERE "desc" IS NOT NULL'
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_on_conflict_without_conflict_where_fails_against_partial_index(db):
    """Reproduces the reported gap: a plain on_conflict=["name"] target has no way to match a
    partial unique index - Postgres requires the ON CONFLICT clause to repeat the index's own
    WHERE predicate, so this fails instead of silently doing the wrong thing."""
    await _add_partial_unique_index()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    with pytest.raises(OperationalError, match="no unique or exclusion constraint"):
        await Tournament.objects.bulk_create(
            [Tournament(id=2, name="A", desc="updated")],
            update_fields=["desc"],
            on_conflict=["name"],
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_do_update_via_conflict_where(db):
    await _add_partial_unique_index()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="updated")],
        update_fields=["desc"],
        on_conflict=["name"],
        conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
    )

    row = await Tournament.objects.get(name="A")
    assert row.id == 1
    assert row.desc == "updated"
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_do_nothing_via_conflict_where(db):
    await _add_partial_unique_index()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="should not apply")],
        ignore_conflicts=True,
        on_conflict=["name"],
        conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
    )

    row = await Tournament.objects.get(name="A")
    assert row.id == 1
    assert row.desc == "original"
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_conflict_where_only_matches_rows_covered_by_the_partial_index(db):
    """A row outside the partial index's predicate (desc IS NULL here) never participates in the
    uniqueness check - inserting a new row with the same name but a non-null desc must succeed
    instead of raising, proving the WHERE clause really scopes the conflict target rather than
    being ignored or silently targeting a full index."""
    await _add_partial_unique_index()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc=None)])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="second")],
        update_fields=["desc"],
        on_conflict=["name"],
        conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
    )

    assert await Tournament.objects.all().count() == 2
    rows = {(row.id, row.desc) for row in await Tournament.objects.all()}
    assert rows == {(1, None), (2, "second")}


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_conflict_where_requires_on_conflict(db):
    with pytest.raises(ValueError, match="requires on_conflict"):
        Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            ignore_conflicts=True,
            conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_conflict_where_and_on_conflict_constraint_are_mutually_exclusive(db):
    with pytest.raises(ValueError, match="mutually exclusive"):
        Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            update_fields=["desc"],
            on_conflict_constraint="some_constraint",
            conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
        )


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_conflict_where_unsupported_on_sqlite(db):
    with pytest.raises(UnSupportedError, match="not supported by the sqlite dialect"):
        await Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            update_fields=["desc"],
            on_conflict=["name"],
            conflict_where=RawSQLTerm('"desc" IS NOT NULL'),
        )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_do_update_via_conflict_where_q(db):
    """A Q condition is rendered against the model's own columns, matching the partial index's
    predicate as RawSQLTerm does."""
    await _add_partial_unique_index()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="updated")],
        update_fields=["desc"],
        on_conflict=["name"],
        conflict_where=Q(desc__isnull=False),
    )

    row = await Tournament.objects.get(name="A")
    assert row.id == 1
    assert row.desc == "updated"


@pytest.mark.asyncio
async def test_conflict_where_refuses_plain_text(db):
    """Raw SQL is RawSQLTerm(...), as everywhere else hare takes SQL text."""
    with pytest.raises(QueryError, match="RawSQLTerm"):
        Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            update_fields=["desc"],
            on_conflict=["name"],
            conflict_where='"desc" IS NOT NULL',
        )

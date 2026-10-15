import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.exceptions import UnSupportedError
from tests.testmodels import CompositePkThing, Tournament

CONSTRAINT_NAME = "tournament_name_test_uniq"


async def _add_named_unique_constraint(field_name: str = "name") -> None:
    """Tournament.name has no unique constraint by default - adds a real, named one via raw DDL
    (DDL is transactional on Postgres, so the `db` fixture's own rollback removes it again).
    A plain hare-generated unique index wouldn't do: Postgres's `ON CONFLICT ON CONSTRAINT` only
    accepts an actual pg_constraint entry, not a bare unique index."""
    conn = Connections.get("models")
    await conn.execute_script(f'ALTER TABLE tournament ADD CONSTRAINT {CONSTRAINT_NAME} UNIQUE ("{field_name}")')


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_do_update_via_on_conflict_constraint(db):
    await _add_named_unique_constraint()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="updated")],
        update_fields=["desc"],
        on_conflict_constraint=CONSTRAINT_NAME,
    )

    row = await Tournament.objects.get(name="A")
    assert row.id == 1
    assert row.desc == "updated"
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_do_nothing_via_on_conflict_constraint(db):
    await _add_named_unique_constraint()
    await Tournament.objects.bulk_create([Tournament(id=1, name="A", desc="original")])

    await Tournament.objects.bulk_create(
        [Tournament(id=2, name="A", desc="should not apply")],
        ignore_conflicts=True,
        on_conflict_constraint=CONSTRAINT_NAME,
    )

    row = await Tournament.objects.get(name="A")
    assert row.id == 1
    assert row.desc == "original"
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_on_conflict_and_on_conflict_constraint_are_mutually_exclusive(db):
    with pytest.raises(ValueError, match="mutually exclusive"):
        Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            update_fields=["desc"],
            on_conflict=["name"],
            on_conflict_constraint=CONSTRAINT_NAME,
        )


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_on_conflict_constraint_unsupported_on_sqlite(db):
    with pytest.raises(UnSupportedError, match="not supported by the sqlite dialect"):
        await Tournament.objects.bulk_create(
            [Tournament(id=1, name="A")],
            update_fields=["desc"],
            on_conflict_constraint=CONSTRAINT_NAME,
        )


@pytest.mark.asyncio
async def test_on_conflict_constraint_pk_names_the_primary_key(db):
    """The constraint of a composite primary key has a generated name - on_conflict_constraint="pk"
    names it, as on_conflict of its fields; of a single key, of its field."""
    await CompositePkThing.objects.bulk_create([CompositePkThing(thing_id=1, revision=1, name="old")])
    await CompositePkThing.objects.bulk_create(
        [CompositePkThing(thing_id=1, revision=1, name="new"), CompositePkThing(thing_id=1, revision=2, name="b")],
        on_conflict_constraint="pk",
        update_fields=["name"],
    )
    assert await CompositePkThing.objects.order_by("revision").values_list("name", flat=True) == ["new", "b"]
    await Tournament.objects.bulk_create([Tournament(id=1, name="A")])
    await Tournament.objects.bulk_create(
        [Tournament(id=1, name="B")], on_conflict_constraint="pk", update_fields=["name"]
    )
    assert await Tournament.objects.values_list("name", flat=True) == ["B"]

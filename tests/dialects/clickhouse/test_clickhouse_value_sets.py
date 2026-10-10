"""Long ``__in`` lists on ClickHouse - bound as one parameter, sent by a read as an external table and written
as a list of literals by any other statement: the same rows as a short list, for single values and rows of
them, with None among them, in a filter and an exclusion, in a mutation and in the checks of a write."""

import uuid

import pytest

from hare.exceptions import IntegrityError
from tests.dialects.clickhouse.models import Player, Revision, Team


@pytest.mark.asyncio
async def test_a_long_list_reads_the_rows_a_short_one_would(clickhouse_db):
    await Player.objects.bulk_create([Player(id=number, name=f"p{number}") for number in range(1200)])
    wanted = list(range(0, 2400, 2))
    # The SQL names an external table, not the values.
    assert "hare_set" not in Player.objects.filter(id__in=wanted).sql()
    assert await Player.objects.filter(id__in=wanted).count() == 600
    assert await Player.objects.filter(id__in=wanted).values_list("id", flat=True) == list(range(0, 1200, 2))
    assert await Player.objects.exclude(id__in=wanted).count() == 600
    assert await Player.objects.filter(id__in=[*wanted, None]).count() == 600
    names = [f"p{number}" for number in range(700)]
    assert await Player.objects.filter(name__in=names).count() == 700


@pytest.mark.asyncio
async def test_a_long_list_of_keys_of_several_columns(clickhouse_db):
    await Revision.objects.bulk_create(
        [Revision(document_id=number % 7, number=number, title="t") for number in range(800)]
    )
    keys = [(number % 7, number) for number in range(0, 800, 2)]
    assert await Revision.objects.filter(pk__in=keys).count() == 400
    # The keys of a long batch of several columns are checked too.
    with pytest.raises(IntegrityError, match="primary key"):
        await Revision.objects.bulk_create(
            [Revision(document_id=9, number=number, title="t") for number in range(799)]
            + [Revision(document_id=0, number=0, title="again")]
        )
    assert await Revision.objects.count() == 800


@pytest.mark.asyncio
async def test_a_long_list_in_a_mutation_and_in_the_checks_of_a_write(clickhouse_db):
    teams = [Team(name=f"t{number}") for number in range(600)]
    await Team.objects.bulk_create(teams)
    ids = [team.id for team in teams]
    # A mutation runs no external table - the list is written as literals.
    assert await Team.objects.filter(id__in=ids[:550]).update(name="renamed") == 550
    assert await Team.objects.filter(name="renamed").count() == 550
    # The keys of a long batch are checked by one read, a stored one found.
    repeated = [Team(id=uuid.uuid4(), name="new") for _ in range(599)] + [Team(id=ids[0], name="again")]
    with pytest.raises(IntegrityError, match="primary key"):
        await Team.objects.bulk_create(repeated)
    assert await Team.objects.count() == 600

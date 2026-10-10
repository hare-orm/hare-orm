"""bulk_create() on ClickHouse loads its rows in one binary insert - no SQL text is built from the
values - and the rows hold what the same objects written by create() hold."""

import datetime
import decimal
import enum
import subprocess
import sys
import textwrap
import uuid

import pytest

pytest.importorskip("clickhouse_connect")

from hare.contrib.test import capture_queries  # noqa: E402
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT  # noqa: E402
from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (  # noqa: E402
    ClickhouseConnectClient,
)
from hare.exceptions import ValidationError  # noqa: E402
from tests.dialects.clickhouse.models import Player, Team  # noqa: E402

PLAYER_FIELDS = ("name", "team_id", "score", "rating", "joined", "born", "active", "notes", "data")
PLAYER_INSERT = (
    "INSERT INTO player (id, name, score, rating, joined, born, active, notes, data, team_id) FORMAT Native"
)


class Level(enum.Enum):
    HIGH = "high"


def make_players(first_id: int, team: Team) -> list[Player]:
    return [
        Player(
            id=first_id,
            name="Ann O'Neil \\ привет",
            team=team,
            score=decimal.Decimal("12.50"),
            rating=4.5,
            joined=datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC),
            born=datetime.date(1960, 5, 6),
            notes="a\nb\t'c'",
            data={"position": "goal", "numbers": [1, 7], "captain": True},
        ),
        Player(
            id=first_id + 1,
            name="Bob",
            joined=datetime.datetime(2024, 1, 2, 3, 4, 5, 123456),
            active=False,
            data={"items": [1, "two", None]},
        ),
        Player(
            id=first_id + 2,
            name="Cid",
            joined=datetime.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=datetime.timezone(datetime.timedelta(hours=3))),
            rating=float("inf"),
            data={"note": "text"},
        ),
        Player(id=first_id + 3, name=""),
    ]


@pytest.mark.asyncio
async def test_loaded_rows_hold_what_created_rows_hold(clickhouse_db):
    team = await Team.objects.create(name="blue")
    for player in make_players(1, team):
        await player.save()
    await Player.objects.bulk_create(make_players(101, team))
    created = await Player.objects.filter(id__lt=100).values_list(*PLAYER_FIELDS)
    loaded = await Player.objects.filter(id__gt=100).values_list(*PLAYER_FIELDS)
    assert len(created) == 4
    assert loaded == created


@pytest.mark.asyncio
async def test_the_rows_load_in_one_binary_insert(clickhouse_db):
    async with capture_queries() as counter:
        await Player.objects.bulk_create([Player(id=number, name=f"p{number}") for number in range(5)])
    # The keys are checked first - ClickHouse keeps no uniqueness.
    key_check, insert = counter.queries
    assert key_check.startswith('SELECT "id" "0" FROM "player" WHERE "id" IN') and insert == PLAYER_INSERT
    assert await Player.objects.count() == 5


@pytest.mark.asyncio
async def test_each_batch_loads_on_its_own(clickhouse_db):
    async with capture_queries() as counter:
        await Player.objects.bulk_create(
            [Player(id=number, name=f"p{number}") for number in range(5)], batch_size=2, use_copy=True
        )
    assert counter.queries[0].startswith('SELECT "id" "0" FROM "player"')
    assert counter.queries[1:] == [PLAYER_INSERT] * 3
    assert await Player.objects.values_list("id", flat=True) == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_a_key_made_by_its_default_is_loaded(clickhouse_db):
    teams = [Team(name="blue"), Team(name="red", description="it's \\ red")]
    await Team.objects.bulk_create(teams)
    assert await Team.objects.order_by("name").values_list("id", "name", "description") == [
        (teams[0].id, "blue", None),
        (teams[1].id, "red", "it's \\ red"),
    ]


@pytest.mark.asyncio
async def test_rows_read_back_are_read_by_their_keys(clickhouse_db):
    # Written without RETURNING; the values the database gave the rows are read by their keys after.
    players = [Player(id=1, name="x"), Player(id=2, name="y")]
    await Player.objects.bulk_create(players, returning=True)
    assert [player._saved_in_db for player in players] == [True, True]
    assert await Player.objects.count() == 2


def test_plain_columns_are_inserted_as_they_are():
    key = uuid.UUID("6f1e5a0e-0000-4000-8000-000000000001")
    records = [
        (1, 1.5, True, decimal.Decimal("2.50"), key, datetime.date(1960, 5, 6), "a", None),
        (2, None, False, decimal.Decimal("3"), None, None, "", "b"),
    ]
    assert ClickhouseConnectClient._get_inserted_columns(records) == [
        (1, 2),
        (1.5, None),
        (True, False),
        (decimal.Decimal("2.50"), decimal.Decimal("3")),
        (key, None),
        (datetime.date(1960, 5, 6), None),
        ("a", ""),
        (None, "b"),
    ]


def test_a_uuid_is_written_as_the_uuid_itself():
    key = uuid.UUID("6f1e5a0e-0000-4000-8000-000000000001")
    write = CLICKHOUSE_DIALECT.types.get_db_writer(Team._meta.fields_map["id"])
    assert write(key, Team) is key
    assert write(str(key), Team) == key
    assert type(write(str(key), Team)) is uuid.UUID
    assert write(None, Team) is None
    with pytest.raises(ValidationError):
        write("not a uuid", Team)
    assert CLICKHOUSE_DIALECT.literals.get_literal_sql(key) == "toUUID('6f1e5a0e-0000-4000-8000-000000000001')"


def test_a_naive_moment_is_inserted_as_its_utc_wall_clock():
    naive = datetime.datetime(2024, 1, 2, 3, 4, 5, 6)
    aware = datetime.datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
    assert ClickhouseConnectClient._get_inserted_columns([(naive,), (aware,), (None,)]) == [
        [naive.replace(tzinfo=datetime.UTC), aware, None]
    ]


@pytest.mark.parametrize(
    ("value", "inserted"),
    [
        (Level.HIGH, "high"),
        (datetime.time(3, 4, 5), "03:04:05"),
        (datetime.timedelta(hours=1, seconds=2), "01:00:02"),
        ({"a": [1, None]}, '{"a":[1,null]}'),
        ([Level.HIGH, 2], ["high", 2]),
        (bytearray(b"\x00\xff"), b"\x00\xff"),
    ],
)
def test_a_value_is_inserted_as_its_literal_writes_it(value, inserted):
    assert ClickhouseConnectClient._get_inserted_columns([(value,), (None,)]) == [[inserted, None]]


def test_text_beside_bytes_is_inserted_as_bytes():
    assert ClickhouseConnectClient._get_inserted_columns([(b"\x01",), ("текст",), (None,)]) == [
        [b"\x01", "текст".encode(), None]
    ]


@pytest.mark.parametrize("records", [[("a\x00b",)], [("a\x00b",), (None,)], [(["a\x00b"],)]])
def test_a_null_byte_is_refused(records):
    with pytest.raises(ValidationError, match="null byte"):
        ClickhouseConnectClient._get_inserted_columns(records)


def run_in_new_process(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scheme", ["clickhouse+clickhouse-connect", "clickhouse+clickhouse-driver"])
def test_the_driver_is_found_without_loading_another_database_drivers(scheme):
    """In a new process, as the drivers this one has loaded stay loaded."""
    run_in_new_process(
        f"""
        import sys

        from hare.dialects.dialect_registry import DialectRegistry

        driver = DialectRegistry.get_driver_for_url_scheme("{scheme}")
        assert driver.dialect.name == "clickhouse"
        assert driver.name == "{scheme}"
        loaded = [name for name in sys.modules if name.startswith(("hare.dialects.postgresql.drivers", "asyncpg"))]
        assert not loaded, loaded
        assert "hare.dialects.sqlite.drivers.aiosqlite.aiosqlite_driver" not in sys.modules
        """
    )


def test_a_driver_registered_first_takes_no_scheme_of_hares_own_driver():
    run_in_new_process(
        """
        from hare.dialects.clickhouse.drivers.clickhouse_connect import clickhouse_connect_driver
        from hare.dialects.dialect_registry import DialectRegistry
        from hare.exceptions import ConfigurationError


        class TakingDriver(clickhouse_connect_driver.ClickhouseConnectDriver):
            name = "taking"
            url_schemes = ("taking", "postgresql+asyncpg")


        try:
            DialectRegistry.register_driver(TakingDriver())
        except ConfigurationError as error:
            assert "already taken" in str(error), str(error)
        else:
            raise AssertionError("the scheme of hare's own driver was taken")
        assert DialectRegistry.get_driver("postgresql+asyncpg").name == "postgresql+asyncpg"
        """
    )

"""What ClickHouse can't do is refused before any SQL is sent: transactions, generated keys, row
locks, correlated subqueries other than an EXISTS by equal columns, RETURNING."""

import pytest

from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.ddl import Index, PartialIndex, RawSQLTerm, UniqueConstraint
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import OuterReference, Subquery
from hare.query.functions import Lower
from hare.transactions.transactions import Transactions
from tests.dialects.clickhouse.models import Player, Team
from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_a_transaction_is_refused(clickhouse_db):
    with pytest.raises(UnSupportedError, match="no transactions"):
        async with Transactions.atomic():
            pass


@pytest.mark.asyncio
async def test_a_model_with_a_generated_key_is_refused(clickhouse_db, monkeypatch):
    # Tournament's key is generated: bound to a ClickHouse connection whose server hands out no keys of
    # a series (no generateSerialID, no Keeper), it is refused.
    connection = Player._meta.connection
    features = connection.features.replace(supports_generated_keys=False, takes_keys_before_insert=False)
    monkeypatch.setattr(connection, "features", features)
    monkeypatch.setattr(Tournament._meta, "default_connection", Player._meta.default_connection)
    with pytest.raises(ConfigurationError, match="generates no keys"):
        ModelConnectionChecks.check_model_writes(Tournament)


@pytest.mark.asyncio
async def test_a_row_lock_is_refused(clickhouse_db):
    with pytest.raises(UnSupportedError, match="select_for_update"):
        await Player.objects.select_for_update()


@pytest.mark.asyncio
async def test_a_correlated_scalar_subquery_is_refused(clickhouse_db):
    first_player = Subquery(Player.objects.filter(team=OuterReference("pk")).order_by("name").values("name")[:1])
    # A server without correlated subqueries refuses each; one with them refuses a subquery ordering its
    # own rows.
    message = (
        "ordering or limiting"
        if Player._meta.connection.features.supports_correlated_subqueries
        else "no correlated subqueries"
    )
    with pytest.raises(UnSupportedError, match=message):
        await Team.objects.annotate(first_player=first_player)
    # Read by a condition or the ordering, not selected, it was sent and failed on the server.
    with pytest.raises(UnSupportedError, match=message):
        await Team.objects.annotate(first_player=first_player).filter(first_player__isnull=False).values_list("id")
    with pytest.raises(UnSupportedError, match=message):
        await Team.objects.annotate(first_player=first_player).order_by("first_player").values_list("id")


@pytest.mark.asyncio
async def test_returning_old_values_is_refused(clickhouse_db):
    # The rows of a RETURNING are read by their keys after the write - the values before it aren't kept.
    with pytest.raises(UnSupportedError, match="RETURNING"):
        await Player.objects.filter(id=1).update(name="x").returning("name", old=["name"])


@pytest.mark.asyncio
async def test_a_column_type_the_server_lacks_is_refused(clickhouse_db, monkeypatch):
    connection = Player._meta.connection
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    monkeypatch.setattr(connection, "missing_data_types", frozenset({"LineString", "Dynamic"}))
    for column_type in ("LineString", "Array(Nullable(Dynamic))"):
        with pytest.raises(UnSupportedError, match="lacks"):
            editor.column_definitions.get_altered_column_type(column_type, nullable=False)
    assert editor.column_definitions.get_altered_column_type("Point", nullable=False) == "Point"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "declarations"),
    [
        ("indexes", [Index(Lower("name"), unique=True, name="player_lower_unique")]),
        ("indexes", [PartialIndex(fields=["name"], unique=True, condition=RawSQLTerm("rank > 0"), name="ranked")]),
        ("constraints", [UniqueConstraint(fields=("name",), condition=RawSQLTerm("rank > 0"), name="ranked")]),
    ],
)
async def test_a_uniqueness_hare_cant_check_is_refused(clickhouse_db, monkeypatch, option, declarations):
    # ClickHouse keeps nothing unique - hare checks what a model declares, but not the values of an
    # expression or a condition only the database reads.
    monkeypatch.setattr(Player._meta, option, declarations)
    with pytest.raises(ConfigurationError, match="keeps nothing unique"):
        ModelConnectionChecks.check_model_writes(Player)


@pytest.mark.parametrize(
    "index",
    [
        Index(Lower("name").desc(), name="player_lower_desc"),
        Index(Lower("name").asc(), name="player_lower_asc"),
        Index(fields=["-name"], name="player_name_desc"),
    ],
)
def test_an_index_key_direction_is_refused(index):
    index.get_expressions(Player)
    with pytest.raises(UnSupportedError, match="keep no key order"):
        index.raise_if_unsupported(CLICKHOUSE_DIALECT)

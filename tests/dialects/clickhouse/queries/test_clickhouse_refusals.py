"""What ClickHouse can't do is refused before any SQL is sent: transactions, generated keys, row
locks, correlated subqueries other than an EXISTS by equal columns, RETURNING; a model declaring a
uniqueness, a relation of ``db_constraint=True`` or ``Meta.optimistic_lock_field`` when it is bound."""

import pytest

from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.ddl import Index, PartialIndex, RawSQLTerm, UniqueConstraint
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError
from hare.query.expressions import OuterReference, Subquery
from hare.query.functions import Lower
from hare.transactions.transactions import Transactions
from tests.dialects.clickhouse.models import Player, Team
from tests.shared_models.testmodels import Tournament


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
    with pytest.raises(UnSupportedError, match="RETURNING"):
        await Player.objects.filter(id=1).update(name="x").returning("name", old=["name"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "declarations"),
    [
        ("indexes", [Index(fields=["name"], unique=True, name="player_name_unique")]),
        ("indexes", [Index(Lower("name"), unique=True, name="player_lower_unique")]),
        ("indexes", [PartialIndex(fields=["name"], unique=True, condition=RawSQLTerm("rank > 0"), name="ranked")]),
        ("constraints", [UniqueConstraint(fields=("name",), name="player_name")]),
        ("constraints", [UniqueConstraint(fields=("name",), condition=RawSQLTerm("rank > 0"), name="ranked")]),
    ],
)
async def test_a_uniqueness_is_refused(clickhouse_db, monkeypatch, option, declarations):
    # ClickHouse keeps nothing unique.
    monkeypatch.setattr(Player._meta, option, declarations)
    with pytest.raises(ConfigurationError, match="keeps nothing unique"):
        ModelConnectionChecks.check_model_writes(Player)


@pytest.mark.asyncio
async def test_a_unique_field_is_refused(clickhouse_db, monkeypatch):
    monkeypatch.setattr(Player._meta.fields_map["name"], "unique", True)
    with pytest.raises(ConfigurationError, match=r"name \(unique=True\).*keeps nothing unique"):
        ModelConnectionChecks.check_model_writes(Player)


@pytest.mark.asyncio
async def test_a_unique_relation_is_named_once(clickhouse_db, monkeypatch):
    """A one-to-one relation was named twice - as itself and as its key field."""
    for field_name in ("team", "team_id"):
        monkeypatch.setattr(Player._meta.fields_map[field_name], "unique", True)
    with pytest.raises(ConfigurationError, match=r"declares team \(unique=True\), but") as refusal:
        ModelConnectionChecks.check_model_writes(Player)
    assert "team_id" not in str(refusal.value)


@pytest.mark.asyncio
async def test_a_relation_of_a_foreign_key_constraint_is_refused(clickhouse_db, monkeypatch):
    # ClickHouse keeps no foreign key - a relation is declared db_constraint=False.
    monkeypatch.setattr(Player._meta.fields_map["team"], "db_constraint", True)
    with pytest.raises(ConfigurationError, match="team with db_constraint=True.*declare db_constraint=False"):
        ModelConnectionChecks.check_model_writes(Player)


@pytest.mark.asyncio
async def test_an_optimistic_lock_field_is_refused(clickhouse_db, monkeypatch):
    # A mutation reports no count of its rows - a stale version would go unnoticed.
    monkeypatch.setattr(Player._meta, "optimistic_lock_field", "score")
    with pytest.raises(ConfigurationError, match="optimistic_lock_field"):
        ModelConnectionChecks.check_model_writes(Player)


@pytest.mark.database_independent
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


@pytest.mark.asyncio
async def test_in_bulk_takes_only_the_primary_key(clickhouse_db):
    # ClickHouse keeps nothing unique - a field of the model is no key of one row.
    with pytest.raises(QueryError, match=r"in_bulk\(\)'s field_name must be a unique field, but 'name' isn't"):
        await Player.objects.in_bulk(["a"], field_name="name")
    assert await Player.objects.in_bulk([-1]) == {}


@pytest.mark.asyncio
async def test_a_many_to_many_relation_of_foreign_key_constraints_is_refused(clickhouse_db, monkeypatch):
    # Its automatic through table would hold a foreign key to each side, which ClickHouse keeps none of.
    monkeypatch.setattr(Player._meta.fields_map["skills"], "db_constraint", True)
    with pytest.raises(ConfigurationError, match="skills with db_constraint=True.*declare db_constraint=False"):
        ModelConnectionChecks.check_model_writes(Player)

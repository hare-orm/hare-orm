"""A connection through a pooler in transaction pooling (``transaction_pooling``): its options are
checked, a session of its own (LISTEN, a session lock timeout) goes to the server itself
(``direct_host``/``direct_port``), and the search path it sets is checked once to reach the server."""

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.client.constants import POSTGRESQL_CURRENT_SEARCH_PATH_SQL
from hare.dialects.postgresql.client.transaction_pooler import TransactionPooler
from hare.exceptions import ConfigurationError
from tests.utils.constants import ASYNCPG_CLIENT_PATH, POSTGRES_CLIENT_PATHS, RUST_PG_CLIENT_PATH
from tests.utils.driver_classes import DriverClasses

POSTGRES_CLIENT_CLASSES = DriverClasses.get_parameters(*POSTGRES_CLIENT_PATHS)


def make_client(client_class, **credentials):
    return client_class(
        user="postgres",
        password="postgres",
        database="test",
        host="127.0.0.1",
        connection_alias="models",
        **credentials,
    )


# The check is the clients' shared __init__: every value on rust_pg's client class, the first value
# on asyncpg's.
INVALID_POOLER_OPTIONS = [
    ({"direct_host": "db.internal"}, "set transaction_pooling=true"),
    ({"direct_port": 5432}, "set transaction_pooling=true"),
    ({"transaction_pooling": True, "direct_port": 5432}, "direct_port needs direct_host"),
    ({"transaction_pooling": True, "direct_host": "db.internal", "direct_port": 0}, "direct_port must be between"),
    ({"transaction_pooling": "maybe"}, "transaction_pooling"),
]


@pytest.mark.parametrize(
    ("client_class", "credentials", "message"),
    [
        *[
            DriverClasses.get_parameter(RUST_PG_CLIENT_PATH, credentials, message)
            for (credentials, message) in INVALID_POOLER_OPTIONS
        ],
        DriverClasses.get_parameter(ASYNCPG_CLIENT_PATH, *INVALID_POOLER_OPTIONS[0]),
    ],
    indirect=["client_class"],
)
def test_pooler_options_are_checked(client_class, credentials, message):
    with pytest.raises(ConfigurationError, match=message):
        make_client(client_class, **credentials)


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES, indirect=True)
def test_a_session_of_its_own_goes_to_the_server_behind_the_pooler(client_class):
    pooled = make_client(
        client_class, port=6432, transaction_pooling=True, direct_host="db.internal", direct_port=5433
    )
    assert TransactionPooler.get_direct_settings(pooled) == {
        "host": "db.internal",
        "port": 5433,
        "schema": None,
        "transaction_pooling": False,
        "direct_host": None,
        "direct_port": None,
    }
    assert (
        TransactionPooler.get_direct_settings(
            make_client(client_class, port=6432, transaction_pooling=True, direct_host="db.internal")
        )["port"]
        == 6432
    )
    straight = make_client(client_class)
    assert TransactionPooler.get_direct_settings(straight) == {}
    assert TransactionPooler.get_direct_client(straight) is straight
    with pytest.raises(ConfigurationError, match="set direct_host"):
        TransactionPooler.get_direct_settings(make_client(client_class, transaction_pooling=True))


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES, indirect=True)
@pytest.mark.asyncio
async def test_listen_behind_the_pooler_needs_the_server_address(client_class):
    with pytest.raises(ConfigurationError, match="set direct_host"):
        await make_client(client_class, transaction_pooling=True).listen("channel", lambda *args: None)


@pytest.mark.database_independent
def test_search_path_schemas_are_compared_as_the_server_lists_them():
    assert TransactionPooler.get_search_path_schemas('"$user", public') == ("$user", "public")
    assert TransactionPooler.get_search_path_schemas("tenant_1,public") == ("tenant_1", "public")
    assert TransactionPooler.get_search_path_schemas(' "Mixed Case" , public ,') == ("Mixed Case", "public")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_search_path_behind_the_pooler_needs_a_server_reporting_it(db):
    client = Connections.get("models").create_independent_client()
    try:
        await client.execute_dicts("SELECT 1")
        client.server_version = (18, 0)
        await TransactionPooler.raise_if_search_path_unreported(client)
        client.server_version = (17, 6)
        with pytest.raises(ConfigurationError, match="PostgreSQL before 18 doesn't tell the pooler its search path"):
            await TransactionPooler.raise_if_search_path_unreported(client)
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_search_path_the_pooler_leaves_out_is_refused(db):
    client = Connections.get("models").create_independent_client()
    try:
        (row,) = await client.execute_dicts(POSTGRESQL_CURRENT_SEARCH_PATH_SQL)
        await TransactionPooler.check_search_path(client, row["search_path"])
        with pytest.raises(ConfigurationError, match="track_extra_parameters = search_path"):
            await TransactionPooler.check_search_path(client, "tenant_that_never_arrives, public")
    finally:
        await client.close()

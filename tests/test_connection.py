import asyncio
import warnings
from unittest.mock import AsyncMock, Mock, PropertyMock, call, patch

import pytest

from hare import ConfigurationError, DatabaseClient
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.connections.connections import Connections
from hare.warnings import HareLoopSwitchWarning


@pytest.fixture
def conn_handler():
    return ConnectionHandler()


def test_init_constructor(conn_handler):
    assert conn_handler._db_config is None
    assert conn_handler._create_db is False
    assert conn_handler._storage == {}


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._init_connections")
async def test_init(mocked_init_connections, conn_handler):
    db_config = {"default": {"HOST": "some_host", "PORT": "1234"}}
    await conn_handler._init(db_config, True)
    mocked_init_connections.assert_awaited_once()
    assert db_config == conn_handler._db_config
    assert conn_handler._create_db is True


def test_db_config_present(conn_handler):
    conn_handler._db_config = {"default": {"HOST": "some_host", "PORT": "1234"}}
    assert conn_handler.db_config == conn_handler._db_config


def test_db_config_not_present(conn_handler):
    err_msg = (
        "DB configuration not initialised. Make sure to call "
        "Hare.init with a valid configuration before attempting "
        "to create connections."
    )
    with pytest.raises(ConfigurationError, match=err_msg):
        _ = conn_handler.db_config


def test_get_storage(conn_handler):
    expected_ret_val = {"default": DatabaseClient("default")}
    conn_handler._storage = expected_ret_val
    ret_val = conn_handler._get_storage()
    assert ret_val == expected_ret_val
    assert ret_val is conn_handler._storage


def test_set_storage(conn_handler):
    new_storage = {"default": DatabaseClient("default")}
    conn_handler._set_storage(new_storage)
    assert conn_handler._storage == new_storage
    assert conn_handler._storage is new_storage


def test_copy_storage(conn_handler):
    original_storage = {"default": DatabaseClient("default")}
    conn_handler._storage = original_storage
    ret_val = conn_handler._copy_storage()
    assert ret_val == original_storage
    assert ret_val is not original_storage


def test_clear_storage(conn_handler):
    conn_handler._storage = {"default": DatabaseClient("default")}
    conn_handler._clear_storage()
    assert conn_handler._storage == {}


def test_get_client_class_of_a_registered_driver(conn_handler):
    from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient, AiosqliteClientWithRegexpSupport

    assert conn_handler._get_client_class({"engine": "sqlite+aiosqlite", "credentials": {}}) is AiosqliteClient
    credentials = {"install_regexp_functions": True}
    assert (
        conn_handler._get_client_class({"engine": "sqlite+aiosqlite", "credentials": credentials})
        is AiosqliteClientWithRegexpSupport
    )


def test_get_client_class_of_an_unknown_engine(conn_handler):
    with pytest.raises(ConfigurationError, match='Unknown database engine "some_engine"'):
        conn_handler._get_client_class({"engine": "some_engine", "credentials": {}})


@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_get_db_info_present(mocked_db_config, conn_handler):
    expected_ret_val = {"HOST": "some_host", "PORT": "1234"}
    mocked_db_config.return_value = {"default": expected_ret_val}
    ret_val = conn_handler._get_db_info("default")
    assert ret_val == expected_ret_val


@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_get_db_info_not_present(mocked_db_config, conn_handler):
    mocked_db_config.return_value = {"default": {"HOST": "some_host", "PORT": "1234"}}
    connection_alias = "blah"
    with pytest.raises(
        ConfigurationError,
        match=f"Unable to get db settings for alias '{connection_alias}'",
    ):
        _ = conn_handler._get_db_info(connection_alias)


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
@patch("hare.core.connections.connection_handler.ConnectionHandler.get")
async def test_init_connections_no_db_create(mocked_get, mocked_db_config, conn_handler):
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    mocked_get.side_effect = [conn_1, conn_2]
    mocked_db_config.return_value = {
        "default": {"HOST": "some_host", "PORT": "1234"},
        "other": {"HOST": "some_other_host", "PORT": "1234"},
    }
    await conn_handler._init_connections()
    mocked_db_config.assert_called_once()
    mocked_get.assert_has_calls([call("default"), call("other")], any_order=True)
    conn_1.db_create.assert_not_awaited()
    conn_2.db_create.assert_not_awaited()


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
@patch("hare.core.connections.connection_handler.ConnectionHandler.get")
async def test_init_connections_db_create(mocked_get, mocked_db_config, conn_handler):
    conn_handler._create_db = True
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    mocked_get.side_effect = [conn_1, conn_2]
    mocked_db_config.return_value = {
        "default": {"HOST": "some_host", "PORT": "1234"},
        "other": {"HOST": "some_other_host", "PORT": "1234"},
    }
    await conn_handler._init_connections()
    mocked_db_config.assert_called_once()
    mocked_get.assert_has_calls([call("default"), call("other")], any_order=True)
    conn_1.db_create.assert_awaited_once()
    conn_2.db_create.assert_awaited_once()


@patch("hare.core.connections.connection_handler.ConnectionHandler._get_db_info")
@patch("hare.core.connections.connection_handler.DbUrlConfigGenerator.expand")
@patch("hare.core.connections.connection_handler.ConnectionHandler._get_client_class")
def test_create_connection_db_info_str(
    mocked_get_client_class,
    mocked_expand_db_url,
    mocked_get_db_info,
    conn_handler,
):
    alias = "default"
    mocked_get_db_info.return_value = "some_db_url"
    mocked_expand_db_url.return_value = {
        "engine": "some_engine",
        "credentials": {"cred_key": "some_val"},
    }
    expected_connection = Mock()
    expected_client_class = Mock(return_value=expected_connection)
    mocked_get_client_class.return_value = expected_client_class
    expected_db_params = {"cred_key": "some_val", "connection_alias": alias}

    ret_val = conn_handler._create_connection(alias)

    mocked_get_db_info.assert_called_once_with(alias)
    mocked_expand_db_url.assert_called_once_with("some_db_url")
    mocked_get_client_class.assert_called_once_with({"engine": "some_engine", "credentials": {"cred_key": "some_val"}})
    expected_client_class.assert_called_once_with(**expected_db_params)
    assert ret_val is expected_connection


@patch("hare.core.connections.connection_handler.ConnectionHandler._get_db_info")
@patch("hare.core.connections.connection_handler.DbUrlConfigGenerator.expand")
@patch("hare.core.connections.connection_handler.ConnectionHandler._get_client_class")
def test_create_connection_db_info_not_str(
    mocked_get_client_class,
    mocked_expand_db_url,
    mocked_get_db_info,
    conn_handler,
):
    alias = "default"
    mocked_get_db_info.return_value = {
        "engine": "some_engine",
        "credentials": {"cred_key": "some_val"},
    }
    expected_connection = Mock()
    expected_client_class = Mock(return_value=expected_connection)
    mocked_get_client_class.return_value = expected_client_class
    expected_db_params = {"cred_key": "some_val", "connection_alias": alias}

    ret_val = conn_handler._create_connection(alias)

    mocked_get_db_info.assert_called_once_with(alias)
    mocked_expand_db_url.assert_not_called()
    mocked_get_client_class.assert_called_once_with({"engine": "some_engine", "credentials": {"cred_key": "some_val"}})
    expected_client_class.assert_called_once_with(**expected_db_params)
    assert ret_val is expected_connection


def test_get_alias_present(conn_handler):
    mock_conn = Mock(_check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False)
    conn_handler._storage = {"default": mock_conn}
    ret_val = conn_handler.get("default")
    assert ret_val is mock_conn


@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
def test_get_alias_not_present(mocked_create_connection, conn_handler):
    conn_handler._storage = {"default": "some_connection"}
    other_connection = Mock(tenant_schema_template=None, tenant_row_level_security=False)
    mocked_create_connection.return_value = other_connection
    ret_val = conn_handler.get("other")
    mocked_create_connection.assert_called_once_with("other")
    assert ret_val is other_connection
    assert conn_handler._storage == {"default": "some_connection", "other": other_connection}


def test_set(conn_handler):
    conn_handler._storage = {"default": "existing_conn"}
    token = conn_handler.set("other", "some_conn")
    assert conn_handler._storage == {"default": "existing_conn", "other": "some_conn"}
    assert token is not None


def test_discard(conn_handler):
    conn_handler._storage = {"default": "some_conn", "other": "other_conn"}
    ret_val = conn_handler.discard("default")
    assert ret_val == "some_conn"
    assert conn_handler._storage == {"other": "other_conn"}


def test_reset(conn_handler):
    conn_handler._storage = {"default": "modified_conn", "other": "other_conn"}
    original_conn = Mock()
    token = Mock(_handler=conn_handler, _alias="default", _old_value=original_conn, _used=False)
    conn_handler.reset(token)
    assert conn_handler._storage["default"] is original_conn
    assert token._used is True


def test_reset_restores_original_connection_for_swapped_alias(conn_handler):
    """Same as test_reset, but through a real set()/reset() round trip (a real contextvars.Token,
    not a Mock) - exercises the ContextVar branch of reset(), not the Mock-token fallback."""
    conn_handler._storage = {"default": "original_conn"}
    token = conn_handler.set("default", "txn_conn")
    assert conn_handler._storage["default"] == "txn_conn"
    conn_handler.reset(token)
    assert conn_handler._storage == {"default": "original_conn"}


def test_reset_preserves_alias_created_during_transaction(conn_handler):
    """Regression test: set() installs a COPY of storage for the duration of a transaction on
    one alias ("default" here) - but .get() mutates whatever storage is currently ambient IN
    PLACE, so a different alias lazily touched for the first time while that transaction is open
    (e.g. connect=False skipped the usual eager warm-up of every alias) gets created
    into that copy, not the original dict reset() restores. Previously that connection was
    silently dropped when reset() discarded the copy - unreachable via any future .get(alias),
    and never closed."""
    conn_handler._storage = {"default": "original_default_conn"}
    token = conn_handler.set("default", "txn_conn")
    # Mirrors what ConnectionHandler.get() itself does for a real, not-yet-created alias:
    # mutate the currently-ambient (transaction-scoped) storage dict in place.
    conn_handler._storage["other"] = "new_other_conn"
    conn_handler.reset(token)
    assert conn_handler._storage == {"default": "original_default_conn", "other": "new_other_conn"}


@pytest.mark.asyncio
async def test_reset_closes_own_connection_when_a_concurrent_task_raced_the_same_new_alias(conn_handler):
    """Regression test for a leak sibling to test_reset_preserves_alias_created_during_transaction
    above: that fix assumed only ONE side (the transaction) ever lazily creates a given
    not-yet-warmed alias while set() is active. If a DIFFERENT, concurrently-running task
    independently touches the SAME alias (its own get() mutates the pre-set() dict directly, in
    place, exactly like the transaction's own get() mutates its private copy), both sides end up
    with their own real client for it. reset()'s own "alias not in restored_storage" check is
    then false (the concurrent task's entry got there first) - previously this silently dropped
    the transaction's OWN connection (the one actually used for queries inside it) with no
    close() at all, a real leaked pool/socket, not just an unreferenced object."""
    conn_handler._storage = {"default": "original_default_conn"}
    # set() installs a COPY for the transaction (_copy_storage() -> dict(...), a genuinely new
    # object) - this reference to the dict active BEFORE set() is what a sibling task with no
    # transaction of its own would still see as its own ambient storage.
    pre_transaction_storage = conn_handler._storage
    token = conn_handler.set("default", "txn_conn")
    # Mirrors what ConnectionHandler.get() itself does for a real, not-yet-created alias, from
    # INSIDE the transaction (mutates the ambient, transaction-scoped copy in place).
    txn_side_conn = AsyncMock(spec=DatabaseClient)
    conn_handler._storage["other"] = txn_side_conn

    # A different, concurrently-running task's own get() on that SAME alias - never having
    # called set() itself, so it mutates the pre-transaction dict directly, not the copy above.
    concurrent_conn = AsyncMock(spec=DatabaseClient)
    pre_transaction_storage["other"] = concurrent_conn

    conn_handler.reset(token)

    # The concurrent task's own entry is what the restored, going-forward storage keeps.
    assert conn_handler._storage["other"] is concurrent_conn
    # The transaction's own connection must not simply vanish unclosed - reset() is sync and
    # can't await close() itself, so give its fire-and-forget cleanup task a chance to run.
    await asyncio.sleep(0)
    txn_side_conn.close.assert_awaited_once()


@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_aliases(mocked_db_config, conn_handler):
    mocked_db_config.return_value = {"default": {}, "other": {}}
    assert conn_handler.aliases() == ["default", "other"]


@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_all(mocked_db_config, conn_handler):
    mock_conn_1 = Mock(_check_loop=Mock(return_value=True))
    mock_conn_2 = Mock(_check_loop=Mock(return_value=True))
    conn_handler._storage = {"default": mock_conn_1, "other": mock_conn_2}
    mocked_db_config.return_value = {"default": {}, "other": {}}
    ret_val = conn_handler.all()
    assert set(ret_val) == {mock_conn_1, mock_conn_2}


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_with_discard(mocked_db_config, conn_handler):
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    conn_handler._storage = {"default": conn_1, "other": conn_2}
    conn_handler._db_config = {
        "default": {},
        "other": {},
    }  # Set _db_config so close_all doesn't early-return
    mocked_db_config.return_value = {"default": {}, "other": {}}
    await conn_handler.close_all()
    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._storage == {}


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_without_discard(mocked_db_config, conn_handler):
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    conn_handler._storage = {"default": conn_1, "other": conn_2}
    conn_handler._db_config = {
        "default": {},
        "other": {},
    }  # Set _db_config so close_all doesn't early-return
    mocked_db_config.return_value = {"default": {}, "other": {}}
    await conn_handler.close_all(discard=False)
    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._storage == {"default": conn_1, "other": conn_2}


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_discards_every_alias_even_if_one_close_fails(mocked_db_config, conn_handler):
    """One connection's close() raising must not stop the others from being awaited, nor leave
    any alias (including the one that failed to close - it's unusable either way) reachable via
    a later get()."""
    conn_1 = AsyncMock(spec=DatabaseClient)
    conn_1.close.side_effect = RuntimeError("simulated close failure")
    conn_2 = AsyncMock(spec=DatabaseClient)
    conn_handler._storage = {"default": conn_1, "other": conn_2}
    conn_handler._db_config = {"default": {}, "other": {}}
    mocked_db_config.return_value = {"default": {}, "other": {}}

    with pytest.raises(RuntimeError, match="simulated close failure"):
        await conn_handler.close_all()

    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._storage == {}


# --- Event loop validation tests ---


@pytest.mark.asyncio
async def test_check_loop_returns_true_when_not_bound():
    client = Mock(spec=DatabaseClient)
    client._bound_loop = None
    client._check_loop = DatabaseClient._check_loop.__get__(client)
    assert client._check_loop() is True


@pytest.mark.asyncio
async def test_check_loop_returns_true_on_same_loop():
    client = Mock(spec=DatabaseClient)
    client._bound_loop = asyncio.get_running_loop()
    client._check_loop = DatabaseClient._check_loop.__get__(client)
    assert client._check_loop() is True


@pytest.mark.asyncio
async def test_check_loop_returns_false_on_different_loop():
    client = Mock(spec=DatabaseClient)
    other_loop = asyncio.new_event_loop()
    try:
        client._bound_loop = other_loop
        client._check_loop = DatabaseClient._check_loop.__get__(client)
        assert client._check_loop() is False
    finally:
        other_loop.close()


@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
def test_get_reconnects_on_loop_change(mocked_create_connection, conn_handler):
    """When _check_loop() returns False, get() should warn and create a new connection."""
    stale_conn = Mock(_check_loop=Mock(return_value=False))
    fresh_conn = Mock(
        _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
    )
    mocked_create_connection.return_value = fresh_conn
    conn_handler._storage = {"default": stale_conn}

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ret_val = conn_handler.get("default")

    assert ret_val is fresh_conn
    assert conn_handler._storage["default"] is fresh_conn
    mocked_create_connection.assert_called_once_with("default")
    loop_warnings = [x for x in w if issubclass(x.category, HareLoopSwitchWarning)]
    assert len(loop_warnings) == 1
    assert "different event loop" in str(loop_warnings[0].message)


@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
async def test_get_closes_stale_connection_on_loop_change(mocked_create_connection, conn_handler):
    """Replacing a connection because its bound event loop changed must not just drop the old
    one on the floor - without an explicit close(), its pool/socket resources are never
    released (a real leak, not just an unreferenced Python object)."""
    stale_conn = Mock(_check_loop=Mock(return_value=False), close=AsyncMock())
    fresh_conn = Mock(
        _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
    )
    mocked_create_connection.return_value = fresh_conn
    conn_handler._storage = {"default": stale_conn}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HareLoopSwitchWarning)
        ret_val = conn_handler.get("default")

    assert ret_val is fresh_conn
    # get() is sync and can't await close() itself - give the fire-and-forget cleanup task a
    # chance to actually run before checking it happened.
    await asyncio.sleep(0)
    stale_conn.close.assert_awaited_once()


# --- Connections (module-level singleton facade) ---


@pytest.mark.asyncio
async def test_connections_aliases_passes_through_to_current_handler(db):
    """Connections.aliases() is a static passthrough to Connections.current().aliases(), matching
    the existing Connections.get()/Connections.current() pattern - it must return exactly what
    the handler's own aliases() (== list(db_config)) gives."""
    assert Connections.aliases() == db.connections.aliases()
    assert Connections.aliases() == list(db.connections.db_config)


def test_reset_keeps_an_alias_discarded_while_the_transaction_was_open(conn_handler):
    """close_all(discard=True) while a transaction is open drops the alias - resetting the
    transaction's token afterwards must not put the finished transaction client back."""
    conn_handler._storage = {"default": "original_conn"}
    # What another task's close_all(discard=True) sees and discards from: the storage in place
    # before set() installed the transaction's own copy.
    pre_transaction_storage = conn_handler._storage
    token = conn_handler.set("default", "txn_conn")
    pre_transaction_storage.pop("default")
    conn_handler.reset(token)
    assert "default" not in conn_handler._storage


@pytest.mark.asyncio
async def test_reset_from_another_task_restores_the_alias_there(conn_handler):
    """A transaction context exited in a task other than the one that entered it resets a token
    created in another context - that must restore the alias instead of raising ValueError."""
    conn_handler._storage = {"default": "original_conn"}
    token = conn_handler.set("default", "txn_conn")

    async def reset_in_another_task() -> dict:
        conn_handler.reset(token)
        return dict(conn_handler._storage)

    storage_seen_by_other_task = await asyncio.ensure_future(reset_in_another_task())
    assert storage_seen_by_other_task == {"default": "original_conn"}
    assert token._used is True

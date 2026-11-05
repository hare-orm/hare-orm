import asyncio
import threading
import warnings
from unittest.mock import AsyncMock, Mock, PropertyMock, call, patch

import pytest

from hare import ConfigurationError, DatabaseClient
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.connections.connections import Connections
from hare.warnings import HareLoopSwitchWarning
from tests.utils.waiting import wait_until


@pytest.fixture
def conn_handler():
    return ConnectionHandler()


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._init_connections")
async def test_init(mocked_init_connections, conn_handler):
    db_config = {"default": {"HOST": "some_host", "PORT": "1234"}}
    await conn_handler._init(db_config, True)
    mocked_init_connections.assert_awaited_once()
    assert db_config == conn_handler._db_config
    assert conn_handler._create_db is True


@pytest.mark.database_independent
def test_db_config_present(conn_handler):
    conn_handler._db_config = {"default": {"HOST": "some_host", "PORT": "1234"}}
    assert conn_handler.db_config == conn_handler._db_config


@pytest.mark.database_independent
def test_db_config_not_present(conn_handler):
    err_msg = (
        r"DB configuration not initialised - Hare.init wasn't called, or close_connections\(\) closed the "
        "connections. Make sure to call Hare.init with a valid configuration before attempting "
        "to create connections."
    )
    with pytest.raises(ConfigurationError, match=err_msg):
        _ = conn_handler.db_config


@pytest.mark.database_independent
def test_get_client_class_of_a_registered_driver(conn_handler):
    from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient, AiosqliteClientWithRegexpSupport

    assert conn_handler._get_client_class({"engine": "sqlite+aiosqlite", "credentials": {}}) is AiosqliteClient
    credentials = {"install_regexp_functions": True}
    assert (
        conn_handler._get_client_class({"engine": "sqlite+aiosqlite", "credentials": credentials})
        is AiosqliteClientWithRegexpSupport
    )


@pytest.mark.database_independent
def test_get_client_class_of_an_unknown_engine(conn_handler):
    with pytest.raises(ConfigurationError, match="Unknown database engine 'some_engine'"):
        conn_handler._get_client_class({"engine": "some_engine", "credentials": {}})


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_get_db_info_present(mocked_db_config, conn_handler):
    expected_ret_val = {"HOST": "some_host", "PORT": "1234"}
    mocked_db_config.return_value = {"default": expected_ret_val}
    ret_val = conn_handler._get_db_info("default")
    assert ret_val == expected_ret_val


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_get_db_info_not_present(mocked_db_config, conn_handler):
    mocked_db_config.return_value = {"default": {"HOST": "some_host", "PORT": "1234"}}
    connection_alias = "blah"
    with pytest.raises(
        ConfigurationError,
        match=f"Unable to get db settings for alias '{connection_alias}'",
    ):
        _ = conn_handler._get_db_info(connection_alias)


@pytest.mark.database_independent
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


@pytest.mark.database_independent
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


@pytest.mark.database_independent
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


@pytest.mark.database_independent
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


@pytest.mark.database_independent
def test_get_alias_present(conn_handler):
    mock_conn = Mock(_check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False)
    conn_handler.root_clients.update({"default": mock_conn})
    ret_val = conn_handler.get("default")
    assert ret_val is mock_conn


@pytest.mark.database_independent
@pytest.mark.parametrize("direct_constructor", [False, True])
def test_derived_client_keeps_source_database_and_driver_settings(conn_handler, direct_constructor):
    from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClientWithRegexpSupport

    conn_handler._init_config({"default": "sqlite+aiosqlite://:memory:"})
    settings = {"file_path": "source.sqlite3", "install_regexp_functions": True}
    if direct_constructor:
        source = AiosqliteClientWithRegexpSupport(connection_alias="default", **settings)
        source.connection_handler = conn_handler
    else:
        source = conn_handler.create_independent("default", settings)

    derived = source.create_independent_client()
    assert derived.filename == "source.sqlite3"
    assert isinstance(derived, AiosqliteClientWithRegexpSupport)
    assert derived.connection_handler is conn_handler
    assert derived.create_independent_client({"file_path": "other.sqlite3"}).filename == "other.sqlite3"
    assert source.filename == "source.sqlite3"
    assert conn_handler.db_config["default"] == "sqlite+aiosqlite://:memory:"


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
def test_get_alias_not_present(mocked_create_connection, conn_handler):
    conn_handler.root_clients.update({"default": "some_connection"})
    other_connection = Mock(tenant_schema_template=None, tenant_row_level_security=False)
    mocked_create_connection.return_value = other_connection
    ret_val = conn_handler.get("other")
    mocked_create_connection.assert_called_once_with("other")
    assert ret_val is other_connection
    assert conn_handler._get_storage() == {"default": "some_connection", "other": other_connection}


@pytest.mark.database_independent
def test_discard(conn_handler):
    conn_handler.root_clients.update({"default": "some_conn", "other": "other_conn"})
    ret_val = conn_handler.discard("default")
    assert ret_val == "some_conn"
    assert conn_handler._get_storage() == {"other": "other_conn"}


@pytest.mark.database_independent
def test_reset_preserves_alias_created_during_transaction(conn_handler):
    root = Mock(_check_loop=Mock(return_value=True))
    created = Mock(_check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False)
    conn_handler.root_clients["default"] = root
    with patch.object(conn_handler, "_create_connection", return_value=created) as factory:
        token = conn_handler.set("default", object())
        assert conn_handler.get("other") is created
        token.reset()
        assert conn_handler.get("other") is created
        assert conn_handler.get_main("default") is root
        factory.assert_called_once_with("other")


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_tasks_share_one_lazily_created_root_across_transaction_frames(conn_handler):
    root = Mock(_check_loop=Mock(return_value=True))
    created = Mock(_check_loop=Mock(return_value=True), close=AsyncMock())
    conn_handler.root_clients["default"] = root
    ready = asyncio.Event()

    async def in_transaction():
        token = conn_handler.set("default", object())
        try:
            await ready.wait()
            return conn_handler.get_main("other")
        finally:
            token.reset()

    async def outside_transaction():
        ready.set()
        return conn_handler.get_main("other")

    with patch.object(conn_handler, "_create_connection", return_value=created) as factory:
        first, second = await asyncio.gather(in_transaction(), outside_transaction())
        assert first is second is created
        factory.assert_called_once_with("other")
        created.close.assert_not_awaited()


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_aliases(mocked_db_config, conn_handler):
    mocked_db_config.return_value = {"default": {}, "other": {}}
    assert conn_handler.aliases() == ["default", "other"]


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
def test_all(mocked_db_config, conn_handler):
    mock_conn_1 = Mock(_check_loop=Mock(return_value=True))
    mock_conn_2 = Mock(_check_loop=Mock(return_value=True))
    conn_handler.root_clients.update({"default": mock_conn_1, "other": mock_conn_2})
    mocked_db_config.return_value = {"default": {}, "other": {}}
    ret_val = conn_handler.all()
    assert set(ret_val) == {mock_conn_1, mock_conn_2}


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_with_discard(mocked_db_config, conn_handler):
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    conn_handler.root_clients.update({"default": conn_1, "other": conn_2})
    conn_handler._db_config = {
        "default": {},
        "other": {},
    }  # Set _db_config so close_all doesn't early-return
    mocked_db_config.return_value = {"default": {}, "other": {}}
    await conn_handler.close_all()
    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._get_storage() == {}


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_without_discard(mocked_db_config, conn_handler):
    conn_1, conn_2 = AsyncMock(spec=DatabaseClient), AsyncMock(spec=DatabaseClient)
    conn_handler.root_clients.update({"default": conn_1, "other": conn_2})
    conn_handler._db_config = {
        "default": {},
        "other": {},
    }  # Set _db_config so close_all doesn't early-return
    mocked_db_config.return_value = {"default": {}, "other": {}}
    await conn_handler.close_all(discard=False)
    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._get_storage() == {"default": conn_1, "other": conn_2}


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler.db_config", new_callable=PropertyMock)
async def test_close_all_discards_every_alias_even_if_one_close_fails(mocked_db_config, conn_handler):
    """One connection's close() raising must not stop the others from being awaited, nor leave
    any alias (including the one that failed to close - it's unusable either way) reachable via
    a later get()."""
    conn_1 = AsyncMock(spec=DatabaseClient)
    conn_1.close.side_effect = RuntimeError("simulated close failure")
    conn_2 = AsyncMock(spec=DatabaseClient)
    conn_handler.root_clients.update({"default": conn_1, "other": conn_2})
    conn_handler._db_config = {"default": {}, "other": {}}
    mocked_db_config.return_value = {"default": {}, "other": {}}

    with pytest.raises(RuntimeError, match="simulated close failure"):
        await conn_handler.close_all()

    conn_1.close.assert_awaited_once()
    conn_2.close.assert_awaited_once()
    assert conn_handler._get_storage() == {}


# --- Event loop validation tests ---


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_check_loop_returns_true_when_not_bound():
    client = Mock(spec=DatabaseClient)
    client._bound_loop = None
    client._check_loop = DatabaseClient._check_loop.__get__(client)
    assert client._check_loop() is True


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_check_loop_returns_true_on_same_loop():
    client = Mock(spec=DatabaseClient)
    client._bound_loop = asyncio.get_running_loop()
    client._check_loop = DatabaseClient._check_loop.__get__(client)
    assert client._check_loop() is True


@pytest.mark.database_independent
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


@pytest.mark.database_independent
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
def test_get_reconnects_on_loop_change(mocked_create_connection, conn_handler):
    """When _check_loop() returns False, get() should warn and create a new connection."""
    stale_conn = Mock(_check_loop=Mock(return_value=False), holds_database_in_connection=Mock(return_value=False))
    fresh_conn = Mock(
        _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
    )
    mocked_create_connection.return_value = fresh_conn
    conn_handler.root_clients.update({"default": stale_conn})

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ret_val = conn_handler.get("default")

    assert ret_val is fresh_conn
    assert conn_handler._get_storage()["default"] is fresh_conn
    mocked_create_connection.assert_called_once_with("default")
    loop_warnings = [x for x in w if issubclass(x.category, HareLoopSwitchWarning)]
    assert len(loop_warnings) == 1
    assert "different event loop" in str(loop_warnings[0].message)


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
async def test_get_closes_stale_connection_on_loop_change(mocked_create_connection, conn_handler):
    """Replacing a connection because its bound event loop changed must not just drop the old
    one on the floor - without an explicit close(), its pool/socket resources are never
    released (a real leak, not just an unreferenced Python object)."""
    stale_conn = Mock(
        _check_loop=Mock(return_value=False), holds_database_in_connection=Mock(return_value=False), close=AsyncMock()
    )
    fresh_conn = Mock(
        _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
    )
    mocked_create_connection.return_value = fresh_conn
    conn_handler.root_clients.update({"default": stale_conn})

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


@pytest.mark.database_independent
def test_reset_keeps_an_alias_discarded_while_the_transaction_was_open(conn_handler):
    root = object()
    conn_handler.root_clients["default"] = root
    token = conn_handler.set("default", object())
    assert conn_handler.discard("default") is root
    token.reset()
    assert "default" not in conn_handler._get_storage()


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
async def test_a_stale_connection_of_a_running_loop_is_closed_on_that_loop(mocked_create_connection, conn_handler):
    """A connection of another thread's loop, still running, was closed from the asking loop - its
    sockets belong to that loop, the close failed, and the pool stayed open (a loop then hung on
    shutdown waiting for them)."""
    thread_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=thread_loop.run_forever)
    thread.start()
    # Running before the connection is replaced.
    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(asyncio.sleep(0), thread_loop))
    closed_on: list[asyncio.AbstractEventLoop] = []

    async def close() -> None:
        closed_on.append(asyncio.get_running_loop())

    try:
        stale_conn = Mock(
            _check_loop=Mock(return_value=False),
            holds_database_in_connection=Mock(return_value=False),
            _bound_loop=thread_loop,
            close=close,
        )
        mocked_create_connection.return_value = Mock(
            _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
        )
        conn_handler.root_clients.update({"default": stale_conn})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", HareLoopSwitchWarning)
            conn_handler.get("default")
        await wait_until(lambda: closed_on)
        assert closed_on == [thread_loop]
    finally:
        thread_loop.call_soon_threadsafe(thread_loop.stop)
        thread.join()
        thread_loop.close()


@pytest.mark.database_independent
@pytest.mark.asyncio
@patch("hare.core.connections.connection_handler.ConnectionHandler._create_connection")
async def test_a_stale_connection_of_a_closed_loop_abandons_its_connections(mocked_create_connection, conn_handler):
    """A connection of a closed loop can't close through it - its connections stayed open on the
    server."""
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    stale_conn = Mock(
        _check_loop=Mock(return_value=False),
        holds_database_in_connection=Mock(return_value=False),
        _bound_loop=closed_loop,
        close=AsyncMock(),
    )
    mocked_create_connection.return_value = Mock(
        _check_loop=Mock(return_value=True), tenant_schema_template=None, tenant_row_level_security=False
    )
    conn_handler.root_clients.update({"default": stale_conn})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HareLoopSwitchWarning)
        conn_handler.get("default")
    stale_conn.abandon_connections.assert_called_once_with()

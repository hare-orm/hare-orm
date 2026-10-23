"""``password_provider``: a connection takes its password from a function - plain or async - asked
again once the password is older than ``password_refresh_seconds``; the settings are checked."""

import asyncio

import pytest

from hare.dialects.base.client.password_provider import PasswordProvider
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.exceptions import ConfigurationError
from tests.utils.waiting import wait_until

CLIENT_CLASSES = [AsyncpgClient, RustPgClient]


def get_password() -> str:
    return "from-the-provider"


def make_client(client_class, **overrides):
    kwargs = {"connection_alias": "default", "user": "postgres", "database": "test", "host": "127.0.0.1"}
    kwargs.update(overrides)
    return client_class(**kwargs)


class CountingPasswords:
    def __init__(self, *, is_async: bool) -> None:
        self.calls = 0
        self.is_async = is_async

    def __call__(self):
        self.calls += 1
        password = f"password-{self.calls}"
        if not self.is_async:
            return password

        async def get():
            await asyncio.sleep(0)
            return password

        return get()


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_the_password_is_asked_for_again_once_it_is_old(is_async):
    passwords = CountingPasswords(is_async=is_async)
    provider = PasswordProvider(passwords, refresh_seconds=60)
    assert await provider.get() == "password-1"
    assert await provider.get() == "password-1"
    # The password turns older than the refresh.
    provider.fetched_at -= 60
    assert await provider.get() == "password-2"
    assert passwords.calls == 2


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_connections_waiting_together_ask_once():
    passwords = CountingPasswords(is_async=True)
    provider = PasswordProvider(passwords, refresh_seconds=60)
    assert await asyncio.gather(*(provider.get() for _ in range(5))) == ["password-1"] * 5
    # A refused password is replaced once for every connection it was refused for.
    refused = provider.password
    assert await asyncio.gather(*(provider.fetch(refused) for _ in range(5))) == ["password-2"] * 5
    assert passwords.calls == 2


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("password", [None, "", 42])
async def test_a_provider_must_give_a_password(password):
    provider = PasswordProvider(lambda: password, refresh_seconds=60)
    with pytest.raises(ConfigurationError, match="password_provider must return a non-empty string"):
        await provider.get()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_function_failing_to_renew_the_password_leaves_the_last_one_in_use():
    """As the background refresh does - a connection opens with the password the server took last,
    not with the function's error; with no password yet the error is raised."""
    state = {"fail": False}

    def get_password_or_fail() -> str:
        if state["fail"]:
            raise RuntimeError("secret store down")
        return "kept"

    provider = PasswordProvider(get_password_or_fail, refresh_seconds=60)
    assert await provider.get() == "kept"
    state["fail"] = True
    provider.fetched_at -= 60
    assert await provider.get() == "kept"
    provider.password = None
    with pytest.raises(RuntimeError, match="secret store down"):
        await provider.get()


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_the_background_refresh_applies_new_passwords():
    passwords = CountingPasswords(is_async=False)
    # A refresh at every turn of the event loop.
    provider = PasswordProvider(passwords, refresh_seconds=0)
    applied = []
    provider.start_refreshing(applied.append)
    await wait_until(lambda: len(applied) >= 2)
    await provider.stop_refreshing()
    count = len(applied)
    assert applied[0] == "password-1"
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(applied) == count


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
@pytest.mark.parametrize(
    "password_provider", [get_password, "tests.configuration.test_password_provider.get_password"]
)
def test_a_client_takes_a_function_or_its_dotted_path(client_class, password_provider):
    client = make_client(client_class, password_provider=password_provider, password_refresh_seconds=30)
    assert client.password_provider.get_password is get_password
    assert client.password_provider.refresh_seconds == 30
    assert "password_provider" not in client.extra


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", CLIENT_CLASSES)
def test_a_client_without_a_provider_has_none(client_class):
    assert make_client(client_class, password="secret").password_provider is None


@pytest.mark.database_independent
def test_the_url_takes_a_dotted_path():
    credentials = DbUrlConfigGenerator.expand(
        "postgresql://postgres@127.0.0.1/test?password_provider=tests.configuration.test_password_provider.get_password"
        "&password_refresh_seconds=120"
    )["credentials"]
    assert credentials["password_provider"] is get_password
    assert credentials["password_refresh_seconds"] == 120


# The check is the clients' shared __init__: every value on the first client class, the first value
# on each other one.
INVALID_PROVIDER_SETTINGS = [
    ({"password": "secret", "password_provider": get_password}, "password and password_provider"),
    ({"password_refresh_seconds": 30}, "password_refresh_seconds needs password_provider"),
    ({"password_provider": get_password, "password_refresh_seconds": 0}, "password_refresh_seconds"),
    ({"password_provider": get_password, "password_refresh_seconds": 86401}, "password_refresh_seconds"),
    ({"password_provider": get_password, "password_refresh_seconds": True}, "password_refresh_seconds"),
    ({"password_provider": "tests.configuration.test_password_provider.missing"}, "password_provider"),
    ({"password_provider": "no_such_module_anywhere.get"}, "password_provider"),
    ({"password_provider": "tests.configuration.test_password_provider.CLIENT_CLASSES"}, "password_provider"),
    ({"password_provider": 42}, "password_provider"),
]


@pytest.mark.parametrize(
    ("client_class", "settings", "message"),
    [
        *[(CLIENT_CLASSES[0], *(settings, message)) for (settings, message) in INVALID_PROVIDER_SETTINGS],
        *[(client_class, *INVALID_PROVIDER_SETTINGS[0]) for client_class in CLIENT_CLASSES[1:]],
    ],
)
def test_the_settings_are_checked(client_class, settings, message):
    with pytest.raises(ConfigurationError, match=message):
        make_client(client_class, **settings)


@pytest.mark.database_independent
def test_a_sqlite_client_refuses_a_provider():
    from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient

    with pytest.raises(ConfigurationError, match="password_provider"):
        AiosqliteClient(connection_alias="default", file_path=":memory:", password_provider=get_password)


@pytest.mark.database_independent
def test_a_clickhouse_client_refuses_a_provider():
    pytest.importorskip("clickhouse_connect")
    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    with pytest.raises(ConfigurationError, match="password_provider"):
        ClickhouseConnectClient(connection_alias="default", host="127.0.0.1", password_provider=get_password)
    with pytest.raises(ConfigurationError, match="password_provider"):
        DbUrlConfigGenerator.expand(
            "clickhouse+clickhouse-connect://default@127.0.0.1:8124/test?password_provider=tests.configuration.test_password_provider.get_password"
        )


@pytest.mark.database_independent
def test_a_clickhouse_client_of_the_native_protocol_refuses_a_provider():
    pytest.importorskip("clickhouse_driver")
    from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_client import (
        ClickhouseDriverClient,
    )

    with pytest.raises(ConfigurationError, match="password_provider"):
        ClickhouseDriverClient(connection_alias="default", host="127.0.0.1", password_provider=get_password)
    with pytest.raises(ConfigurationError, match="password_provider"):
        DbUrlConfigGenerator.expand(
            "clickhouse+clickhouse-driver://default@127.0.0.1:9124/test?password_provider=tests.configuration.test_password_provider.get_password"
        )

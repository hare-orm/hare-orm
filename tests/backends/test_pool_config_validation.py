"""Connection pool credentials are validated for type and range, identically for both Postgres
drivers."""

import pytest

from hare.exceptions import ConfigurationError
from tests.utils.constants import ASYNCPG_CLIENT_PATH, POSTGRES_CLIENT_PATHS, RUST_PG_CLIENT_PATH
from tests.utils.driver_classes import DriverClasses

CLIENT_CLASSES = DriverClasses.get_parameters(*POSTGRES_CLIENT_PATHS)


def _make_client(client_class, **credentials):
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
INVALID_POOL_CREDENTIALS = [
    ({"pool_acquire_timeout": -1}, "pool_acquire_timeout must be greater than 0"),
    ({"pool_acquire_timeout": 0}, "pool_acquire_timeout must be greater than 0"),
    ({"pool_acquire_timeout": "0"}, "pool_acquire_timeout must be greater than 0"),
    ({"pool_acquire_timeout": float("nan")}, "pool_acquire_timeout must be greater than 0"),
    ({"pool_acquire_timeout": 10**9}, "pool_acquire_timeout must be greater than 0"),
    ({"pool_acquire_timeout": "soon"}, "pool_acquire_timeout must be a number"),
    ({"pool_acquire_timeout": True}, "pool_acquire_timeout must be a number"),
    ({"min_size": 5, "max_size": 2}, "min_size \\(5\\) must not be greater than max_size \\(2\\)"),
    ({"max_size": 0}, "max_size must be between 1"),
    ({"max_size": "0"}, "max_size must be between 1"),
    ({"min_size": -1}, "min_size must be between 0"),
    ({"max_size": 10**6}, "max_size must be between 1"),
    ({"min_size": 1.5}, "min_size must be a whole number"),
    ({"max_size": "many"}, "max_size must be a whole number"),
    ({"minsize": 1}, "Unknown connection parameter\\(s\\) \\['minsize'\\]"),
    ({"maxsize": 2}, "Unknown connection parameter\\(s\\) \\['maxsize'\\]"),
]


@pytest.mark.parametrize(
    ("client_class", "credentials", "message"),
    [
        *[
            DriverClasses.get_parameter(RUST_PG_CLIENT_PATH, credentials, message)
            for (credentials, message) in INVALID_POOL_CREDENTIALS
        ],
        DriverClasses.get_parameter(ASYNCPG_CLIENT_PATH, *INVALID_POOL_CREDENTIALS[0]),
    ],
    indirect=["client_class"],
)
def test_invalid_pool_credentials_are_rejected(client_class, credentials, message):
    with pytest.raises(ConfigurationError, match=message):
        _make_client(client_class, **credentials)


@pytest.mark.database_independent
@pytest.mark.parametrize("client_class", CLIENT_CLASSES, indirect=True)
@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        ({}, (1, 16, None)),
        ({"min_size": "0", "max_size": "3", "pool_acquire_timeout": "2.5"}, (0, 3, 2.5)),
        ({"min_size": 4, "max_size": 4, "pool_acquire_timeout": 1}, (4, 4, 1.0)),
        ({"min_size": 2, "max_size": 5}, (2, 5, None)),
    ],
)
def test_valid_pool_credentials_are_accepted(client_class, credentials, expected):
    client = _make_client(client_class, **credentials)
    assert (client.pool_minsize, client.pool_maxsize, client.pool_acquire_timeout) == expected


@pytest.mark.requires("native")
@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_rust_driver_rejects_a_negative_acquire_timeout_without_panicking():
    from rust.native import pg

    with pytest.raises(ValueError, match="pool_acquire_timeout"):
        await pg.connect("127.0.0.1", 1, "postgres", None, None, pool_acquire_timeout=-1.0)

"""ssl_root_cert of a DB_URL - asyncpg refused it as an unknown parameter, though rust_pg takes it, and a
file that can't be used failed differently on each driver (a retried connection error, a bare ValueError)."""

import ssl
import urllib.parse

import asyncpg
import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent

CLIENTS = [(AsyncpgClient, "postgresql+asyncpg"), (RustPgClient, "postgresql")]


def make_client(client_class, url):
    credentials = DbUrlConfigGenerator.expand(url)["credentials"]
    return client_class(connection_alias="default", **credentials)


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_class", "scheme"), CLIENTS)
async def test_a_root_certificate_that_cannot_be_read_is_a_configuration_error(tmp_path, client_class, scheme):
    missing_file = (tmp_path / "missing-root.crt").as_posix()
    client = make_client(
        client_class, f"{scheme}://orders@127.0.0.1:1/orders?sslmode=verify-full&ssl_root_cert={missing_file}"
    )
    with pytest.raises(ConfigurationError, match="missing-root.crt' cannot be used"):
        await client.create_connection(with_db=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_class", "scheme"), CLIENTS)
async def test_a_root_certificate_file_holding_no_certificate_is_a_configuration_error(tmp_path, client_class, scheme):
    certificate_file = tmp_path / "root.crt"
    certificate_file.write_text("no certificate")
    client = make_client(
        client_class,
        f"{scheme}://orders@127.0.0.1:1/orders?sslmode=verify-full&ssl_root_cert={certificate_file.as_posix()}",
    )
    with pytest.raises(ConfigurationError, match="root.crt' cannot be used"):
        await client.create_connection(with_db=True)


@pytest.mark.asyncio
async def test_asyncpg_checks_the_server_against_the_root_certificate_of_the_db_url(monkeypatch):
    taken_dsns = []

    def create_pool(dsn, **settings):
        taken_dsns.append(dsn)
        raise RuntimeError("no pool")

    client = make_client(
        AsyncpgClient, "postgresql+asyncpg://orders@127.0.0.1:1/orders?sslmode=verify-full&ssl_root_cert=/etc/a b.pem"
    )
    monkeypatch.setattr(client, "raise_if_root_certificate_unusable", lambda path: None)
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    with pytest.raises(RuntimeError, match="no pool"):
        await client.create_connection(with_db=True)
    # asyncpg reads the root certificate only from its DSN.
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(dsn).query) for dsn in taken_dsns] == [
        {"sslrootcert": ["/etc/a b.pem"]}
    ]


def test_the_root_certificate_takes_a_tls_mode_not_an_ssl_context():
    with pytest.raises(ConfigurationError, match="SSLContext"):
        AsyncpgClient(
            connection_alias="default",
            host="127.0.0.1",
            ssl=ssl.create_default_context(),
            ssl_root_cert="/etc/ca.pem",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_class", "scheme"), CLIENTS)
async def test_a_root_certificate_path_with_a_null_byte_is_a_configuration_error(client_class, scheme):
    client = make_client(client_class, f"{scheme}://orders@127.0.0.1:1/orders?sslmode=verify-full&ssl_root_cert=x%00y")
    with pytest.raises(ConfigurationError, match="cannot be used"):
        await client.create_connection(with_db=True)

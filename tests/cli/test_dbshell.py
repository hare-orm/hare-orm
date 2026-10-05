"""``hare dbshell``: the database's own interactive client on a configured connection - ``psql``,
``sqlite3``, ``clickhouse-client`` - its password in the environment, its exit code passed on."""

from __future__ import annotations

import contextlib
import importlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import hare.cli.hare_cli as cli_module
from hare.cli.commands import database_shell_commands as database_shell
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.dialects.sqlite.client.sqlite_client import SqliteClient


async def run_cli(args: list[str]) -> SimpleNamespace:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = await cli_module.HareCLI.run_cli_async(args)
    return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())


def write_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_url: str) -> str:
    package = tmp_path / "cli_dbshell"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "models.py").write_text(
        "from hare import fields\nfrom hare.models import Model\n\n\n"
        "class Note(Model):\n    id = fields.IntField(primary_key=True)\n",
        encoding="utf-8",
    )
    module_name = f"cli_dbshell_settings_{tmp_path.name}"
    (tmp_path / f"{module_name}.py").write_text(
        f'HARE_ORM = {{"connections": {{"default": {db_url!r}, "other": {db_url!r}}}, '
        '"apps": {"app": {"models": ["cli_dbshell.models"]}}}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return f"{module_name}.HARE_ORM"


class StartedPrograms:
    """Stands in for starting the client: records what would run, exits with a chosen code."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.started: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def __call__(self, *arguments: str, env: dict[str, str]) -> SimpleNamespace:
        self.started.append((arguments, env))

        async def wait() -> int:
            return self.exit_code

        return SimpleNamespace(wait=wait)


@pytest.fixture
def started_programs(monkeypatch: pytest.MonkeyPatch) -> StartedPrograms:
    programs = StartedPrograms(exit_code=0)
    monkeypatch.setattr(database_shell.asyncio, "create_subprocess_exec", programs)
    monkeypatch.setattr(database_shell.shutil, "which", lambda program: f"/usr/bin/{program}")
    return programs


@pytest.mark.asyncio
async def test_dbshell_runs_the_client_of_the_connection(tmp_path, monkeypatch, started_programs):
    db_file = (tmp_path / "shell.sqlite3").as_posix()
    config = write_project(tmp_path, monkeypatch, f"sqlite+aiosqlite:///{db_file.lstrip('/')}")
    started_programs.exit_code = 3

    result = await run_cli(["-c", config, "dbshell", "--connection", "other"])

    assert result.exit_code == 3
    ((arguments, _),) = started_programs.started
    assert arguments == ("/usr/bin/sqlite3", db_file)


@pytest.mark.asyncio
async def test_dbshell_refuses_an_in_memory_database(tmp_path, monkeypatch, started_programs):
    config = write_project(tmp_path, monkeypatch, "sqlite+aiosqlite://:memory:")

    result = await run_cli(["-c", config, "dbshell"])

    assert result.exit_code == 1
    assert "in-memory" in result.output
    assert started_programs.started == []


@pytest.mark.asyncio
async def test_dbshell_names_a_missing_client(tmp_path, monkeypatch, started_programs):
    config = write_project(tmp_path, monkeypatch, f"sqlite+aiosqlite:///{(tmp_path / 'shell.sqlite3').as_posix()}")
    monkeypatch.setattr(database_shell.shutil, "which", lambda program: None)

    result = await run_cli(["-c", config, "dbshell"])

    assert result.exit_code == 1
    assert "sqlite3 is not installed" in result.output


@pytest.mark.asyncio
async def test_dbshell_refuses_a_dialect_without_a_client(tmp_path, monkeypatch, started_programs):
    config = write_project(tmp_path, monkeypatch, f"sqlite+aiosqlite:///{(tmp_path / 'shell.sqlite3').as_posix()}")

    async def no_client(client):
        return None

    monkeypatch.setattr(SqliteClient, "get_shell_command", no_client)

    result = await run_cli(["-c", config, "dbshell"])

    assert result.exit_code == 1
    assert "names no interactive client" in result.output


@pytest.mark.asyncio
async def test_dbshell_refuses_an_unknown_connection(tmp_path, monkeypatch, started_programs):
    config = write_project(tmp_path, monkeypatch, "sqlite+aiosqlite://:memory:")

    result = await run_cli(["-c", config, "dbshell", "--connection", "ghost"])

    assert result.exit_code == 2
    assert "ghost" in result.output


def make_postgres_client(client_class, **settings):
    return client_class(
        connection_alias="default", host="db.internal", port=6543, user="orders", database="orders", **settings
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [AsyncpgClient, RustPgClient])
async def test_psql_takes_the_password_from_the_environment(client_class):
    client = make_postgres_client(client_class, password="secret", schema="app", application_name="api")

    command = await client.get_shell_command()

    assert command.arguments == (
        "psql",
        "--port",
        "6543",
        "--host",
        "db.internal",
        "--username",
        "orders",
        "--dbname",
        "orders",
    )
    assert "secret" not in command.arguments
    assert command.environment == {"PGPASSWORD": "secret", "PGAPPNAME": "api", "PGOPTIONS": "-c search_path=app"}


async def get_rotated_password() -> str:
    return "rotated"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", [AsyncpgClient, RustPgClient])
async def test_psql_takes_the_password_of_the_provider(client_class):
    client = make_postgres_client(client_class, password_provider=get_rotated_password)

    assert (await client.get_shell_command()).environment["PGPASSWORD"] == "rotated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_class", "settings", "environment"),
    [
        (AsyncpgClient, {"ssl": "verify-full"}, {"PGSSLMODE": "verify-full"}),
        (AsyncpgClient, {"ssl": True}, {"PGSSLMODE": "require"}),
        (AsyncpgClient, {"ssl": False}, {"PGSSLMODE": "disable"}),
        (
            RustPgClient,
            {"ssl_mode": "verify-ca", "ssl_root_cert": "/etc/ca.pem"},
            {"PGSSLMODE": "verify-ca", "PGSSLROOTCERT": "/etc/ca.pem"},
        ),
    ],
)
async def test_psql_gets_the_tls_settings(client_class, settings, environment):
    client = make_postgres_client(client_class, **settings)

    assert (await client.get_shell_command()).environment == environment


@pytest.mark.asyncio
async def test_clickhouse_client_speaks_the_native_protocol():
    pytest.importorskip("clickhouse_connect")
    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    client = ClickhouseConnectClient(
        connection_alias="default",
        host="ch.internal",
        user="reader",
        password="secret",
        database="events",
        secure=True,
        native_port=9440,
    )

    command = await client.get_shell_command()

    assert command.arguments == (
        "clickhouse-client",
        "--host",
        "ch.internal",
        "--user",
        "reader",
        "--port",
        "9440",
        "--database",
        "events",
        "--secure",
    )
    assert command.environment == {"CLICKHOUSE_PASSWORD": "secret"}

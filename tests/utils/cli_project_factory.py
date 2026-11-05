"""The project_factory fixture: CliProject instances on a database of their own each - a SQLite file,
or a database of the reusable test database pool when HARE_TEST_DB points at a server."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio

from hare.contrib.test import ReusableTestDatabases
from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig as DatabaseConfig
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_parts import DbUrlParts
from tests.utils.cli_project import CliProject, get_probe_config
from tests.utils.database_under_test import DatabaseUnderTest


def get_postgres_database_template() -> str | None:
    raw_database_url = os.environ.get("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.is_file_database(raw_database_url):
        return None
    return raw_database_url.replace("\\{", "{").replace("\\}", "}")


@pytest_asyncio.fixture
async def project_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any]:
    """Builds CliProject instances on a fresh database each; drops Postgres databases afterwards."""
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    (tmp_path / "cli_rt_probe.py").write_text("", encoding="utf-8")
    postgres_template = get_postgres_database_template()
    created_projects: list[CliProject] = []
    database_configs: list[DatabaseConfig] = []
    cleanup = ResourceCleanup()

    async def release_database(database: DatabaseConfig) -> None:
        async with HareContext() as context:
            await context.init(config=get_probe_config(database.config))
            await database.bind(context.get_connection()).release()

    async def create_project(*app_labels: str) -> CliProject:
        project_index = len(created_projects)
        project_root = tmp_path / f"project_{project_index}"
        project_root.mkdir()
        monkeypatch.syspath_prepend(str(project_root))
        if postgres_template is None:
            scheme = DatabaseUnderTest.get_url().split("://", 1)[0]
            database_url = f"{scheme}://{(project_root / 'db.sqlite3').as_posix()}?synchronous=OFF"
        else:
            parsed_url = DbUrlParts.parse(postgres_template, has_userinfo=True).parsed
            if "{}" not in parsed_url.path:
                parsed_url = parsed_url._replace(path=parsed_url.path + "_{}")
            database_template = parsed_url.geturl()
            # A database of the reusable test database pool - reset instead of created and dropped.
            database = DatabaseConfig.from_url(database_template, reuse=ReusableTestDatabases.is_enabled())
            database_configs.append(database)
            leased_connection_config = database.config
            # The leased database's full name in place of the template's database name.
            database_name = leased_connection_config["credentials"]["database"]
            database_url = parsed_url._replace(path="/" + quote(database_name, safe=""), params="").geturl()
            async with HareContext() as context:
                await context.init(config=get_probe_config(leased_connection_config))
                await database.bind(context.get_connection()).create()
                cleanup.push(release_database, database)
                await context.connections.close_all(discard=False)
        project = CliProject(
            project_root, database_url, app_labels or ("app",), f"cli_rt_settings_{tmp_path.name}_{project_index}"
        )
        created_projects.append(project)
        cleanup.push(project.close_query_context)
        return project

    try:
        yield create_project
    finally:
        try:
            await cleanup.close()
        finally:
            ReusableTestDatabases.release_leases(
                database.lease for database in database_configs if database.lease is not None
            )
        cleanup.raise_errors()

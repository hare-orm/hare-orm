"""The project_factory fixture: CliProject instances on a database of their own each - a SQLite file,
or a database of the reusable test database pool when HARE_TEST_DB points at a server."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import ReusableTestDatabases
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.exceptions import OperationalError
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
    leases: dict[str, int] = {}

    async def create_project(*app_labels: str) -> CliProject:
        project_index = len(created_projects)
        project_root = tmp_path / f"project_{project_index}"
        project_root.mkdir()
        monkeypatch.syspath_prepend(str(project_root))
        if postgres_template is None:
            scheme = DatabaseUnderTest.get_url().split("://", 1)[0]
            database_url = f"{scheme}://{(project_root / 'db.sqlite3').as_posix()}?synchronous=OFF"
        else:
            database_template = postgres_template if "{}" in postgres_template else f"{postgres_template}_{{}}"
            # A database of the reusable test database pool - reset instead of created and dropped.
            with ReusableTestDatabases.track_new_leases() as new_leases:
                leased_connection_config = DbUrlConfigGenerator.expand(
                    database_template, testing=True, reuse_databases=ReusableTestDatabases.is_enabled()
                )
            leases.update(new_leases)
            # The leased database's full name in place of the template's database name.
            base_url, _, database_and_query = database_template.rpartition("/")
            _, question_mark, query = database_and_query.partition("?")
            database_url = f"{base_url}/{leased_connection_config['credentials']['database']}{question_mark}{query}"
            async with HareContext() as context:
                await context.init(config=get_probe_config(leased_connection_config), _create_db=True)
                await context.connections.close_all(discard=False)
        project = CliProject(
            project_root, database_url, app_labels or ("app",), f"cli_rt_settings_{tmp_path.name}_{project_index}"
        )
        if postgres_template is not None:
            project.leased_connection_config = leased_connection_config
        created_projects.append(project)
        return project

    yield create_project

    try:
        for project in created_projects:
            await project.close_query_context()
            if project.leased_connection_config is None:
                continue
            async with HareContext() as context:
                await context.init(config=get_probe_config(project.leased_connection_config))
                connection = Connections.get("default")
                await context.connections.close_all(discard=False)
                for attempt in range(20):
                    try:
                        await connection.db_delete()
                        break
                    except OperationalError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.5)
    finally:
        # A lease whose database was never reset goes back to the pool marked for a reset.
        ReusableTestDatabases.release_leases(leases)

"""hare's pytest plugin in a pytest run of its own: the test databases made from ``hare_db_url`` for
every configured connection - the configured database is never opened - each test's writes rolled
back or emptied, and the ``hare_requires`` marker."""

import pytest

pytestmark = pytest.mark.database_independent

pytest_plugins = ["pytester"]

MODELS = """
from hare import fields
from hare.models import Model


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
"""
# The configured database doesn't exist - the plugin must not open it.
SETTINGS = """
HARE_ORM = {
    "connections": {"default": "postgresql://nobody@nowhere.invalid/production"},
    "apps": {"models": {"models": ["plugin_models"]}},
}
"""
INI = """
[pytest]
hare_config = plugin_settings.HARE_ORM
asyncio_mode = strict
asyncio_default_fixture_loop_scope = session
asyncio_default_test_loop_scope = session
"""


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    # The run of its own is no worker of this one's xdist - its database names carry no worker.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makepyfile(plugin_models=MODELS, plugin_settings=SETTINGS)
    pytester.makeini(INI)
    return pytester


def run(project: pytest.Pytester, *args: str) -> pytest.RunResult:
    return project.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider", *args)


def test_each_test_starts_with_empty_tables(project):
    project.makepyfile(
        test_notes="""
import pytest
from plugin_models import Note


@pytest.mark.asyncio
async def test_writes(hare_db, hare_assert_query_count):
    async with hare_assert_query_count(1):
        await Note.objects.create(id=1, text="first")
    assert await Note.objects.count() == 1


@pytest.mark.asyncio
async def test_sees_none_of_them(hare_db):
    assert await Note.objects.count() == 0
"""
    )
    run(project).assert_outcomes(passed=2)


def test_a_transactional_test_commits_and_its_tables_are_emptied(project):
    project.makepyfile(
        test_commits="""
import pytest
from hare.transactions import Transactions
from plugin_models import Note

committed = []


@pytest.mark.asyncio
async def test_commits(hare_transactional_db):
    async with Transactions.atomic():
        await Note.objects.create(id=1, text="committed")
        Transactions.on_commit(lambda: committed.append(1))
    assert committed == [1]


@pytest.mark.asyncio
async def test_after_it(hare_db):
    assert await Note.objects.count() == 0
"""
    )
    run(project).assert_outcomes(passed=2)


def test_on_commit_callbacks_are_captured(project):
    project.makepyfile(
        test_callbacks="""
import pytest
from hare.transactions import Transactions


@pytest.mark.asyncio
async def test_captured(hare_db, hare_capture_on_commit):
    ran = []
    async with hare_capture_on_commit(execute=True) as callbacks:
        Transactions.on_commit(lambda: ran.append(1))
    assert len(callbacks) == 1
    assert ran == [1]
"""
    )
    run(project).assert_outcomes(passed=1)


def test_the_marker_skips_by_features(project):
    project.makepyfile(
        test_marker="""
import pytest


@pytest.mark.hare_requires(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgres_only(hare_db):
    pass


@pytest.mark.hare_requires(supports_transactions=True)
@pytest.mark.asyncio
async def test_with_transactions(hare_db):
    pass


@pytest.mark.hare_requires(supports_transactions=True)
def test_without_a_hare_fixture():
    pass
"""
    )
    result = run(project, "-rs")
    result.assert_outcomes(passed=1, skipped=1, errors=0, failed=1)
    result.stdout.fnmatch_lines(["*needs one of the fixtures*", "*dialect != postgresql*"])


def test_the_command_line_names_the_test_database(project):
    database_file = (project.path / "plugin.sqlite3").as_posix()
    project.makepyfile(
        test_url=f"""
import pytest


@pytest.mark.asyncio
async def test_url(hare_database):
    assert hare_database.get_connection().filename == {database_file!r}
"""
    )
    run(project, f"--hare-db-url=sqlite+aiosqlite:///{database_file.lstrip('/')}").assert_outcomes(passed=1)


def test_the_configuration_must_be_named(pytester):
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = session\n")
    pytester.makepyfile(
        test_unconfigured="""
import pytest


@pytest.mark.asyncio
async def test_unconfigured(hare_db):
    pass
"""
    )
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*hare's fixtures need the configuration*"])

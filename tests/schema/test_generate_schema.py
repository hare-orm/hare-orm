# pylint: disable=C0301
import os
import re
from unittest.mock import MagicMock, patch

import pytest

from hare import Connections, Hare, fields
from hare.core.context import HareContext
from hare.ddl.enums import TriggerEvent, TriggerTiming
from hare.ddl.triggers import Trigger
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models import Model
from tests.utils.context_apps_reset import ContextAppsReset
from tests.utils.database_under_test import DatabaseUnderTest

# Save the original classproperty before any test can shadow it
_original_apps_prop = Hare.__dict__["apps"]


# Safe schema SQL expected for SQLite
SAFE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "defaultpk" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "val" INT NOT NULL
);
CREATE TABLE IF NOT EXISTS "group" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" TEXT NOT NULL,
    "uuid" CHAR(36) NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS "employee" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" TEXT NOT NULL,
    "group_id" CHAR(36) NOT NULL REFERENCES "group" ("uuid") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_employee_group_i_ad35d9cc3c90" ON "employee" ("group_id");
CREATE TABLE IF NOT EXISTS "inheritedmodel" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "zero" INT NOT NULL,
    "one" VARCHAR(40),
    "new_field" VARCHAR(100) NOT NULL,
    "two" VARCHAR(40) NOT NULL,
    "name" TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS "sometable" (
    "sometable_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "some_chars_table" VARCHAR(255) NOT NULL,
    "fk_sometable" INT REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_sometable_some_ch_3d69ebb3b598" ON "sometable" ("some_chars_table");
CREATE INDEX IF NOT EXISTS "idx_sometable_fk_some_0141815c40a8" ON "sometable" ("fk_sometable");
CREATE TABLE IF NOT EXISTS "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY /* The TEAM name (and PK) */,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
) /* The TEAMS! */;
CREATE INDEX IF NOT EXISTS "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX IF NOT EXISTS "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
CREATE TABLE IF NOT EXISTS "address" (
    "city" VARCHAR(50) NOT NULL /* City */,
    "country" VARCHAR(50) NOT NULL /* Country */,
    "street" VARCHAR(128) NOT NULL /* Street Address */,
    "team_id" VARCHAR(50) NOT NULL PRIMARY KEY REFERENCES "team" ("name") ON DELETE CASCADE
) /* The Team's address */;
CREATE TABLE IF NOT EXISTS "location" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(128) NOT NULL,
    "capacity" INT NOT NULL /* No. of seats */,
    "rent" REAL NOT NULL,
    "team_id" VARCHAR(50) UNIQUE REFERENCES "team" ("name") ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS "tournament" (
    "tid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(100) NOT NULL /* Tournament name */,
    "created" TIMESTAMP NOT NULL /* Created *\\/'`\\/* datetime */
) /* What Tournaments *\\/'`\\/* we have */;
CREATE INDEX IF NOT EXISTS "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
CREATE TABLE IF NOT EXISTS "event" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL /* Event ID */,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMP NOT NULL,
    "prize" VARCHAR(40),
    "token" VARCHAR(100) NOT NULL UNIQUE /* Unique token */,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE /* FK to tournament */
) /* This table contains a list of all the events */;
CREATE UNIQUE INDEX IF NOT EXISTS "uid_event_tournam_a5b7304a103a" ON "event" ("tournament_id", "key");
CREATE TABLE IF NOT EXISTS "sometable_self" (
    "backward_sts" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE,
    "sts_forward" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_sometable_s_backwar_fc8fc8d8b358" ON "sometable_self" ("backward_sts", "sts_forward");
CREATE INDEX IF NOT EXISTS "idx_sometable_s_sts_for_75f7cf864c96" ON "sometable_self" ("sts_forward");
CREATE TABLE IF NOT EXISTS "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX IF NOT EXISTS "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
CREATE TABLE IF NOT EXISTS "teamevents" (
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE RESTRICT,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE RESTRICT
) /* How participants relate */;
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX IF NOT EXISTS "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
""".strip()


async def _reset_hare():
    """Helper to reset Hare state before each test.

    Note: We MUST NOT set Hare.apps = None
    because it is a classproperty and setting it shadows the property
    with a class attribute, breaking future access.
    """
    # Restore the original classproperty if it was shadowed
    if not isinstance(Hare.__dict__.get("apps"), type(_original_apps_prop)):
        type.__setattr__(Hare, "apps", _original_apps_prop)

    # Get the current context and properly reset it
    ctx = HareContext.get_current()
    if ctx is not None:
        # Clear db_config first to prevent close_all from trying to import bad backends
        if ctx._connections is not None:
            # Clear storage without closing (to avoid importing bad backends)
            ctx._connections._storage.clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None
    else:
        # No context exists - create one for the test
        ctx = HareContext()
        ctx.__enter__()


async def _teardown_hare():
    """Helper to teardown Hare state after each test."""
    ContextAppsReset.reset_apps()


def _get_engine():
    """Get the current test engine."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    config = DbUrlConfigGenerator.build(db_url, app_modules={"models": []}, connection_label="models")
    return config["connections"]["models"]["engine"]


def _get_sql(sqls: list[str], text: str) -> str:
    """Get SQL statement containing the given text."""
    return re.sub(r"[ \t\n\r]+", " ", " ".join([sql for sql in sqls if text in sql]))


# ============================================================================
# SQLite Tests
# ============================================================================


async def _init_for_sqlite(module: str, safe: bool = False) -> list[str]:
    """Initialize Hare for SQLite and return SQL statements."""
    with patch("hare.dialects.sqlite.client.SqliteClient.create_connection", new=MagicMock()):
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {"models": {"models": [module], "default_connection": "default"}},
            }
        )
        return Connections.get("default").get_schema_sql(safe).split(";\n")


@pytest.mark.asyncio
async def test_noid():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, '"noid"')
        assert '"name" VARCHAR(255)' in sql
        assert '"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL' in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_minrelation():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, '"minrelation"')
        assert '"tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("id") ON DELETE CASCADE' in sql
        assert "participants" not in sql

        sql = _get_sql(sqls, '"minrelation_team"')
        assert '"minrelation_id" INT NOT NULL REFERENCES "minrelation" ("id") ON DELETE CASCADE' in sql
        assert '"team_id" INT NOT NULL REFERENCES "team" ("id") ON DELETE CASCADE' in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_safe_generation():
    """Assert that the IF NOT EXISTS clause is included when safely generating schema."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels", True)
        sql = _get_sql(sqls, "")
        assert "IF NOT EXISTS" in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_unsafe_generation():
    """Assert that the IF NOT EXISTS clause is not included when generating schema."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels", False)
        sql = _get_sql(sqls, "")
        assert "IF NOT EXISTS" not in sql
    finally:
        await _teardown_hare()


@pytest.mark.parametrize(
    ("error_match", "models_module"),
    [
        pytest.param("Can't create schema due to cyclic fk references", "tests.schema.models_cyclic", id="cyclic"),
        pytest.param(
            'ForeignKeyField accepts model name in format "app.Model"',
            "tests.schema.models_fk_1",
            id="fk_bad_model_name",
        ),
        pytest.param(
            "on_delete can only be CASCADE, RESTRICT, SET_NULL, SET_DEFAULT, NO_ACTION or PROTECT",
            "tests.schema.models_fk_2",
            id="fk_bad_on_delete",
        ),
        pytest.param(
            "If on_delete is SET_NULL, then field must have null=True set",
            "tests.schema.models_fk_3",
            id="fk_bad_null",
        ),
        pytest.param(
            "If on_delete is SET_DEFAULT, then field must have db_default set when db_constraint=True",
            "tests.schema.models_fk_4",
            id="fk_bad_set_default",
        ),
        # A real FK constraint makes the database itself run ON DELETE SET DEFAULT, resetting the
        # column to its DDL DEFAULT - a Python-side default= is never emitted there, so it would be
        # silently reset to NULL instead. The error must explain exactly that.
        pytest.param(
            "never emitted into the DDL",
            "tests.schema.models_fk_6",
            id="fk_set_default_with_only_default_and_constraint_rejected",
        ),
        pytest.param(
            "on_delete can only be CASCADE, RESTRICT, SET_NULL, SET_DEFAULT, NO_ACTION or PROTECT",
            "tests.schema.models_o2o_2",
            id="o2o_bad_on_delete",
        ),
        pytest.param(
            "If on_delete is SET_NULL, then field must have null=True set",
            "tests.schema.models_o2o_3",
            id="o2o_bad_null",
        ),
        pytest.param(
            'ManyToManyField accepts model name in format "app.Model"',
            "tests.schema.models_m2m_1",
            id="m2m_bad_model_name",
        ),
        # SET_DEFAULT can't work on a ManyToManyField - there is no way to declare a default target
        # row for a through-table column (no `default=`/`db_default=` kwarg exists for either side).
        pytest.param(
            "on_delete=SET_DEFAULT is not supported on a ManyToManyField",
            "tests.schema.models_m2m_4",
            id="m2m_bad_set_default",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bad_model_declaration_fails_schema_generation(error_match, models_module):
    await _reset_hare()
    try:
        with pytest.raises(ConfigurationError, match=error_match):
            await _init_for_sqlite(models_module)
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_cyclic_with_db_constraint_false_does_not_raise():
    """A model-level FK cycle where every relation in the cycle has db_constraint=False has no
    real FK constraint DDL at all, so it must not trip the topological-sort cyclic check that
    only exists to prevent an unsatisfiable CREATE TABLE ordering."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_cyclic_no_constraint")
        cycle_sql = " ".join(sql for sql in sqls if re.search(r'CREATE TABLE "(one|two|three)"', sql))
        assert "REFERENCES" not in cycle_sql
        assert '"tournament_id" INT NOT NULL' in _get_sql(sqls, '"one"')
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_cyclic_fk_with_no_db_constraint_is_not_a_real_cycle():
    """A db_constraint=False relation emits no FK constraint and has no DDL-level dependency on
    its related table, so it must never count toward the cyclic-fk check - neither a symmetric
    cycle where BOTH sides are db_constraint=False, nor an asymmetric one where only one side is
    (which isn't even a real cycle in the dependency graph, just a single one-way dependency)."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_cyclic_no_constraint")

        def create_table_sql(table: str) -> str:
            return next(sql for sql in sqls if f'CREATE TABLE "{table}"' in sql)

        assert "REFERENCES" not in create_table_sql("symmetrica")
        assert "REFERENCES" not in create_table_sql("symmetricb")
        assert 'REFERENCES "asymmetricd"' in create_table_sql("asymmetricc")
        assert "REFERENCES" not in create_table_sql("asymmetricd")
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_postgres_only_fields_raise_unsupported_error_on_sqlite():
    """models_postgres_fields.py's TSVectorField/ArrayField columns are Postgres-only - schema
    generation against a SQLite connection must raise a clear UnSupportedError naming the
    offending field instead of silently emitting Postgres-flavored DDL (TSVECTOR, TEXT[], ...)
    that SQLite would then fail on with a confusing raw driver error, or worse, partially accept."""
    await _reset_hare()
    try:
        with pytest.raises(
            UnSupportedError, match="only exists on postgresql and can't generate DDL for the sqlite dialect"
        ):
            await _init_for_sqlite("tests.schema.models_postgres_fields")
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_create_index():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "CREATE INDEX")
        assert re.search(r"idx_tournament_created_\w+", sql) is not None
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_create_index_with_custom_name():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "f3")
        assert "model_with_indexes__f3" in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_fk_set_default_with_only_default_and_no_constraint_succeeds():
    """Without a real FK constraint only hare's own Python-side cascade resets the column, and it
    reads default= - so default= alone is enough, and no DDL DEFAULT/REFERENCES is emitted."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_fk_7")
        sql = _get_sql(sqls, '"one"')
        assert "DEFAULT" not in sql
        assert "REFERENCES" not in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_fk_set_default_with_db_default_succeeds():
    """SET_DEFAULT with db_default set is valid - the column gets a real SQL-level DEFAULT that
    ON DELETE SET DEFAULT can actually fall back to."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_fk_5")
        sql = _get_sql(sqls, '"one"')
        assert "DEFAULT 1" in sql
        assert "ON DELETE SET DEFAULT" in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_m2m_protect_generates_deferrable_no_action_fk():
    """on_delete=PROTECT has no SQL keyword of its own (enforced in Python before the DELETE is
    issued, see ManyToManyFieldInstance.db_on_delete) - the through-table's real FK constraint
    falls back to a deferrable NO ACTION in DDL, on both sides, as a defense-in-depth backstop."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "m2mondeleteprotectparent_m2mondeleteprotectpeer")
        assert re.search(
            r'"m2mondeleteprotectparent_id" INT NOT NULL REFERENCES "m2mondeleteprotectparent" \("id"\) '
            r"ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE",
            sql,
        )
        assert re.search(
            r'"m2mondeleteprotectpeer_id" INT NOT NULL REFERENCES "m2mondeleteprotectpeer" \("id"\) '
            r"ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE",
            sql,
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_m2m_set_null_generates_nullable_column():
    """on_delete=SET_NULL needs the through-table's own FK column(s) to actually accept NULL for
    the database's ON DELETE SET NULL constraint action to be legal DDL at all - both sides get
    the nullable column, since on_delete is mirrored onto the auto-generated backward field too
    (see Apps._init_relations)."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "m2mondeletesetnullparent_m2mondeletesetnullpeer")
        assert re.search(
            r'"m2mondeletesetnullparent_id" INT REFERENCES "m2mondeletesetnullparent" \("id"\) ON DELETE SET NULL',
            sql,
        )
        assert 'm2mondeletesetnullparent_id" INT NOT NULL' not in sql
        assert re.search(
            r'"m2mondeletesetnullpeer_id" INT REFERENCES "m2mondeletesetnullpeer" \("id"\) ON DELETE SET NULL',
            sql,
        )
        assert 'm2mondeletesetnullpeer_id" INT NOT NULL' not in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_composite_pk_m2m_protect_generates_deferrable_no_action_constraint():
    """Same as test_m2m_protect_generates_deferrable_no_action_fk, but for a composite-PK owner - the
    table-level composite FOREIGN KEY constraint (not an inline column REFERENCES clause) falls
    back to a deferrable NO ACTION the same way."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "compositepkm2mprotectowner_compositepkm2mprotectpeer")
        assert re.search(
            r'FOREIGN KEY \("compositepkm2mprotectowner_a", "compositepkm2mprotectowner_b"\) '
            r'REFERENCES "compositepkm2mprotectowner" \("a", "b"\) ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE',
            sql,
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_multi_m2m_fields_in_a_model():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_m2m_2")
        sql = _get_sql(sqls, "CASCADE")
        assert not re.search(r'REFERENCES [`"]three_one[`"]', sql)
        assert not re.search(r'REFERENCES [`"]three_two[`"]', sql)
        assert re.search(r'REFERENCES [`"](one|two|three)[`"]', sql)
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_table_and_row_comment_generation():
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.testmodels")
        sql = _get_sql(sqls, "comments")
        assert re.search(r".*\/\* Upvotes done on the comment.*\*\/", sql)
        assert re.search(r".*\\n.*", sql)
        assert "\\/" in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_schema_no_db_constraint():
    await _reset_hare()
    try:
        await _init_for_sqlite("tests.schema.models_no_db_constraint")
        sql = Connections.get("default").get_schema_sql(safe=False)
        # event's FK to tournament, and both team<->team relations, are all db_constraint=False -
        # no real DDL-level dependency, so they no longer force tournament/team to be created
        # before event (see the cyclic-fk dependency-ordering fix: only a reference.db_constraint
        # relation counts as a table dependency now).
        assert (
            sql.strip()
            == r"""CREATE TABLE "event" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL /* Event ID */,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMP NOT NULL,
    "prize" VARCHAR(40),
    "token" VARCHAR(100) NOT NULL UNIQUE /* Unique token */,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL /* FK to tournament */
) /* This table contains a list of all the events */;
CREATE UNIQUE INDEX "uid_event_tournam_a5b7304a103a" ON "event" ("tournament_id", "key");
CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY /* The TEAM name (and PK) */,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50)
) /* The TEAMS! */;
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
CREATE TABLE "tournament" (
    "tid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(100) NOT NULL /* Tournament name */,
    "created" TIMESTAMP NOT NULL /* Created *\/'`\/* datetime */
) /* What Tournaments *\/'`\/* we have */;
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
CREATE TABLE "teamevents" (
    "event_id" BIGINT NOT NULL,
    "team_id" VARCHAR(50) NOT NULL
) /* How participants relate */;
CREATE UNIQUE INDEX "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL,
    "team_id" VARCHAR(50) NOT NULL
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");"""
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_schema():
    await _reset_hare()
    try:
        await _init_for_sqlite("tests.schema.models_schema_create")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql.strip()
            == """
CREATE TABLE "defaultpk" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "val" INT NOT NULL
);
CREATE TABLE "group" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" TEXT NOT NULL,
    "uuid" CHAR(36) NOT NULL UNIQUE
);
CREATE TABLE "employee" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" TEXT NOT NULL,
    "group_id" CHAR(36) NOT NULL REFERENCES "group" ("uuid") ON DELETE CASCADE
);
CREATE INDEX "idx_employee_group_i_ad35d9cc3c90" ON "employee" ("group_id");
CREATE TABLE "inheritedmodel" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "zero" INT NOT NULL,
    "one" VARCHAR(40),
    "new_field" VARCHAR(100) NOT NULL,
    "two" VARCHAR(40) NOT NULL,
    "name" TEXT NOT NULL
);
CREATE TABLE "sometable" (
    "sometable_id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "some_chars_table" VARCHAR(255) NOT NULL,
    "fk_sometable" INT REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE INDEX "idx_sometable_some_ch_3d69ebb3b598" ON "sometable" ("some_chars_table");
CREATE INDEX "idx_sometable_fk_some_0141815c40a8" ON "sometable" ("fk_sometable");
CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY /* The TEAM name (and PK) */,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
) /* The TEAMS! */;
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
CREATE TABLE "address" (
    "city" VARCHAR(50) NOT NULL /* City */,
    "country" VARCHAR(50) NOT NULL /* Country */,
    "street" VARCHAR(128) NOT NULL /* Street Address */,
    "team_id" VARCHAR(50) NOT NULL PRIMARY KEY REFERENCES "team" ("name") ON DELETE CASCADE
) /* The Team's address */;
CREATE TABLE "location" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(128) NOT NULL,
    "capacity" INT NOT NULL /* No. of seats */,
    "rent" REAL NOT NULL,
    "team_id" VARCHAR(50) UNIQUE REFERENCES "team" ("name") ON DELETE SET NULL
);
CREATE TABLE "tournament" (
    "tid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(100) NOT NULL /* Tournament name */,
    "created" TIMESTAMP NOT NULL /* Created *\\/'`\\/* datetime */
) /* What Tournaments *\\/'`\\/* we have */;
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
CREATE TABLE "event" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL /* Event ID */,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMP NOT NULL,
    "prize" VARCHAR(40),
    "token" VARCHAR(100) NOT NULL UNIQUE /* Unique token */,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE /* FK to tournament */
) /* This table contains a list of all the events */;
CREATE UNIQUE INDEX "uid_event_tournam_a5b7304a103a" ON "event" ("tournament_id", "key");
CREATE TABLE "sometable_self" (
    "backward_sts" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE,
    "sts_forward" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_sometable_s_backwar_fc8fc8d8b358" ON "sometable_self" ("backward_sts", "sts_forward");
CREATE INDEX "idx_sometable_s_sts_for_75f7cf864c96" ON "sometable_self" ("sts_forward");
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
CREATE TABLE "teamevents" (
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE RESTRICT,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE RESTRICT
) /* How participants relate */;
CREATE UNIQUE INDEX "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
""".strip()
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_schema_safe():
    await _reset_hare()
    try:
        await _init_for_sqlite("tests.schema.models_schema_create")
        sql = Connections.get("default").get_schema_sql(safe=True)
        assert sql.strip() == SAFE_SCHEMA_SQL
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_m2m_no_auto_create():
    await _reset_hare()
    try:
        await _init_for_sqlite("tests.schema.models_no_auto_create_m2m")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql.strip()
            == r"""CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY /* The TEAM name (and PK) */,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
) /* The TEAMS! */;
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
CREATE TABLE "tournament" (
    "tid" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "name" VARCHAR(100) NOT NULL /* Tournament name */,
    "created" TIMESTAMP NOT NULL /* Created *\/'`\/* datetime */
) /* What Tournaments *\/'`\/* we have */;
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
CREATE TABLE "event" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL /* Event ID */,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMP NOT NULL,
    "prize" VARCHAR(40),
    "token" VARCHAR(100) NOT NULL UNIQUE /* Unique token */,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE /* FK to tournament */
) /* This table contains a list of all the events */;
CREATE UNIQUE INDEX "uid_event_tournam_a5b7304a103a" ON "event" ("tournament_id", "key");
CREATE TABLE "teamevents" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "score" INT NOT NULL,
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
) /* How participants relate */;
CREATE INDEX "idx_teamevents_event_i_6ad77ecabf77" ON "teamevents" ("event_id");
CREATE UNIQUE INDEX "uid_teamevents_team_id_9e89fcd1ffad" ON "teamevents" ("team_id", "event_id");
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
""".strip()
        )
    finally:
        await _teardown_hare()


# ============================================================================
# PostgreSQL Tests (asyncpg)
# ============================================================================


async def _init_for_asyncpg(module: str, safe: bool = False) -> list[str]:
    """Initialize Hare for asyncpg and return SQL statements."""
    try:
        with patch("asyncpg.create_pool", new=MagicMock()):
            await Hare.init(
                {
                    "connections": {
                        "default": {
                            "engine": "postgresql+asyncpg",
                            "credentials": {
                                "database": "test",
                                "host": "127.0.0.1",
                                "password": "foomip",
                                "port": 5432,
                                "user": "root",
                            },
                        }
                    },
                    "apps": {"models": {"models": [module], "default_connection": "default"}},
                }
            )
            return Connections.get("default").get_schema_sql(safe).split("; ")
    except ImportError:
        pytest.skip("asyncpg not installed")


@pytest.mark.asyncio
async def test_asyncpg_noid():
    await _reset_hare()
    try:
        sqls = await _init_for_asyncpg("tests.testmodels")
        sql = _get_sql(sqls, '"noid"')
        assert '"name" VARCHAR(255)' in sql
        assert '"id" SERIAL NOT NULL PRIMARY KEY' in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_table_and_row_comment_generation():
    await _reset_hare()
    try:
        sqls = await _init_for_asyncpg("tests.testmodels")
        sql = _get_sql(sqls, "comments")
        assert "COMMENT ON TABLE \"comments\" IS 'Test Table comment'" in sql
        assert (
            'COMMENT ON COLUMN "comments"."escaped_comment_field" IS '
            "'This column acts as it''s own comment'" in sql
        )
        # A literal embedded newline, not a "\n" escape sequence - Postgres's standard string
        # literals (standard_conforming_strings=on) don't interpret backslash escapes at all, so
        # a "\n" sequence would be stored as the two characters backslash+n, not a real newline.
        # _get_sql() collapses the real embedded newline (plus its surrounding spaces) down to a
        # single space before this comparison, hence "Some comment" here, not "Some \n comment".
        assert 'COMMENT ON COLUMN "comments"."multiline_comment" IS \'Some comment\'' in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_schema_no_db_constraint():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_no_db_constraint")
        sql = Connections.get("default").get_schema_sql(safe=False)
        # event's FK to tournament, and both team<->team relations, are all db_constraint=False -
        # no real DDL-level dependency, so they no longer force tournament/team to be created
        # before event (see the cyclic-fk dependency-ordering fix: only a reference.db_constraint
        # relation counts as a table dependency now).
        assert (
            sql.strip()
            == r"""CREATE TABLE "event" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMPTZ NOT NULL,
    "prize" DECIMAL(10,2),
    "token" VARCHAR(100) NOT NULL UNIQUE,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL,
    CONSTRAINT "uid_event_tournam_a5b7304a103a" UNIQUE ("tournament_id", "key")
);
COMMENT ON COLUMN "event"."id" IS 'Event ID';
COMMENT ON COLUMN "event"."token" IS 'Unique token';
COMMENT ON COLUMN "event"."tournament_id" IS 'FK to tournament';
COMMENT ON TABLE "event" IS 'This table contains a list of all the events';
CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50)
);
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
COMMENT ON COLUMN "team"."name" IS 'The TEAM name (and PK)';
COMMENT ON TABLE "team" IS 'The TEAMS!';
CREATE TABLE "tournament" (
    "tid" SMALLSERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created" TIMESTAMPTZ NOT NULL
);
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
COMMENT ON COLUMN "tournament"."name" IS 'Tournament name';
COMMENT ON COLUMN "tournament"."created" IS 'Created */''`/* datetime';
COMMENT ON TABLE "tournament" IS 'What Tournaments */''`/* we have';
CREATE TABLE "teamevents" (
    "event_id" BIGINT NOT NULL,
    "team_id" VARCHAR(50) NOT NULL
);
COMMENT ON TABLE "teamevents" IS 'How participants relate';
CREATE UNIQUE INDEX "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL,
    "team_id" VARCHAR(50) NOT NULL
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");"""
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_schema():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_schema_create")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql.strip()
            == """
CREATE TABLE "defaultpk" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "val" INT NOT NULL
);
CREATE TABLE "group" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "uuid" UUID NOT NULL UNIQUE
);
CREATE TABLE "employee" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "group_id" UUID NOT NULL REFERENCES "group" ("uuid") ON DELETE CASCADE
);
CREATE INDEX "idx_employee_group_i_ad35d9cc3c90" ON "employee" ("group_id");
CREATE TABLE "inheritedmodel" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "zero" INT NOT NULL,
    "one" VARCHAR(40),
    "new_field" VARCHAR(100) NOT NULL,
    "two" VARCHAR(40) NOT NULL,
    "name" TEXT NOT NULL
);
CREATE TABLE "sometable" (
    "sometable_id" SERIAL NOT NULL PRIMARY KEY,
    "some_chars_table" VARCHAR(255) NOT NULL,
    "fk_sometable" INT REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE INDEX "idx_sometable_some_ch_3d69ebb3b598" ON "sometable" ("some_chars_table");
CREATE INDEX "idx_sometable_fk_some_0141815c40a8" ON "sometable" ("fk_sometable");
CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
COMMENT ON COLUMN "team"."name" IS 'The TEAM name (and PK)';
COMMENT ON TABLE "team" IS 'The TEAMS!';
CREATE TABLE "address" (
    "city" VARCHAR(50) NOT NULL,
    "country" VARCHAR(50) NOT NULL,
    "street" VARCHAR(128) NOT NULL,
    "team_id" VARCHAR(50) NOT NULL PRIMARY KEY REFERENCES "team" ("name") ON DELETE CASCADE
);
COMMENT ON COLUMN "address"."city" IS 'City';
COMMENT ON COLUMN "address"."country" IS 'Country';
COMMENT ON COLUMN "address"."street" IS 'Street Address';
COMMENT ON TABLE "address" IS 'The Team''s address';
CREATE TABLE "location" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(128) NOT NULL,
    "capacity" INT NOT NULL,
    "rent" DOUBLE PRECISION NOT NULL,
    "team_id" VARCHAR(50) UNIQUE REFERENCES "team" ("name") ON DELETE SET NULL
);
COMMENT ON COLUMN "location"."capacity" IS 'No. of seats';
CREATE TABLE "tournament" (
    "tid" SMALLSERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created" TIMESTAMPTZ NOT NULL
);
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
COMMENT ON COLUMN "tournament"."name" IS 'Tournament name';
COMMENT ON COLUMN "tournament"."created" IS 'Created */''`/* datetime';
COMMENT ON TABLE "tournament" IS 'What Tournaments */''`/* we have';
CREATE TABLE "event" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMPTZ NOT NULL,
    "prize" DECIMAL(10,2),
    "token" VARCHAR(100) NOT NULL UNIQUE,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE,
    CONSTRAINT "uid_event_tournam_a5b7304a103a" UNIQUE ("tournament_id", "key")
);
COMMENT ON COLUMN "event"."id" IS 'Event ID';
COMMENT ON COLUMN "event"."token" IS 'Unique token';
COMMENT ON COLUMN "event"."tournament_id" IS 'FK to tournament';
COMMENT ON TABLE "event" IS 'This table contains a list of all the events';
CREATE TABLE "sometable_self" (
    "backward_sts" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE,
    "sts_forward" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_sometable_s_backwar_fc8fc8d8b358" ON "sometable_self" ("backward_sts", "sts_forward");
CREATE INDEX "idx_sometable_s_sts_for_75f7cf864c96" ON "sometable_self" ("sts_forward");
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
CREATE TABLE "teamevents" (
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE RESTRICT,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE RESTRICT
);
COMMENT ON TABLE "teamevents" IS 'How participants relate';
CREATE UNIQUE INDEX "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
""".strip()
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_schema_safe():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_schema_create")
        sql = Connections.get("default").get_schema_sql(safe=True)
        assert (
            sql.strip()
            == """
CREATE TABLE IF NOT EXISTS "defaultpk" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "val" INT NOT NULL
);
CREATE TABLE IF NOT EXISTS "group" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "uuid" UUID NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS "employee" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "group_id" UUID NOT NULL REFERENCES "group" ("uuid") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_employee_group_i_ad35d9cc3c90" ON "employee" ("group_id");
CREATE TABLE IF NOT EXISTS "inheritedmodel" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "zero" INT NOT NULL,
    "one" VARCHAR(40),
    "new_field" VARCHAR(100) NOT NULL,
    "two" VARCHAR(40) NOT NULL,
    "name" TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS "sometable" (
    "sometable_id" SERIAL NOT NULL PRIMARY KEY,
    "some_chars_table" VARCHAR(255) NOT NULL,
    "fk_sometable" INT REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_sometable_some_ch_3d69ebb3b598" ON "sometable" ("some_chars_table");
CREATE INDEX IF NOT EXISTS "idx_sometable_fk_some_0141815c40a8" ON "sometable" ("fk_sometable");
CREATE TABLE IF NOT EXISTS "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX IF NOT EXISTS "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
COMMENT ON COLUMN "team"."name" IS 'The TEAM name (and PK)';
COMMENT ON TABLE "team" IS 'The TEAMS!';
CREATE TABLE IF NOT EXISTS "address" (
    "city" VARCHAR(50) NOT NULL,
    "country" VARCHAR(50) NOT NULL,
    "street" VARCHAR(128) NOT NULL,
    "team_id" VARCHAR(50) NOT NULL PRIMARY KEY REFERENCES "team" ("name") ON DELETE CASCADE
);
COMMENT ON COLUMN "address"."city" IS 'City';
COMMENT ON COLUMN "address"."country" IS 'Country';
COMMENT ON COLUMN "address"."street" IS 'Street Address';
COMMENT ON TABLE "address" IS 'The Team''s address';
CREATE TABLE IF NOT EXISTS "location" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(128) NOT NULL,
    "capacity" INT NOT NULL,
    "rent" DOUBLE PRECISION NOT NULL,
    "team_id" VARCHAR(50) UNIQUE REFERENCES "team" ("name") ON DELETE SET NULL
);
COMMENT ON COLUMN "location"."capacity" IS 'No. of seats';
CREATE TABLE IF NOT EXISTS "tournament" (
    "tid" SMALLSERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created" TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
COMMENT ON COLUMN "tournament"."name" IS 'Tournament name';
COMMENT ON COLUMN "tournament"."created" IS 'Created */''`/* datetime';
COMMENT ON TABLE "tournament" IS 'What Tournaments */''`/* we have';
CREATE TABLE IF NOT EXISTS "event" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMPTZ NOT NULL,
    "prize" DECIMAL(10,2),
    "token" VARCHAR(100) NOT NULL UNIQUE,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE,
    CONSTRAINT "uid_event_tournam_a5b7304a103a" UNIQUE ("tournament_id", "key")
);
COMMENT ON COLUMN "event"."id" IS 'Event ID';
COMMENT ON COLUMN "event"."token" IS 'Unique token';
COMMENT ON COLUMN "event"."tournament_id" IS 'FK to tournament';
COMMENT ON TABLE "event" IS 'This table contains a list of all the events';
CREATE TABLE IF NOT EXISTS "sometable_self" (
    "backward_sts" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE,
    "sts_forward" INT NOT NULL REFERENCES "sometable" ("sometable_id") ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_sometable_s_backwar_fc8fc8d8b358" ON "sometable_self" ("backward_sts", "sts_forward");
CREATE INDEX IF NOT EXISTS "idx_sometable_s_sts_for_75f7cf864c96" ON "sometable_self" ("sts_forward");
CREATE TABLE IF NOT EXISTS "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX IF NOT EXISTS "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
CREATE TABLE IF NOT EXISTS "teamevents" (
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE RESTRICT,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE RESTRICT
);
COMMENT ON TABLE "teamevents" IS 'How participants relate';
CREATE UNIQUE INDEX IF NOT EXISTS "uidx_teamevents_event_i_664dbc51aed8" ON "teamevents" ("event_id", "team_id");
CREATE INDEX IF NOT EXISTS "idx_teamevents_team_id_147f04d59b94" ON "teamevents" ("team_id");
""".strip()
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_index_unsafe():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_postgres_index")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql
            == """CREATE TABLE "index" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "bloom" VARCHAR(200) NOT NULL,
    "brin" VARCHAR(200) NOT NULL,
    "gin" TSVECTOR NOT NULL,
    "gist" TSVECTOR NOT NULL,
    "sp_gist" VARCHAR(200) NOT NULL,
    "hash" VARCHAR(200) NOT NULL,
    "partial" VARCHAR(200) NOT NULL,
    "title" TEXT NOT NULL,
    "body" TEXT NOT NULL,
    "path" VARCHAR(200) NOT NULL
);
CREATE INDEX "idx_index_bloom_6375e065502c" ON "index" USING BLOOM ("bloom");
CREATE INDEX "idx_index_brin_f4428eee7598" ON "index" USING BRIN ("brin");
CREATE INDEX "idx_index_gin_c59b6d432f5d" ON "index" USING GIN ("gin");
CREATE INDEX "idx_index_gist_cc203a3ffdb7" ON "index" USING GIST ("gist");
CREATE INDEX "idx_index_sp_gist_939be3501a54" ON "index" USING SPGIST ("sp_gist");
CREATE INDEX "idx_index_hash_c08b870a8bf2" ON "index" USING HASH ("hash");
CREATE INDEX "idx_index_partial_f3f681bff192" ON "index" ("partial") WHERE ("id"=1);
CREATE INDEX "idx_index_expr_6b3aac52573a" ON "index" USING GIN ((TO_TSVECTOR('english',((COALESCE("title",'') || ' ') || COALESCE("body",'')))));
CREATE INDEX "idx_index_path_21f724a059db" ON "index" ("path" "varchar_pattern_ops");"""
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_index_safe():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_postgres_index")
        sql = Connections.get("default").get_schema_sql(safe=True)
        assert (
            sql
            == """CREATE TABLE IF NOT EXISTS "index" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "bloom" VARCHAR(200) NOT NULL,
    "brin" VARCHAR(200) NOT NULL,
    "gin" TSVECTOR NOT NULL,
    "gist" TSVECTOR NOT NULL,
    "sp_gist" VARCHAR(200) NOT NULL,
    "hash" VARCHAR(200) NOT NULL,
    "partial" VARCHAR(200) NOT NULL,
    "title" TEXT NOT NULL,
    "body" TEXT NOT NULL,
    "path" VARCHAR(200) NOT NULL
);
CREATE INDEX IF NOT EXISTS "idx_index_bloom_6375e065502c" ON "index" USING BLOOM ("bloom");
CREATE INDEX IF NOT EXISTS "idx_index_brin_f4428eee7598" ON "index" USING BRIN ("brin");
CREATE INDEX IF NOT EXISTS "idx_index_gin_c59b6d432f5d" ON "index" USING GIN ("gin");
CREATE INDEX IF NOT EXISTS "idx_index_gist_cc203a3ffdb7" ON "index" USING GIST ("gist");
CREATE INDEX IF NOT EXISTS "idx_index_sp_gist_939be3501a54" ON "index" USING SPGIST ("sp_gist");
CREATE INDEX IF NOT EXISTS "idx_index_hash_c08b870a8bf2" ON "index" USING HASH ("hash");
CREATE INDEX IF NOT EXISTS "idx_index_partial_f3f681bff192" ON "index" ("partial") WHERE ("id"=1);
CREATE INDEX IF NOT EXISTS "idx_index_expr_6b3aac52573a" ON "index" USING GIN ((TO_TSVECTOR('english',((COALESCE("title",'') || ' ') || COALESCE("body",'')))));
CREATE INDEX IF NOT EXISTS "idx_index_path_21f724a059db" ON "index" ("path" "varchar_pattern_ops");"""
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_m2m_no_auto_create():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_no_auto_create_m2m")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql.strip()
            == r"""CREATE TABLE "team" (
    "name" VARCHAR(50) NOT NULL PRIMARY KEY,
    "key" INT NOT NULL,
    "manager_id" VARCHAR(50) REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE INDEX "idx_team_manager_676134d72f2e" ON "team" ("manager_id", "key");
CREATE INDEX "idx_team_manager_ef8f69f87cd2" ON "team" ("manager_id", "name");
COMMENT ON COLUMN "team"."name" IS 'The TEAM name (and PK)';
COMMENT ON TABLE "team" IS 'The TEAMS!';
CREATE TABLE "tournament" (
    "tid" SMALLSERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "created" TIMESTAMPTZ NOT NULL
);
CREATE INDEX "idx_tournament_name_6fe200b8b694" ON "tournament" ("name");
COMMENT ON COLUMN "tournament"."name" IS 'Tournament name';
COMMENT ON COLUMN "tournament"."created" IS 'Created */''`/* datetime';
COMMENT ON TABLE "tournament" IS 'What Tournaments */''`/* we have';
CREATE TABLE "event" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "modified" TIMESTAMPTZ NOT NULL,
    "prize" DECIMAL(10,2),
    "token" VARCHAR(100) NOT NULL UNIQUE,
    "key" VARCHAR(100) NOT NULL,
    "tournament_id" SMALLINT NOT NULL REFERENCES "tournament" ("tid") ON DELETE CASCADE,
    CONSTRAINT "uid_event_tournam_a5b7304a103a" UNIQUE ("tournament_id", "key")
);
COMMENT ON COLUMN "event"."id" IS 'Event ID';
COMMENT ON COLUMN "event"."token" IS 'Unique token';
COMMENT ON COLUMN "event"."tournament_id" IS 'FK to tournament';
COMMENT ON TABLE "event" IS 'This table contains a list of all the events';
CREATE TABLE "teamevents" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "score" INT NOT NULL,
    "event_id" BIGINT NOT NULL REFERENCES "event" ("id") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    CONSTRAINT "uid_teamevents_team_id_9e89fcd1ffad" UNIQUE ("team_id", "event_id")
);
CREATE INDEX "idx_teamevents_event_i_6ad77ecabf77" ON "teamevents" ("event_id");
COMMENT ON TABLE "teamevents" IS 'How participants relate';
CREATE TABLE "team_team" (
    "team_rel_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE,
    "team_id" VARCHAR(50) NOT NULL REFERENCES "team" ("name") ON DELETE CASCADE
);
CREATE UNIQUE INDEX "uidx_team_team_team_re_d994dfe24491" ON "team_team" ("team_rel_id", "team_id");
CREATE INDEX "idx_team_team_team_id_d77a3bb352d4" ON "team_team" ("team_id");
""".strip()
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_pgfields_unsafe():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_postgres_fields")
        sql = Connections.get("default").get_schema_sql(safe=False)
        assert (
            sql
            == """CREATE TABLE "postgres_fields" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tsvector" TSVECTOR NOT NULL,
    "text_array" TEXT[] NOT NULL,
    "varchar_array" VARCHAR(32)[] NOT NULL,
    "int_array" INT[],
    "real_array" DOUBLE PRECISION[] NOT NULL
);
COMMENT ON COLUMN "postgres_fields"."real_array" IS 'this is array of real numbers';"""
        )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_asyncpg_pgfields_safe():
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_postgres_fields")
        sql = Connections.get("default").get_schema_sql(safe=True)
        assert (
            sql
            == """CREATE TABLE IF NOT EXISTS "postgres_fields" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tsvector" TSVECTOR NOT NULL,
    "text_array" TEXT[] NOT NULL,
    "varchar_array" VARCHAR(32)[] NOT NULL,
    "int_array" INT[],
    "real_array" DOUBLE PRECISION[] NOT NULL
);
COMMENT ON COLUMN "postgres_fields"."real_array" IS 'this is array of real numbers';"""
        )
    finally:
        await _teardown_hare()


# ============================================================================
# Schema-Qualified Table Tests
# ============================================================================


@pytest.mark.asyncio
async def test_sqlite_schema_qualified_ignores_schema():
    """SQLite should ignore Meta.schema and produce standard table names."""
    await _reset_hare()
    try:
        sqls = await _init_for_sqlite("tests.schema.models_schema_qualified", safe=True)
        sql = " ".join(sqls)
        # SQLite should NOT have schema-qualified names
        assert '"custom"."category"' not in sql
        # Should have regular quoted table names
        assert '"category"' in sql
        assert '"product"' in sql
        assert '"tag"' in sql
        assert "CREATE SCHEMA" not in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_sqlite_schema_qualified_models_run_queries():
    """SQLite's DDL creates a Meta.schema model's table unqualified, while every query used to
    address it as "custom"."category" - failing with "no such table" on the first INSERT."""
    from hare.contrib.test.helpers import hare_test_context

    async with hare_test_context(["tests.schema.models_schema_qualified"], db_url="sqlite://:memory:") as ctx:
        category_model = ctx.apps.get_model("models", "SchemaCategory")
        product_model = ctx.apps.get_model("models", "SchemaProduct")
        tag_model = ctx.apps.get_model("models", "SchemaTag")

        category = await category_model.objects.create(name="books")
        product = await product_model.objects.create(name="novel", category=category)
        tag = await tag_model.objects.create(label="fiction")
        await product.tags.add(tag)

        fetched_product = await product_model.objects.filter(category__name="books").select_related("category").get()
        assert fetched_product.category.name == "books"
        assert [row.label for row in await fetched_product.tags.all()] == ["fiction"]
        assert await tag_model.objects.filter(products__name="novel").count() == 1

        await product_model.objects.filter(pk=product.pk).update(name="poem")
        assert (await product_model.objects.get(pk=product.pk)).name == "poem"
        await category.delete()
        assert await product_model.objects.all().count() == 0


@pytest.mark.asyncio
async def test_generate_schemas_skips_unmanaged_models():
    """generate_schemas() used to create a Meta.managed = False model's table like any other."""
    from hare.contrib.test.helpers import hare_test_context

    async with hare_test_context(
        ["tests.schema.models_unmanaged"], db_url="sqlite://:memory:", connection_label="models"
    ):
        connection = Connections.get("models")
        sql = connection.get_schema_sql(safe=True)
        rows = await connection.execute_dicts("SELECT name FROM sqlite_master WHERE type = 'table'")

        assert 'CREATE TABLE IF NOT EXISTS "external_report"' not in sql
        assert 'CREATE TABLE IF NOT EXISTS "report_note"' in sql
        assert {row["name"] for row in rows} >= {"report_note"}
        assert "external_report" not in {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_asyncpg_schema_qualified_safe():
    """Postgres (asyncpg) should produce schema-qualified names with CREATE SCHEMA."""
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_schema_qualified")
        sql = Connections.get("default").get_schema_sql(safe=True)
        # Should have CREATE SCHEMA
        assert 'CREATE SCHEMA IF NOT EXISTS "custom";' in sql
        # Should have schema-qualified CREATE TABLE
        assert 'CREATE TABLE IF NOT EXISTS "custom"."category"' in sql
        assert 'CREATE TABLE IF NOT EXISTS "custom"."product"' in sql
        assert 'CREATE TABLE IF NOT EXISTS "custom"."tag"' in sql
        # FK should reference schema-qualified table
        assert 'REFERENCES "custom"."category"' in sql
        # M2M through table should be schema-qualified
        assert 'CREATE TABLE IF NOT EXISTS "custom"."product_tags"' in sql
        # M2M FK references should be schema-qualified
        assert 'REFERENCES "custom"."product"' in sql
        assert 'REFERENCES "custom"."tag"' in sql
        # Comments should use schema-qualified table
        assert 'COMMENT ON COLUMN "custom"."category"."name" IS \'Category name\'' in sql
        assert 'COMMENT ON TABLE "custom"."product" IS \'Products table\'' in sql
        # Unique index on M2M through table should use schema-qualified table
        assert 'ON "custom"."product_tags"' in sql
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_schema_qualified_single_schema_creation():
    """Only one CREATE SCHEMA per unique schema value."""
    await _reset_hare()
    try:
        await _init_for_asyncpg("tests.schema.models_schema_qualified")
        sql = Connections.get("default").get_schema_sql(safe=True)
        # All 3 models share schema "custom" — should create only once
        assert sql.count('CREATE SCHEMA IF NOT EXISTS "custom"') == 1
    finally:
        await _teardown_hare()


class GeneratedTriggerCounterSqlite(Model):
    """A model whose Meta.triggers entry must be created by generate_schemas() on SQLite."""

    id = fields.IntField(primary_key=True)
    n = fields.IntField(default=0)
    m = fields.IntField(default=0)

    class Meta:
        app = "generate_schema_triggers_sqlite"
        table = "generated_trigger_counter"
        triggers = (
            Trigger(
                name="trg_generated_counter",
                on=TriggerEvent.INSERT,
                body="UPDATE generated_trigger_counter SET m = NEW.n * 2 WHERE id = NEW.id;",
            ),
        )


class GeneratedTriggerCounterPostgres(Model):
    """The Postgres twin of GeneratedTriggerCounterSqlite - a trigger body isn't portable."""

    id = fields.IntField(primary_key=True)
    n = fields.IntField(default=0)
    m = fields.IntField(default=0)

    class Meta:
        app = "generate_schema_triggers_postgres"
        table = "generated_trigger_counter"
        triggers = (
            Trigger(
                name="trg_generated_counter",
                on=TriggerEvent.INSERT,
                timing=TriggerTiming.BEFORE,
                body="NEW.m := NEW.n * 2;\nRETURN NEW;",
            ),
        )


@pytest.mark.asyncio
async def test_generate_schemas_creates_meta_triggers():
    """generate_schemas() created the tables but never their Meta.triggers - a trigger the model
    declares silently didn't exist on a database set up without migrations."""
    from hare.contrib.test.helpers import hare_test_context

    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    is_sqlite = DatabaseUnderTest.get_engine_name(DatabaseUnderTest.get_dialect(db_url)) == "sqlite"
    app_label = "generate_schema_triggers_sqlite" if is_sqlite else "generate_schema_triggers_postgres"
    model_name = "GeneratedTriggerCounterSqlite" if is_sqlite else "GeneratedTriggerCounterPostgres"
    async with hare_test_context(
        ["tests.schema.test_generate_schema"], db_url=db_url, app_label=app_label, connection_label="models"
    ) as ctx:
        counter_model = ctx.apps.get_model(app_label, model_name)
        first_counter = await counter_model.objects.create(n=3)
        await first_counter.refresh_from_db()

        assert first_counter.m == 6
        assert "trg_generated_counter" in Connections.get("models").get_schema_sql(safe=False)

        await ctx.generate_schemas(safe=True)
        second_counter = await counter_model.objects.create(n=5)
        await second_counter.refresh_from_db()

        assert second_counter.m == 10

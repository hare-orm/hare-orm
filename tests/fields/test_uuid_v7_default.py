"""UuidV7() as a db_default: PostgreSQL writes uuidv7(); a dialect without it and the standard SQL refuse
it, and a migration file rebuilds it."""

from __future__ import annotations

import pytest

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import UnSupportedError
from hare.fields.db_defaults import RandomHex, UuidV7
from hare.migrations.writer.import_manager import ImportManager
from hare.migrations.writer.migration_writer import MigrationWriter


def test_postgresql_writes_uuidv7():
    assert UuidV7().get_sql(POSTGRESQL_DIALECT) == "uuidv7()"


def test_a_dialect_without_it_refuses_it():
    with pytest.raises(UnSupportedError, match="UuidV7"):
        UuidV7().get_sql(SQLITE_DIALECT)
    with pytest.raises(UnSupportedError, match="UuidV7"):
        UuidV7().get_sql()


def test_it_is_a_value_and_a_migration_rebuilds_it():
    assert UuidV7() == UuidV7()
    assert UuidV7() != RandomHex()
    assert len({UuidV7(), UuidV7()}) == 1
    imports = ImportManager()
    assert MigrationWriter.render_value(UuidV7(), imports) == "UuidV7()"

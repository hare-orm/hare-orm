import pytest

from hare import fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.dialects.postgresql.fields.ltree_field import LtreeField
from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.dialects.postgresql.fields.ranges import (
    BigIntRangeField,
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
)
from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField
from hare.exceptions import UnSupportedError
from hare.fields import ArrayField

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    ("make_field", "expected_sql_type"),
    [
        pytest.param(IntRangeField, "int4range", id="int_range"),
        pytest.param(BigIntRangeField, "int8range", id="big_int_range"),
        pytest.param(DecimalRangeField, "numrange", id="decimal_range"),
        pytest.param(DateRangeField, "daterange", id="date_range"),
        pytest.param(DateTimeRangeField, "tstzrange", id="datetime_range"),
        pytest.param(lambda: ArrayField(base_field=fields.TextField()), "TEXT[]", id="array"),
        pytest.param(PostGISField, "geography(Point,4326)", id="postgis"),
        pytest.param(TSVectorField, "TSVECTOR", id="tsvector"),
        pytest.param(HStoreField, "hstore", id="hstore"),
        pytest.param(CitextField, "CITEXT", id="citext"),
        pytest.param(LtreeField, "ltree", id="ltree"),
    ],
)
def test_a_postgresql_only_column_type_is_refused_on_another_dialect(make_field, expected_sql_type):
    """A Postgres-only column type resolved for another dialect's DDL (a model with such a field
    that ends up on a SQLite connection) must raise a clear error instead of silently handing back
    Postgres syntax for schema generation to choke on, and still resolves on PostgreSQL."""
    field = make_field()
    field.model_field_name = "column"
    with pytest.raises(UnSupportedError, match=f"{type(field).__name__}.*column.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == expected_sql_type

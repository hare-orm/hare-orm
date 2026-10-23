"""A value assigned in Python to a PostgreSQL field of the wrong shape is refused with a
ValidationError naming the field - the parsing of the database's own text form never sees it."""

import pytest

from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.exceptions import ValidationError
from hare.vectors import VectorField

pytestmark = pytest.mark.database_independent


def named(field, name):
    field.model_field_name = name
    return field


@pytest.mark.parametrize(
    ("make_field", "field_name", "value", "message"),
    [
        *[
            (HStoreField, "attributes", value, "attributes: expected a dict")
            for value in (["x"], 5, {"x"}, '"a"=>"1"')
        ],
        *[
            (PostGISField, "location", value, r"location: expected a \(latitude, longitude\) tuple")
            for value in (5, {"lat": 1})
        ],
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(make_field, field_name, value, message):
    with pytest.raises(ValidationError, match=message):
        named(make_field(), field_name).to_python(value)


def test_postgis_holds_a_point_assigned_as_a_list_as_a_tuple():
    assert named(PostGISField(), "location").to_python([55.75, 37.62]) == (55.75, 37.62)


def test_postgis_refuses_text_that_isnt_a_point():
    with pytest.raises(ValidationError, match="location"):
        named(PostGISField(), "location").to_python("not hex")


def test_vector_refuses_a_value_that_isnt_a_list():
    with pytest.raises(ValidationError, match="embedding: expected a list of floats"):
        named(VectorField(3), "embedding").to_python({1.0, 2.0})

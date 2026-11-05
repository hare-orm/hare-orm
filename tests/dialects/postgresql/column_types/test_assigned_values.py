"""A value assigned in Python to a PostgreSQL field of the wrong shape is refused with a
ValidationError naming the field - the parsing of the database's own text form never sees it."""

import pytest

from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.exceptions import ValidationError
from hare.gis import Point, PointField
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
        *[(PointField, "location", value, "location") for value in (5, {"lat": 1})],
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(make_field, field_name, value, message):
    with pytest.raises(ValidationError, match=message):
        named(make_field(), field_name).to_python(value)


def test_point_field_reads_geo_json_coordinates():
    assert named(PointField(), "location").to_python({"type": "Point", "coordinates": [37.62, 55.75]}) == Point(
        37.62, 55.75, srid=4326
    )


def test_point_field_refuses_text_that_is_not_a_geometry():
    with pytest.raises(ValidationError, match="location"):
        named(PointField(), "location").to_python("not a geometry")


def test_vector_refuses_a_value_that_isnt_a_list():
    with pytest.raises(ValidationError, match="embedding: expected a list/tuple of floats"):
        named(VectorField(3), "embedding").to_python({1.0, 2.0})

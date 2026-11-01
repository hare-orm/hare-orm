"""A filter on a literal annotation took a value of another type as it was: asyncpg refused the parameter
with an OperationalError, rust_pg and ClickHouse cast it on the server and matched, SQLite matched
nothing. The value goes through the literal's own field, as a field's value does."""

import pytest

from hare.exceptions import ValidationError
from hare.query.expressions import F, Value
from tests.shared_models import testmodels
from tests.shared_models.testmodels import Event, Tournament


@pytest.mark.asyncio
async def test_a_value_of_another_type_than_the_literal_is_refused_on_every_driver(db):
    await testmodels.IntFields.objects.create(intnum=1)
    annotated = testmodels.IntFields.objects.annotate(five=Value(5))
    assert await annotated.filter(five=5).count() == 1
    assert await annotated.filter(five__in=[4, 5]).count() == 1
    for refused in (lambda: annotated.filter(five="5").count(), lambda: annotated.filter(five__in=["5"]).count()):
        with pytest.raises(ValidationError, match="five: expected an int"):
            await refused()


@pytest.mark.asyncio
async def test_a_filter_through_a_relation_reads_the_related_field_beside_an_annotation_of_its_name(db):
    """The rest of the path was looked up among the filtered model's annotations - tournament__desc compared
    the annotation desc: no row, or "no field name on Tournament" for an annotation reading a field."""
    tournament = await Tournament.objects.create(id=1, name="cup", desc="about")
    final = await Event.objects.create(event_id=1, name="final", tournament=tournament)
    for annotation in (Value("wrong"), F("name")):
        events = Event.objects.annotate(desc=annotation).filter(tournament__desc="about")
        assert await events.values_list("event_id", flat=True) == [final.event_id]
    # A value naming an annotation still reads the annotation.
    assert await Event.objects.annotate(label=Value("about")).filter(tournament__desc=F("label")).values_list(
        "event_id", flat=True
    ) == [final.event_id]

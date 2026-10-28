"""Field contract matrix, the factories column: a factory of every field sample's model, declaring the sampled
field, builds an object, creates and saves one, and the saved row holds what a plain ``create()`` of the same
value saves; a relation sample's factory creates the related row too."""

import pytest

from tests.contracts.constants import SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.sample_database import SampleDatabase
from tests.contracts.sample_factories import SampleFactories

PLAIN_ROW_KEY_OFFSET = 1000


def is_same_value(first: object, second: object) -> bool:
    """Whether two read values are one - a NaN equals a NaN."""
    return first == second or repr(first) == repr(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [sample.get_parameter() for sample in FIELD_SAMPLES if sample.registered_live])
async def test_a_factory_builds_creates_and_saves_the_value_of_a_field(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    models = await SampleDatabase.create(contract_database, sample)
    row_model = models[-1]
    target_model = models[-2] if sample.related else None
    if sample.related:
        await target_model.objects.all().delete()

    values = sample.values if sample.values and not sample.related else (None,)
    for number, value in enumerate(values, 1):
        factory = SampleFactories.make_row_factory(sample, row_model, target_model, value)
        built = factory.build()
        assert isinstance(built, row_model)
        created = await factory.create(id=number)
        saved = await row_model.objects.get(pk=created.pk)
        if sample.related:
            assert await target_model.objects.exists()
        elif sample.values:
            plain = await row_model.objects.create(id=PLAIN_ROW_KEY_OFFSET + created.pk, **{sample.column_name: value})
            plain = await row_model.objects.get(pk=plain.pk)
            assert is_same_value(getattr(saved, sample.column_name), getattr(plain, sample.column_name)), value
        assert saved.pk == created.pk
    assert SAMPLED_FIELD_NAME in row_model._meta.fields_map

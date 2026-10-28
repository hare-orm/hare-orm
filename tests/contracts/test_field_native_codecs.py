"""Native and Python codecs agree on sampled fields and actual values returned by the driver."""

import pytest

from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.generated_field import GeneratedField
from tests.contracts.constants import GENERATED_VALUE_FIELD_NAME, SAMPLED_FIELD_NAME
from tests.contracts.field_samples import FIELD_SAMPLES
from tests.contracts.generated_field_contract import GeneratedFieldContract
from tests.contracts.sample_database import SampleDatabase
from tests.native_acceleration.test_field_codecs import assert_reads_match, assert_writes_match

REQUIRES = ("native",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample", [sample.get_parameter() for sample in FIELD_SAMPLES if sample.values and not sample.related]
)
async def test_sampled_fields_have_the_same_native_and_python_codecs(contract_database, sample):
    SampleDatabase.skip_unless_stored(sample)
    model = (await SampleDatabase.create(contract_database, GeneratedFieldContract.with_generated_copy(sample)))[-1]
    await SampleDatabase.create_rows(model, sample)
    connection = model.get_connection()
    table = model._meta.basetable
    field_names = (SAMPLED_FIELD_NAME, GENERATED_VALUE_FIELD_NAME)
    query = (
        connection.query_class.from_(table)
        .select(*(table[model._meta.fields_db_projection[name]] for name in field_names))
        .orderby(table[model._meta.fields_db_projection["id"]])
    )
    _, raw_rows = await connection.execute(*query.get_parameterized_sql(), returns_rows=True, rows_by_position=True)
    connections = [(str(connection.dialect.name), connection.dialect.types, connection.native_python_types)]
    for position, field_name in enumerate(field_names):
        assert_reads_match(model, field_name, [row[position] for row in raw_rows], connections=connections)
        field = model._meta.fields_map[field_name]
        assert_writes_match(
            field,
            list(sample.values),
            instance=model,
            connections=connections,
            compare_decoded=isinstance(GeneratedField.get_effective_field(field), EncryptedFieldBase),
        )

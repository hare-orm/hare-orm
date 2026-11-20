"""RETURNING identifies distinct-NULL inserts independently of conflicting updates."""

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.core.hare_context import HareContext
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.query.expressions import F
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest_asyncio.fixture(params=[False, True], ids=["scalar", "composite"])
@requires_features(supports_returning=True, supports_unique_constraints=True)
async def nullable_returning_record(contract_database, request, monkeypatch):
    composite = request.param
    publishing = [False]
    repeated_writes = []

    class NullableCode(fields.TextField):
        def to_db_value(self, value, instance):
            if publishing[0]:
                repeated_writes.append(value)
            return None if value == "NULL" else super().to_db_value(value, instance)

    class NullableReturningRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=False, source_field="number_db")
        if composite:
            region = fields.TextField(source_field="region_db")
            pk = fields.CompositePrimaryKey("region", "number")
            edition = fields.IntField()
        code = NullableCode(null=True, unique=not composite, source_field="code_db")
        amount = fields.IntField()
        stamp = fields.IntField(db_default=77, source_field="stamp_db")
        total = fields.GeneratedField(F("amount") * 2, output_field=fields.IntField(), source_field="total_db")

        class Meta:
            constraints = [UniqueConstraint(fields=("code", "edition"), name="code_edition")] if composite else []

    original = ReturnedRowsMatching.populate_returned_fields_from_returning_rows

    def publish(*args, **kwargs):
        publishing[0] = True
        try:
            return original(*args, **kwargs)
        finally:
            publishing[0] = False

    monkeypatch.setattr(ReturnedRowsMatching, "populate_returned_fields_from_returning_rows", staticmethod(publish))
    contract_database.register_live_models([NullableReturningRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    return NullableReturningRecord, composite, contract_database.get_connection(CONTRACT_CONNECTION), repeated_writes


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [1, 10])
@pytest.mark.parametrize("values", ["nulls", "converted-null", "repeated-input-key"])
async def test_upsert_returning_populates_every_distinct_null_input(nullable_returning_record, batch_size, values):
    model, composite, connection, repeated_writes = nullable_returning_record
    extra = {"region": "r", "edition": 1} if composite else {}
    await model.objects.create(number=100, code="X", amount=5, **extra)
    inputs = (
        [(7, "X", 20), (7, None, 30)]
        if values == "repeated-input-key"
        else [(3, None, 30), (2, "X", 20), (1, "NULL" if values == "converted-null" else None, 10)]
    )
    objects = [model(number=number, code=code, amount=amount, **extra) for number, code, amount in inputs]
    target = ["code", "edition"] if composite else ["code"]
    result = await model.objects.bulk_create(
        objects, update_fields=["amount"], on_conflict=target, returning=True, batch_size=batch_size
    )
    assert all(actual is obj for actual, obj in zip(result, objects, strict=True))
    assert [(obj.number, obj.total, obj.stamp, obj._saved_in_db) for obj in objects] == [
        (100 if code == "X" else number, amount * 2, 77, True) for number, code, amount in inputs
    ]
    assert repeated_writes == []
    expected = [
        {
            "number_db": 100 if code == "X" else number,
            "code_db": "X" if code == "X" else None,
            "amount": amount,
            "total_db": amount * 2,
            "stamp_db": 77,
        }
        for number, code, amount in inputs
    ]
    assert await connection.execute_dicts(
        f"SELECT number_db,code_db,amount,total_db,stamp_db FROM "
        f'"{HareContext.require_current().apps.get_binding(model).db_table}" ORDER BY number_db'
    ) == sorted(expected, key=lambda row: row["number_db"])


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
@pytest.mark.parametrize("batch_size", [1, 10])
@pytest.mark.parametrize("composite", [False, True], ids=["generated-key", "composite-key"])
async def test_ignored_conflicts_publish_database_chosen_keys(contract_database, batch_size, composite):
    class DefaultConflictRecord(Model):
        number = fields.IntField(primary_key=not composite, generated=not composite, source_field="number_db")
        if composite:
            region = fields.TextField(source_field="region_db")
            pk = fields.CompositePrimaryKey("region", "number")
            edition = fields.IntField(source_field="edition_db")
        code = fields.IntField(null=True, unique=not composite, db_default=5, source_field="code_db")
        amount = fields.IntField()
        total = fields.GeneratedField(F("amount") * 2, output_field=fields.IntField(), source_field="total_db")

        class Meta:
            constraints = [UniqueConstraint(fields=("code", "edition"), name="default_conflict")] if composite else []

    contract_database.register_live_models([DefaultConflictRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    objects = [
        DefaultConflictRecord(amount=7, **({"region": "r", "number": number, "edition": 1} if composite else {}))
        for number in (1, 2)
    ]
    result = await DefaultConflictRecord.objects.bulk_create(
        objects,
        ignore_conflicts=True,
        on_conflict=["code", "edition"] if composite else ["code"],
        batch_size=batch_size,
    )
    assert len(result) == 1 and result[0] is objects[0]
    assert objects[0]._saved_in_db and not objects[1]._saved_in_db
    assert objects[0].code == 5 and objects[0].total == 14
    if not composite:
        assert objects[0].pk is not None and objects[1].pk is None
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    assert await connection.execute_dicts(
        f"SELECT code_db,amount,total_db FROM "
        f'"{HareContext.require_current().apps.get_binding(DefaultConflictRecord).db_table}"'
    ) == [{"code_db": 5, "amount": 7, "total_db": 14}]

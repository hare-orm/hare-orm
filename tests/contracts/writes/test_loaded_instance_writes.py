"""Read paths and write modes preserve an object's explicit key and computed values."""

from contextlib import AsyncExitStack

import pytest

from hare import Model, fields
from hare.contrib.test import capture_queries, requires_features
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.exceptions import IntegrityError, NoValuesFetched, QueryError
from hare.instrumentation import Observers, RowsChanged
from hare.transactions import Transactions
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.writes.loaded_write_models import Book, Shelf


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", (None, 1), ids=("one-batch", "across-batches"))
@pytest.mark.parametrize("composite", (False, True), ids=("scalar", "composite"))
@pytest.mark.parametrize("key_kind", ("citext", "exact", "nan", "distinct"))
async def test_bulk_update_refuses_database_equivalent_keys_before_writing(
    contract_database, batch_size, composite, key_kind
):
    class Entry(Model):
        key_type = fields.FloatField if key_kind == "nan" else CitextField
        code = key_type(primary_key=not composite)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "code")
        amount = fields.IntField()
        version = fields.IntField(default=0)

        class Meta:
            table = "bulk_update_key_equality"
            optimistic_lock_field = "version"

    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if key_kind != "nan":
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS citext")
    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    first, other = (float("nan"), 3.5) if key_kind == "nan" else ("Alpha", "Beta")
    await Entry.objects.create(code=first, amount=11)
    await Entry.objects.create(code=other, amount=22)
    left = await Entry.objects.get(amount=11)
    right = await Entry.objects.get(amount=22 if key_kind == "distinct" else 11)
    if key_kind == "citext":
        right.code = "aLPHa"
        assert await Entry.objects.filter(pk=right.pk).count() == 1
    left.amount, right.amount = 111, 222
    update = Entry.objects.bulk_update([left, right], fields=["amount"], batch_size=batch_size)
    stored_rows = "SELECT amount, version FROM bulk_update_key_equality ORDER BY amount"

    if key_kind == "distinct":
        assert await update == 2
        assert (left.version, right.version) == (1, 1)
        assert await connection.execute_dicts(stored_rows) == [
            {"amount": 111, "version": 1},
            {"amount": 222, "version": 1},
        ]
    else:
        async with capture_queries() as captured:
            with pytest.raises(QueryError, match="same primary key"):
                await update
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in captured.queries)
        assert (left.version, right.version) == (0, 0)
        assert await connection.execute_dicts(stored_rows) == [
            {"amount": 11, "version": 0},
            {"amount": 22, "version": 0},
        ]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("returning", (False, True), ids=("no-returning", "returning"))
@pytest.mark.parametrize("composite", (False, True), ids=("scalar-target", "composite-target"))
@pytest.mark.parametrize(
    ("batch_size", "external_target"),
    ((1, False), (3, False), (3, True)),
    ids=("separate-batches", "same-batch", "external-constraint"),
)
async def test_bulk_create_applies_database_equivalent_upsert_keys_in_input_order(
    contract_database, returning, composite, batch_size, external_target
):
    target = ("region", "code") if composite else ("code",)

    class Entry(Model):
        id = fields.IntField(primary_key=True)
        code = CitextField()
        region = fields.IntField(default=7)
        amount = fields.IntField()

        class Meta:
            table = "bulk_create_key_equality"
            constraints = [] if external_target else [UniqueConstraint(fields=target, name="bulk_create_key_target")]

    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS citext")
    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    if external_target:
        columns = "region, code" if composite else "code"
        await connection.execute_script(
            f"ALTER TABLE bulk_create_key_equality ADD CONSTRAINT bulk_create_key_target UNIQUE ({columns})"
        )
        conflict = {"on_conflict_constraint": "bulk_create_key_target"}
    else:
        conflict = {"on_conflict": list(target)}
    objects = [
        Entry(id=id_, code=code, amount=amount)
        for id_, code, amount in ((1, "Alpha", 11), (2, "Beta", 22), (3, "aLPHa", 33))
    ]
    assert (
        await Entry.objects.bulk_create(
            objects, update_fields=["amount"], returning=returning, batch_size=batch_size, **conflict
        )
        == objects
    )
    assert await connection.execute_dicts(
        "SELECT id, code, region, amount FROM bulk_create_key_equality ORDER BY id"
    ) == [
        {"id": 1, "code": "Alpha", "region": 7, "amount": 33},
        {"id": 2, "code": "Beta", "region": 7, "amount": 22},
    ]
    if returning:
        assert [obj.pk for obj in objects] == [1, 2, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("read_path", ("get", "iterator", "select_related", "prefetch_related", "reassigned"))
@pytest.mark.parametrize("write_path", ("insert", "ignore", "upsert", "upsert_returning", "copy"))
async def test_loaded_key_survives_bulk_create(contract_database, read_path, write_path):
    features = contract_database.get_connection().features
    if write_path in {"ignore", "upsert", "upsert_returning"} and not features.supports_insert_conflicts:
        pytest.skip("conflict clauses are not supported")
    if write_path == "copy" and not features.supports_copy:
        pytest.skip("COPY is not supported")
    contract_database.register_live_models([Shelf, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Shelf.objects.create(id=1)
    await Book.objects.bulk_create([Book(id=key, amount=key * 10, shelf_id=1) for key in (1, 2, 3)])
    query = Book.objects.filter(id=3)
    if read_path == "iterator":
        obj = [obj async for obj in query.iterator(chunk_size=1)][0]
    elif read_path in {"select_related", "prefetch_related"}:
        obj = await getattr(query, read_path)("shelf").get()
    else:
        obj = await query.get()
    if read_path == "reassigned":
        obj.pk = 40
    target_key = obj.pk
    obj.amount = 99
    options = {}
    if write_path == "ignore":
        options["ignore_conflicts"] = True
    elif write_path.startswith("upsert"):
        options.update(update_fields=["amount"], on_conflict=["id"], returning=write_path == "upsert_returning")
    elif write_path == "copy":
        options["use_copy"] = True
    expected = [(1, 10), (2, 20), (3, 30)]
    raises_duplicate = write_path in {"insert", "copy"} and features.supports_unique_constraints and target_key == 3
    if raises_duplicate:
        with pytest.raises(IntegrityError):
            await Book.objects.bulk_create([obj], **options)
    else:
        await Book.objects.bulk_create([obj], **options)
        if write_path.startswith("upsert") and target_key == 3:
            expected[-1] = (3, 99)
        elif write_path != "ignore" or target_key == 40:
            expected.append((target_key, 99))
        if write_path != "ignore":
            try:
                assert obj.doubled == 198
            except NoValuesFetched:
                assert write_path != "upsert_returning"
    assert obj.pk == target_key
    assert sorted(await Book.objects.values_list("id", "amount")) == sorted(expected)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
@pytest.mark.parametrize("nested", (False, True))
@pytest.mark.parametrize("batch_size", (None, 1))
async def test_caught_bulk_insert_failure_preserves_only_the_callers_rows(contract_database, nested, batch_size):
    contract_database.register_live_models([Shelf, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Shelf.objects.create(id=1)
    await Book.objects.create(id=1, amount=10, shelf_id=1)
    fresh = Book(id=2, amount=20, shelf_id=1)
    duplicate = Book(id=1, amount=99, shelf_id=1)
    reported = []
    Observers.observe(RowsChanged, reported.append, models=[Book])
    async with Transactions.atomic(CONTRACT_CONNECTION):
        await Book.objects.create(id=3, amount=30, shelf_id=1)
        async with AsyncExitStack() as stack:
            if nested:
                await stack.enter_async_context(Transactions.atomic(CONTRACT_CONNECTION))
            with pytest.raises(IntegrityError):
                await Book.objects.bulk_create([fresh, duplicate], batch_size=batch_size)
            assert await Book.objects.order_by("id").values_list("id", "amount") == [(1, 10), (3, 30)]
    assert not fresh._saved_in_db
    assert not duplicate._saved_in_db
    assert [event.pks for event in reported] == [(3,)]
    assert await Book.objects.order_by("id").values_list("id", "amount") == [(1, 10), (3, 30)]

import asyncio
from collections import UserList
from copy import copy
from uuid import UUID

import pytest
import pytest_asyncio

from hare import Model, Transactions, fields
from hare.contrib.test import capture_queries, hare_test_context, requires_features
from hare.core.hare_context import HareContext
from hare.exceptions import IntegrityError, ValidationError
from hare.fields.database_default import DatabaseDefault
from hare.instrumentation import Observers, QueryWrapper
from hare.models.tenancy.tenancy import Tenancy
from tests.contracts import cascade_default_models
from tests.contracts.callable_default_models import (
    AsyncDefaultChild,
    AsyncDefaultDetail,
    AsyncDefaultKeyRecord,
    AsyncDefaultRecord,
    DefaultTarget,
    SyncDefaultChild,
    SyncDefaultDetail,
)
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.default_key_values import DefaultKeyValues
from tests.utils.async_events import AsyncEvents
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["create", "save", "bulk"])
async def test_explicit_null_and_value_do_not_invoke_defaults(contract_database, write):
    calls = []

    def default():
        calls.append("default")
        return 17

    class Entry(Model):
        number = fields.IntField(primary_key=True, generated=False)
        python_value = fields.IntField(null=True, default=default)
        database_value = fields.IntField(null=True, db_default=19)

        class Meta:
            table = "default_entry"

    contract_database.register_live_models([Entry], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    connection = contract_database.get_connection()
    returns_values = connection.features.supports_returning
    values = [
        {"number": 1},
        {"number": 2, "python_value": None, "database_value": None},
        {"number": 3, "python_value": 38, "database_value": 38},
    ]
    if write == "create":
        objects = [await Entry.objects.create(**row) for row in values]
    else:
        objects = [Entry(**row) for row in values]
        if write == "bulk":
            await Entry.objects.bulk_create(objects, returning=returns_values)
        else:
            for obj in objects:
                await obj.save()

    expected = [(17, 19), (None, None), (38, 38)]
    assert [obj.python_value for obj in objects] == [17, None, 38]
    if not returns_values:
        assert isinstance(objects[0].database_value, DatabaseDefault)
        await objects[0].refresh_from_db()
    assert [obj.database_value for obj in objects] == [19, None, 38]
    assert await connection.execute_dicts(
        "SELECT python_value, database_value FROM default_entry ORDER BY number"
    ) == [
        {"python_value": python_value, "database_value": database_value} for python_value, database_value in expected
    ]
    assert await Entry.objects.order_by("number").values_list("python_value", "database_value") == expected
    assert calls == ["default"]


@pytest.mark.asyncio
@pytest.mark.parametrize("composite_target", [False, True], ids=["scalar-target", "composite-target"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync-default", "async-default"])
@pytest.mark.parametrize("explicit", [False, True], ids=["fresh-default", "explicit-key"])
async def test_clone_uses_one_relation_default_for_a_primary_key(
    contract_database, monkeypatch, composite_target, asynchronous, explicit
):
    monkeypatch.setattr(DefaultKeyValues, "calls", 0)
    monkeypatch.setattr(DefaultKeyValues, "returns_null", False)
    monkeypatch.setattr(DefaultKeyValues, "key_container", tuple)
    connection = contract_database.get_connection()
    default = (
        (DefaultKeyValues.get_async_key if asynchronous else DefaultKeyValues.get_key)
        if composite_target
        else (DefaultKeyValues.get_async_scalar_key if asynchronous else DefaultKeyValues.get_scalar_key)
    )

    class Author(Model):
        number = fields.IntField(primary_key=not composite_target, generated=False)
        if composite_target:
            region = fields.IntField()
            pk = fields.CompositePrimaryKey("region", "number")

    class Book(Model):
        author = fields.ForeignKeyField(
            f"{CONTRACT_APP}.Author",
            default=default,
            db_constraint=connection.features.supports_foreign_keys,
        )
        sequence = fields.IntField(default=2)
        pk = fields.CompositePrimaryKey("author", "sequence")

    contract_database.register_live_models([Author, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    target = await Author.objects.create(
        number=101 if composite_target else 1, **({"region": 1} if composite_target else {})
    )
    source = await Author.objects.create(
        number=109 if composite_target else 9, **({"region": 9} if composite_target else {})
    )
    original = await Book.objects.create(author=source, sequence=1)
    given_key = (*target.pk, 2) if composite_target else (target.pk, 2)
    clone = original.clone(pk=given_key) if explicit else original.clone()
    await clone.save()
    assert clone.pk == given_key
    assert DefaultKeyValues.calls == (0 if explicit else 1)
    assert original.pk == ((*source.pk, 1) if composite_target else (source.pk, 1))
    assert await Book.objects.count() == 2


@pytest_asyncio.fixture
async def default_rows(monkeypatch):
    monkeypatch.setattr(DefaultKeyValues, "calls", 0)
    monkeypatch.setattr(DefaultKeyValues, "returns_null", False)
    monkeypatch.setattr(DefaultKeyValues, "key_container", tuple)
    async with hare_test_context(
        ["tests.contracts.callable_default_models"], db_url=DatabaseUnderTest.get_url()
    ) as context:
        yield context


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment", ["omitted", "explicit-none", "late-none"])
async def test_async_primary_key_defaults_normalize_returned_values(default_rows, assignment):
    obj = AsyncDefaultKeyRecord(**({"id": None} if assignment == "explicit-none" else {}))
    if assignment == "late-none":
        obj.id = None
    await obj.save()
    assert isinstance(obj.id, UUID)
    assert obj.id == UUID(int=1)
    assert (await AsyncDefaultKeyRecord.objects.get(pk=obj.id)).id == obj.id


@pytest_asyncio.fixture(
    params=[
        (cascade_default_models.SyncCascadeDefaultTarget, cascade_default_models.SyncCascadeChild),
        (cascade_default_models.AsyncCascadeDefaultTarget, cascade_default_models.AsyncCascadeChild),
        (cascade_default_models.SyncScalarDefaultTarget, cascade_default_models.SyncScalarCascadeChild),
        (cascade_default_models.AsyncScalarDefaultTarget, cascade_default_models.AsyncScalarCascadeChild),
    ],
    ids=["composite-sync", "composite-async", "single-sync", "single-async"],
)
async def cascade_default_rows(request, monkeypatch):
    target, child = request.param
    monkeypatch.setattr(DefaultKeyValues, "calls", 0)
    monkeypatch.setattr(DefaultKeyValues, "returns_null", False)
    monkeypatch.setattr(DefaultKeyValues, "key_container", tuple)
    async with hare_test_context(["tests.contracts.cascade_default_models"], db_url=DatabaseUnderTest.get_url()):
        yield target, child


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["hard_delete", "delete"])
@pytest.mark.parametrize("returns_null", [False, True])
async def test_python_set_default_awaits_its_value_and_clears_nullable_composite_keys(
    cascade_default_rows, method, returns_null, monkeypatch
):
    target_model, child_model = cascade_default_rows
    monkeypatch.setattr(DefaultKeyValues, "returns_null", returns_null)
    composite = HareContext.require_current().apps.get_binding(target_model).has_composite_primary_key
    await target_model.objects.create(**({"region": 1, "number": 101} if composite else {"id": 1}))
    target = await target_model.objects.create(**({"region": 9, "number": 109} if composite else {"id": 9}))
    await child_model.objects.create(id=1, parent=target)
    await getattr(target, method)()
    fetched = await child_model.objects.get(pk=1)
    relation = HareContext.require_current().apps.get_binding(child_model).fields_map["parent"]
    expected = (None,) * len(relation.source_fields) if returns_null else ((1, 101) if composite else (1,))
    assert tuple(getattr(fetched, name) for name in relation.source_fields) == expected
    assert DefaultKeyValues.calls == 1
    assert not await target_model.objects.filter(pk=target.pk).exists()


@pytest.fixture(params=[SyncDefaultChild, AsyncDefaultChild, SyncDefaultDetail, AsyncDefaultDetail])
def default_model(request, default_rows):
    if (
        request.param in (SyncDefaultDetail, AsyncDefaultDetail)
        and not default_rows.get_connection().features.supports_unique_constraints
    ):
        pytest.skip("The database does not enforce one-to-one uniqueness")
    return request.param


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "save", "bulk"])
@pytest.mark.parametrize("returns_null", [False, True])
async def test_a_callable_relation_default_supplies_one_complete_key(default_model, path, returns_null, monkeypatch):
    monkeypatch.setattr(DefaultKeyValues, "returns_null", returns_null)
    model = default_model
    if path == "create":
        obj = await model.objects.create(id=1)
    else:
        obj = model(id=1)
        if path == "save":
            await obj.save()
        else:
            await model.objects.bulk_create([obj])
    expected = (None, None) if returns_null else (1, 101)
    assert (obj.parent_region, obj.parent_number) == expected
    assert DefaultKeyValues.calls == 1
    fetched = await model.objects.get(pk=1)
    assert (fetched.parent_region, fetched.parent_number) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assigned", ["first", "both", "relation", "null", "late-first", "late-both", "late-relation", "late-null"]
)
async def test_explicit_keys_replace_only_their_pending_default_components(default_model, assigned):
    model = default_model
    parent = await DefaultTarget.objects.create(region=9, number=109)
    arguments = {"parent_region": 9}
    if assigned.endswith("both"):
        arguments["parent_number"] = 109
    elif assigned.endswith("relation"):
        arguments = {"parent": parent}
    elif assigned.endswith("null"):
        arguments = {"parent": None}
    if assigned.startswith("late-"):
        obj = model(id=1)
        initial_calls = DefaultKeyValues.calls
        for name, value in arguments.items():
            setattr(obj, name, value)
    else:
        obj = model(id=1, **arguments)
        initial_calls = 0
    await obj.save()
    expected_number = 101 if assigned.endswith("first") else 109
    expected = (None, None) if assigned.endswith("null") else (9, expected_number)
    assert (obj.parent_region, obj.parent_number) == expected
    assert DefaultKeyValues.calls == (1 if assigned.endswith("first") else initial_calls)
    fetched = await model.objects.get(pk=1)
    assert (fetched.parent_region, fetched.parent_number) == expected


@pytest.mark.asyncio
async def test_an_explicit_async_column_keeps_its_value_beside_a_relation_default(default_model):
    obj = default_model(id=1, parent_region=DefaultKeyValues.get_number)
    await obj.save()
    assert (obj.parent_region, obj.parent_number) == (7, 101)
    assert DefaultKeyValues.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [list, UserList])
async def test_callable_relation_defaults_keep_indexable_return_types(default_model, container, monkeypatch):
    monkeypatch.setattr(DefaultKeyValues, "key_container", container)
    obj = await default_model.objects.create(id=1)
    assert (obj.parent_region, obj.parent_number) == (1, 101)
    assert DefaultKeyValues.calls == 1


@pytest.mark.asyncio
async def test_construct_calls_a_sync_relation_default_once_and_leaves_async_defaults_unset(default_model):
    obj = default_model.construct(id=1)
    is_async = default_model in (AsyncDefaultChild, AsyncDefaultDetail)
    assert (obj.parent_region, obj.parent_number) == ((None, None) if is_async else (1, 101))
    assert DefaultKeyValues.calls == (0 if is_async else 1)
    assert not obj._await_when_save


@pytest.mark.asyncio
async def test_copied_instances_evaluate_pending_relation_defaults_independently(default_rows):
    original = AsyncDefaultChild(id=1)
    cloned = copy(original)
    cloned.id = 2
    await AsyncDefaultChild.objects.bulk_create([original, cloned])
    assert DefaultKeyValues.calls == 2
    assert (original.parent_region, original.parent_number) == (1, 101)
    assert (cloned.parent_region, cloned.parent_number) == (2, 102)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "save", "bulk", "callable-kwargs"])
async def test_async_default_results_use_field_normalization(default_rows, path):
    if path == "create":
        obj = await AsyncDefaultRecord.objects.create(id=1)
    else:
        kwargs = {}
        if path == "callable-kwargs":
            kwargs = {
                "number": DefaultKeyValues.get_number,
                "token": DefaultKeyValues.get_token,
            }
        obj = AsyncDefaultRecord(id=1, **kwargs)
        if path == "bulk":
            await AsyncDefaultRecord.objects.bulk_create([obj])
        else:
            await obj.save()
    fetched = await AsyncDefaultRecord.objects.get(pk=1)
    for instance in (obj, fetched):
        assert instance.number == 7 and type(instance.number) is int
        assert instance.token == UUID(int=1)


@pytest_asyncio.fixture(params=(False, True), ids=("single-key", "composite-key"))
async def preparation_model(contract_database, request):
    async def automatic_value():
        return 73

    class Tally(Model):
        number = fields.IntField(primary_key=not request.param, generated=False)
        if request.param:
            revision = fields.IntField()
            pk = fields.CompositePrimaryKey("number", "revision")
        tenant = fields.IntField()
        amount = fields.IntField(default=1)
        automatic = fields.IntField(default=automatic_value)

        class Meta:
            tenant_field = "tenant"
            track_dirty_fields = True

    contract_database.register_live_models([Tally], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    yield Tally


@pytest.mark.asyncio
async def test_failed_bulk_preparation_restores_defaults_pending_work_and_tenant(preparation_model):
    model = preparation_model
    objs = [
        model(
            pk=(index, 1) if HareContext.require_current().apps.get_binding(model).has_composite_primary_key else index
        )
        for index in (1, 2)
    ]
    pending = [dict(obj._await_when_save) for obj in objs]
    objs[1].amount = "invalid"
    with Tenancy.scope(42):
        async with capture_queries(using=CONTRACT_CONNECTION) as queries:
            with pytest.raises(ValidationError):
                await model.objects.bulk_create(objs, batch_size=1, returning=False)
        assert queries.count == 0
    for obj, previous in zip(objs, pending, strict=True):
        assert obj.tenant is None
        assert not hasattr(obj, "automatic")
        assert dict(obj._await_when_save) == previous
        assert not obj._saved_in_db
    assert await model.objects.all_tenants().count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("save", "bulk"))
async def test_cancelled_default_preparation_can_be_retried(preparation_model, operation):
    started, released = asyncio.Event(), asyncio.Event()

    async def delayed_default():
        started.set()
        await released.wait()
        return 73

    model = preparation_model
    obj = model(
        pk=(1, 1) if HareContext.require_current().apps.get_binding(model).has_composite_primary_key else 1,
        automatic=delayed_default,
    )
    pending = dict(obj._await_when_save)
    completes_whole_write = operation == "bulk" and not model.get_connection().features.supports_transactions
    with Tenancy.scope(42):
        async with capture_queries(using=CONTRACT_CONNECTION) as queries:
            task = asyncio.ensure_future(
                obj.save() if operation == "save" else model.objects.bulk_create([obj], returning=False)
            )
            try:
                await AsyncEvents.wait_for(started, task)
                task.cancel()
                if completes_whole_write:
                    # Without transactions the whole bulk write drains before cancellation
                    # returns. Its default must be allowed to finish, like a running statement.
                    released.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        if completes_whole_write:
            assert queries.count > 0
            assert obj._saved_in_db
            assert not obj._await_when_save
            stored = await model.objects.get(pk=obj.pk)
            assert (stored.tenant, stored.automatic) == (42, 73)
            return
        assert queries.count == 0
        assert obj.tenant is None
        assert not hasattr(obj, "automatic")
        assert dict(obj._await_when_save) == pending
        released.set()
        await obj.save()
        stored = await model.objects.get(pk=obj.pk)
        assert (stored.tenant, stored.automatic) == (42, 73)


@pytest.mark.asyncio
async def test_rollback_restores_automatic_preparation_for_retry(preparation_model):
    model = preparation_model
    if not model.get_connection().features.supports_transactions:
        pytest.skip("The dialect has no transactions")
    obj = model(pk=(1, 1) if HareContext.require_current().apps.get_binding(model).has_composite_primary_key else 1)
    pending = dict(obj._await_when_save)
    with Tenancy.scope(42):
        with pytest.raises(RuntimeError, match="undo write"):
            async with Transactions.atomic(CONTRACT_CONNECTION):
                await obj.save()
                assert (obj.tenant, obj.automatic) == (42, 73)
                raise RuntimeError("undo write")
        assert obj.tenant is None
        assert not hasattr(obj, "automatic")
        assert dict(obj._await_when_save) == pending
        assert not obj._saved_in_db
        assert await model.objects.count() == 0
        await obj.save()
        assert (await model.objects.get(pk=obj.pk)).automatic == 73


@pytest_asyncio.fixture(params=(False, True), ids=("single-key", "composite-key"))
async def pending_key_model(contract_database, request):
    calls = []

    async def automatic_key():
        calls.append("key")
        return 71

    class Tally(Model):
        number = fields.IntField(primary_key=not request.param, generated=False, default=automatic_key)
        if request.param:
            revision = fields.IntField(default=3)
            pk = fields.CompositePrimaryKey("revision", "number")
        amount = fields.IntField(default=1)
        note = fields.IntField(default=0)

        class Meta:
            table = "pending_key_record"

    contract_database.register_live_models([Tally], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    yield Tally, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("save", "bulk"))
@pytest.mark.parametrize("failure", ("validation", "sql", "rollback"))
async def test_pending_primary_keys_survive_failed_writes_and_can_be_retried(pending_key_model, operation, failure):
    model, calls = pending_key_model
    connection = model.get_connection()
    if failure == "sql" and not connection.features.supports_unique_constraints:
        pytest.skip("The dialect does not enforce primary-key uniqueness")
    if failure == "rollback" and not connection.features.supports_transactions:
        pytest.skip("The dialect has no transactions")
    obj = model()
    if failure == "validation":
        obj.amount = "invalid"
    pending = dict(obj._await_when_save)
    if failure == "sql":
        await model.objects.create(number=71)
    expected_error = (
        ValidationError if failure == "validation" else IntegrityError if failure == "sql" else RuntimeError
    )
    with pytest.raises(expected_error):
        if failure == "rollback":
            async with Transactions.atomic(CONTRACT_CONNECTION):
                if operation == "save":
                    await obj.save()
                else:
                    await model.objects.bulk_create([obj], returning=False)
                raise RuntimeError("undo write")
        elif operation == "save":
            await obj.save()
        else:
            await model.objects.bulk_create([obj], returning=False)
    assert "number" not in obj.__dict__
    assert dict(obj._await_when_save) == pending
    assert not obj._saved_in_db
    assert calls == ["key"]
    if failure == "sql":
        await model.objects.filter(number=71).delete()
    assert await model.objects.count() == 0
    obj.amount = 1
    if operation == "save":
        await obj.save()
    else:
        await model.objects.bulk_create([obj], returning=False)
    assert calls == ["key", "key"]
    assert obj.pk == (
        (3, 71) if HareContext.require_current().apps.get_binding(model).has_composite_primary_key else 71
    )
    assert (await model.objects.get(pk=obj.pk)).amount == 1


@pytest.mark.asyncio
async def test_returning_accepts_a_primary_key_filled_by_an_async_default(pending_key_model):
    model, calls = pending_key_model
    if not model.get_connection().features.supports_returning:
        pytest.skip("The dialect has no RETURNING")
    obj = model()
    await model.objects.bulk_create([obj], returning=True)
    assert calls == ["key"]
    assert (await model.objects.get(pk=obj.pk)).amount == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", (False, True))
async def test_update_or_create_evaluates_defaults_inside_its_write_transaction(pending_key_model, existing):
    model, _calls = pending_key_model
    if not model.get_connection().features.supports_transactions:
        pytest.skip("The dialect has no transactions")
    if existing:
        await model.objects.create(number=71, amount=1)
    checks = []

    async def automatic_amount():
        checks.append(model.get_connection(for_write=True).is_transaction_client)
        return 9

    obj, created = await model.objects.update_or_create(number=71, defaults={"amount": automatic_amount})
    assert created is not existing
    assert checks == [True]
    assert (await model.objects.get(pk=obj.pk)).amount == 9


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_conflicting_update_or_create_locks_the_row_until_its_update_finishes(pending_key_model):
    model, _calls = pending_key_model
    connection = model.get_connection()
    first_entered, first_release, second_entered, second_release, dispatched = (asyncio.Event() for _ in range(5))
    calls = []

    class Dispatch(QueryWrapper):
        async def around(self, call, proceed):
            if (
                call.sql.lstrip().upper().startswith("UPDATE")
                and "pending_key_record" in call.sql
                and '"note"' in call.sql
                and '"amount"' not in call.sql
            ):
                dispatched.set()
            return await proceed()

    async def automatic_amount():
        calls.append(1)
        if len(calls) == 1:
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()
        return 9

    wrapper = Dispatch()
    Observers.wrap_queries(wrapper)
    operation = writer = None
    try:
        operation = asyncio.create_task(
            model.objects.update_or_create(number=1, defaults={"amount": automatic_amount})
        )
        await AsyncEvents.wait_for(first_entered, operation)
        await model.objects.create(number=1, amount=1, note=5)
        first_release.set()
        await AsyncEvents.wait_for(second_entered, operation)
        writer = asyncio.ensure_future(model.objects.filter(number=1).update(note=7))
        await dispatched.wait()
        observed_lock = False
        for _attempt in range(100):
            locks = await connection.execute_dicts(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND state='active' AND wait_event_type='Lock' "
                "AND lower(query) LIKE 'update%pending_key_record%note%'"
            )
            if locks:
                observed_lock = True
                break
            if writer.done():
                break
        assert observed_lock or writer.done(), "The competing UPDATE neither blocked nor completed"
        if not observed_lock:
            await writer
        second_release.set()
        _obj, created = await operation
        await writer
        assert not created
        assert observed_lock
        assert await model.objects.values_list("amount", "note") == [(9, 7)]
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(*(task for task in (operation, writer) if task is not None), return_exceptions=True)
        Observers.unwrap_queries(wrapper)

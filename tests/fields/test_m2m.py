import pytest

from hare import prefetch_related_objects
from hare.contrib.test import requires_features
from hare.exceptions import (
    NoValuesFetched,
    QueryError,
    ValidationError,
)
from hare.fields import ManyToManyField
from hare.transactions.transactions import Transactions
from tests import testmodels


@pytest.mark.asyncio
async def test_empty(db):
    """Test creating M2M model without relations."""
    one = await testmodels.M2MOne.objects.create()
    assert await one.two.all().count() == 0


@pytest.mark.asyncio
async def test__add(db):
    """Test adding a related object via M2M relation."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two)
    assert await one.two == [two]
    assert await two.one == [one]


@pytest.mark.asyncio
async def test__add__nothing(db):
    """Test adding nothing to M2M relation."""
    one = await testmodels.M2MOne.objects.create(name="One")
    await one.two.add()
    assert await one.two.all().count() == 0


@pytest.mark.asyncio
async def test__add__reverse(db):
    """Test adding via reverse M2M relation."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    await two.one.add(one)
    assert await one.two == [two]
    assert await two.one == [one]


@pytest.mark.asyncio
async def test__add__many(db):
    """Test adding same object multiple times (should be idempotent)."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two)
    await one.two.add(two)
    await two.one.add(one)
    assert await one.two == [two]
    assert await two.one == [one]


@pytest.mark.asyncio
async def test__add__two(db):
    """Test adding multiple related objects at once."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two1 = await testmodels.M2MTwo.objects.create(name="Two")
    two2 = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two1, two2)
    assert await one.two == [two1, two2]
    assert await two1.one == [one]
    assert await two2.one == [one]


@pytest.mark.asyncio
async def test__remove(db):
    """Test removing one related object from M2M relation."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two1 = await testmodels.M2MTwo.objects.create(name="Two")
    two2 = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two1, two2)
    await one.two.remove(two1)
    assert await one.two == [two2]
    assert await two1.one == []
    assert await two2.one == [one]


@pytest.mark.asyncio
async def test__remove__many(db):
    """Test removing multiple related objects at once."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two1 = await testmodels.M2MTwo.objects.create(name="Two1")
    two2 = await testmodels.M2MTwo.objects.create(name="Two2")
    two3 = await testmodels.M2MTwo.objects.create(name="Two3")
    await one.two.add(two1, two2, two3)
    await one.two.remove(two1, two2)
    assert await one.two == [two3]
    assert await two1.one == []
    assert await two2.one == []
    assert await two3.one == [one]


@pytest.mark.asyncio
async def test__remove__blank(db):
    """Test that removing nothing raises OperationalError."""
    one = await testmodels.M2MOne.objects.create(name="One")
    with pytest.raises(QueryError, match=r"remove\(\) called on no instances"):
        await one.two.remove()


@pytest.mark.asyncio
async def test__clear(db):
    """Test clearing all related objects from M2M relation."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two1 = await testmodels.M2MTwo.objects.create(name="Two")
    two2 = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two1, two2)
    await one.two.clear()
    assert await one.two == []
    assert await two1.one == []
    assert await two2.one == []


@pytest.mark.asyncio
async def test__uninstantiated_add(db):
    """Test that adding to unsaved model raises OperationalError."""
    one = testmodels.M2MOne(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    with pytest.raises(QueryError, match=r"You should first call .save\(\) on <M2MOne>"):
        await one.two.add(two)


@pytest.mark.asyncio
async def test__add_uninstantiated(db):
    """Test that adding unsaved model raises OperationalError."""
    one = testmodels.M2MOne(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    with pytest.raises(QueryError, match=r"You should first call .save\(\) on <M2MOne>"):
        await two.one.add(one)


@pytest.mark.asyncio
async def test_create_unique_index(db):
    with pytest.raises(TypeError, match="create_unique_index"):
        ManyToManyField("models.Foo", create_unique_index=False)
    field = ManyToManyField(
        "models.Group",
    )
    assert field.unique is True
    field = ManyToManyField(
        "models.Group",
        "user_group",
        "user_id",
        "group_id",
        "users",
        "CASCADE",
        True,
        False,
    )
    assert field.unique is False


def test_long_through_table_names_do_not_collide():
    """m2m_object.through used to be built by plain concatenation with no length/hash cap - the
    same bug class as the cumulative JOIN aliases (Selectable.as_): two DIFFERENT M2M fields
    sharing the same 63-byte prefix would get the same through-table name after Postgres's
    truncation."""
    from hare import fields as f
    from hare.migrations.state.apps import StateApps
    from hare.models import Model

    shared_prefix = "x" * 40

    class TableA(Model):
        id = f.IntField(primary_key=True)

        class Meta:
            table = "a" * 40
            app = "models"

    class TableB1(Model):
        id = f.IntField(primary_key=True)
        rel = f.ManyToManyField("models.TableA", related_name="b1s")

        class Meta:
            table = f"b_{shared_prefix}_one"
            app = "models"

    class TableB2(Model):
        id = f.IntField(primary_key=True)
        rel = f.ManyToManyField("models.TableA", related_name="b2s")

        class Meta:
            table = f"b_{shared_prefix}_two"
            app = "models"

    apps = StateApps()
    for model in (TableA, TableB1, TableB2):
        apps.register_model("models", model)
    apps._init_relations()

    through1 = TableB1._meta.fields_map["rel"].through
    through2 = TableB2._meta.fields_map["rel"].through
    assert through1 != through2
    assert len(through1.encode()) <= 63
    assert len(through2.encode()) <= 63


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_cache_populated_inside_a_rolled_back_transaction_is_reset(db):
    """A read after add() inside a scope that then rolls back must not leave a phantom relation
    in the in-memory cache."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")

    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await one.two.add(two)
            async for _ in one.two:
                pass
            assert list(one.two) == [two]
            raise RuntimeError

    assert await one.two == []
    with pytest.raises(NoValuesFetched):
        list(one.two)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_cache_populated_inside_a_rolled_back_savepoint_after_outer_registration_is_reset(db):
    """A savepoint rollback must reset the cache even when the enclosing transaction already
    registered its own reset earlier through an add()."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    three = await testmodels.M2MTwo.objects.create(name="Three")
    await one.two.add(two)
    await prefetch_related_objects([one], "two")

    async with Transactions.atomic():
        await one.two.add(three)
        await prefetch_related_objects([one], "two")
        assert {item.pk for item in one.two} == {two.pk, three.pk}
        with pytest.raises(RuntimeError):
            async with Transactions.atomic():
                await one.two.clear()
                await prefetch_related_objects([one], "two")
                assert list(one.two) == []
                raise RuntimeError
        with pytest.raises(NoValuesFetched):
            list(one.two)
        assert {item.pk for item in await one.two} == {two.pk, three.pk}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_cache_populated_inside_a_committed_transaction_is_kept(db):
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")

    async with Transactions.atomic():
        await one.two.add(two)
        async for _ in one.two:
            pass

    assert list(one.two) == [two]


@pytest.mark.asyncio
async def test__add__wrong_model_type_with_matching_pk_is_rejected(db):
    """An instance of an unrelated model must not link a same-pk row of the related model."""
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    other_one = await testmodels.M2MOne.objects.create(name="Other one")
    matching_pk_stranger = await testmodels.Tournament.objects.create(id=two.pk, name="Same pk")

    with pytest.raises(ValidationError, match="Expected model type 'M2MTwo', but got 'Tournament'"):
        await one.two.add(matching_pk_stranger)
    with pytest.raises(ValidationError, match="Expected model type 'M2MTwo', but got 'M2MOne'"):
        await one.two.add(two, other_one)
    assert await one.two == []


@pytest.mark.asyncio
async def test__remove__wrong_model_type_with_matching_pk_is_rejected(db):
    one = await testmodels.M2MOne.objects.create(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    await one.two.add(two)
    matching_pk_stranger = await testmodels.Tournament.objects.create(id=two.pk, name="Same pk")

    with pytest.raises(ValidationError, match="Expected model type 'M2MTwo', but got 'Tournament'"):
        await one.two.remove(matching_pk_stranger)
    assert await one.two == [two]


@pytest.mark.asyncio
async def test__add_and_remove__none_are_rejected(db):
    one = await testmodels.M2MOne.objects.create(name="One")
    with pytest.raises(ValidationError, match="but got 'NoneType'"):
        await one.two.add(None)
    with pytest.raises(ValidationError, match="but got 'NoneType'"):
        await one.two.remove(None)


@pytest.mark.asyncio
async def test__unsaved_owner_gives_uniform_operational_error(db):
    one = testmodels.M2MOne(name="One")
    two = await testmodels.M2MTwo.objects.create(name="Two")
    message = r"You should first call .save\(\) on <M2MOne>"
    with pytest.raises(QueryError, match=message):
        await one.two.add(two)
    with pytest.raises(QueryError, match=message):
        await one.two.remove(two)
    with pytest.raises(QueryError, match=message):
        await one.two.clear()
    with pytest.raises(QueryError, match=message):
        await one.two.set(two)
    with pytest.raises(QueryError, match=message):
        await prefetch_related_objects([one], "two")


@pytest.mark.asyncio
async def test__remove_unsaved_related_instance_gives_operational_error(db):
    one = await testmodels.M2MOne.objects.create(name="One")
    with pytest.raises(QueryError, match=r"You should first call .save\(\) on <M2MTwo>"):
        await one.two.remove(testmodels.M2MTwo(name="Unsaved"))

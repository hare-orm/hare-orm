import pytest

from hare import prefetch_related_objects
from hare.exceptions import NoValuesFetched, QueryError
from tests.shared_models import testmodels

# A parent model with its reverse relation to a child model's foreign key: (parent class, parent
# values, child class, foreign key name, reverse relation name). UUID keys need escaping, are populated
# before the row is committed and are stored differently per database; the source-field variant has
# customized database names; School's relation goes to its unique non-key `id` field.
REVERSE_RELATIONS = [
    pytest.param(testmodels.Tournament, {"name": "Team1"}, testmodels.Event, "tournament", "events", id="int_fk"),
    pytest.param(testmodels.UUIDPkModel, {}, testmodels.UUIDFkRelatedModel, "model", "children", id="uuid_fk"),
    pytest.param(
        testmodels.UUIDPkSourceModel,
        {},
        testmodels.UUIDFkRelatedSourceModel,
        "model",
        "children",
        id="uuid_fk_with_source_fields",
    ),
    pytest.param(
        testmodels.School,
        {"id": 1024, "name": "School1"},
        testmodels.Student,
        "school",
        "students",
        id="fk_to_a_unique_field",
    ),
]
REVERSE_RELATION_FIELDS = ("parent_class", "parent_values", "child_class", "foreign_key_name", "related_name")


async def create_children(child_class, foreign_key_name, parent, *names):
    return [await child_class.objects.create(name=name, **{foreign_key_name: parent}) for name in names]


@pytest.mark.parametrize(
    "read_children",
    [
        pytest.param(lambda children: anext(aiter(children)), id="uninstantiated_iterate"),
        pytest.param(lambda children: children, id="uninstantiated_await"),
    ],
)
@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_an_unsaved_parent_refuses_reading_its_reverse_relation(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name, read_children
):
    parent = parent_class(**parent_values)
    with pytest.raises(QueryError, match="isn't saved yet"):
        await read_children(getattr(parent, related_name))


@pytest.mark.parametrize(
    "use_children",
    [
        pytest.param(lambda children: "a" in children, id="unfetched_contains"),
        pytest.param(lambda children: [*children], id="unfetched_iter"),
        pytest.param(len, id="unfetched_len"),
        pytest.param(bool, id="unfetched_bool"),
        pytest.param(lambda children: children[0], id="unfetched_getitem"),
    ],
)
@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_an_unfetched_reverse_relation_is_refused(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name, use_children
):
    parent = await parent_class.objects.create(**parent_values)
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        use_children(getattr(parent, related_name))


@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_a_saved_parent_iterates_and_awaits_its_reverse_relation(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name
):
    parent = await parent_class.objects.create(**parent_values)
    children = await create_children(child_class, foreign_key_name, parent, "Child")

    assert [item async for item in getattr(parent, related_name)] == children
    assert list(await getattr(parent, related_name)) == children


@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_a_fetched_reverse_relation_reads_like_a_list(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name
):
    parent = await parent_class.objects.create(**parent_values)
    await prefetch_related_objects([parent], related_name)
    assert not bool(getattr(parent, related_name))

    (child,) = await create_children(child_class, foreign_key_name, parent, "Child")
    await prefetch_related_objects([parent], related_name)
    children = getattr(parent, related_name)
    assert bool(children)
    assert child in children
    assert list(children) == [child]
    assert len(children) == 1
    assert children[0] == child

    with pytest.raises(IndexError):
        children[1]  # pylint: disable=W0104


@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_a_reverse_relation_filters_and_orders(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name
):
    parent = await parent_class.objects.create(**parent_values)
    first, second = await create_children(child_class, foreign_key_name, parent, "Child1", "Child2")
    children = getattr(parent, related_name)

    assert await children.filter(name="Child1") == [first]
    assert await children.filter(name="Child2") == [second]
    assert await children.filter(name="Child3") == []
    assert set(await children.all()) == {first, second}
    assert await children.order_by("-name") == [second, first]
    assert await children.order_by("name") == [first, second]


@pytest.mark.parametrize(
    ("slice_children", "expected_names"),
    [
        pytest.param(lambda children: children.limit(2), ["Child1", "Child2"], id="limit"),
        pytest.param(lambda children: children.offset(1), ["Child2", "Child3"], id="offset"),
    ],
)
@pytest.mark.parametrize(REVERSE_RELATION_FIELDS, REVERSE_RELATIONS)
@pytest.mark.asyncio
async def test_a_slice_of_a_reverse_relation(
    db, parent_class, parent_values, child_class, foreign_key_name, related_name, slice_children, expected_names
):
    parent = await parent_class.objects.create(**parent_values)
    children = await create_children(child_class, foreign_key_name, parent, "Child1", "Child2", "Child3")
    children_by_name = {child.name: child for child in children}

    ordered = getattr(parent, related_name).order_by("name")
    assert await slice_children(ordered) == [children_by_name[name] for name in expected_names]

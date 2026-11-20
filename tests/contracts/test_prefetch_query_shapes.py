"""Sliced prefetches preserve masks, duplicates and composite identities within and across databases."""

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.models import Model
from hare.query.expressions import Window
from hare.query.functions.window import RowNumber
from hare.query.relation_loading import Prefetch
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.multi_database_context import MultiDatabaseTestContext


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", (False, True), ids=("scalar", "composite"))
@pytest.mark.parametrize(("text_key", "explicit"), [(False, False), (False, True), (True, True)])
async def test_prefetch_matches_keys_using_database_equality(contract_database, composite, text_key, explicit):
    class Tag(Model):
        number = fields.IntField(primary_key=True, generated=False)

    class Owner(Model):
        key_field = CitextField if text_key else fields.FloatField
        code = key_field(primary_key=not composite, source_field="stored_code")
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "code")
        label = fields.TextField()
        tags = fields.ManyToManyField(Tag, related_name="owners", through=f"{CONTRACT_APP}.Link" if explicit else None)

    class Child(Model):
        number = fields.IntField(primary_key=True, generated=False)
        owner = fields.ForeignKeyField(Owner, related_name="children", null=True)

    class Profile(Model):
        number = fields.IntField(primary_key=True, generated=False)
        owner = fields.OneToOneField(Owner, related_name="profile")

    class Link(Model):
        number = fields.IntField(primary_key=True, generated=False)
        owner = fields.ForeignKeyField(Owner, related_name="links")
        tag = fields.ForeignKeyField(Tag, related_name="links")

    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if text_key:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS citext")
    models = [Tag, Owner, Child, Profile, Link] if explicit else [Tag, Owner, Child, Profile]
    contract_database.register_live_models(models, CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()

    first_code, equivalent, second_code = ("Alpha", "aLPHa", "Beta") if text_key else (float("nan"), float("nan"), 2.5)

    def owner_values(model, value):
        relation = contract_database.apps.get_binding(model).fields_map["owner"]
        values = {"region": 7, "code": value}
        return {
            left: values[right] for left, right in zip(relation.source_fields, relation.to_field_names, strict=True)
        }

    first = await Owner.objects.create(code=first_code, label="first")
    second = await Owner.objects.create(code=second_code, label="second")
    await Owner.objects.create(code="Gamma" if text_key else 3.5, label="third")
    tag = await Tag.objects.create(number=1)
    for number, value in ((1, equivalent), (2, second_code)):
        await Child.objects.create(number=number, **owner_values(Child, value))
        await Profile.objects.create(number=number, **owner_values(Profile, value))
        if explicit:
            await Link.objects.create(number=number, tag=tag, **owner_values(Link, value))
    if not explicit:
        await first.tags.add(tag)
        await second.tags.add(tag)
    await Child.objects.create(number=3, **owner_values(Child, first_code))
    await Child.objects.create(number=4, owner=None)

    owner_binding = contract_database.apps.get_binding(Owner)
    child_binding = contract_database.apps.get_binding(Child)
    relation = child_binding.fields_map["owner"]
    quote = connection.dialect.literals.quote_identifier
    join = " AND ".join(
        f"c.{quote(child_binding.fields_db_projection[left])} = o.{quote(owner_binding.fields_db_projection[right])}"
        for left, right in zip(relation.source_fields, relation.to_field_names, strict=True)
    )
    sql = (
        f"SELECT c.number, o.label FROM {quote(child_binding.db_table)} c "
        f"LEFT JOIN {quote(owner_binding.db_table)} o ON {join} ORDER BY c.number"
    )
    expected = ["first", "second", "first", None]
    assert [row["label"] for row in await connection.execute_dicts(sql)] == expected

    owners = Owner.objects.order_by("label")
    for _ in range(2):
        children = await Child.objects.order_by("number").prefetch_related(
            Prefetch("owner", Owner.objects.only("label"))
        )
        assert [child.owner.label if child.owner is not None else None for child in children] == expected
        profiles = await Profile.objects.order_by("number").prefetch_related("owner")
        assert [profile.owner.label for profile in profiles] == expected[:2]
        owner_rows = await owners.prefetch_related("profile", Prefetch("children", Child.objects.order_by("number")))
        assert [owner.profile.number if owner.profile is not None else None for owner in owner_rows] == [1, 2, None]
        assert [[child.number for child in owner.children] for owner in owner_rows] == [[1, 3], [2], []]
        sliced = await owners.prefetch_related(Prefetch("children", Child.objects.order_by("number")[:1]))
        assert [[child.number for child in owner.children] for owner in sliced] == [[1], [2], []]
        for target in (Tag.objects.order_by("number"), Tag.objects.order_by("number")[:1]):
            owner_rows = await owners.prefetch_related(Prefetch("tags", target))
            assert [[label.number for label in owner.tags] for owner in owner_rows] == [[1], [1], []]
        for target in (owners, owners[:2]):
            tag_with_owners = await Tag.objects.prefetch_related(Prefetch("owners", target)).get(number=1)
            assert [owner.label for owner in tag_with_owners.owners] == ["first", "second"]


@pytest_asyncio.fixture(params=[False, True], ids=["same_connection", "cross_connection"])
async def prefetch_contract(request, monkeypatch):
    class QueryTag(Model):
        code = fields.CharField(max_length=12)
        number = fields.IntField()
        rank = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        pk = fields.CompositePrimaryKey("code", "number")

        class Meta:
            app = "contract_target"
            soft_delete_field = "deleted_at"

    class QueryParent(Model):
        a = fields.IntField()
        b = fields.IntField()
        tags = fields.ManyToManyField("contract_target.QueryTag", related_name="parents", db_constraint=False)
        pk = fields.CompositePrimaryKey("a", "b")

        class Meta:
            app = "contract_parent"

    target_connection = "target" if request.param else "parent"
    apps = {}
    for model, connection in ((QueryTag, target_connection), (QueryParent, "parent")):
        module_name = f"tests._{model.Meta.app}_query_shapes"
        module = types.ModuleType(module_name)
        setattr(module, model.__name__, model)
        monkeypatch.setitem(sys.modules, module_name, module)
        apps[model.Meta.app] = {"models": [module_name], "default_connection": connection}
    async with MultiDatabaseTestContext.open(
        DatabaseUnderTest.get_url(), sorted({"parent", target_connection}), apps=apps
    ) as context:
        await context.generate_schemas()
        tags = [await QueryTag.objects.create(code="same", number=number, rank=number) for number in (1, 2, 3, 4)]
        parents = [await QueryParent.objects.create(a=first, b=second) for first, second in ((1, 1), (1, 2), (2, 1))]
        links = [[1, 2, 3], [2, 3, 4], []]
        for parent, numbers in zip(parents, links, strict=True):
            await parent.tags.add(*(tags[number - 1] for number in numbers))
        await tags[1].delete()
        yield QueryParent, QueryTag, links, request.param


@pytest.mark.asyncio
async def test_sliced_prefetch_shapes_match_the_linked_rows(prefetch_contract):
    parent_model, tag_model, links, cross_connection = prefetch_contract
    cells = [
        ("plain", False),
        ("only", False),
        ("defer", True),
        ("window_only", True),
        ("distinct", True),
    ]
    if not cross_connection:
        cells.extend((("fanout_only", False), ("distinct_fanout", True)))
    for shape, include_deleted in cells:
        child = tag_model.objects.include_deleted() if include_deleted else tag_model.objects.all()
        child = child.order_by("-rank", "code", "number")
        if "fanout" in shape:
            child = child.filter(parents__b__gte=1)
        if "distinct" in shape:
            child = child.distinct()
        if "only" in shape:
            child = child.only("pk", "rank")
        if shape == "defer":
            child = child.defer("deleted_at")
        if "window" in shape:
            child = child.annotate(position=Window(RowNumber(), order_by=("-rank", "code", "number")))
        visible = {number for numbers in links for number in numbers if include_deleted or number != 2}
        positions = {number: position for position, number in enumerate(sorted(visible, reverse=True), 1)}
        for start, stop in ((0, 1), (1, 3), (1, None), (0, 0)):
            query = (
                parent_model.objects.only("a", "b")
                .order_by("a", "b")
                .prefetch_related(Prefetch("tags", queryset=child[start:stop], to_attribute="picked"))
            )
            expected = []
            for numbers in links:
                selected = []
                for number in numbers:
                    if number not in visible:
                        continue
                    repeats = sum(number in other for other in links) if shape == "fanout_only" else 1
                    selected.extend([number] * repeats)
                expected.append(sorted(selected, reverse=True)[start:stop])
            parents = await query
            assert [[tag.number for tag in parent.picked] for parent in parents] == expected, (shape, start, stop)
            for parent, numbers in zip(parents, expected, strict=True):
                for tag, number in zip(parent.picked, numbers, strict=True):
                    assert tag.pk == ("same", number)
                    assert tag.rank == number
                    if "window" in shape:
                        assert tag.position == positions[number]
                    partial = "only" in shape or shape == "defer"
                    assert tag._partial == partial
                    assert ("deleted_at" in vars(tag)) != partial


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("after", "before", "window"))
@pytest.mark.parametrize("window_annotation", (False, True))
async def test_cursor_prefetch_slices_each_parents_bounded_rows(prefetch_contract, boundary, window_annotation):
    parent_model, tag_model, links, _ = prefetch_contract
    for descending in (False, True):
        ordered = sorted(range(1, 5), reverse=descending)
        positions = {number: index for index, number in enumerate(ordered)}
        child = tag_model.objects.include_deleted().order_by("-rank" if descending else "rank", "code", "number")
        if window_annotation:
            child = child.annotate(position=Window(RowNumber(), order_by=("rank", "code", "number")))
        if boundary in {"after", "window"}:
            child = child.after_cursor(ordered[0], "same", ordered[0])
        if boundary in {"before", "window"}:
            child = child.before_cursor(ordered[-1], "same", ordered[-1])
        for start, stop in ((0, 1), (1, 3), (1, None), (0, 0), (5, 7)):
            parents = await parent_model.objects.order_by("a", "b").prefetch_related(
                Prefetch("tags", child.only("pk", "rank")[start:stop], to_attribute="picked")
            )
            expected = []
            for numbers in links:
                selected = sorted(numbers, key=positions.__getitem__)
                if boundary in {"after", "window"}:
                    selected = [number for number in selected if positions[number] > 0]
                if boundary in {"before", "window"}:
                    selected = [number for number in selected if positions[number] < len(ordered) - 1]
                    selected = selected[::-1][start:stop][::-1]
                else:
                    selected = selected[start:stop]
                expected.append(selected)
            assert [[tag.number for tag in parent.picked] for parent in parents] == expected, (
                boundary,
                window_annotation,
                descending,
                start,
                stop,
            )

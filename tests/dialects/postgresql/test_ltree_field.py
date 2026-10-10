"""LtreeField: ltree columns - paths written as text or lists of labels, the tree lookups, lquery and
ltxtquery patterns, the depth as a path, Subpath - and the values and databases they refuse."""

from __future__ import annotations

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.enums import DialectName
from hare.dialects.postgresql.fields.ltree_field import LtreeField
from hare.dialects.postgresql.functions.ltree import Subpath
from hare.exceptions import UnSupportedError, ValidationError
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.query.expressions import F
from tests.dialects.postgresql.models_ltree import TreeCategory

PATHS = [
    "Top",
    "Top.Science",
    "Top.Science.Astronomy",
    "Top.Science.Astronomy.Cosmology",
    "Top.Hobbies",
    "Top.Hobbies.Amateurs_Astronomy",
    "Top.Collections.Pictures.Astronomy.Stars",
]


async def create_categories() -> None:
    for number, path in enumerate(PATHS, start=1):
        parent_path = path.rpartition(".")[0] or None
        await TreeCategory.objects.create(id=number, path=path, parent_path=parent_path)


async def get_paths(**kwargs) -> list[str]:
    return list(await TreeCategory.objects.filter(**kwargs).order_by("id").values_list("path", flat=True))


@pytest.mark.asyncio
async def test_paths_are_written_and_read_as_text(db_ltree):
    await create_categories()
    await TreeCategory.objects.create(id=20, path=["Top", "Shop"])
    category = await TreeCategory.objects.get(id=20)
    assert category.path == "Top.Shop"
    assert TreeCategory(id=21, path=("A", "B")).path == "A.B"
    assert await get_paths(path="Top.Science") == ["Top.Science"]
    assert await get_paths(path__in=["Top", ["Top", "Hobbies"]]) == ["Top", "Top.Hobbies"]


@pytest.mark.asyncio
async def test_tree_lookups(db_ltree):
    await create_categories()
    assert await get_paths(path__descendant_of="Top.Science") == [
        "Top.Science",
        "Top.Science.Astronomy",
        "Top.Science.Astronomy.Cosmology",
    ]
    assert await get_paths(path__ancestor_of="Top.Science.Astronomy") == [
        "Top",
        "Top.Science",
        "Top.Science.Astronomy",
    ]
    assert await get_paths(path__descendant_of=["Top", "Hobbies"]) == ["Top.Hobbies", "Top.Hobbies.Amateurs_Astronomy"]
    assert await get_paths(path__descendant_of=F("parent_path"), id__gt=1) == PATHS[1:]


@pytest.mark.asyncio
async def test_pattern_lookups(db_ltree):
    await create_categories()
    assert await get_paths(path__matches="*.Astronomy.*") == [
        "Top.Science.Astronomy",
        "Top.Science.Astronomy.Cosmology",
        "Top.Collections.Pictures.Astronomy.Stars",
    ]
    assert await get_paths(path__matches="Top.*{1}") == ["Top.Science", "Top.Hobbies"]
    assert await get_paths(path__matches_any=["Top.Hobbies", "*.Cosmology"]) == [
        "Top.Science.Astronomy.Cosmology",
        "Top.Hobbies",
    ]
    assert await get_paths(path__matches_text="Astro*% & !pictures@") == [
        "Top.Science.Astronomy",
        "Top.Science.Astronomy.Cosmology",
        "Top.Hobbies.Amateurs_Astronomy",
    ]


@pytest.mark.asyncio
async def test_ordering_depth_and_subpath(db_ltree):
    await create_categories()
    assert await get_paths(path__depth=2) == ["Top.Science", "Top.Hobbies"]
    assert await TreeCategory.objects.filter(id__in=[1, 4]).order_by("id").values_list("path__depth", flat=True) == [
        1,
        4,
    ]
    assert await get_paths(path__lt="Top.Hobbies") == ["Top", "Top.Collections.Pictures.Astronomy.Stars"]
    second_labels = (
        await TreeCategory.objects.filter(path__depth__gte=2)
        .annotate(section=Subpath("path", 1, 1))
        .order_by("section")
        .distinct()
        .values_list("section", flat=True)
    )
    assert second_labels == ["Collections", "Hobbies", "Science"]
    assert await TreeCategory.objects.filter(id=4).annotate(rest=Subpath("path", -2)).values_list(
        "rest", flat=True
    ) == ["Astronomy.Cosmology"]


@pytest.mark.asyncio
async def test_queryset_update(db_ltree):
    await create_categories()
    assert await TreeCategory.objects.filter(id=2).update(path=["Top", "Sciences"]) == 1
    assert (await TreeCategory.objects.get(id=2)).path == "Top.Sciences"


@pytest.mark.asyncio
async def test_a_wrong_query_value_is_refused(db_ltree):
    with pytest.raises(ValidationError, match="not an ltree path"):
        await TreeCategory.objects.filter(path__descendant_of="Top..Science").count()
    with pytest.raises(ValidationError, match="expected an lquery str"):
        await TreeCategory.objects.filter(path__matches=5).count()
    with pytest.raises(ValidationError, match="non-empty list of lquery"):
        await TreeCategory.objects.filter(path__matches_any=[]).count()
    with pytest.raises(ValidationError, match="expected an ltxtquery str"):
        await TreeCategory.objects.filter(path__matches_text=["a"]).count()


@pytest.mark.parametrize("value", ["Top..Science", "Top.Sci ence", ".Top", "Top.", 5, ["Top", 5], "Top.Наука"])
def test_a_wrong_path_is_refused(value):
    field = LtreeField()
    field.model_field_name = "path"
    with pytest.raises(ValidationError, match=r"^path"):
        field.to_db_value(value, None)


def test_the_empty_path_is_taken():
    field = LtreeField()
    field.model_field_name = "path"
    assert field.to_db_value("", None) == ""
    assert field.to_db_value([], None) == ""


@pytest.mark.parametrize("value", ["1", 1.5, None])
def test_subpath_refuses_a_non_int_position(value):
    with pytest.raises(ValidationError, match="must be an int"):
        Subpath("path", value)


def test_inspectdb_maps_ltree_and_network_columns():
    expected_paths = {
        ("USER-DEFINED", "ltree"): "hare.dialects.postgresql.fields.ltree_field.LtreeField",
        ("inet", None): "hare.dialects.postgresql.fields.network.InetField",
        ("cidr", None): "hare.dialects.postgresql.fields.network.CidrField",
        ("macaddr", None): "hare.dialects.postgresql.fields.network.MacAddressField",
    }
    for (db_type, udt_name), expected_path in expected_paths.items():
        column = ColumnInfo(
            name="value", db_type=db_type, nullable=True, is_pk=False, is_unique=False, udt_name=udt_name
        )
        assert ColumnTypeMapper.map_column_type(DialectName.POSTGRESQL, column)[::2] == (expected_path, False)
    macaddr8 = ColumnInfo(name="value", db_type="macaddr8", nullable=True, is_pk=False, is_unique=False)
    assert ColumnTypeMapper.map_column_type(DialectName.POSTGRESQL, macaddr8)[2] is True


def test_a_database_without_the_type_refuses_the_field():
    with pytest.raises(UnSupportedError):
        LtreeField().get_column_type(DialectRegistry.get_dialect("sqlite"))

"""A query routed to a connection of another dialect than its model's default connection renders
that connection's SQL: placeholders, functions, casts, lookups and value encoding."""

import datetime
import os
import sys
import types
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import fields
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import QueryError
from hare.models import Model
from hare.query.expressions import Case, Exists, F, OuterRef, Q, Subquery, Value, When
from hare.query.functions import Coalesce, Count, Length, Round, Upper
from tests.utils.timezone_context import override_timezone

MODULE_NAME = "tests._cross_dialect_routing_models"


class RoutedItem(Model):
    id = fields.IntField(primary_key=True)
    ratio = fields.FloatField(default=0)
    name = fields.CharField(max_length=40, default="")
    data = fields.JSONField(default=dict)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    stamp = fields.DatetimeField(null=True)

    class Meta:
        app = "cross_dialect"
        table = "cross_dialect_item"


class RoutedTag(Model):
    id = fields.IntField(primary_key=True)
    item = fields.ForeignKeyField("cross_dialect.RoutedItem", related_name="tags")
    label = fields.CharField(max_length=20)

    class Meta:
        app = "cross_dialect"
        table = "cross_dialect_tag"


def get_postgresql_url() -> str | None:
    raw_db_url = os.getenv("HARE_TEST_DB", "")
    if not raw_db_url.startswith("postgresql"):
        return None
    return raw_db_url.replace("\\{", "{").replace("\\}", "}")


ROUTED_TABLE_SQL = {
    "pg": (
        'CREATE TABLE "cross_dialect_item" ("id" SERIAL PRIMARY KEY, "ratio" DOUBLE PRECISION NOT NULL, '
        '"name" VARCHAR(40) NOT NULL, "data" JSONB NOT NULL, "amount" NUMERIC(10,2) NOT NULL, "stamp" TIMESTAMPTZ);'
        'CREATE TABLE "cross_dialect_tag" ("id" SERIAL PRIMARY KEY, "label" VARCHAR(20) NOT NULL, '
        '"item_id" INT NOT NULL REFERENCES "cross_dialect_item" ("id") ON DELETE CASCADE)'
    ),
    "lite": (
        'CREATE TABLE "cross_dialect_item" ("id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, "ratio" REAL NOT NULL, '
        '"name" VARCHAR(40) NOT NULL, "data" JSON_TEXT NOT NULL, "amount" VARCHAR(40) NOT NULL, "stamp" TIMESTAMP);'
        'CREATE TABLE "cross_dialect_tag" ("id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
        '"label" VARCHAR(20) NOT NULL, '
        '"item_id" INT NOT NULL REFERENCES "cross_dialect_item" ("id") ON DELETE CASCADE)'
    ),
}


@pytest_asyncio.fixture(
    params=["lite", "pg"], ids=["sqlite-default-routed-to-postgresql", "postgresql-default-routed-to-sqlite"]
)
async def routed_context(request):
    postgresql_url = get_postgresql_url()
    if postgresql_url is None:
        pytest.skip("needs a PostgreSQL connection next to a SQLite one")
    default_alias = request.param
    routed_alias = "pg" if default_alias == "lite" else "lite"
    module = types.ModuleType(MODULE_NAME)
    module.RoutedItem = RoutedItem  # type: ignore[attr-defined]
    module.RoutedTag = RoutedTag  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    context = HareContext()
    await context.__aenter__()
    try:
        await context.init(
            config={
                "connections": {
                    "lite": "sqlite://:memory:?install_regexp_functions=True",
                    "pg": DbUrlConfigGenerator.expand(postgresql_url, testing=True),
                },
                "apps": {"cross_dialect": {"models": [MODULE_NAME], "default_connection": default_alias}},
            },
            _create_db=True,
        )
        await context.generate_schemas()
        routed = context.connections.get(routed_alias)
        await routed.execute_script(ROUTED_TABLE_SQL[routed_alias])
        stamp = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        await RoutedItem.objects.using(routed).create(
            ratio=1.25, name="ab", data={"a": 5, "s": "x"}, amount=Decimal("1.50"), stamp=stamp
        )
        await RoutedTag.objects.using(routed).create(id=1, item_id=1, label="first")
        await RoutedTag.objects.using(routed).create(id=2, item_id=1, label="second")
        yield routed
    finally:
        await context.connections.close_all(discard=False)
        await context.connections.get("pg").db_delete()
        await context.__aexit__(None, None, None)
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_functions_and_literals_render_for_the_routed_dialect(routed_context):
    routed = routed_context
    assert await RoutedItem.objects.all().using(routed).annotate(r=Round("ratio", 1)).values_list("r", flat=True) == [
        1.3
    ]
    assert await RoutedItem.objects.all().using(routed).annotate(v=Value(3)).filter(v=3).values_list(
        "id", flat=True
    ) == [1]
    case = Case(When(Q(ratio__gt=1), then=Value(1)), default=Value(0))
    assert await RoutedItem.objects.all().using(routed).annotate(c=case).values_list("c", flat=True) == [1]
    assert await RoutedItem.objects.all().using(routed).annotate(c=Coalesce("stamp", Value(None))).count() == 1
    assert await RoutedItem.objects.all().using(routed).annotate(u=Upper("name"), n=Length("name")).values_list(
        "u", "n"
    ) == [("AB", 2)]
    assert await RoutedItem.objects.all().using(routed).annotate(n=Count("id")).values_list("n", flat=True) == [1]


@pytest.mark.asyncio
async def test_lookups_render_for_the_routed_dialect(routed_context):
    routed = routed_context
    for filtered in (
        RoutedItem.objects.filter(data__a__gt=3),
        RoutedItem.objects.filter(data__s="x"),
        RoutedItem.objects.filter(data__contains={"a": 5}),
        RoutedItem.objects.filter(data__has_key="a"),
        RoutedItem.objects.filter(amount__gt=Decimal("1.2")),
        RoutedItem.objects.filter(name__posix_regex="^a"),
        RoutedItem.objects.filter(id__in=[1, 2, 3]),
        RoutedItem.objects.filter(ratio__gt=F("amount") - 1),
        RoutedItem.objects.filter(stamp__year=2026),
        RoutedItem.objects.filter(stamp=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)),
    ):
        assert await filtered.using(routed).values_list("id", flat=True) == [1], filtered.using(routed).sql()


@pytest.mark.asyncio
async def test_writes_render_for_the_routed_dialect(routed_context):
    routed = routed_context
    assert await RoutedItem.objects.filter(id=1).using(routed).update(name="cd", ratio=F("ratio") * 2) == 1
    item = await RoutedItem.objects.using(routed).get(id=1)
    assert (item.name, item.ratio) == ("cd", 2.5)
    await RoutedItem.objects.using(routed).bulk_create([RoutedItem(id=2, name="ef"), RoutedItem(id=3, name="gh")])
    assert await RoutedItem.objects.all().using(routed).order_by("id").values_list("name", flat=True) == [
        "cd",
        "ef",
        "gh",
    ]
    assert await RoutedItem.objects.filter(id__gte=2).using(routed).delete() == 2


@pytest.mark.asyncio
async def test_naive_datetimes_use_the_routed_dialect_storage_with_use_tz_false(routed_context):
    routed = routed_context
    wall_clock = datetime.datetime(2026, 5, 6, 7, 8, 9)
    later_wall_clock = wall_clock + datetime.timedelta(hours=1)
    with override_timezone(use_tz=False):
        await RoutedItem.objects.using(routed).create(id=2, stamp=wall_clock)
        await RoutedItem.objects.using(routed).bulk_create([RoutedItem(id=3, stamp=wall_clock)])
        stamps = RoutedItem.objects.filter(id__in=[2, 3]).using(routed).order_by("id").values_list("stamp", flat=True)
        assert await stamps == [wall_clock, wall_clock]
        assert await RoutedItem.objects.filter(stamp=wall_clock).using(routed).count() == 2
        assert (await RoutedItem.objects.using(routed).get(id=2)).stamp == wall_clock
        assert await RoutedItem.objects.filter(id=3).using(routed).update(stamp=later_wall_clock) == 1
        assert (await RoutedItem.objects.using(routed).get(id=3)).stamp == later_wall_clock
    _, rows = await routed.execute("SELECT stamp FROM cross_dialect_item WHERE id = 2")
    stored = rows[0]["stamp"]
    if isinstance(stored, str):
        assert datetime.datetime.fromisoformat(stored) == wall_clock
    else:
        assert stored == wall_clock.astimezone()


@pytest.mark.asyncio
async def test_subquery_and_exists_run_on_the_outer_query_connection(routed_context):
    routed = routed_context
    last_label = Subquery(RoutedTag.objects.filter(item_id=OuterRef("id")).order_by("-id").limit(1).values("label"))
    labelled = RoutedItem.objects.all().using(routed).annotate(last_label=last_label)
    assert await labelled.values_list("last_label", flat=True) == ["second"]
    tagged = RoutedItem.objects.filter(Exists(RoutedTag.objects.filter(item_id=OuterRef("id"), label="first"))).using(
        routed
    )
    assert await tagged.values_list("id", flat=True) == [1]
    with_first_tag = RoutedItem.objects.filter(
        id__in=Subquery(RoutedTag.objects.filter(label="first").values("item_id"))
    )
    assert await with_first_tag.using(routed).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_subquery_pinned_to_another_connection_is_rejected(routed_context):
    routed = routed_context
    default = RoutedItem._meta.db
    pinned_tags = RoutedTag.objects.filter(item_id=OuterRef("id")).using(default).values("label")
    with pytest.raises(QueryError, match="different database connection"):
        await RoutedItem.objects.all().using(routed).annotate(label=Subquery(pinned_tags)).values_list("label")
    pinned_exists = Exists(RoutedTag.objects.filter(item_id=OuterRef("id")).using(default))
    with pytest.raises(QueryError, match="different database connection"):
        await RoutedItem.objects.filter(pinned_exists).using(routed).count()
    routed_tags = (
        RoutedTag.objects.filter(item_id=OuterRef("id")).using(routed).order_by("id").limit(1).values("label")
    )
    assert await RoutedItem.objects.all().using(routed).annotate(label=Subquery(routed_tags)).values_list(
        "label", flat=True
    ) == ["first"]


@pytest.mark.asyncio
async def test_prefetch_related_runs_on_the_routed_connection(routed_context):
    routed = routed_context
    items = await RoutedItem.objects.all().using(routed).prefetch_related("tags")
    assert [sorted(tag.label for tag in item.tags) for item in items] == [["first", "second"]]
    tags = await RoutedTag.objects.all().using(routed).order_by("id").prefetch_related("item")
    assert [tag.item.name for tag in tags] == ["ab", "ab"]

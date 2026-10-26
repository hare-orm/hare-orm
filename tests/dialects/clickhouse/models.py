"""Models of the ClickHouse dialect's tests - every key set by the application, as ClickHouse
generates none."""

import decimal
import uuid
from enum import IntEnum, StrEnum

from hare import fields
from hare.contrib.versioning.versioned_model import VersionedModel
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.indexes import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.fields import (
    ClickhouseDateField,
    ClickhouseDatetimeField,
    FixedStringField,
    Float32Field,
    Int256Field,
    LowCardinalityField,
    UInt8Field,
    UInt64Field,
)
from hare.dialects.clickhouse.indexes import (
    BloomFilterIndex,
    MinMaxIndex,
    NgramBloomFilterIndex,
    SetIndex,
    TokenBloomFilterIndex,
)
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary, ClickhouseMaterializedView
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
from hare.fields.db_defaults import Now
from hare.models import Model
from hare.query.expressions import Q
from hare.query.functions import Lower, Upper


class Team(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    name = fields.CharField(max_length=50)
    description = fields.TextField(null=True, description="What the team does")

    players: fields.ReverseRelation["Player"]

    class Meta:
        table_description = "Teams of players"


class Skill(Model):
    id = fields.IntField(primary_key=True, generated=False)
    name = fields.CharField(max_length=50)


class Player(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    name = fields.CharField(max_length=50)
    team: fields.ForeignKeyNullableRelation[Team] = fields.ForeignKeyField(
        "models.Team", related_name="players", null=True, db_constraint=False
    )
    score = fields.DecimalField(max_digits=10, decimal_places=2, default=decimal.Decimal("0"))
    rating = fields.FloatField(null=True)
    joined = fields.DatetimeField(null=True)
    born = fields.DateField(null=True)
    active = fields.BooleanField(default=True)
    notes = fields.TextField(null=True)
    data = fields.JSONField(null=True)
    skills: fields.ManyToManyRelation[Skill] = fields.ManyToManyField(
        "models.Skill", related_name="players", db_constraint=False
    )

    class Meta:
        ordering = ["id"]


class PageView(Model):
    """A table of the analytical type - partitioned by month, sorted by site and time."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    site = fields.CharField(max_length=50)
    viewed_at = fields.DatetimeField()
    duration_ms = fields.IntField(default=0)

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                order_by=("id", "site", "viewed_at"),
                partition_by=RawSQLTerm("toYYYYMM(viewed_at)"),
                settings=(("index_granularity", 1024),),
                # A projection of a ReplacingMergeTree needs a server rebuilding it as rows merge away.
                projections=(ClickhouseProjection("by_site", RawSQLTerm("select site, count() group by site")),),
            )
        ]


class PageVersion(VersionedModel):
    """A page kept in versions - a new row of the same id for each change."""

    title = fields.CharField(max_length=50)


class Revision(Model):
    """A row keyed by two columns."""

    document_id = fields.IntField()
    number = fields.IntField()
    title = fields.CharField(max_length=50)
    score = fields.FloatField(null=True)

    pk = fields.CompositePrimaryKey("document_id", "number")


class Reading(Model):
    """A table keeping versions of a row - merged into the last one by FINAL - read in samples by a
    hash of its key."""

    id = fields.BigIntField(primary_key=True, generated=False)
    sensor = fields.CharField(max_length=20)
    value = fields.IntField()
    version = fields.IntField(default=0)

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                engine="ReplacingMergeTree(version)",
                order_by=("id", RawSQLTerm("intHash32(id)")),
                sample_by=RawSQLTerm("intHash32(id)"),
            )
        ]


class Quote(Model):
    """A price of a symbol from a moment on."""

    id = fields.BigIntField(primary_key=True, generated=False)
    symbol = fields.CharField(max_length=10)
    quoted_at = fields.IntField()
    price = fields.DecimalField(max_digits=10, decimal_places=2)


class Trade(Model):
    """A trade of a symbol at a moment - priced by the last quote before it."""

    id = fields.BigIntField(primary_key=True, generated=False)
    symbol = fields.CharField(max_length=10)
    traded_at = fields.IntField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                projections=(ClickhouseProjection("by_symbol", RawSQLTerm("SELECT symbol, count() GROUP BY symbol")),)
            )
        ]


class Hit(Model):
    """A page hit - its table declaring every option of a ClickHouse table."""

    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=20)
    page = fields.TextField()
    duration = fields.IntField(source_field="duration_ms")
    seen = fields.DatetimeField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                engine="ReplacingMergeTree( duration_ms )",
                order_by=("id", "site", RawSQLTerm("intHash32( id )")),
                sample_by=RawSQLTerm("intHash32( id )"),
                partition_by=RawSQLTerm("toYYYYMM(seen)"),
                ttl=RawSQLTerm("toDateTime(seen) + INTERVAL 30 DAY"),
                settings=(("merge_with_ttl_timeout", 3600), ("index_granularity", 8192)),
                column_codecs=(("page", "ZSTD(3)"), ("duration", "Delta, ZSTD")),
                column_ttls=(("page", RawSQLTerm("toDateTime(seen) + INTERVAL 7 DAY")),),
            )
        ]


class Country(Model):
    """A country - its name and its people read by its code through a dictionary."""

    code = fields.CharField(max_length=2, primary_key=True)
    name = fields.CharField(max_length=50, source_field="title")
    population = fields.BigIntField(null=True)

    class Meta:
        dictionaries = [
            ClickhouseDictionary("country_names", key=("code",), attributes=("name", "population"), lifetime=300)
        ]


class Sale(Model):
    """A sale of a shop - read through a view, summed by a materialized view of its own storage and
    by one writing to the shops' totals."""

    id = fields.BigIntField(primary_key=True, generated=False)
    shop = fields.CharField(max_length=20)
    amount = fields.IntField()

    class Meta:
        views = [View("big_sales", RawSQLTerm("SELECT id, shop, amount FROM sale WHERE amount > 100"))]
        materialized_views = [
            MaterializedView(
                "sales_by_shop",
                RawSQLTerm("SELECT shop, toInt64(sum(amount)) AS total FROM sale GROUP BY shop"),
                unique_columns=("shop",),
            ),
            ClickhouseMaterializedView(
                "sale_totals",
                RawSQLTerm("select shop, toInt64( amount ) as total from sale"),
                to="shop_total",
            ),
        ]


class ShopTotal(Model):
    """The total of each shop's sales - its rows summed by the table's engine."""

    shop = fields.CharField(max_length=20, primary_key=True)
    total = fields.BigIntField()

    class Meta:
        table = "shop_total"
        table_options = [ClickhouseTableOptions(engine="SummingMergeTree")]


class Account(Model):
    """An account of an owner."""

    id = fields.BigIntField(primary_key=True, generated=False)
    owner = fields.CharField(max_length=20)
    balance = fields.IntField(default=0)


class Member(Model):
    """A member of a club, and the team sponsoring it."""

    id = fields.BigIntField(primary_key=True, generated=False)
    email = fields.CharField(max_length=50, null=True)
    club: fields.ForeignKeyRelation[Team] = fields.ForeignKeyField(
        "models.Team", related_name="members", db_constraint=False
    )
    sponsor: fields.ForeignKeyNullableRelation[Team] = fields.ForeignKeyField(
        "models.Team", related_name="sponsored", null=True, db_constraint=False
    )
    number = fields.IntField()
    active = fields.BooleanField(default=True)


class Shipment(Model):
    """Containers of every shape, held in one another to any depth."""

    id = fields.BigIntField(primary_key=True, generated=False)
    tags = fields.ArrayField(fields.CharField(max_length=20))
    scores = fields.ArrayField(fields.IntField(null=True))
    grid = fields.ArrayField(fields.ArrayField(fields.IntField()))
    prices = fields.MapField(fields.CharField(max_length=3), fields.DecimalField(max_digits=10, decimal_places=2))
    labels = fields.MapField(fields.CharField(max_length=5), fields.ArrayField(fields.TextField()))
    point = fields.TupleField({"lon": fields.FloatField(), "lat": fields.FloatField()})
    bounds = fields.TupleField([fields.IntField(), fields.DatetimeField()])
    items = fields.NestedField({"sku": fields.CharField(max_length=10), "quantity": fields.IntField()})
    deep = fields.ArrayField(
        fields.MapField(
            fields.CharField(max_length=5),
            fields.ArrayField(
                fields.TupleField([fields.IntField(), fields.MapField(fields.IntField(), fields.UUIDField(null=True))])
            ),
        )
    )


class Measurement(Model):
    """Values at the edges of their types, held in containers."""

    id = fields.BigIntField(primary_key=True, generated=False)
    counts = fields.ArrayField(fields.BigIntField())
    totals = fields.MapField(fields.BigIntField(), fields.BigIntField())
    marks = fields.ArrayField(fields.TupleField([fields.BigIntField(), fields.DatetimeField()]))
    moments = fields.ArrayField(fields.DatetimeField())


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class DeviceLevel(IntEnum):
    LOW = 1
    HIGH = 500


class Device(Model):
    """ClickHouse's own scalar types."""

    id = fields.BigIntField(primary_key=True, generated=False)
    tiny = UInt8Field()
    big = UInt64Field()
    huge = Int256Field(null=True)
    ratio = Float32Field()
    code = FixedStringField(4)
    country = LowCardinalityField(fields.CharField(max_length=2))
    city = LowCardinalityField(fields.CharField(max_length=20), null=True)
    status = fields.CharEnumField(DeviceStatus)
    level = fields.IntEnumField(DeviceLevel, null=True)
    address = fields.IPAddressField()
    gateway = fields.IPv4AddressField(null=True)
    labels = fields.ArrayField(LowCardinalityField(fields.CharField(max_length=10)))


class Article(Model):
    """A table with a data skipping index of every type."""

    id = fields.BigIntField(primary_key=True, generated=False)
    title = fields.CharField(max_length=100)
    body = fields.TextField()
    views = fields.IntField()
    tags = fields.ArrayField(fields.CharField(max_length=20))

    class Meta:
        indexes = [
            MinMaxIndex(fields=["views"], granularity=4, name="article_views_idx"),
            SetIndex(fields=["title"], max_rows=100, name="article_title_idx"),
            BloomFilterIndex(fields=["tags"], false_positive=0.01, name="article_tags_idx"),
            NgramBloomFilterIndex(
                fields=["body"], ngram_size=4, filter_size=512, hash_functions=3, name="article_body_ngram_idx"
            ),
            TokenBloomFilterIndex(fields=["body"], granularity=2, name="article_body_token_idx"),
            Index(fields=["title", "views"], name="article_plain_idx"),
            MinMaxIndex(fields=["id"], name="article_id_idx"),
            BloomFilterIndex(Lower("title"), name="article_lower_title_idx"),
            Index(Upper("body")),
        ]
        constraints = [CheckConstraint(check=Q(views__gte=0), name="article_views_counted")]


class Invoice(Model):
    """Columns the database computes - on write (``MATERIALIZED``) and on read (``ALIAS``)."""

    id = fields.BigIntField(primary_key=True, generated=False)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    total = fields.GeneratedField(RawSQLTerm("price * quantity"), fields.DecimalField(max_digits=18, decimal_places=2))
    label = fields.GeneratedField(
        RawSQLTerm("concat('#', toString(id))"), fields.CharField(max_length=30), stored=False
    )
    note = fields.GeneratedField(
        RawSQLTerm("if(quantity > 1, 'many', NULL)"), fields.CharField(max_length=10), null=True
    )


class Attachment(Model):
    """Bytes and a JSON document of any shape."""

    id = fields.BigIntField(primary_key=True, generated=False)
    content = fields.BinaryField(null=True)
    document = fields.JSONField(null=True)


class Ticket(Model):
    """A row stamped on every write and tracking its unsaved changes."""

    id = fields.BigIntField(primary_key=True, generated=False)
    title = fields.CharField(max_length=50)
    status = fields.CharField(max_length=20, default="open")
    changed = fields.DatetimeField(auto_now=True)
    version = fields.IntField(default=1)

    class Meta:
        track_dirty_fields = True


class RevisionComment(Model):
    """A comment on a revision - a relation to a key of two columns."""

    id = fields.BigIntField(primary_key=True, generated=False)
    revision: fields.ForeignKeyNullableRelation[Revision] = fields.ForeignKeyField(
        "models.Revision", related_name="comments", null=True, db_constraint=False
    )
    text = fields.CharField(max_length=50)


class Folder(Model):
    """A folder inside another one - a relation to its own model."""

    id = fields.BigIntField(primary_key=True, generated=False)
    parent: fields.ForeignKeyNullableRelation["Folder"] = fields.ForeignKeyField(
        "models.Folder", related_name="folders", null=True, db_constraint=False
    )


class BackslashName(Model):
    """A table and a column named with a backslash - an escape inside a name ClickHouse quotes."""

    id = fields.BigIntField(primary_key=True, generated=False)
    label = fields.CharField(max_length=20, source_field="la\\bel")

    class Meta:
        table = "back\\slash"


class KeywordName(Model):
    """A table and a key column named after keywords - ClickHouse takes no alias after "from"
    without AS."""

    from_field = fields.BigIntField(primary_key=True, generated=False, source_field="from")
    label = fields.CharField(max_length=20)

    class Meta:
        table = "select"


class TenantLabel(Model):
    """A label of a tenant, keyed by two columns."""

    code = fields.CharField(max_length=10)
    region = fields.CharField(max_length=10)
    tenant_id = fields.IntField()
    pk = fields.CompositePrimaryKey("code", "region")

    class Meta:
        tenant_field = "tenant_id"


class TenantDocument(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    title = fields.CharField(max_length=10)
    tenant_id = fields.IntField()
    labels: fields.ManyToManyRelation[TenantLabel] = fields.ManyToManyField(
        "models.TenantLabel", related_name="documents", db_constraint=False
    )

    class Meta:
        tenant_field = "tenant_id"


class TenantNote(Model):
    """A note of a tenant's document - ClickHouse keeps no key unique: two tenants' documents may share one."""

    id = fields.BigIntField(primary_key=True, generated=False)
    tenant_id = fields.IntField()
    document: fields.ForeignKeyRelation[TenantDocument] = fields.ForeignKeyField(
        "models.TenantDocument", related_name="notes", on_delete=fields.CASCADE, db_constraint=False
    )

    class Meta:
        tenant_field = "tenant_id"


class DayCounter(Model):
    """A key of a day the database fills in by default, and a number."""

    day = fields.DateField(db_default=Now())
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("day", "number")


class EscapedLabel(StrEnum):
    BACKSLASH = "back\\slash"
    NEW_LINE = "two\nlines"
    TAB = "a\tb"
    QUOTE = "it's"
    PLAIN = "plain"


class Labelled(Model):
    """Enum labels the server escapes in the text of the type."""

    id = fields.BigIntField(primary_key=True, generated=False)
    label = fields.CharEnumField(EscapedLabel)


class Holding(Model):
    """Containers of values ClickHouse orders otherwise than PostgreSQL."""

    id = fields.BigIntField(primary_key=True, generated=False)
    keys = fields.ArrayField(fields.UUIDField())
    statuses = fields.ArrayField(fields.CharEnumField(DeviceStatus))
    counts = fields.MapField(fields.CharEnumField(DeviceStatus), fields.IntField())
    pair = fields.TupleField([fields.UUIDField(), fields.IntField()])


class KeyedQuote(Model):
    """A price of a symbol from a moment on, keyed by both."""

    symbol = fields.CharField(max_length=10)
    quoted_at = fields.IntField()
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    pk = fields.CompositePrimaryKey("symbol", "quoted_at")


class MomentReading(Model):
    """A reading kept in ClickHouse's own date and time types."""

    id = fields.BigIntField(primary_key=True, generated=False)
    day = ClickhouseDateField(null=True)
    second = ClickhouseDatetimeField(null=True)
    milli = ClickhouseDatetimeField(precision=3, null=True)
    local = ClickhouseDatetimeField(precision=3, timezone="Europe/Moscow", null=True)
    count = fields.IntField(null=True)
    lasts = fields.TimeDeltaField(null=True)


class ArchivedShelf(Model):
    """A soft-deleted row whose soft deletion cascades to its books."""

    id = fields.BigIntField(primary_key=True, generated=False)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class ArchivedBook(Model):
    """A book of a shelf - soft-deleted with its shelf."""

    id = fields.BigIntField(primary_key=True, generated=False)
    isbn = fields.CharField(max_length=20)
    shelf = fields.ForeignKeyField(
        "models.ArchivedShelf", related_name="books", on_delete=fields.CASCADE, db_constraint=False
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class WideAmount(Model):
    """Decimals of a narrow and a wide type beside a 64-bit integer - their arithmetic outgrows each type."""

    id = fields.BigIntField(primary_key=True, generated=False)
    wide = fields.DecimalField(max_digits=38, decimal_places=10)
    narrow = fields.DecimalField(max_digits=9, decimal_places=4)
    count = fields.BigIntField()


class Passport(Model):
    """A row keyed by a sensitive value."""

    code = fields.CharField(max_length=50, primary_key=True, sensitive=True)


class Visa(Model):
    """A relation to a sensitive key - its values are the key's."""

    id = fields.BigIntField(primary_key=True, generated=False)
    passport = fields.ForeignKeyField("models.Passport", related_name="visas", db_constraint=False)


class NetworkHost(Model):
    """An address with a port - one address written in several texts."""

    id = fields.BigIntField(primary_key=True, generated=False)
    address = fields.IPAddressField()
    port = fields.IntField(default=0)


class NetworkRoute(Model):
    """A row keyed by an address - a key written in several texts."""

    address = fields.IPAddressField()
    hop = fields.IntField()

    pk = fields.CompositePrimaryKey("address", "hop")


class EventLog(Model):
    """An append-only log row - a table without a primary key."""

    source = fields.CharField(max_length=20)
    duration = fields.IntField(default=0)

    class Meta:
        primary_key = None
        ordering = ("source",)

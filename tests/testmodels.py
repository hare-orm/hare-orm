"""
This is the testing Models
"""

from __future__ import annotations

import binascii
import datetime
import functools
import json
import os
import re
import uuid
from contextvars import ContextVar
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from hare import fields, prefetch_related_objects
from hare.contrib.versioning.versioned_model import VersionedModel
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import NoValuesFetched, ValidationError
from hare.fields import CASCADE, NO_ACTION, PROTECT, RESTRICT, SET_DEFAULT, SET_NULL
from hare.fields.db_defaults import Now, RandomHex, SqlDefault
from hare.fields.generated_field import GeneratedField
from hare.fields.validators import (
    CommaSeparatedIntegerListValidator,
    MaxValueValidator,
    MinLengthValidator,
    MinValueValidator,
    RegexValidator,
    validate_ipv4_address,
    validate_ipv6_address,
    validate_ipv46_address,
)
from hare.models import Model
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.f import F
from hare.query.functions import Coalesce
from hare.query.managers.manager import Manager
from hare.query.queryset import QuerySet
from hare.time import UTC


def generate_token():
    return binascii.hexlify(os.urandom(16)).decode("ascii")


class TestSchemaForJSONField(BaseModel):
    foo: int
    bar: str
    __test__ = False


json_pydantic_default = TestSchemaForJSONField(foo=1, bar="baz")


class Author(Model):
    name = fields.CharField(max_length=255)


class Book(Model):
    name = fields.CharField(max_length=255)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(Author, related_name="books")
    rating = fields.FloatField()
    subject = fields.CharField(max_length=255, null=True)


class BookNoConstraint(Model):
    name = fields.CharField(max_length=255)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField("models.Author", db_constraint=False)
    rating = fields.FloatField()


class Tournament(Model):
    id = fields.SmallIntField(primary_key=True)
    name = fields.CharField(max_length=255)
    desc = fields.TextField(null=True)
    created = fields.DatetimeField(auto_now_add=True, db_index=True)

    events: fields.ReverseRelation[Event]
    minrelations: fields.ReverseRelation[MinRelation]
    uniquetogetherfieldswithfks: fields.ReverseRelation[UniqueTogetherFieldsWithFK]
    composite_owners: fields.ReverseRelation[CompositePkOwningFK]

    class PydanticMeta:
        exclude = ("minrelations", "uniquetogetherfieldswithfks", "composite_owners")

    def __str__(self):
        return self.name


class Reporter(Model):
    """Whom is assigned as the reporter"""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    events: fields.ReverseRelation[Event]

    class Meta:
        table = "re_port_er"

    def __str__(self):
        return self.name


class Event(Model):
    """Events on the calendar"""

    event_id = fields.BigIntField(primary_key=True)
    #: The name
    name = fields.TextField()
    #: What tournaments is a happenin'
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        to="models.Tournament", related_name="events"
    )
    reporter: fields.ForeignKeyNullableRelation[Reporter] = fields.ForeignKeyField(to=Reporter, null=True)
    participants: fields.ManyToManyRelation[Team] = fields.ManyToManyField(
        "models.Team",
        related_name="events",
        through="event_team",
        backward_key="idEvent",
    )
    modified = fields.DatetimeField(auto_now=True)
    token = fields.TextField(default=generate_token)
    alias = fields.IntField(null=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ModelTestPydanticMetaBackwardRelations1(Model):
    class PydanticMeta:
        backward_relations = False


class ModelTestPydanticMetaBackwardRelations2(Model): ...


class ModelTestPydanticMetaBackwardRelations3(Model):
    one: fields.ForeignKeyRelation[ModelTestPydanticMetaBackwardRelations1] = fields.ForeignKeyField(
        "models.ModelTestPydanticMetaBackwardRelations1", related_name="threes"
    )
    two: fields.ForeignKeyRelation[ModelTestPydanticMetaBackwardRelations2] = fields.ForeignKeyField(
        "models.ModelTestPydanticMetaBackwardRelations2", related_name="threes"
    )


class ModelTestPydanticAnnotatedBackwardRel(Model):
    """Model with backward_relations=False but an annotated ReverseRelation."""

    annotated_children: fields.ReverseRelation[ModelTestPydanticAnnotatedChild]

    class PydanticMeta:
        backward_relations = False


class ModelTestPydanticAnnotatedChild(Model):
    parent: fields.ForeignKeyRelation[ModelTestPydanticAnnotatedBackwardRel] = fields.ForeignKeyField(
        "models.ModelTestPydanticAnnotatedBackwardRel", related_name="annotated_children"
    )


class ModelTestPydanticUnannotatedChild(Model):
    parent: fields.ForeignKeyRelation[ModelTestPydanticAnnotatedBackwardRel] = fields.ForeignKeyField(
        "models.ModelTestPydanticAnnotatedBackwardRel",
        related_name="unannotated_children",
    )


class Node(Model):
    name = fields.CharField(max_length=10)


class Tree(Model):
    parent: fields.ForeignKeyRelation[Node] = fields.ForeignKeyField("models.Node", related_name="parent_trees")
    child: fields.ForeignKeyRelation[Node] = fields.ForeignKeyField(
        "models.Node", related_name="children_trees", on_delete=NO_ACTION
    )


class Category(Model):
    """Self-referential (parent_id column, not a real FK constraint - simpler for raw-SQL
    recursive-CTE tree walks in test_cte.py, which build their own JOINs by hand anyway)."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    parent_id = fields.IntField(null=True)


class Address(Model):
    city = fields.CharField(max_length=64)
    street = fields.CharField(max_length=128)

    event: fields.OneToOneRelation[Event] = fields.OneToOneField(
        "models.Event",
        on_delete=fields.CASCADE,
        related_name="address",
        primary_key=True,
    )


class M2mWithO2oPk(Model):
    name = fields.CharField(max_length=64)
    address: fields.ManyToManyRelation[Address] = fields.ManyToManyField("models.Address")


class O2oPkModelWithM2m(Model):
    author: fields.OneToOneRelation[Author] = fields.OneToOneField(
        "models.Author",
        on_delete=fields.CASCADE,
        primary_key=True,
    )
    nodes: fields.ManyToManyRelation[Node] = fields.ManyToManyField("models.Node")


class Dest_null(Model):
    name = fields.CharField(max_length=64)


class O2O_null(Model):
    name = fields.CharField(max_length=64)
    event: fields.OneToOneNullableRelation[Event] = fields.OneToOneField(
        "models.Dest_null",
        on_delete=fields.CASCADE,
        related_name="address_null",
        null=True,
    )


class Team(Model):
    """
    Team that is a playing
    """

    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    events: fields.ManyToManyRelation[Event]
    minrelation_through: fields.ManyToManyRelation[MinRelation]
    alias = fields.IntField(null=True)

    class Meta:
        ordering = ["id"]

    class PydanticMeta:
        exclude = ("minrelations",)

    def __str__(self):
        return self.name


class EventTwo(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    tournament_id = fields.IntField()
    # Here we make link to events.Team, not models.Team
    participants: fields.ManyToManyRelation[TeamTwo] = fields.ManyToManyField("events.TeamTwo")

    class Meta:
        app = "events"

    def __str__(self):
        return self.name


class TeamTwo(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    eventtwo_through: fields.ManyToManyRelation[EventTwo]

    class Meta:
        app = "events"

    def __str__(self):
        return self.name


class IntFields(Model):
    id = fields.IntField(primary_key=True)
    intnum = fields.IntField()
    intnum_null = fields.IntField(null=True)


class BigIntFields(Model):
    id = fields.BigIntField(primary_key=True)
    intnum = fields.BigIntField()
    intnum_null = fields.BigIntField(null=True)


class SmallIntFields(Model):
    id = fields.IntField(primary_key=True)
    smallintnum = fields.SmallIntField()
    smallintnum_null = fields.SmallIntField(null=True)


class CharFields(Model):
    id = fields.IntField(primary_key=True)
    char = fields.CharField(max_length=255)
    char_null = fields.CharField(max_length=255, null=True)


class TextFields(Model):
    id = fields.IntField(primary_key=True)
    text = fields.TextField()
    text_null = fields.TextField(null=True)


class BooleanFields(Model):
    id = fields.IntField(primary_key=True)
    boolean = fields.BooleanField()
    boolean_null = fields.BooleanField(null=True)


class BinaryFields(Model):
    id = fields.IntField(primary_key=True)
    binary = fields.BinaryField()
    binary_null = fields.BinaryField(null=True)


class IndexedBinaryFields(Model):
    """A unique and an indexed BinaryField."""

    id = fields.IntField(primary_key=True)
    digest = fields.BinaryField(unique=True)
    tag = fields.BinaryField(db_index=True, null=True)


class DecimalFields(Model):
    id = fields.IntField(primary_key=True)
    decimal = fields.DecimalField(max_digits=18, decimal_places=4)
    decimal_nodec = fields.DecimalField(max_digits=18, decimal_places=0)
    decimal_null = fields.DecimalField(max_digits=18, decimal_places=4, null=True)


class HighPrecisionDecimalFields(Model):
    """max_digits above decimal's own default context precision (28) - Postgres NUMERIC itself
    supports far higher precision than that, so DecimalField must not rely on the ambient
    decimal.getcontext() when quantizing a value."""

    id = fields.IntField(primary_key=True)
    big = fields.DecimalField(max_digits=38, decimal_places=18)


class DatetimeFields(Model):
    id = fields.IntField(primary_key=True)
    datetime = fields.DatetimeField()
    datetime_null = fields.DatetimeField(null=True)
    datetime_auto = fields.DatetimeField(auto_now=True)
    datetime_add = fields.DatetimeField(auto_now_add=True)


class TimeDeltaFields(Model):
    id = fields.IntField(primary_key=True)
    timedelta = fields.TimeDeltaField()
    timedelta_null = fields.TimeDeltaField(null=True)


class DateFields(Model):
    id = fields.IntField(primary_key=True)
    date = fields.DateField()
    date_null = fields.DateField(null=True)

    class Meta:
        get_latest_by = ("date", "id")


class TimeFields(Model):
    id = fields.IntField(primary_key=True)
    time = fields.TimeField()
    time_null = fields.TimeField(null=True)
    time_auto = fields.TimeField(auto_now=True)


class FloatFields(Model):
    id = fields.IntField(primary_key=True)
    floatnum = fields.FloatField()
    floatnum_null = fields.FloatField(null=True)


def raise_if_not_dict_or_list(value: dict | list):
    if not isinstance(value, (dict, list)):
        raise ValidationError("Value must be a dict or list.")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)

    @classmethod
    def dumps(cls, obj: Any) -> str:
        return json.dumps(obj, cls=cls)


class IndexEncoder(DecimalEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Index):
            return obj.deconstruct()[2]
        return super().default(obj)


class JSONFields(Model):
    """
    This model contains many JSON blobs
    """

    id = fields.IntField(primary_key=True)
    data = fields.JSONField()  # type: ignore # Test cases where generics are not provided
    data_null = fields.JSONField[dict | list](null=True)
    data_default = fields.JSONField[dict](default={"a": 1})

    # From Python 3.10 onwards, validator can be defined with staticmethod
    data_validate = fields.JSONField[dict | list](null=True, validators=[raise_if_not_dict_or_list])

    # Test cases where generics are provided and the type is a pydantic base model
    data_pydantic = fields.JSONField[TestSchemaForJSONField](
        default=json_pydantic_default, field_type=TestSchemaForJSONField
    )

    # Test cases where encoders are provided
    data_decimal = fields.JSONField[dict | list](null=True, encoder=DecimalEncoder.dumps)
    data_index = fields.JSONField[dict | list](null=True, encoder=IndexEncoder.dumps)


class UUIDFields(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid1)
    data = fields.UUIDField()
    data_auto = fields.UUIDField(default=uuid.uuid4)
    data_null = fields.UUIDField(null=True)


class MinRelation(Model):
    id = fields.IntField(primary_key=True)
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField("models.Tournament")
    participants: fields.ManyToManyRelation[Team] = fields.ManyToManyField("models.Team")


class M2MOne(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=255, null=True)
    two: fields.ManyToManyRelation[M2MTwo] = fields.ManyToManyField("models.M2MTwo", related_name="one")


class M2MTwo(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=255, null=True)

    one: fields.ManyToManyRelation[M2MOne]


class NoID(Model):
    name = fields.CharField(max_length=255, null=True)
    desc = fields.TextField(null=True)


class UniqueName(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, null=True, unique=True)
    optional = fields.CharField(max_length=20, null=True)
    other_optional = fields.CharField(max_length=20, null=True)


class PkOnly(Model):
    """Every field is generated (the auto-incrementing PK, and nothing else) - regression model
    for the empty-columns INSERT bug (see test_create_pk_only_model_actually_inserts_a_row)."""

    id = fields.IntField(primary_key=True)


class UniqueTogetherFields(Model):
    id = fields.IntField(primary_key=True)
    first_name = fields.CharField(max_length=64)
    last_name = fields.CharField(max_length=64)

    class Meta:
        constraints = (UniqueConstraint(fields=("first_name", "last_name")),)


class UniqueTogetherFieldsWithFK(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=64)
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField("models.Tournament")

    class Meta:
        constraints = (UniqueConstraint(fields=("text", "tournament")),)


class ImplicitPkModel(Model):
    value = fields.TextField()


class UUIDPkModel(Model):
    id = fields.UUIDField(primary_key=True)

    children: fields.ReverseRelation[UUIDFkRelatedModel]
    children_null: fields.ReverseRelation[UUIDFkRelatedNullModel]
    peers: fields.ManyToManyRelation[UUIDM2MRelatedModel]


class UUIDFkRelatedModel(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=50, null=True)
    model: fields.ForeignKeyRelation[UUIDPkModel] = fields.ForeignKeyField(
        "models.UUIDPkModel", related_name="children"
    )


class UUIDFkRelatedNullModel(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=50, null=True)
    model: fields.ForeignKeyNullableRelation[UUIDPkModel] = fields.ForeignKeyField(
        "models.UUIDPkModel", related_name=False, null=True
    )
    parent: fields.OneToOneNullableRelation[UUIDPkModel] = fields.OneToOneField(
        "models.UUIDPkModel", related_name=False, null=True, on_delete=NO_ACTION
    )


class UUIDM2MRelatedModel(Model):
    id = fields.UUIDField(primary_key=True)
    value = fields.TextField(default="test")
    models: fields.ManyToManyRelation[UUIDPkModel] = fields.ManyToManyField("models.UUIDPkModel", related_name="peers")


class UUIDPkSourceModel(Model):
    id = fields.UUIDField(primary_key=True, source_field="a")

    class Meta:
        table = "upsm"


class UUIDFkRelatedSourceModel(Model):
    id = fields.UUIDField(primary_key=True, source_field="b")
    name = fields.CharField(max_length=50, null=True, source_field="c")
    model: fields.ForeignKeyRelation[UUIDPkSourceModel] = fields.ForeignKeyField(
        "models.UUIDPkSourceModel", related_name="children", source_field="d"
    )

    class Meta:
        table = "ufrsm"


class UUIDFkRelatedNullSourceModel(Model):
    id = fields.UUIDField(primary_key=True, source_field="i")
    name = fields.CharField(max_length=50, null=True, source_field="j")
    model: fields.ForeignKeyNullableRelation[UUIDPkSourceModel] = fields.ForeignKeyField(
        "models.UUIDPkSourceModel",
        related_name="children_null",
        source_field="k",
        null=True,
    )

    class Meta:
        table = "ufrnsm"


class UUIDM2MRelatedSourceModel(Model):
    id = fields.UUIDField(primary_key=True, source_field="e")
    value = fields.TextField(default="test", source_field="f")
    models: fields.ManyToManyRelation[UUIDPkSourceModel] = fields.ManyToManyField(
        "models.UUIDPkSourceModel",
        related_name="peers",
        forward_key="e",
        backward_key="h",
    )

    class Meta:
        table = "umrsm"


class CharPkModel(Model):
    id = fields.CharField(max_length=64, primary_key=True)


class CharFkRelatedModel(Model):
    model: fields.ForeignKeyRelation[CharPkModel] = fields.ForeignKeyField(
        "models.CharPkModel", related_name="children"
    )


class CharM2MRelatedModel(Model):
    value = fields.TextField(default="test")
    models: fields.ManyToManyRelation[CharPkModel] = fields.ManyToManyField("models.CharPkModel", related_name="peers")


class TimestampMixin:
    created_at = fields.DatetimeField(null=True, auto_now_add=True)
    modified_at = fields.DatetimeField(null=True, auto_now=True)


class NameMixin:
    name = fields.CharField(40, unique=True)


class MyAbstractBaseModel(NameMixin, Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        abstract = True


class MyDerivedModel(TimestampMixin, MyAbstractBaseModel):
    first_name = fields.CharField(20, null=True)


class MyOtherDerivedModel(TimestampMixin, MyAbstractBaseModel):
    """A second concrete sibling of MyDerivedModel, sharing both MyAbstractBaseModel and
    TimestampMixin - exists to prove sibling models don't share Field instances (and thus don't
    corrupt each other's Field.model) between them."""

    last_name = fields.CharField(20, null=True)


class CommentModel(Model):
    class Meta:
        table = "comments"
        table_description = "Test Table comment"

    id = fields.IntField(primary_key=True, description="Primary key \r*/'`/*\n field for the comments")
    message = fields.TextField(description="Comment messages entered in the blog post")
    rating = fields.IntField(description="Upvotes done on the comment")
    escaped_comment_field = fields.TextField(description="This column acts as it's own comment")
    multiline_comment = fields.TextField(description="Some \n comment")
    commented_by = fields.TextField()


class Employee(Model):
    name = fields.CharField(max_length=50)

    manager: fields.ForeignKeyNullableRelation[Employee] = fields.ForeignKeyField(
        "models.Employee", related_name="team_members", null=True, on_delete=NO_ACTION
    )
    team_members: fields.ReverseRelation[Employee]

    talks_to: fields.ManyToManyRelation[Employee] = fields.ManyToManyField(
        "models.Employee", related_name="gets_talked_to", on_delete=NO_ACTION
    )
    gets_talked_to: fields.ManyToManyRelation[Employee]

    def __str__(self):
        return self.name

    async def full_hierarchy__async_for(self, level=0):
        """
        Demonstrates ``async for` to fetch relations

        An async iterator will fetch the relationship on-demand.
        """
        text = [
            "{}{} (to: {}) (from: {})".format(
                level * "  ",
                self,
                ", ".join(sorted([str(val) async for val in self.talks_to])),
                ", ".join(sorted([str(val) async for val in self.gets_talked_to])),
            )
        ]
        async for member in self.team_members:
            text.append(await member.full_hierarchy__async_for(level + 1))
        return "\n".join(text)

    async def full_hierarchy__fetch_related(self, level=0):
        """
        Demonstrates ``await .fetch_related`` to fetch relations

        On prefetching the data, the relationship files will contain a regular list.

        This is how one would get relations working on sync serialisation/templating frameworks.
        """
        await prefetch_related_objects([self], "team_members", "talks_to", "gets_talked_to")
        text = [
            "{}{} (to: {}) (from: {})".format(
                level * "  ",
                self,
                ", ".join(sorted(str(val) for val in self.talks_to)),
                ", ".join(sorted(str(val) for val in self.gets_talked_to)),
            )
        ]
        for member in self.team_members:
            text.append(await member.full_hierarchy__fetch_related(level + 1))
        return "\n".join(text)

    def name_length(self) -> int:
        # Computes length of name
        # Note that this function needs to be annotated with a return type so that pydantic
        # can generate a valid schema
        return len(self.name)

    def team_size(self) -> int:
        """
        Computes team size.

        Note that this function needs to be annotated with a return type so that pydantic can
         generate a valid schema.

        Note that the pydantic serializer can't call async methods, but the hare helpers
         pre-fetch relational data, so that it is available before serialization. So we don't
         need to await the relation. We do however have to protect against the case where no
         prefetching was done, hence catching and handling the
         ``hare.exceptions.NoValuesFetched`` exception.
        """
        try:
            return len(self.team_members)
        except (NoValuesFetched, AttributeError):
            return 0

    def not_annotated(self):
        raise NotImplementedError("Not Done")

    class Meta:
        ordering = ["id"]

    class PydanticMeta:
        computed = ["name_length", "team_size", "not_annotated"]
        exclude = ["manager", "gets_talked_to"]
        allow_cycles = True
        max_recursion = 2


class StraightFields(Model):
    eyedee = fields.IntField(primary_key=True, description="Da PK")
    chars = fields.CharField(max_length=50, db_index=True, description="Some chars")
    blip = fields.CharField(max_length=50, default="BLIP")
    nullable = fields.CharField(max_length=50, null=True)

    fk: fields.ForeignKeyNullableRelation[StraightFields] = fields.ForeignKeyField(
        "models.StraightFields",
        related_name="fkrev",
        null=True,
        description="Tree!",
        on_delete=NO_ACTION,
    )
    fkrev: fields.ReverseRelation[StraightFields]

    o2o: fields.OneToOneNullableRelation[StraightFields] = fields.OneToOneField(
        "models.StraightFields",
        related_name="o2o_rev",
        null=True,
        description="Line",
        on_delete=NO_ACTION,
    )
    o2o_rev: fields.Field

    rel_to: fields.ManyToManyRelation[StraightFields] = fields.ManyToManyField(
        "models.StraightFields",
        related_name="rel_from",
        description="M2M to myself",
        on_delete=fields.NO_ACTION,
    )
    rel_from: fields.ManyToManyRelation[StraightFields]

    class Meta:
        constraints = (UniqueConstraint(fields=("chars", "blip")),)
        table_description = "Straight auto-mapped fields"


class SourceFields(Model):
    """
    A Docstring.
    """

    eyedee = fields.IntField(primary_key=True, source_field="sometable_id", description="Da PK")
    # A regular comment
    chars = fields.CharField(
        max_length=50,
        source_field="some_chars_table",
        db_index=True,
        description="Some chars",
    )
    #: A docstring comment
    blip = fields.CharField(max_length=50, default="BLIP", source_field="da_blip")
    nullable = fields.CharField(max_length=50, null=True, source_field="some_nullable")

    fk: fields.ForeignKeyNullableRelation[SourceFields] = fields.ForeignKeyField(
        "models.SourceFields",
        related_name="fkrev",
        null=True,
        source_field="fk_sometable",
        description="Tree!",
        on_delete=NO_ACTION,
    )
    fkrev: fields.ReverseRelation[SourceFields]

    o2o: fields.OneToOneNullableRelation[SourceFields] = fields.OneToOneField(
        "models.SourceFields",
        related_name="o2o_rev",
        null=True,
        source_field="o2o_sometable",
        description="Line",
        on_delete=NO_ACTION,
    )
    o2o_rev: fields.Field

    rel_to: fields.ManyToManyRelation[SourceFields] = fields.ManyToManyField(
        "models.SourceFields",
        related_name="rel_from",
        through="sometable_self",
        forward_key="sts_forward",
        backward_key="backward_sts",
        description="M2M to myself",
        on_delete=fields.NO_ACTION,
    )
    rel_from: fields.ManyToManyRelation[SourceFields]

    class Meta:
        table = "sometable"
        constraints = (UniqueConstraint(fields=("chars", "blip")),)
        table_description = "Source mapped fields"


class Service(IntEnum):
    python_programming = 1
    database_design = 2
    system_administration = 3


class Currency(StrEnum):
    HUF = "HUF"
    EUR = "EUR"
    USD = "USD"


class EnumFields(Model):
    service: Service = fields.IntEnumField(Service)
    currency: Currency = fields.CharEnumField(Currency, default=Currency.HUF)


class DoubleFK(Model):
    name = fields.CharField(max_length=50)
    left: fields.ForeignKeyNullableRelation[DoubleFK] = fields.ForeignKeyField(
        "models.DoubleFK", null=True, related_name="left_rel", on_delete=NO_ACTION
    )
    right: fields.ForeignKeyNullableRelation[DoubleFK] = fields.ForeignKeyField(
        "models.DoubleFK", null=True, related_name="right_rel", on_delete=NO_ACTION
    )


class AliasSourceModelWithAVeryLongClassNameForTruncation(Model):
    name = fields.TextField()

    targets: fields.ReverseRelation["AliasTargetModelWithAVeryLongClassNameForTruncation"]


class AliasTargetModelWithAVeryLongClassNameForTruncation(Model):
    name = fields.TextField()
    source_with_a_very_long_relation_field_name_for_truncation: fields.ForeignKeyRelation[
        AliasSourceModelWithAVeryLongClassNameForTruncation
    ] = fields.ForeignKeyField(
        "models.AliasSourceModelWithAVeryLongClassNameForTruncation",
        related_name="targets",
    )


class LazyJoinedParent(Model):
    name = fields.TextField()

    children: fields.ReverseRelation["LazyJoinedChild"]
    composite_children: fields.ReverseRelation["LazyJoinedCompositeOwnerChild"]


class LazyJoinedChild(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[LazyJoinedParent] = fields.ForeignKeyField(
        "models.LazyJoinedParent", related_name="children", lazy="joined"
    )


class LazySelectParent(Model):
    name = fields.TextField()

    children: fields.ReverseRelation["LazySelectChild"]


class LazySelectChild(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[LazySelectParent] = fields.ForeignKeyField(
        "models.LazySelectParent", related_name="children", lazy="select"
    )


class LazyPlainChild(Model):
    """No lazy= at all - the regression control."""

    name = fields.TextField()
    parent: fields.ForeignKeyRelation[LazyJoinedParent] = fields.ForeignKeyField(
        "models.LazyJoinedParent", related_name="plain_children"
    )


class LazyJoinedCompositeOwnerChild(Model):
    """lazy='joined' declared on a forward FK owned by a composite-PK model - the FK owner having
    a composite PK is orthogonal to how its target gets auto-joined; this combination used to
    crash Hare.init() entirely (get_backward_fk_filters read the owner's nonexistent single .pk),
    not something specific to lazy= itself."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[LazyJoinedParent] = fields.ForeignKeyField(
        "models.LazyJoinedParent", related_name="composite_children", lazy="joined"
    )

    pk = fields.CompositePrimaryKey("a", "b")


class LazySelectSoftDeleteParent(Model):
    """FK target of a lazy='select' auto-prefetch that also has its own soft_delete_field - the
    auto-prefetch must respect the parent's own default manager filter, same as an explicit
    .prefetch_related() would."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    children: fields.ReverseRelation["LazySelectSoftDeleteChild"]

    class Meta:
        soft_delete_field = "deleted_at"


class LazySelectSoftDeleteChild(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[LazySelectSoftDeleteParent] = fields.ForeignKeyField(
        "models.LazySelectSoftDeleteParent", related_name="children", lazy="select"
    )


class LazyM2MRight(Model):
    name = fields.TextField()


class LazyM2MLeft(Model):
    name = fields.TextField()
    rights: fields.ManyToManyRelation[LazyM2MRight] = fields.ManyToManyField(
        "models.LazyM2MRight", related_name="lefts", lazy="select"
    )


class LazySelectMentee(Model):
    """A self-referential lazy='select' FK plus a lazy='select' M2M on the same model."""

    name = fields.TextField()
    mentor: fields.ForeignKeyNullableRelation[LazySelectMentee] = fields.ForeignKeyField(
        "models.LazySelectMentee", related_name="mentees", null=True, lazy="select", on_delete=SET_NULL
    )
    skills: fields.ManyToManyRelation[LazyM2MRight] = fields.ManyToManyField(
        "models.LazyM2MRight", related_name="skilled_mentees", lazy="select"
    )


class DirtyTrackedThing(Model):
    name = fields.TextField()
    count = fields.IntField(default=0)
    nullable = fields.TextField(null=True)
    data = fields.JSONField(default=dict)

    class Meta:
        track_dirty_fields = True


class DirtyTrackedDbDefault(Model):
    """A db_default field left to the database - its placeholder must not read as dirty."""

    name = fields.TextField()
    counter = fields.IntField(db_default=3)

    class Meta:
        track_dirty_fields = True


class DirtyTrackedUnique(Model):
    """track_dirty_fields plus a unique column, to force a real ON CONFLICT for
    bulk_create(ignore_conflicts=True)."""

    sku = fields.CharField(max_length=20, unique=True)
    name = fields.TextField()

    class Meta:
        track_dirty_fields = True


class DirtyTrackedParent(Model):
    """Both this and DirtyTrackedChild have track_dirty_fields - a related object hydrated via
    select_related()/prefetch_related() must get its own independent snapshot, not share one with
    (or leak changes into) the model it was fetched through."""

    name = fields.TextField()
    children: fields.ReverseRelation["DirtyTrackedChild"]

    class Meta:
        track_dirty_fields = True


class DirtyTrackedChild(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[DirtyTrackedParent] = fields.ForeignKeyField(
        "models.DirtyTrackedParent", related_name="children"
    )

    class Meta:
        track_dirty_fields = True


class DirtyTrackedComposite(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        track_dirty_fields = True


class CompositePkThing(Model):
    thing_id = fields.IntField()
    revision = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("thing_id", "revision")


class CompositePkTriple(Model):
    """3-column composite PK, to prove it's not hardcoded to a pair."""

    a = fields.IntField()
    b = fields.IntField()
    c = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b", "c")


class CompositePkVersioned(Model):
    """VersionedModel-shaped composite PK: a stable id plus a version component carrying its
    own default= - exercises clone()'s per-component default resolution and Pydantic schema
    generation for a composite PK with a defaulted member, not just a toy 2-plain-column PK."""

    id = fields.IntField()
    version = fields.IntField(default=1)
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("id", "version")


class CompositePkBothDefaulted(Model):
    """Every composite PK component carries its own default= - proves clone() resolves each
    component independently instead of raising unconditionally for any composite PK."""

    a = fields.IntField(default=1)
    b = fields.IntField(default=2)
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


async def _composite_pk_async_default_version() -> int:
    return 7


class CompositePkAsyncDefault(Model):
    """A composite PK component with a coroutine default= - clone() must defer it into
    _await_when_save the same way a single-column PK's async default already does."""

    id = fields.IntField(default=1)
    version = fields.IntField(default=_composite_pk_async_default_version)
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("id", "version")


class CompositePkDbDefault(Model):
    """A composite PK component with only a db_default= (no Python-side default) - clone() must
    send the DatabaseDefault sentinel for it, same as a single-column db_default-only PK."""

    id = fields.IntField(default=1)
    version = fields.IntField(db_default=1)
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("id", "version")


class CompositePkOwningFK(Model):
    """A composite-PK model that itself owns a forward FK to a normal, single-PK model - Django
    allows this (only a composite PK being the *target* of a relation is unsupported), so this
    must init and filter cleanly too, exercising the backward relation registered on Tournament."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="composite_owners"
    )

    pk = fields.CompositePrimaryKey("a", "b")


class VersionedDocument(VersionedModel):
    title = fields.TextField()


class VersionedDocumentWithExcludedField(VersionedModel):
    title = fields.TextField()
    single_use_token = fields.TextField(default="unused")

    class Meta:
        new_version_excluded_fields = ("single_use_token",)


class VersionedDocumentWithAuthor(VersionedModel):
    """A regular (single-column-PK-target) forward FK on a VersionedModel row - the FK is just an
    ordinary field like any other, unrelated to the id/version composite PK itself."""

    title = fields.TextField()
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(
        "models.Author", related_name="versioned_documents"
    )


class JSONFieldsDeclaredType(Model):
    """JSONFields whose field_type is a pydantic model and a list of them."""

    id = fields.IntField(primary_key=True)
    item = fields.JSONField[TestSchemaForJSONField](field_type=TestSchemaForJSONField, null=True)
    items = fields.JSONField[list[TestSchemaForJSONField]](field_type=list[TestSchemaForJSONField], null=True)


class VersionedDocumentWithReviewer(VersionedModel):
    """Two nullable forward FKs on a VersionedModel row, one of them listed in
    Meta.new_version_excluded_fields by its relation name (not its shadow column)."""

    title = fields.TextField()
    body = fields.TextField(default="")
    author: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField(
        "models.Author", related_name="authored_versioned_reviews", null=True
    )
    reviewer: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField(
        "models.Author", related_name="reviewed_versioned_documents", null=True
    )

    class Meta:
        new_version_excluded_fields = ("reviewer",)


class VersionedDirtyTrackedDocument(VersionedModel):
    class Meta:
        track_dirty_fields = True

    title = fields.TextField()


class VersionedDocumentWithJson(VersionedModel):
    title = fields.TextField()
    data = fields.JSONField[dict](default=dict)


class VersionedDocumentWithSoftDelete(VersionedModel):
    """Both VersionedModel (append-only "new row per change") and Meta.soft_delete_field (an
    in-place delete()/restore() flag) on the same model - get_new_version() must NOT carry the
    old version's deleted_at forward onto the brand-new row it builds."""

    title = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class VersionedDocumentWithComputedColumns(VersionedModel):
    """A database-computed column and an auto_now column on a VersionedModel row."""

    title = fields.TextField()
    price = fields.IntField(default=1)
    quantity = fields.IntField(default=2)
    total = GeneratedField(expression=RawSQLTerm("price * quantity"), output_field=fields.IntField(), stored=True)
    created = fields.DatetimeField(auto_now_add=True)
    modified = fields.DatetimeField(auto_now=True)


class DocumentRevisionNote(Model):
    """A FK to a *specific version* of a VersionedModel row - the exact target use case a
    composite-target FK/O2O exists for: to_field is left unset, defaulting to the whole
    id+version composite PK, and gets a real table-level FOREIGN KEY constraint (see
    test_operations_real_db.py's migration-path tests, and the generate_schemas() path already
    covered by test_composite_primary_key.py)."""

    document: fields.ForeignKeyRelation[VersionedDocument] = fields.ForeignKeyField(
        "models.VersionedDocument", related_name="revision_notes"
    )
    note = fields.TextField()


class VersionedTag(VersionedModel):
    name = fields.TextField()


class VersionedArticle(VersionedModel):
    """A ManyToManyField between two VersionedModel rows - the M2M counterpart of
    DocumentRevisionNote's FK, target use case for composite-target M2M through-table support."""

    title = fields.TextField()
    tags: fields.ManyToManyRelation[VersionedTag] = fields.ManyToManyField(
        "models.VersionedTag", related_name="articles"
    )


class VersionedThing(Model):
    name = fields.TextField()
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"


class VersionedUnique(Model):
    """optimistic_lock_field combined with a unique field on a DIFFERENT column - lets a save() fail
    with a real IntegrityError (not just the "0 rows affected" staleness case) to test that the
    in-memory version bump gets rolled back on ANY save failure, not only a stale-version one."""

    name = fields.TextField()
    tag = fields.CharField(max_length=50, unique=True)
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"


class VersionedUniqueAutoNow(Model):
    """Same shape as VersionedUnique (a unique field on a different column to force a real
    IntegrityError, not just the "0 rows affected" staleness case) plus an auto_now field -
    save()'s to_db_value() call mutates auto_now fields in-memory before the write outcome is
    known, same as it does optimistic_lock_field, so a failed save must roll both back."""

    name = fields.TextField()
    tag = fields.CharField(max_length=50, unique=True)
    version = fields.IntField(default=0)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        optimistic_lock_field = "version"


class VersionedComposite(Model):
    """optimistic_lock_field on a model that also has a composite PK - optimistic_lock_field's own WHERE-clause
    addition is just one more AND alongside however many pk columns precede it."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    version = fields.IntField(default=0)

    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        optimistic_lock_field = "version"


class VersionedDirtyTracked(Model):
    """optimistic_lock_field combined with track_dirty_fields - the version bump itself must not leak
    into get_dirty_fields() after a successful save() (save() already re-snapshots everything
    afterward, so this is really confirming that path, not adding new logic)."""

    name = fields.TextField()
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"
        track_dirty_fields = True


class SoftDeleteStandalone(Model):
    """No incoming relations at all - exercises the bulk-delete fast path."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteVersioned(Model):
    """Both soft_delete_field and optimistic_lock_field on the same model - delete()/restore()'s
    soft-delete UPDATE goes through the same executor path save() uses, which bumps optimistic_lock_field
    unconditionally regardless of what's in update_fields."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=0)

    class Meta:
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"


class SoftDeleteDirtyTracked(Model):
    """Both soft_delete_field and track_dirty_fields - delete()/restore() bypass save(), so they
    need their own targeted dirty-snapshot sync rather than a full re-snapshot (which would
    incorrectly launder any OTHER unsaved field change as clean too)."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"
        track_dirty_fields = True


class SoftDeleteComposite(Model):
    """Composite PK on a soft_delete_field model - single-instance delete()/restore() only. A
    composite-PK model can still take incoming FK/O2O relations (see
    test_composite_primary_key.py's own cascade tests for that) - this model just doesn't declare
    any, so its own soft-delete cascade walk is a no-op by construction, not something this model
    needs to exercise. M2M targeting/owning a composite-PK model is still rejected outright (see
    test_composite_primary_key.py's rejection tests)."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteGhostChild(Model):
    """FK to SoftDeleteVersioned with no cascade concern (on_delete=NO_ACTION is never actually
    triggered in its own test - the point is a NEW row created pointing at an ALREADY-soft-deleted
    parent, which never goes through the cascade-delete path at all)."""

    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation["SoftDeleteVersioned"] = fields.ForeignKeyField(
        "models.SoftDeleteVersioned", related_name="ghost_children", null=True, on_delete=NO_ACTION
    )


class SoftDeleteParent(Model):
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    cascade_children: fields.ReverseRelation["SoftDeleteChildCascadeSoft"]
    cascade_children_with_override: fields.ReverseRelation["SoftDeleteChildCascadeSoftWithDeleteOverride"]
    cascade_hard_children: fields.ReverseRelation["SoftDeleteChildCascadeHard"]
    protected_children: fields.ReverseRelation["SoftDeleteChildProtect"]
    nullable_children: fields.ReverseRelation["SoftDeleteChildSetNull"]
    restricted_children: fields.ReverseRelation["SoftDeleteChildRestrict"]
    protected_o2o_child: fields.OneToOneRelation["SoftDeleteChildProtectO2O"]
    set_default_callable_children: fields.ReverseRelation["SoftDeleteChildSetDefaultCallable"]

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteChildCascadeSoft(Model):
    """CASCADE onto a child that ALSO has soft_delete_field - the recursive delete() call must
    soft-delete the child too, not hard-delete it."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="cascade_children", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteChildCascadeSoftWithDeleteOverride(Model):
    """CASCADE onto a child that ALSO has soft_delete_field AND overrides delete() - the
    cascade's own iterative fast path must not silently skip this override just because the
    child's own soft-delete dispatch mode matches the cascade's."""

    delete_override_calls: ClassVar[list[Any]] = []

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="cascade_children_with_override", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class SoftDeleteChildCascadeHard(Model):
    """CASCADE onto a child with NO soft_delete_field - the recursive delete() call must
    hard-delete it, matching what a real DB-level ON DELETE CASCADE would have done."""

    name = fields.TextField()
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="cascade_hard_children", on_delete=CASCADE
    )


class SoftDeleteChildProtect(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="protected_children", on_delete=PROTECT
    )


class SoftDeleteChildSetNull(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="nullable_children", null=True, on_delete=SET_NULL
    )


class SoftDeleteChildRestrict(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent", related_name="restricted_children", on_delete=NO_ACTION
    )


class SoftDeleteChildProtectO2O(Model):
    """A OneToOneField (not ForeignKeyField) with on_delete=PROTECT - backward_one_to_one_fields, not
    backward_foreign_key_fields, so this exercises the same PROTECT/CASCADE/SET_NULL/SET_DEFAULT cascade
    machinery through the other relation type that shares its shape."""

    name = fields.TextField()
    parent: fields.OneToOneRelation[SoftDeleteParent] = fields.OneToOneField(
        "models.SoftDeleteParent", related_name="protected_o2o_child", on_delete=PROTECT
    )


class SoftDeleteChildO2O(Model):
    """A soft-deletable OneToOneField child - bare lookups on its backward relation must hide it
    once soft-deleted."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.OneToOneRelation[SoftDeleteParent] = fields.OneToOneField(
        "models.SoftDeleteParent", related_name="soft_o2o_child", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteSelfReferentialChain(Model):
    """Self-referential on_delete=CASCADE, with Meta.soft_delete_field set - builds a genuinely
    deep chain (thousands of rows, each pointing at the next) to prove
    ReverseRelationCascade._apply_cascade()'s own walk is iterative, not recursive: the old
    recursive version (one nested `await related_obj.delete()` per level) overflowed Python's
    call stack for a chain this deep, regardless of db_constraint."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyNullableRelation["SoftDeleteSelfReferentialChain"] = fields.ForeignKeyField(
        "models.SoftDeleteSelfReferentialChain", related_name="children", null=True, on_delete=CASCADE
    )
    children: fields.ReverseRelation["SoftDeleteSelfReferentialChain"]

    class Meta:
        soft_delete_field = "deleted_at"


class HardDeleteSelfReferentialChain(Model):
    """Self-referential on_delete=CASCADE with a real DB-level FK constraint (default
    db_constraint=True) and no Meta.soft_delete_field - a genuinely deep chain here is deleted by
    a single real DELETE, relying entirely on the database's own native FK cascade to remove every
    descendant, unlike SoftDeleteSelfReferentialChain's Python-side iterative walk."""

    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation["HardDeleteSelfReferentialChain"] = fields.ForeignKeyField(
        "models.HardDeleteSelfReferentialChain", related_name="children", null=True, on_delete=CASCADE
    )
    children: fields.ReverseRelation["HardDeleteSelfReferentialChain"]


class HardDeleteChainWithRestrictEdge(Model):
    """Same self-referential CASCADE shape as HardDeleteSelfReferentialChain, plus a SEPARATE
    self-referential on_delete=RESTRICT field ('restrictor') between two nodes of the same tree -
    exercises ReverseRelationCascade.apply_hard_delete_cascade_for_all_relations's
    restrict_check_exclude (a RESTRICT/NO_ACTION check against a row that's ALSO part of this same
    cascade tree must not false-positive just because that row hasn't been persisted yet under
    bottom_up_persist)."""

    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation["HardDeleteChainWithRestrictEdge"] = fields.ForeignKeyField(
        "models.HardDeleteChainWithRestrictEdge", related_name="children", null=True, on_delete=CASCADE
    )
    children: fields.ReverseRelation["HardDeleteChainWithRestrictEdge"]
    restrictor: fields.ForeignKeyNullableRelation["HardDeleteChainWithRestrictEdge"] = fields.ForeignKeyField(
        "models.HardDeleteChainWithRestrictEdge", related_name="restricted_by", null=True, on_delete=RESTRICT
    )
    restricted_by: fields.ReverseRelation["HardDeleteChainWithRestrictEdge"]


class DiamondCascadeTop(Model):
    """Root of a cascade diamond: CASCADEs onto two independent children
    (DiamondCascadeChildB/DiamondCascadeChildC) that both, in turn, CASCADE onto the SAME
    DiamondCascadeSharedDescendant row - like a join table with two FKs converging on one common
    target. Exercises ReverseRelationCascade._apply_cascade()'s iterative wave-based walk when a
    descendant is reachable through two different parents in the same wave."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    b_children: fields.ReverseRelation["DiamondCascadeChildB"]
    c_children: fields.ReverseRelation["DiamondCascadeChildC"]

    class Meta:
        soft_delete_field = "deleted_at"


class DiamondCascadeChildB(Model):
    """One arm of the cascade diamond - see DiamondCascadeTop's docstring."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    top: fields.ForeignKeyRelation[DiamondCascadeTop] = fields.ForeignKeyField(
        "models.DiamondCascadeTop", related_name="b_children", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"


class DiamondCascadeChildC(Model):
    """Other arm of the cascade diamond - see DiamondCascadeTop's docstring."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    top: fields.ForeignKeyRelation[DiamondCascadeTop] = fields.ForeignKeyField(
        "models.DiamondCascadeTop", related_name="c_children", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"


class DiamondCascadeSharedDescendant(Model):
    """Reachable from DiamondCascadeTop through TWO different cascade paths in the same wave
    (both DiamondCascadeChildB and DiamondCascadeChildC CASCADE onto the same row here). Both
    soft_delete_field and optimistic_lock_field are set, matching the real-world shape (optimistic locking
    plus soft delete) that exposed a frontier-deduplication bug: without dedup, this row was
    queued and persisted twice within the same wave, and the second persist saw a stale in-memory
    version and raised StaleObjectError, rolling back the whole cascade."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=0)
    via_b: fields.ForeignKeyRelation[DiamondCascadeChildB] = fields.ForeignKeyField(
        "models.DiamondCascadeChildB", related_name="shared_descendants", on_delete=CASCADE
    )
    via_c: fields.ForeignKeyRelation[DiamondCascadeChildC] = fields.ForeignKeyField(
        "models.DiamondCascadeChildC", related_name="shared_descendants", on_delete=CASCADE
    )

    class Meta:
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"


def _default_category_id() -> int:
    """A callable default, matching how a real dynamic default (e.g. "the current system
    default category") would be declared - not a static literal."""
    return 999


class SoftDeleteChildSetDefaultCallable(Model):
    """A SET_DEFAULT field whose default= is a callable, not a static value. The real FK
    constraint requires a db_default as well (the database performs its own ON DELETE SET DEFAULT
    from it) - the soft-delete cascade still prefers the callable default=."""

    name = fields.TextField()
    parent: fields.ForeignKeyRelation[SoftDeleteParent] = fields.ForeignKeyField(
        "models.SoftDeleteParent",
        related_name="set_default_callable_children",
        on_delete=SET_DEFAULT,
        default=_default_category_id,
        db_default=999,
    )


class SoftDeleteM2MPeer(Model):
    """The "other side" of a soft-deletable M2M relation - also soft-deletable itself, so both
    the forward-declared field's on_delete (exercised by soft-deleting SoftDeleteM2MParent) and
    the auto-generated backward field's on_delete (exercised by soft-deleting this model, via its
    `parents` field) get covered."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    restricting_parents: fields.ReverseRelation["SoftDeleteM2MRestrictedParent"]
    protecting_parents: fields.ReverseRelation["SoftDeleteM2MProtectedParent"]
    nullable_parents: fields.ReverseRelation["SoftDeleteM2MSetNullParent"]

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteM2MParent(Model):
    """M2M relations are invisible to get_backward_relations() (FK/O2O only) - the through table
    otherwise keeps "live" links to a soft-deleted row forever unless the cascade also walks
    many_to_many_fields directly."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    peers: fields.ManyToManyRelation[SoftDeleteM2MPeer] = fields.ManyToManyField(
        "models.SoftDeleteM2MPeer", related_name="parents"
    )

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteM2MRestrictedParent(Model):
    """on_delete=NO_ACTION on a ManyToManyField - SET_DEFAULT is rejected at field-construction
    time for M2M (no way to declare a default target row for a through-table column), so
    CASCADE/RESTRICT/NO_ACTION/PROTECT/SET_NULL are the cases the cascade needs to handle (see
    SoftDeleteM2MProtectedParent/SoftDeleteM2MSetNullParent for the latter two)."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    restricted_peers: fields.ManyToManyRelation[SoftDeleteM2MPeer] = fields.ManyToManyField(
        "models.SoftDeleteM2MPeer", related_name="restricting_parents", on_delete=NO_ACTION
    )

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteM2MProtectedParent(Model):
    """on_delete=PROTECT on a ManyToManyField - soft-deleting (or hard-deleting) an instance while
    a through-table row still links it to a peer must raise ProtectedError, checked in Python by
    ReverseRelationCascade.check_protected before soft-delete's own UPDATE ever runs."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    protected_peers: fields.ManyToManyRelation[SoftDeleteM2MPeer] = fields.ManyToManyField(
        "models.SoftDeleteM2MPeer", related_name="protecting_parents", on_delete=PROTECT
    )

    class Meta:
        soft_delete_field = "deleted_at"


class SoftDeleteM2MSetNullParent(Model):
    """on_delete=SET_NULL on a ManyToManyField - soft-deleting this model must null out this
    field's own backward_keys column(s) on through-table rows pointing at it (keeping the row,
    unlike CASCADE's delete), reproduced in Python since soft-delete never issues a real DELETE
    for the database's own FK constraint to act on."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    nullable_peers: fields.ManyToManyRelation[SoftDeleteM2MPeer] = fields.ManyToManyField(
        "models.SoftDeleteM2MPeer", related_name="nullable_parents", on_delete=SET_NULL
    )

    class Meta:
        soft_delete_field = "deleted_at"


class HardDeleteUnconstrainedParent(Model):
    """No Meta.soft_delete_field - a real hard DELETE. Every child below points back via
    db_constraint=False, so the schema generator never emits a real FK constraint for any of
    them (see BaseSchemaGenerator) - nothing at the database level enforces their on_delete
    either, unless Model.delete()/QuerySet.delete() run the same Python-side cascade the
    soft-delete path above already runs unconditionally."""

    name = fields.TextField()

    cascade_children: fields.ReverseRelation["HardDeleteUnconstrainedChildCascade"]
    nullable_children: fields.ReverseRelation["HardDeleteUnconstrainedChildSetNull"]
    restricted_children: fields.ReverseRelation["HardDeleteUnconstrainedChildRestrict"]
    set_default_children: fields.ReverseRelation["HardDeleteUnconstrainedChildSetDefault"]
    protected_children: fields.ReverseRelation["HardDeleteUnconstrainedChildProtect"]
    peers: fields.ManyToManyRelation["HardDeleteUnconstrainedM2MPeer"] = fields.ManyToManyField(
        "models.HardDeleteUnconstrainedM2MPeer", related_name="parents", db_constraint=False
    )
    protected_peers: fields.ManyToManyRelation["HardDeleteUnconstrainedM2MPeer"] = fields.ManyToManyField(
        "models.HardDeleteUnconstrainedM2MPeer",
        related_name="protecting_parents",
        through="harddeleteunconstrained_protected_peer",
        on_delete=PROTECT,
        db_constraint=False,
    )
    nullable_peers: fields.ManyToManyRelation["HardDeleteUnconstrainedM2MPeer"] = fields.ManyToManyField(
        "models.HardDeleteUnconstrainedM2MPeer",
        related_name="nullable_peer_parents",
        through="harddeleteunconstrained_nullable_peer",
        on_delete=SET_NULL,
        db_constraint=False,
    )


class HardDeleteUnconstrainedChildProtect(Model):
    """PROTECT already worked unconditionally before this bugfix (check_protected() has no
    db_constraint dependency) - kept here as a regression guard that the CASCADE/SET_NULL/
    SET_DEFAULT/RESTRICT fix for db_constraint=False didn't disturb it."""

    name = fields.TextField()
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParent] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParent",
        related_name="protected_children",
        on_delete=PROTECT,
        db_constraint=False,
    )


class HardDeleteUnconstrainedChildCascade(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParent] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParent",
        related_name="cascade_children",
        on_delete=CASCADE,
        db_constraint=False,
    )


class HardDeleteUnconstrainedChildSetNull(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation[HardDeleteUnconstrainedParent] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParent",
        related_name="nullable_children",
        null=True,
        on_delete=SET_NULL,
        db_constraint=False,
    )


class HardDeleteUnconstrainedChildRestrict(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParent] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParent",
        related_name="restricted_children",
        on_delete=NO_ACTION,
        db_constraint=False,
    )


class HardDeleteUnconstrainedChildSetDefault(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParent] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParent",
        related_name="set_default_children",
        on_delete=SET_DEFAULT,
        default=_default_category_id,
        db_constraint=False,
    )


class SetDefaultDbDefaultParent(Model):
    """Target of the real-FK-constraint SET_DEFAULT children below - a hard-delete model, so a
    delete relies entirely on the database's own ON DELETE SET DEFAULT."""

    name = fields.TextField()
    nullable_children: fields.ReverseRelation["SetDefaultDbDefaultChildNullable"]
    required_children: fields.ReverseRelation["SetDefaultDbDefaultChildRequired"]
    one_to_one_child: fields.OneToOneRelation["SetDefaultDbDefaultChildOneToOne"]


class SetDefaultDbDefaultChildNullable(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation[SetDefaultDbDefaultParent] = fields.ForeignKeyField(
        "models.SetDefaultDbDefaultParent",
        related_name="nullable_children",
        null=True,
        on_delete=SET_DEFAULT,
        db_default=999,
    )


class SetDefaultDbDefaultChildRequired(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[SetDefaultDbDefaultParent] = fields.ForeignKeyField(
        "models.SetDefaultDbDefaultParent",
        related_name="required_children",
        on_delete=SET_DEFAULT,
        db_default=999,
    )


class SetDefaultDbDefaultChildOneToOne(Model):
    name = fields.TextField()
    parent: fields.OneToOneRelation[SetDefaultDbDefaultParent] = fields.OneToOneField(
        "models.SetDefaultDbDefaultParent",
        related_name="one_to_one_child",
        on_delete=SET_DEFAULT,
        db_default=999,
    )


class HardDeleteUnconstrainedM2MPeer(Model):
    name = fields.TextField()
    parents: fields.ReverseRelation["HardDeleteUnconstrainedParent"]
    protecting_parents: fields.ReverseRelation["HardDeleteUnconstrainedParent"]
    nullable_peer_parents: fields.ReverseRelation["HardDeleteUnconstrainedParent"]


class HardDeleteUnconstrainedParentWithSoftDeletedChildren(Model):
    """Same shape as HardDeleteUnconstrainedParent - db_constraint=False children, no real FK
    constraint enforcing anything - but each child below ALSO has its own Meta.soft_delete_field,
    to exercise check_protected()/RESTRICT's own existence check against a child row that's
    already been soft-deleted (still physically present, FK still pointing at this parent) but
    would otherwise be invisible to a plain .filter()/.exists() through the child's own ambient
    soft-delete scope."""

    name = fields.TextField()
    protected_children: fields.ReverseRelation["HardDeleteUnconstrainedSoftDeletedChildProtect"]
    restricted_children: fields.ReverseRelation["HardDeleteUnconstrainedSoftDeletedChildRestrict"]


class HardDeleteUnconstrainedSoftDeletedChildProtect(Model):
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParentWithSoftDeletedChildren] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParentWithSoftDeletedChildren",
        related_name="protected_children",
        on_delete=PROTECT,
        db_constraint=False,
    )

    class Meta:
        soft_delete_field = "deleted_at"


class HardDeleteUnconstrainedSoftDeletedChildRestrict(Model):
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyRelation[HardDeleteUnconstrainedParentWithSoftDeletedChildren] = fields.ForeignKeyField(
        "models.HardDeleteUnconstrainedParentWithSoftDeletedChildren",
        related_name="restricted_children",
        on_delete=NO_ACTION,
        db_constraint=False,
    )

    class Meta:
        soft_delete_field = "deleted_at"


class TransitiveCascadeGrandparent(Model):
    """Root of a hard-delete chain reached through a real, db_constraint=True FK CASCADE
    (TransitiveCascadeParent.grandparent below) before ever reaching a db_constraint=False
    relation - unlike HardDeleteUnconstrainedParent above, whose own children are unconstrained
    directly. Deleting this instance must still discover, and correctly act on,
    TransitiveCascadeParent's own db_constraint=False children even though Python itself never
    deletes TransitiveCascadeParent (the database's real ON DELETE CASCADE constraint does)."""

    name = fields.TextField()
    children: fields.ReverseRelation["TransitiveCascadeParent"]


class TransitiveCascadeParent(Model):
    """Reached from TransitiveCascadeGrandparent via a real (db_constraint=True) FK CASCADE
    constraint - the database deletes this row on its own once the grandparent is deleted, but
    its own children below are all db_constraint=False, with no real FK constraint enforcing
    their on_delete either."""

    name = fields.TextField()
    grandparent: fields.ForeignKeyRelation[TransitiveCascadeGrandparent] = fields.ForeignKeyField(
        "models.TransitiveCascadeGrandparent", related_name="children", on_delete=CASCADE
    )

    cascade_children: fields.ReverseRelation["TransitiveCascadeChildCascade"]
    nullable_children: fields.ReverseRelation["TransitiveCascadeChildSetNull"]
    restricted_children: fields.ReverseRelation["TransitiveCascadeChildRestrict"]
    protected_children: fields.ReverseRelation["TransitiveCascadeChildProtect"]


class TransitiveCascadeChildCascade(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[TransitiveCascadeParent] = fields.ForeignKeyField(
        "models.TransitiveCascadeParent",
        related_name="cascade_children",
        on_delete=CASCADE,
        db_constraint=False,
    )


class TransitiveCascadeChildSetNull(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation[TransitiveCascadeParent] = fields.ForeignKeyField(
        "models.TransitiveCascadeParent",
        related_name="nullable_children",
        null=True,
        on_delete=SET_NULL,
        db_constraint=False,
    )


class TransitiveCascadeChildRestrict(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[TransitiveCascadeParent] = fields.ForeignKeyField(
        "models.TransitiveCascadeParent",
        related_name="restricted_children",
        on_delete=NO_ACTION,
        db_constraint=False,
    )


class TransitiveCascadeChildProtect(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[TransitiveCascadeParent] = fields.ForeignKeyField(
        "models.TransitiveCascadeParent",
        related_name="protected_children",
        on_delete=PROTECT,
        db_constraint=False,
    )


class M2MOnDeleteProtectPeer(Model):
    name = fields.TextField()
    protecting_parents: fields.ReverseRelation["M2MOnDeleteProtectParent"]


class M2MOnDeleteProtectParent(Model):
    """on_delete=PROTECT on a plain (db_constraint=True, the default) ManyToManyField - the
    through-table's real FK constraint itself falls back to a deferred NO ACTION in DDL (PROTECT
    has no SQL keyword of its own, see ManyToManyFieldInstance.db_on_delete), so this exercises the
    Python-side ReverseRelationCascade.check_protected/check_protected_bulk check that runs
    before either a real hard DELETE or a soft-delete UPDATE is issued, independent of
    db_constraint."""

    name = fields.TextField()
    peers: fields.ManyToManyRelation[M2MOnDeleteProtectPeer] = fields.ManyToManyField(
        "models.M2MOnDeleteProtectPeer", related_name="protecting_parents", on_delete=PROTECT
    )


class M2MOnDeleteSetNullPeer(Model):
    name = fields.TextField()
    parents: fields.ReverseRelation["M2MOnDeleteSetNullParent"]


class M2MOnDeleteSetNullParent(Model):
    """on_delete=SET_NULL on a plain (db_constraint=True, the default) ManyToManyField - the
    through-table's own FK columns are nullable for this field (see BaseSchemaGenerator.
    _get_m2m_side_columns), so a real hard DELETE relies entirely on the database's own
    ON DELETE SET NULL constraint action to null the through row's backward key rather than
    delete the row - no Python-side cascade runs at all here (has_unconstrained_relations is
    False for a db_constraint=True relation)."""

    name = fields.TextField()
    peers: fields.ManyToManyRelation[M2MOnDeleteSetNullPeer] = fields.ManyToManyField(
        "models.M2MOnDeleteSetNullPeer", related_name="parents", on_delete=SET_NULL
    )


class CompositePkM2MProtectPeer(Model):
    name = fields.TextField()
    protecting_owners: fields.ReverseRelation["CompositePkM2MProtectOwner"]


class CompositePkM2MProtectOwner(Model):
    """Composite-PK model owning a PROTECT ManyToManyField - exercises
    ReverseRelationCascade.check_protected_bulk()'s composite-PK branch (no single `__in=` filter
    exists for a composite-PK model, so it ORs one equality Q per pk tuple instead)."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    peers: fields.ManyToManyRelation[CompositePkM2MProtectPeer] = fields.ManyToManyField(
        "models.CompositePkM2MProtectPeer", related_name="protecting_owners", on_delete=PROTECT
    )

    pk = fields.CompositePrimaryKey("a", "b")


class CompositePkM2MFilterTarget(Model):
    """Composite-PK model targeted BY a ManyToManyField (unlike CompositePkM2MProtectOwner
    above, which OWNS one) - exercises get_m2m_filters()'s composite-PK branch (`=`/`__not`/
    `__in`/`__not_in`) rather than check_protected_bulk()'s."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    owners: fields.ReverseRelation["CompositePkM2MFilterOwner"]

    pk = fields.CompositePrimaryKey("a", "b")


class CompositePkM2MFilterOwner(Model):
    name = fields.TextField()
    peers: fields.ManyToManyRelation[CompositePkM2MFilterTarget] = fields.ManyToManyField(
        "models.CompositePkM2MFilterTarget", related_name="owners"
    )


class ProtectedParent(Model):
    name = fields.TextField()

    children: fields.ReverseRelation["ProtectedChild"]


class ProtectedChild(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[ProtectedParent] = fields.ForeignKeyField(
        "models.ProtectedParent", related_name="children", on_delete=PROTECT
    )


class ProtectedParentWithCode(Model):
    """PK is 'id', but the FK below targets 'code' (a separate unique column) - the shadow FK
    column on ProtectedChildByCode stores a code value, not an id value."""

    id = fields.IntField(primary_key=True)
    code = fields.IntField(unique=True)

    children: fields.ReverseRelation["ProtectedChildByCode"]


class ProtectedChildByCode(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[ProtectedParentWithCode] = fields.ForeignKeyField(
        "models.ProtectedParentWithCode", related_name="children", to_field="code", on_delete=PROTECT
    )


class DefaultOrdered(Model):
    one = fields.TextField()
    second = fields.IntField()

    class Meta:
        ordering = ["one", "second"]


class FKToDefaultOrdered(Model):
    link: fields.ForeignKeyRelation[DefaultOrdered] = fields.ForeignKeyField(
        "models.DefaultOrdered", related_name="related"
    )
    value = fields.IntField()


class OrderedByRelation(Model):
    """Meta.ordering naming a forward FK itself - ordered by its key column."""

    link: fields.ForeignKeyRelation[DefaultOrdered] = fields.ForeignKeyField(
        "models.DefaultOrdered", related_name="ordered_by_relation"
    )
    value = fields.IntField()

    class Meta:
        ordering = ["-link", "value"]


class DefaultOrderedDesc(Model):
    one = fields.TextField()
    second = fields.IntField()

    class Meta:
        ordering = ["-one"]


class DefaultOrderedNullsLast(Model):
    label = fields.TextField()
    score = fields.IntField(null=True)

    class Meta:
        ordering = [F("score").asc(nulls_last=True), "label"]


class SourceFieldPk(Model):
    id = fields.IntField(primary_key=True, source_field="counter")
    name = fields.CharField(max_length=255)


class DefaultOrderedInvalid(Model):
    one = fields.TextField()
    second = fields.IntField()

    class Meta:
        ordering = ["one", "third"]


class DefaultOrderedByPk(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()

    class Meta:
        ordering = ["-pk"]


class CompositePkOrderedByPk(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        ordering = ["pk"]


class School(Model):
    uuid = fields.UUIDField(primary_key=True)
    name = fields.TextField()
    id = fields.IntField(unique=True)

    students: fields.ReverseRelation[Student]
    principal: fields.ReverseRelation[Principal]


class Student(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    school: fields.ForeignKeyRelation[School] = fields.ForeignKeyField(
        "models.School", related_name="students", to_field="id"
    )


class Principal(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    school: fields.OneToOneRelation[School] = fields.OneToOneField(
        "models.School",
        on_delete=fields.CASCADE,
        related_name="principal",
        to_field="id",
    )


class Signals(Model):
    name = fields.CharField(max_length=255)


class DefaultUpdate(Model):
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class DefaultModel(Model):
    int_default = fields.IntField(db_default=1)
    float_default = fields.FloatField(db_default=1.5)
    decimal_default = fields.DecimalField(max_digits=8, decimal_places=2, db_default=Decimal(1))
    bool_default = fields.BooleanField(db_default=True)
    char_default = fields.CharField(max_length=20, db_default="hare")
    date_default = fields.DateField(db_default=datetime.date(year=2020, month=5, day=21))
    datetime_default = fields.DatetimeField(db_default=datetime.datetime(year=2020, month=5, day=20, tzinfo=UTC))


class SqlDefaultModel(Model):
    """Model with SqlDefault expressions for db_default."""

    name = fields.CharField(max_length=100)
    created_at = fields.DatetimeField(db_default=Now())
    counter = fields.IntField(db_default=SqlDefault("0"))
    computed = fields.IntField(db_default=SqlDefault("40 + 2"))
    tracking_id = fields.CharField(max_length=36, null=True, db_default=RandomHex())

    class Meta:
        table = "sql_default_model"


class PkWithDbDefaultModel(Model):
    """A non-auto-increment primary key that ALSO carries its own DB-level default (e.g. a UUID/
    random-token PK generated by the database, not by hare-orm's own Python-side default=) -
    db_default is independent of being a PK, unlike an auto-increment sequence default (already
    implied by primary_key=True, never round-tripped as a db_default= kwarg)."""

    token = fields.CharField(max_length=64, primary_key=True, db_default=RandomHex())
    name = fields.CharField(max_length=64, null=True)


class NoFetchDefaultModel(Model):
    """Model whose every value but the primary key is a database default."""

    int_val = fields.IntField(db_default=1)
    char_val = fields.CharField(max_length=20, db_default="test")

    class Meta:
        table = "no_fetch_default"


class RequiredPKModel(Model):
    id = fields.CharField(primary_key=True, max_length=100)
    name = fields.CharField(max_length=255)


class ValidatorModel(Model):
    regex = fields.CharField(max_length=100, null=True, validators=[RegexValidator("abc.+", re.I)])
    max_length = fields.CharField(max_length=5, null=True)
    min_length = fields.CharField(max_length=5, null=True, validators=[MinLengthValidator(3)])
    ipv4 = fields.CharField(max_length=100, null=True, validators=[validate_ipv4_address])
    ipv6 = fields.CharField(max_length=100, null=True, validators=[validate_ipv6_address])
    ipv46 = fields.CharField(max_length=100, null=True, validators=[validate_ipv46_address])
    max_value = fields.IntField(null=True, validators=[MaxValueValidator(20.0)])
    min_value = fields.IntField(null=True, validators=[MinValueValidator(10.0)])
    max_value_decimal = fields.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        validators=[MaxValueValidator(Decimal("2.0"))],
    )
    min_value_decimal = fields.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        validators=[MinValueValidator(Decimal("1.0"))],
    )
    comma_separated_integer_list = fields.CharField(
        max_length=100, null=True, validators=[CommaSeparatedIntegerListValidator()]
    )


class NumberSourceField(Model):
    number = fields.IntField(source_field="counter", default=0)


class StatusQuerySet(QuerySet):
    def active(self):
        return self.filter(status=1)


class StatusManager(Manager):
    def get_queryset(self):
        return self.queryset_class(self._model)


class AbstractManagerModel(Model):
    all_objects = Manager()
    status = fields.IntField(default=0)

    class Meta:
        manager = StatusManager()
        abstract = True


class User(Model):
    id = fields.IntField(primary_key=True)
    username = fields.CharField(max_length=32)
    mail = fields.CharField(max_length=64)
    bio = fields.TextField()


class ManagerModel(AbstractManagerModel):
    class Meta:
        manager = StatusManager(StatusQuerySet)


class ManagerModelExtra(AbstractManagerModel):
    extra = fields.CharField(max_length=200)


class ActiveManager(Manager):
    """A default-scope Manager whose get_queryset() takes no parameters at all (unlike the base
    one) and just chains one extra domain-specific filter onto super().get_queryset()."""

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class ActiveAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    is_active = fields.BooleanField(default=True)
    books: fields.ReverseRelation["ActiveAuthorBook"]

    class Meta:
        manager = ActiveManager()


class ActiveAuthorBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author: fields.ForeignKeyNullableRelation[ActiveAuthor] = fields.ForeignKeyField(
        "models.ActiveAuthor", related_name="books", null=True, on_delete=NO_ACTION
    )


class TenantActiveAuthor(Model):
    """Custom Manager filter AND Meta.tenant_field/Meta.soft_delete_field on one model."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    is_active = fields.BooleanField(default=True)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    books: fields.ReverseRelation["TenantActiveAuthorBook"]

    class Meta:
        manager = ActiveManager()
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class TenantActiveAuthorBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author: fields.ForeignKeyNullableRelation[TenantActiveAuthor] = fields.ForeignKeyField(
        "models.TenantActiveAuthor", related_name="books", null=True, on_delete=NO_ACTION
    )
    tags: fields.ManyToManyRelation[ActiveAuthorTag] = fields.ManyToManyField(
        "models.ActiveAuthorTag", related_name="books"
    )


class BareManagerTenantAuthor(Model):
    """Meta.tenant_field/Meta.soft_delete_field with a manager returning a bare queryset (no
    ambient scope of its own) - a JOIN must still be scoped by both."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        manager = StatusManager()
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class BareManagerTenantAuthorBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author: fields.ForeignKeyNullableRelation[BareManagerTenantAuthor] = fields.ForeignKeyField(
        "models.BareManagerTenantAuthor", related_name="books", null=True, on_delete=NO_ACTION
    )


class TenantActiveReview(Model):
    """Tenant/soft-delete-scoped OUTER model pointing at TenantActiveAuthor - lets
    .all_tenants()/.include_deleted() on the outer query propagate onto the JOIN."""

    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    author: fields.ForeignKeyNullableRelation[TenantActiveAuthor] = fields.ForeignKeyField(
        "models.TenantActiveAuthor", related_name="reviews", null=True, on_delete=NO_ACTION
    )

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class ActiveAuthorTag(Model):
    """M2M target with a custom Manager filter - reached through TenantActiveAuthorBook.tags."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    is_active = fields.BooleanField(default=True)
    books: fields.ManyToManyRelation[TenantActiveAuthorBook]

    class Meta:
        manager = ActiveManager()


class CrossRelationManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(owner__is_active=True)


class CrossRelationScopedAuthor(Model):
    """Default scope filters across a further relation - has no JOIN ON equivalent."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    owner: fields.ForeignKeyNullableRelation[ActiveAuthor] = fields.ForeignKeyField(
        "models.ActiveAuthor", related_name="cross_scoped_authors", null=True, on_delete=NO_ACTION
    )

    class Meta:
        manager = CrossRelationManager()


class CrossRelationScopedBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author: fields.ForeignKeyNullableRelation[CrossRelationScopedAuthor] = fields.ForeignKeyField(
        "models.CrossRelationScopedAuthor", related_name="books", null=True, on_delete=NO_ACTION
    )


class AnnotatedManager(Manager):
    def get_queryset(self):
        return super().get_queryset().annotate(shout=Coalesce("name", "name"))


class AnnotatedScopedAuthor(Model):
    """Default scope applies annotate() - has no JOIN ON equivalent."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        manager = AnnotatedManager()


class AnnotatedScopedBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    author: fields.ForeignKeyNullableRelation[AnnotatedScopedAuthor] = fields.ForeignKeyField(
        "models.AnnotatedScopedAuthor", related_name="books", null=True, on_delete=NO_ACTION
    )


class Extra(Model):
    """Dumb model, has no fk.
    src: https://github.com/hare/hare-orm/pull/826#issuecomment-883341557
    """

    id = fields.IntField(primary_key=True)
    # currently, hare don't save models with single pk field for some reason \_0_/
    some_name = fields.CharField(default=lambda: str(uuid.uuid4()), max_length=64)


class Single(Model):
    """Dumb model, having single fk
    src: https://github.com/hare/hare-orm/pull/826#issuecomment-883341557
    """

    id = fields.IntField(primary_key=True)
    extra: fields.ForeignKeyNullableRelation[Extra] = fields.ForeignKeyField(
        "models.Extra", related_name="singles", null=True
    )


class Pair(Model):
    """Dumb model, having double fk
    src: https://github.com/hare/hare-orm/pull/826#issuecomment-883341557
    """

    id = fields.IntField(primary_key=True)
    left: fields.ForeignKeyNullableRelation[Single] = fields.ForeignKeyField(
        "models.Single", related_name="lefts", null=True
    )
    right: fields.ForeignKeyNullableRelation[Single] = fields.ForeignKeyField(
        "models.Single", related_name="rights", null=True, on_delete=NO_ACTION
    )


class OldStyleModel(Model):
    id = fields.IntField(primary_key=True)
    external_id = fields.IntField(db_index=True)


def camelize_var(var_name: str):
    var_parts: list[str] = var_name.split("_")
    return var_parts[0] + "".join([part.title() for part in var_parts[1:]])


class CamelCaseAliasPerson(Model):
    """CamelCaseAliasPerson model.

    - A model that generates camelized aliases automatically by
        configuring config_class.
    """

    id = fields.IntField(primary_key=True)
    first_name = fields.CharField(max_length=255)
    last_name = fields.CharField(max_length=255)
    full_address = fields.TextField(null=True)

    class PydanticMeta:
        """Defines the default config for pydantic model generator."""

        model_config = ConfigDict(
            title="My custom title",
            extra="ignore",
            alias_generator=camelize_var,
            populate_by_name=True,
        )


def callable_default() -> str:
    return "callable_default"


async def async_callable_default() -> str:
    return "async_callable_default"


class AsyncCallableObjectDefault:
    """A callable object (not a bare `async def` function) whose `__call__` is itself async."""

    async def __call__(self) -> str:
        return "async_callable_object_default"


async def _async_default_with_argument(suffix: str) -> str:
    return f"async_partial_default_{suffix}"


async_partial_default = functools.partial(_async_default_with_argument, "value")


class CallableDefault(Model):
    id = fields.IntField(primary_key=True)
    callable_default = fields.CharField(max_length=32, default=callable_default)
    async_default = fields.CharField(max_length=32, default=async_callable_default)
    async_callable_object_default = fields.CharField(max_length=64, default=AsyncCallableObjectDefault())
    async_partial_default = fields.CharField(max_length=64, default=async_partial_default)


class ModelWithIndexes(Model):
    id = fields.IntField(primary_key=True)
    indexed = fields.CharField(max_length=16, db_index=True)
    unique_indexed = fields.CharField(max_length=16, unique=True)
    f1 = fields.CharField(max_length=16)
    f2 = fields.CharField(max_length=16)
    f3 = fields.CharField(max_length=16)
    u1 = fields.IntField()
    u2 = fields.IntField()

    class Meta:
        indexes = [
            Index(fields=["f1", "f2"]),
            Index(fields=["f3"], name="model_with_indexes__f3"),
        ]
        constraints = (UniqueConstraint(fields=("u1", "u2")),)


class ModelWithNullSafeUniqueIndex(Model):
    """A composite uniqueness rule where one column is nullable and NULL must still
    participate in the uniqueness check (unlike a plain UniqueConstraint, where NULL is always
    exempt) - the canonical use for Index(unique=True) with a Coalesce()-wrapped expression."""

    id = fields.IntField(primary_key=True)
    group = fields.CharField(max_length=16)
    variant = fields.CharField(max_length=16, null=True)

    class Meta:
        indexes = [Index(F("group"), Coalesce(F("variant"), ""), unique=True)]


class Flavor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class Drink(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    flavors = fields.ManyToManyField(Flavor, related_name="drinks", through="drink_flavor")
    toppings = fields.ManyToManyField(Flavor, related_name="topping_drinks", through="drink_topping")


class TimestampPkTarget(Model):
    """PK is auto_now_add - to_db_value() for it depends on the `instance` argument it's given
    (checks getattr(instance, model_field_name) is None), unlike a plain IntField/UUIDField PK
    where `instance` is unused. Used to reproduce ManyToManyRelation.add()'s wrong-instance bug
    in its non-unique dedup path (see test_relations.py)."""

    created_at = fields.DatetimeField(primary_key=True, auto_now_add=True)
    name = fields.CharField(max_length=50)


class TimestampPkOwner(Model):
    name = fields.CharField(max_length=50)
    targets: fields.ManyToManyRelation[TimestampPkTarget] = fields.ManyToManyField(
        TimestampPkTarget, related_name="owners", unique=False
    )


class Person(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    groups: fields.ManyToManyRelation["Group"] = fields.ManyToManyField(
        "models.Group", related_name="members", through="models.Membership"
    )


class Group(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class Membership(Model):
    """User-declared through model for `Person.groups` (`ManyToManyField(..., through=...)`) -
    carries its own extra field (`joined_date`) alongside the FK pair to both sides of the
    relation, and stays independently queryable/filterable like any other model."""

    id = fields.IntField(primary_key=True)
    person = fields.ForeignKeyField(Person, related_name="membership_rows")
    group = fields.ForeignKeyField(Group, related_name="membership_rows")
    joined_date = fields.DateField(null=True)


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    @property
    def name_upper(self) -> str:
        return self.name.upper()

    @functools.cached_property
    def name_length(self) -> int:
        return len(self.name)


class AggregationShop(Model):
    """Parent of two independent to-many relations (products, orders) - fixture for aggregate
    fan-out and annotation-name-collision regression tests."""

    name = fields.CharField(max_length=50)
    rank = fields.IntField(default=0)

    products: fields.ReverseRelation["AggregationProduct"]
    orders: fields.ReverseRelation["AggregationOrder"]


class AggregationTag(Model):
    name = fields.CharField(max_length=50)


class AggregationProduct(Model):
    shop: fields.ForeignKeyRelation[AggregationShop] = fields.ForeignKeyField(
        "models.AggregationShop", related_name="products"
    )
    name = fields.CharField(max_length=50)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    qty = fields.IntField()
    weight = fields.FloatField(null=True)
    score = fields.IntField(null=True)
    tags: fields.ManyToManyRelation[AggregationTag] = fields.ManyToManyField(
        "models.AggregationTag", related_name="products"
    )

    reviews: fields.ReverseRelation["AggregationReview"]


class AggregationReview(Model):
    product: fields.ForeignKeyRelation[AggregationProduct] = fields.ForeignKeyField(
        "models.AggregationProduct", related_name="reviews"
    )
    stars = fields.IntField()


class AggregationOrder(Model):
    shop: fields.ForeignKeyRelation[AggregationShop] = fields.ForeignKeyField(
        "models.AggregationShop", related_name="orders"
    )
    total = fields.DecimalField(max_digits=10, decimal_places=2)


class WidgetTagMembership(Model):
    """A second, independent `through=` model - declared with a live class reference (rather
    than an "app.Model" string like `Membership` above) on `Widget.tags` below, covering both
    accepted forms of `through=`. `weight` uses `db_default=` (not a plain Python-level
    `default=`) - `.add()` inserts only the through row's FK columns, so an extra field left at
    its default needs either `null=True` or a real SQL-level default to survive that raw INSERT,
    same constraint Django's own `through=` support has."""

    id = fields.IntField(primary_key=True)
    widget = fields.ForeignKeyField("models.Widget", related_name="tag_membership_rows")
    tag = fields.ForeignKeyField(Tag, related_name="widget_membership_rows")
    weight = fields.IntField(db_default=1)
    weight_doubled = GeneratedField(expression=RawSQLTerm("weight * 2"), output_field=fields.IntField())


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField(
        Tag, related_name="widgets", through=WidgetTagMembership
    )


class SoftDeleteThroughCrew(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    mates: fields.ManyToManyRelation["SoftDeleteThroughMate"] = fields.ManyToManyField(
        "models.SoftDeleteThroughMate", related_name="crews", through="models.SoftDeleteThroughMembership"
    )


class SoftDeleteThroughMate(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class SoftDeleteThroughMembership(Model):
    """A `through=` model with its own `Meta.soft_delete_field` - ManyToManyRelation.remove()/
    clear() used to always hard-delete this row regardless, ignoring the option entirely (unlike
    Model.delete()/QuerySet.delete() on every other soft-delete-enabled model)."""

    id = fields.IntField(primary_key=True)
    crew = fields.ForeignKeyField(SoftDeleteThroughCrew, related_name="membership_rows")
    mate = fields.ForeignKeyField("models.SoftDeleteThroughMate", related_name="membership_rows")
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class VersionedThroughOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    targets: fields.ManyToManyRelation["VersionedThroughTarget"] = fields.ManyToManyField(
        "models.VersionedThroughTarget", related_name="owners", through="models.VersionedThroughLink"
    )


class VersionedThroughTarget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class DefaultsThroughOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    targets: fields.ManyToManyRelation["DefaultsThroughTarget"] = fields.ManyToManyField(
        "models.DefaultsThroughTarget", related_name="owners", through="models.DefaultsThroughLink"
    )


class DefaultsThroughTarget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class DefaultsThroughLink(Model):
    """A `through=` model whose own fields rely on Python-side defaults (a UUID primary key,
    auto_now_add, a plain default=) - ManyToManyRelation.add() used to insert only the FK
    columns, so any of these made the raw INSERT violate a NOT NULL constraint."""

    id = fields.UUIDField(primary_key=True)
    owner = fields.ForeignKeyField(DefaultsThroughOwner, related_name="link_rows")
    target: fields.ForeignKeyRelation["DefaultsThroughTarget"] = fields.ForeignKeyField(
        "models.DefaultsThroughTarget", related_name="link_rows"
    )
    created_at = fields.DatetimeField(auto_now_add=True)
    note = fields.CharField(max_length=20, default="linked")


class VersionedThroughLink(Model):
    """A soft-delete `through=` model that ALSO has optimistic_lock_field and an auto_now field -
    ManyToManyRelation.remove()/clear() soft-deleted its rows with a bare
    `SET deleted_at = ...`, skipping the version/auto_now bump every other write path applies."""

    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField(VersionedThroughOwner, related_name="link_rows")
    target = fields.ForeignKeyField("models.VersionedThroughTarget", related_name="link_rows")
    deleted_at = fields.DatetimeField(null=True)
    # db_default: left to the database when add() inserts a through row - the plain default=
    # variant is covered separately by DefaultsThroughLink.
    version = fields.IntField(db_default=0)
    updated_at = fields.DatetimeField(null=True, auto_now=True)

    class Meta:
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"


class ClubMember(Model):
    """Soft-delete model - `Club.delete()` below goes through
    ReverseRelationCascade.apply_soft_delete_cascade()'s Python-side cascade unconditionally
    (Finding 2's exact reproduction: that path used to race the through model's own FK field's
    default CASCADE, regardless of db_constraint)."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    deleted_at = fields.DatetimeField(null=True)
    clubs: fields.ReverseRelation["Club"]

    class Meta:
        soft_delete_field = "deleted_at"


class Club(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    deleted_at = fields.DatetimeField(null=True)
    members: fields.ManyToManyRelation[ClubMember] = fields.ManyToManyField(
        ClubMember, related_name="clubs", through="models.ClubMembership", on_delete=SET_NULL
    )

    class Meta:
        soft_delete_field = "deleted_at"


class ClubMembership(Model):
    """`Club.members`'s own on_delete=SET_NULL is declared here (the M2M field), not on either of
    this through model's own FK fields (both left at their own CASCADE default) - reconciled onto
    `club`/`member` at Apps.init_relations time (see reconcile_many_to_many_through_on_delete), rather
    than racing the FK's own default CASCADE the way it used to (Finding 2)."""

    id = fields.IntField(primary_key=True)
    club = fields.ForeignKeyField(Club, related_name="membership_rows", null=True)
    member = fields.ForeignKeyField(ClubMember, related_name="club_membership_rows", null=True)


class HardDeleteAssociationMember(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    associations: fields.ReverseRelation["HardDeleteAssociation"]


class HardDeleteAssociation(Model):
    """Same reconciliation as Club/ClubMembership above, but neither side has
    Meta.soft_delete_field - exercises the OTHER path Finding 2's fix touches: the through
    model's own FK field's on_delete is what schema generation reads for real DDL, so the
    reconciled SET_NULL must show up as a real `ON DELETE SET NULL` constraint, relied on
    entirely by the database itself for an ordinary hard DELETE."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    members: fields.ManyToManyRelation[HardDeleteAssociationMember] = fields.ManyToManyField(
        HardDeleteAssociationMember,
        related_name="associations",
        through="models.HardDeleteAssociationMembership",
        on_delete=SET_NULL,
    )


class HardDeleteAssociationMembership(Model):
    id = fields.IntField(primary_key=True)
    association = fields.ForeignKeyField(HardDeleteAssociation, related_name="membership_rows", null=True)
    member = fields.ForeignKeyField(HardDeleteAssociationMember, related_name="association_membership_rows", null=True)


class TenantScopedTag(Model):
    """The M2M-related side of TenantScopedWidget.tags - both sides have their own
    Meta.tenant_field, so ManyToManyRelation.add()/remove() must validate BOTH against the
    active tenant, not just one - see test_tenancy.py's M2M cross-tenant guard tests."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    company_id = fields.IntField()
    widgets: fields.ManyToManyRelation["TenantScopedWidget"]

    class Meta:
        tenant_field = "company_id"


class TenantScopedFactory(Model):
    """Both Meta.tenant_field AND Meta.soft_delete_field configured - the target of the second
    hop of a chained select_related() (see test_tenancy.py's select_related() scoping tests)
    and of the all_tenants()/include_deleted() independently-controllable chaining tests."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    widgets: fields.ReverseRelation["TenantScopedWidget"]

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class TenantScopedWidget(Model):
    """Meta.tenant_field auto-filters every default-manager query (.filter()/.all()/.get()/...)
    to whatever tenant is active via hare.models.tenancy.tenancy.Tenancy.scope() - see
    tests/test_tenancy.py."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=255)
    company_id = fields.IntField()
    factory: fields.ForeignKeyNullableRelation[TenantScopedFactory] = fields.ForeignKeyField(
        "models.TenantScopedFactory", related_name="widgets", null=True, on_delete=NO_ACTION
    )
    orders: fields.ReverseRelation["TenantScopedOrder"]
    tags: fields.ManyToManyRelation[TenantScopedTag] = fields.ManyToManyField(
        "models.TenantScopedTag", related_name="widgets"
    )

    class Meta:
        tenant_field = "company_id"
        constraints = (UniqueConstraint(fields=("company_id", "name")),)


async def _async_default_tenant_id() -> int:
    return 1


class TenantScopedAsyncDefault(Model):
    """Meta.tenant_field itself declares an async default= callable - regression model for the
    bulk_update() tenant-mismatch check crashing with a raw AttributeError instead of raising a
    clear ConfigurationError when that default is still unresolved (see test_tenancy.py)."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=255)
    company_id = fields.IntField(default=_async_default_tenant_id)

    class Meta:
        tenant_field = "company_id"


class TenantScopedOrder(Model):
    """No Meta.tenant_field of its own - exercises select_related()'s JOIN-level scoping onto a
    related model (TenantScopedWidget, and transitively TenantScopedFactory) that DOES have one,
    even though this model's own default-manager queries never need an active tenant scope at
    all. See test_tenancy.py."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    widget: fields.ForeignKeyNullableRelation[TenantScopedWidget] = fields.ForeignKeyField(
        "models.TenantScopedWidget", related_name="orders", null=True, on_delete=NO_ACTION
    )


class TenantScopedCascadeSoftOrg(Model):
    """Both Meta.tenant_field AND Meta.soft_delete_field configured, CASCADEs onto
    TenantScopedCascadeSoftDept - regression models for the soft-delete cascade wrongly applying
    the active-tenant WHERE guard to a descendant it already found via .all_tenants(), instead of
    just to the instance delete() was directly called on. See test_tenancy.py."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    departments: fields.ReverseRelation["TenantScopedCascadeSoftDept"]

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class TenantScopedCascadeSoftDept(Model):
    """See TenantScopedCascadeSoftOrg - deliberately allowed to hold a DIFFERENT company_id than
    its parent, the same way real data can end up split across tenants regardless of the parent's
    own tenant."""

    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    org: fields.ForeignKeyRelation[TenantScopedCascadeSoftOrg] = fields.ForeignKeyField(
        "models.TenantScopedCascadeSoftOrg", related_name="departments", on_delete=CASCADE
    )

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class TenantActiveAuthorNote(Model):
    """A custom no-parameter get_queryset() Manager plus Meta.tenant_field/Meta.soft_delete_field,
    CASCADE-reached from TenantScopedCascadeSoftOrg - the model-level escape hatches and the
    delete cascade must work through such a manager."""

    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
    is_active = fields.BooleanField(default=True)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    org: fields.ForeignKeyNullableRelation[TenantScopedCascadeSoftOrg] = fields.ForeignKeyField(
        "models.TenantScopedCascadeSoftOrg", related_name="active_notes", null=True, on_delete=CASCADE
    )

    class Meta:
        manager = ActiveManager()
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class UuidTenantLabel(Model):
    """UUIDField Meta.tenant_field on both sides of an auto-through M2M (UuidTenantDoc.labels)."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    org = fields.UUIDField()
    deleted_at = fields.DatetimeField(null=True)
    docs: fields.ManyToManyRelation["UuidTenantDoc"]

    class Meta:
        tenant_field = "org"
        soft_delete_field = "deleted_at"


class UuidTenantDoc(Model):
    """UUIDField Meta.tenant_field plus Meta.soft_delete_field, a CASCADE self-reference (a child
    may belong to another tenant than its parent) and an auto-through M2M."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    org = fields.UUIDField()
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyNullableRelation["UuidTenantDoc"] = fields.ForeignKeyField(
        "models.UuidTenantDoc", related_name="children", null=True, on_delete=CASCADE
    )
    children: fields.ReverseRelation["UuidTenantDoc"]
    labels: fields.ManyToManyRelation[UuidTenantLabel] = fields.ManyToManyField(
        "models.UuidTenantLabel", related_name="docs"
    )

    class Meta:
        tenant_field = "org"
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class SharedTopic(Model):
    """No Meta.tenant_field of its own - the unscoped side of TenantArticle.topics."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    articles: fields.ManyToManyRelation["TenantArticle"]


class TenantArticle(Model):
    """Tenant-scoped and soft-deleted, linked by an auto-through M2M to the unscoped SharedTopic."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    topics: fields.ManyToManyRelation[SharedTopic] = fields.ManyToManyField(
        "models.SharedTopic", related_name="articles"
    )
    collections: fields.ManyToManyRelation["SharedCollection"]

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class SharedCollection(Model):
    """Unscoped owner of a custom-through M2M to the tenant-scoped TenantArticle."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    articles: fields.ManyToManyRelation[TenantArticle] = fields.ManyToManyField(
        "models.TenantArticle", related_name="collections", through="models.SharedCollectionEntry"
    )


class SharedCollectionEntry(Model):
    """A through model with its own Meta.soft_delete_field."""

    id = fields.IntField(primary_key=True)
    collection: fields.ForeignKeyRelation[SharedCollection] = fields.ForeignKeyField(
        "models.SharedCollection", related_name="entries"
    )
    article: fields.ForeignKeyRelation[TenantArticle] = fields.ForeignKeyField(
        "models.TenantArticle", related_name="collection_entries"
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class TenantWikiPage(Model):
    """Tenant-scoped and soft-deleted, with a self-referential M2M."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    see_also: fields.ManyToManyRelation["TenantWikiPage"] = fields.ManyToManyField(
        "models.TenantWikiPage", related_name="referenced_by", through="tenant_wiki_page_see_also"
    )
    referenced_by: fields.ManyToManyRelation["TenantWikiPage"]

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"


class BulkDeleteCountParent(Model):
    """Hard-delete model with BOTH an on_delete=PROTECT relation (which routes
    DeleteQuery._execute() through its pk-list branch) and a plain to-many CASCADE relation to
    filter across - a filter through that to-many relation JOINs, producing one row per matching
    child, and QuerySet.delete() must still report each parent only once."""

    name = fields.TextField()
    items: fields.ReverseRelation["BulkDeleteCountItem"]
    guards: fields.ReverseRelation["BulkDeleteCountGuard"]


class BulkDeleteCountItem(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[BulkDeleteCountParent] = fields.ForeignKeyField(
        "models.BulkDeleteCountParent", related_name="items", on_delete=CASCADE
    )


class BulkDeleteCountGuard(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[BulkDeleteCountParent] = fields.ForeignKeyField(
        "models.BulkDeleteCountParent", related_name="guards", on_delete=PROTECT
    )


class BulkDeleteCountUnguardedParent(Model):
    """Same shape as BulkDeleteCountParent without any PROTECT relation - routes
    DeleteQuery._execute() through its COUNT(*) branch instead of the pk-list one."""

    name = fields.TextField()
    items: fields.ReverseRelation["BulkDeleteCountUnguardedItem"]


class BulkDeleteCountUnguardedItem(Model):
    name = fields.TextField()
    parent: fields.ForeignKeyRelation[BulkDeleteCountUnguardedParent] = fields.ForeignKeyField(
        "models.BulkDeleteCountUnguardedParent", related_name="items", on_delete=CASCADE
    )


class BulkDeleteCountCompositePeer(Model):
    name = fields.TextField()
    parents: fields.ReverseRelation["BulkDeleteCountCompositeParent"]
    guarded_parents: fields.ReverseRelation["BulkDeleteCountCompositeParent"]


class BulkDeleteCountCompositeParent(Model):
    """Composite-PK model with a PROTECT ManyToManyField (pk-list branch of DeleteQuery._execute(),
    where each pk is a tuple) plus a plain ManyToManyField to filter across."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    peers: fields.ManyToManyRelation[BulkDeleteCountCompositePeer] = fields.ManyToManyField(
        "models.BulkDeleteCountCompositePeer", related_name="parents"
    )
    guard_peers: fields.ManyToManyRelation[BulkDeleteCountCompositePeer] = fields.ManyToManyField(
        "models.BulkDeleteCountCompositePeer",
        related_name="guarded_parents",
        through="bulkdeletecountcomposite_guard_peer",
        on_delete=PROTECT,
    )

    pk = fields.CompositePrimaryKey("a", "b")


class TransitiveProtectRoot(Model):
    """Top of a hard-delete CASCADE chain: TransitiveProtectRoot -CASCADE-> TransitiveProtectMiddle
    -CASCADE-> TransitiveProtectLeaf, with a PROTECT guard hanging off each of the last two - a
    PROTECT that is one and two CASCADE hops away from the row actually being deleted, none of it
    a direct relation of the root itself. No Meta.soft_delete_field anywhere, so every delete is a
    real DELETE resolved by the database's own ON DELETE CASCADE."""

    name = fields.TextField()
    middles: fields.ReverseRelation["TransitiveProtectMiddle"]
    notes: fields.ReverseRelation["TransitiveProtectNote"]
    tenant_middles: fields.ReverseRelation["TransitiveProtectTenantMiddle"]


class TransitiveProtectNote(Model):
    """A CASCADE child of TransitiveProtectRoot with no PROTECT anywhere below it - the cascade
    branch a PROTECT-seeking walk has no reason to enter."""

    name = fields.TextField()
    root: fields.ForeignKeyRelation[TransitiveProtectRoot] = fields.ForeignKeyField(
        "models.TransitiveProtectRoot", related_name="notes", on_delete=CASCADE
    )


class TransitiveProtectMiddle(Model):
    name = fields.TextField()
    root: fields.ForeignKeyRelation[TransitiveProtectRoot] = fields.ForeignKeyField(
        "models.TransitiveProtectRoot", related_name="middles", on_delete=CASCADE
    )
    guards: fields.ReverseRelation["TransitiveProtectGuard"]
    leaves: fields.ReverseRelation["TransitiveProtectLeaf"]


class TransitiveProtectGuard(Model):
    """PROTECTs a TransitiveProtectMiddle row."""

    name = fields.TextField()
    middle: fields.ForeignKeyRelation[TransitiveProtectMiddle] = fields.ForeignKeyField(
        "models.TransitiveProtectMiddle", related_name="guards", on_delete=PROTECT
    )


class TransitiveProtectLeaf(Model):
    name = fields.TextField()
    middle: fields.ForeignKeyRelation[TransitiveProtectMiddle] = fields.ForeignKeyField(
        "models.TransitiveProtectMiddle", related_name="leaves", on_delete=CASCADE
    )
    guards: fields.ReverseRelation["TransitiveProtectLeafGuard"]


class TransitiveProtectLeafGuard(Model):
    """PROTECTs a TransitiveProtectLeaf row - two CASCADE hops below TransitiveProtectRoot."""

    name = fields.TextField()
    leaf: fields.ForeignKeyRelation[TransitiveProtectLeaf] = fields.ForeignKeyField(
        "models.TransitiveProtectLeaf", related_name="guards", on_delete=PROTECT
    )


class TransitiveProtectTenantMiddle(Model):
    """A Meta.tenant_field-scoped CASCADE child of TransitiveProtectRoot, itself PROTECTed by
    TransitiveProtectTenantGuard - the cascade walk must reach it whatever tenant scope (or none)
    the caller happens to have active."""

    name = fields.TextField()
    company_id = fields.IntField()
    root: fields.ForeignKeyRelation[TransitiveProtectRoot] = fields.ForeignKeyField(
        "models.TransitiveProtectRoot", related_name="tenant_middles", on_delete=CASCADE
    )
    guards: fields.ReverseRelation["TransitiveProtectTenantGuard"]

    class Meta:
        tenant_field = "company_id"


class TransitiveProtectTenantGuard(Model):
    name = fields.TextField()
    middle: fields.ForeignKeyRelation[TransitiveProtectTenantMiddle] = fields.ForeignKeyField(
        "models.TransitiveProtectTenantMiddle", related_name="guards", on_delete=PROTECT
    )


class TransitiveProtectSelfReferential(Model):
    """Self-referential CASCADE ('parent') plus a self-referential PROTECT ('guardian': the row
    a `guardian` points at is the one PROTECTed by it) - a descendant reached through the CASCADE
    chain can be PROTECTed by another row of the very same model, which is either outside the
    cascade tree (blocks) or inside it (doesn't)."""

    name = fields.TextField()
    parent: fields.ForeignKeyNullableRelation["TransitiveProtectSelfReferential"] = fields.ForeignKeyField(
        "models.TransitiveProtectSelfReferential", related_name="children", null=True, on_delete=CASCADE
    )
    guardian: fields.ForeignKeyNullableRelation["TransitiveProtectSelfReferential"] = fields.ForeignKeyField(
        "models.TransitiveProtectSelfReferential", related_name="guarded", null=True, on_delete=PROTECT
    )
    children: fields.ReverseRelation["TransitiveProtectSelfReferential"]
    guarded: fields.ReverseRelation["TransitiveProtectSelfReferential"]


class TransitiveProtectCycleAlpha(Model):
    """Two models CASCADE-ing onto each other (Alpha.beta and Beta.alpha), with a PROTECT guard on
    Beta - the cascade graph cycles back on itself, so a naive walk over it never ends. One of the
    two FKs is db_constraint=False: the schema generator can't order two tables that each
    REFERENCE the other."""

    name = fields.TextField()
    beta: fields.ForeignKeyNullableRelation["TransitiveProtectCycleBeta"] = fields.ForeignKeyField(
        "models.TransitiveProtectCycleBeta", related_name="alphas", null=True, on_delete=CASCADE
    )
    betas: fields.ReverseRelation["TransitiveProtectCycleBeta"]


class TransitiveProtectCycleBeta(Model):
    name = fields.TextField()
    alpha: fields.ForeignKeyNullableRelation[TransitiveProtectCycleAlpha] = fields.ForeignKeyField(
        "models.TransitiveProtectCycleAlpha", related_name="betas", null=True, on_delete=CASCADE, db_constraint=False
    )
    alphas: fields.ReverseRelation[TransitiveProtectCycleAlpha]
    guards: fields.ReverseRelation["TransitiveProtectCycleGuard"]


class TransitiveProtectCycleGuard(Model):
    name = fields.TextField()
    beta: fields.ForeignKeyRelation[TransitiveProtectCycleBeta] = fields.ForeignKeyField(
        "models.TransitiveProtectCycleBeta", related_name="guards", on_delete=PROTECT
    )


class TransitiveProtectLooseRoot(Model):
    """Like TransitiveProtectRoot, but it also has a db_constraint=False CASCADE child
    (TransitiveProtectLooseNote), which sends every hard delete of it through the Python-side
    cascade walk (Model.delete()) / the row-by-row fallback (QuerySet.delete()) instead of a
    single DELETE - the PROTECT one CASCADE hop down (TransitiveProtectLooseGuard) must still
    surface as ProtectedError there."""

    name = fields.TextField()
    notes: fields.ReverseRelation["TransitiveProtectLooseNote"]
    middles: fields.ReverseRelation["TransitiveProtectLooseMiddle"]


class TransitiveProtectLooseNote(Model):
    name = fields.TextField()
    root: fields.ForeignKeyRelation[TransitiveProtectLooseRoot] = fields.ForeignKeyField(
        "models.TransitiveProtectLooseRoot", related_name="notes", on_delete=CASCADE, db_constraint=False
    )


class TransitiveProtectLooseMiddle(Model):
    name = fields.TextField()
    root: fields.ForeignKeyRelation[TransitiveProtectLooseRoot] = fields.ForeignKeyField(
        "models.TransitiveProtectLooseRoot", related_name="middles", on_delete=CASCADE
    )
    guards: fields.ReverseRelation["TransitiveProtectLooseGuard"]


class TransitiveProtectLooseGuard(Model):
    name = fields.TextField()
    middle: fields.ForeignKeyRelation[TransitiveProtectLooseMiddle] = fields.ForeignKeyField(
        "models.TransitiveProtectLooseMiddle", related_name="guards", on_delete=PROTECT
    )


class TemporalBatch(Model):
    """Parent of TemporalRecord - a correlated-subquery target for date/time aggregates."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    records: fields.ReverseRelation["TemporalRecord"]


class TemporalRecord(Model):
    """One of every date/time field type, for date/time arithmetic tests."""

    id = fields.IntField(primary_key=True)
    started_at = fields.DatetimeField()
    finished_at = fields.DatetimeField(null=True)
    day = fields.DateField()
    other_day = fields.DateField(null=True)
    at_time = fields.TimeField(null=True)
    duration = fields.TimeDeltaField(null=True)
    other_duration = fields.TimeDeltaField(null=True)
    quantity = fields.IntField(null=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    updated_at = fields.DatetimeField(auto_now=True)
    batch: fields.ForeignKeyNullableRelation[TemporalBatch] = fields.ForeignKeyField(
        "models.TemporalBatch", related_name="records", null=True
    )


class AutoNowThing(Model):
    """An auto_now field alongside plain ones - a partial (.only()/.defer()) instance that leaves
    the auto_now field unloaded must still be writable, since a save only ever WRITES it."""

    name = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


class SoftDeleteAutoNow(Model):
    """soft_delete_field plus an auto_now field - delete()/restore() run the same UPDATE path
    save() does, which bumps the auto_now field along with the soft-delete marker."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        soft_delete_field = "deleted_at"


class DeletePreviewAuthor(Model):
    """Root of the delete_preview() fixture: a hard-delete CASCADE tree three levels deep (the
    last hop db_constraint=False, so Python cascades it), SET_NULL/SET_DEFAULT/RESTRICT children,
    a transitive PROTECT and CASCADE/SET_NULL M2M relations."""

    name = fields.TextField()
    tags: fields.ManyToManyRelation["DeletePreviewTag"] = fields.ManyToManyField(
        "models.DeletePreviewTag", related_name="authors", through="delete_preview_author_tag"
    )
    books: fields.ReverseRelation["DeletePreviewBook"]
    editors: fields.ReverseRelation["DeletePreviewEditor"]


class DeletePreviewTag(Model):
    name = fields.TextField()
    authors: fields.ManyToManyRelation[DeletePreviewAuthor]


class DeletePreviewShelf(Model):
    name = fields.TextField()


class DeletePreviewBook(Model):
    name = fields.TextField()
    author: fields.ForeignKeyRelation[DeletePreviewAuthor] = fields.ForeignKeyField(
        "models.DeletePreviewAuthor", related_name="books", on_delete=CASCADE
    )
    shelves: fields.ManyToManyRelation[DeletePreviewShelf] = fields.ManyToManyField(
        "models.DeletePreviewShelf", related_name="books", through="delete_preview_book_shelf", on_delete=SET_NULL
    )
    chapters: fields.ReverseRelation["DeletePreviewChapter"]
    reviews: fields.ReverseRelation["DeletePreviewReview"]
    loans: fields.ReverseRelation["DeletePreviewLoan"]


class DeletePreviewChapter(Model):
    name = fields.TextField()
    book: fields.ForeignKeyRelation[DeletePreviewBook] = fields.ForeignKeyField(
        "models.DeletePreviewBook", related_name="chapters", on_delete=CASCADE
    )
    pages: fields.ReverseRelation["DeletePreviewPage"]
    locks: fields.ReverseRelation["DeletePreviewChapterLock"]


class DeletePreviewPage(Model):
    name = fields.TextField()
    chapter: fields.ForeignKeyRelation[DeletePreviewChapter] = fields.ForeignKeyField(
        "models.DeletePreviewChapter", related_name="pages", on_delete=CASCADE, db_constraint=False
    )


class DeletePreviewChapterLock(Model):
    """PROTECTs a chapter - two CASCADE hops below DeletePreviewAuthor."""

    name = fields.TextField()
    chapter: fields.ForeignKeyRelation[DeletePreviewChapter] = fields.ForeignKeyField(
        "models.DeletePreviewChapter", related_name="locks", on_delete=PROTECT
    )


class DeletePreviewReview(Model):
    name = fields.TextField()
    book: fields.ForeignKeyNullableRelation[DeletePreviewBook] = fields.ForeignKeyField(
        "models.DeletePreviewBook", related_name="reviews", null=True, on_delete=SET_NULL
    )


class DeletePreviewLoan(Model):
    name = fields.TextField()
    book: fields.ForeignKeyRelation[DeletePreviewBook] = fields.ForeignKeyField(
        "models.DeletePreviewBook", related_name="loans", on_delete=RESTRICT
    )


class DeletePreviewEditor(Model):
    """SET_DEFAULT onto DeletePreviewAuthor pk 999, which the tests create as the fallback row."""

    name = fields.TextField()
    author: fields.ForeignKeyRelation[DeletePreviewAuthor] = fields.ForeignKeyField(
        "models.DeletePreviewAuthor", related_name="editors", on_delete=SET_DEFAULT, db_default=999
    )


class DeletePreviewSoftFolder(Model):
    """Soft-delete root: soft-deleted notes, hard-deleted attachments and SET_NULL comments below."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    notes: fields.ReverseRelation["DeletePreviewSoftNote"]

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewSoftNote(Model):
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    folder: fields.ForeignKeyRelation[DeletePreviewSoftFolder] = fields.ForeignKeyField(
        "models.DeletePreviewSoftFolder", related_name="notes", on_delete=CASCADE
    )
    attachments: fields.ReverseRelation["DeletePreviewAttachment"]
    comments: fields.ReverseRelation["DeletePreviewSoftComment"]

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewAttachment(Model):
    """No soft_delete_field - the soft-delete cascade removes it with a real DELETE."""

    name = fields.TextField()
    note: fields.ForeignKeyRelation[DeletePreviewSoftNote] = fields.ForeignKeyField(
        "models.DeletePreviewSoftNote", related_name="attachments", on_delete=CASCADE
    )


class DeletePreviewSoftComment(Model):
    name = fields.TextField()
    note: fields.ForeignKeyNullableRelation[DeletePreviewSoftNote] = fields.ForeignKeyField(
        "models.DeletePreviewSoftNote", related_name="comments", null=True, on_delete=SET_NULL
    )


class DeletePreviewTenantProject(Model):
    name = fields.TextField()
    company_id = fields.IntField()
    tasks: fields.ReverseRelation["DeletePreviewTenantTask"]

    class Meta:
        tenant_field = "company_id"


class DeletePreviewTenantTask(Model):
    name = fields.TextField()
    company_id = fields.IntField()
    project: fields.ForeignKeyRelation[DeletePreviewTenantProject] = fields.ForeignKeyField(
        "models.DeletePreviewTenantProject", related_name="tasks", on_delete=CASCADE
    )

    class Meta:
        tenant_field = "company_id"


class ExpressionTypeParity(Model):
    """Mixed-type row for checking annotation result types match across backends."""

    num = fields.IntField()
    big = fields.BigIntField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)
    flag = fields.BooleanField()
    grp = fields.CharField(max_length=20)
    ts = fields.DatetimeField()


class ExpressionTypeRow(Model):
    """Row of every numeric/temporal field type for checking mixed-type expression results."""

    num = fields.IntField()
    fl = fields.FloatField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)
    dec3 = fields.DecimalField(max_digits=12, decimal_places=3)
    d = fields.DateField()
    dt = fields.DatetimeField()
    td = fields.TimeDeltaField()
    s = fields.CharField(max_length=50)
    flag = fields.BooleanField()
    num_null = fields.IntField(null=True)
    dec_null = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    children: fields.ReverseRelation["ExpressionTypeChild"]


class ExpressionTypeChild(Model):
    """Child of `ExpressionTypeRow`, for aggregates and subqueries across a relation."""

    row: fields.ForeignKeyRelation[ExpressionTypeRow] = fields.ForeignKeyField(
        "models.ExpressionTypeRow", related_name="children"
    )
    num = fields.IntField()
    dec = fields.DecimalField(max_digits=10, decimal_places=2)


class DeletePreviewDiamondOwner(Model):
    """Soft-delete owner of diamond roots - its cascade removes each root with a DELETE of its own."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    roots: fields.ReverseRelation["DeletePreviewDiamondRoot"]

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewDiamondRoot(Model):
    """Two CASCADE branches (left/right) joined again by the leaves below them."""

    name = fields.TextField()
    owner: fields.ForeignKeyNullableRelation[DeletePreviewDiamondOwner] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondOwner", related_name="roots", null=True, on_delete=CASCADE
    )
    lefts: fields.ReverseRelation["DeletePreviewDiamondLeft"]
    rights: fields.ReverseRelation["DeletePreviewDiamondRight"]


class DeletePreviewDiamondLeft(Model):
    root: fields.ForeignKeyRelation[DeletePreviewDiamondRoot] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondRoot", related_name="lefts", on_delete=CASCADE
    )


class DeletePreviewDiamondRight(Model):
    root: fields.ForeignKeyRelation[DeletePreviewDiamondRoot] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondRoot", related_name="rights", on_delete=CASCADE
    )


class DeletePreviewDiamondNoActionLeaf(Model):
    left: fields.ForeignKeyRelation[DeletePreviewDiamondLeft] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondLeft", related_name="no_action_leaves", on_delete=CASCADE
    )
    right: fields.ForeignKeyRelation[DeletePreviewDiamondRight] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondRight", related_name="no_action_leaves", on_delete=NO_ACTION
    )


class DeletePreviewDiamondRestrictLeaf(Model):
    left: fields.ForeignKeyRelation[DeletePreviewDiamondLeft] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondLeft", related_name="restrict_leaves", on_delete=CASCADE
    )
    right: fields.ForeignKeyRelation[DeletePreviewDiamondRight] = fields.ForeignKeyField(
        "models.DeletePreviewDiamondRight", related_name="restrict_leaves", on_delete=RESTRICT
    )


class DeletePreviewSoftBin(Model):
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewBinHardItem(Model):
    """No soft_delete_field - removed by a real DELETE when its soft-delete bin is deleted."""

    bin: fields.ForeignKeyRelation[DeletePreviewSoftBin] = fields.ForeignKeyField(
        "models.DeletePreviewSoftBin", related_name="hard_items", on_delete=CASCADE
    )


class DeletePreviewBinSoftGuard(Model):
    """Soft-deleted along with its bin, so it stays in the table and keeps protecting the item."""

    deleted_at = fields.DatetimeField(null=True)
    bin: fields.ForeignKeyRelation[DeletePreviewSoftBin] = fields.ForeignKeyField(
        "models.DeletePreviewSoftBin", related_name="guards", on_delete=CASCADE
    )
    item: fields.ForeignKeyRelation[DeletePreviewBinHardItem] = fields.ForeignKeyField(
        "models.DeletePreviewBinHardItem", related_name="guards", on_delete=PROTECT
    )

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewHardCrate(Model):
    name = fields.TextField()


class DeletePreviewCrateSoftBox(Model):
    deleted_at = fields.DatetimeField(null=True)
    crate: fields.ForeignKeyRelation[DeletePreviewHardCrate] = fields.ForeignKeyField(
        "models.DeletePreviewHardCrate", related_name="boxes", on_delete=CASCADE, db_constraint=False
    )

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewCrateBoxItem(Model):
    box: fields.ForeignKeyRelation[DeletePreviewCrateSoftBox] = fields.ForeignKeyField(
        "models.DeletePreviewCrateSoftBox", related_name="items", on_delete=CASCADE
    )


class DeletePreviewCrateSoftGuard(Model):
    deleted_at = fields.DatetimeField(null=True)
    crate: fields.ForeignKeyRelation[DeletePreviewHardCrate] = fields.ForeignKeyField(
        "models.DeletePreviewHardCrate", related_name="guards", on_delete=CASCADE, db_constraint=False
    )
    item: fields.ForeignKeyRelation[DeletePreviewCrateBoxItem] = fields.ForeignKeyField(
        "models.DeletePreviewCrateBoxItem", related_name="guards", on_delete=PROTECT
    )

    class Meta:
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewTenantSoftProject(Model):
    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class DeletePreviewTenantHardTask(Model):
    """No soft_delete_field under a soft-delete project - its own real DELETE."""

    company_id = fields.IntField()
    project: fields.ForeignKeyRelation[DeletePreviewTenantSoftProject] = fields.ForeignKeyField(
        "models.DeletePreviewTenantSoftProject", related_name="hard_tasks", on_delete=CASCADE
    )

    class Meta:
        tenant_field = "company_id"


class DeletePreviewTenantAuditedTask(Model):
    """Overrides delete() - the cascade has to call it instead of its internal delete path."""

    company_id = fields.IntField()
    project: fields.ForeignKeyRelation[DeletePreviewTenantSoftProject] = fields.ForeignKeyField(
        "models.DeletePreviewTenantSoftProject", related_name="audited_tasks", on_delete=CASCADE
    )
    delete_override_calls: ClassVar[list[Any]] = []

    class Meta:
        tenant_field = "company_id"

    async def delete(self, using: Any = None) -> None:
        self.delete_override_calls.append(self.pk)
        await super().delete(using=using)


class DeletePreviewTenantHardProject(Model):
    company_id = fields.IntField()

    class Meta:
        tenant_field = "company_id"


class DeletePreviewTenantLooseSoftTask(Model):
    """Soft-delete child reached through a db_constraint=False edge - Python deletes it."""

    company_id = fields.IntField()
    deleted_at = fields.DatetimeField(null=True)
    project: fields.ForeignKeyRelation[DeletePreviewTenantHardProject] = fields.ForeignKeyField(
        "models.DeletePreviewTenantHardProject", related_name="loose_tasks", on_delete=CASCADE, db_constraint=False
    )

    class Meta:
        tenant_field = "company_id"
        soft_delete_field = "deleted_at"
        soft_delete_hard_cascade = True


class SoftDeleteVersionedDirtyTracked(Model):
    """soft_delete_field, optimistic_lock_field, an auto_now field and track_dirty_fields together."""

    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)
    version = fields.IntField(default=0)
    modified = fields.DatetimeField(auto_now=True)

    class Meta:
        soft_delete_field = "deleted_at"
        optimistic_lock_field = "version"
        track_dirty_fields = True


class NullableCodeCountry(Model):
    """A nullable unique ``to_field=`` target - a row whose code is NULL is referenced by no row."""

    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=8, unique=True, null=True)
    deleted_at = fields.DatetimeField(null=True)
    cities: fields.ReverseRelation["NullableCodeCity"]
    shops: fields.ReverseRelation["NullableCodeShop"]
    capital: fields.BackwardOneToOneRelation["NullableCodeCapital"]

    class Meta:
        soft_delete_field = "deleted_at"


class NullableCodeCity(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=32)
    country: fields.ForeignKeyNullableRelation[NullableCodeCountry] = fields.ForeignKeyField(
        "models.NullableCodeCountry", related_name="cities", to_field="code", null=True
    )
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class NullableCodeShop(Model):
    id = fields.IntField(primary_key=True)
    country: fields.ForeignKeyNullableRelation[NullableCodeCountry] = fields.ForeignKeyField(
        "models.NullableCodeCountry", related_name="shops", to_field="code", null=True, db_constraint=False
    )


class NullableCodeCapital(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=32)
    country: fields.OneToOneNullableRelation[NullableCodeCountry] = fields.OneToOneField(
        "models.NullableCodeCountry", related_name="capital", to_field="code", null=True
    )


class O2oPkAccount(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=32)


class O2oPkProfile(Model):
    """Its primary key is a OneToOneField(primary_key=True)."""

    account: fields.OneToOneRelation[O2oPkAccount] = fields.OneToOneField(
        "models.O2oPkAccount", related_name="profile", primary_key=True
    )
    bio = fields.CharField(max_length=32)
    notes: fields.ReverseRelation["O2oPkProfileNote"]


class O2oPkProfileNote(Model):
    """A FK to a model whose own primary key is a OneToOneField(primary_key=True)."""

    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=64)
    profile: fields.ForeignKeyRelation[O2oPkProfile] = fields.ForeignKeyField(
        "models.O2oPkProfile", related_name="notes", on_delete=CASCADE
    )


class TenantTeam(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    members: fields.ManyToManyRelation["TenantTeamMember"] = fields.ManyToManyField(
        "models.TenantTeamMember", through="models.TenantTeamMembership", related_name="teams"
    )

    class Meta:
        tenant_field = "company_id"


class TenantTeamMember(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    teams: fields.ManyToManyRelation[TenantTeam]

    class Meta:
        tenant_field = "company_id"


class TenantTeamMembership(Model):
    """A real through model with its own Meta.tenant_field."""

    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    team: fields.ForeignKeyRelation[TenantTeam] = fields.ForeignKeyField("models.TenantTeam")
    member: fields.ForeignKeyRelation[TenantTeamMember] = fields.ForeignKeyField("models.TenantTeamMember")
    role = fields.CharField(max_length=16, default="member")

    class Meta:
        tenant_field = "company_id"


class TenantFkCompany(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=32)
    orders: fields.ReverseRelation["TenantFkOrder"]
    named_orders: fields.ReverseRelation["TenantFkNamedOrder"]


class TenantFkOrder(Model):
    """Meta.tenant_field is the shadow column of a ForeignKeyField."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=32)
    company: fields.ForeignKeyRelation[TenantFkCompany] = fields.ForeignKeyField(
        "models.TenantFkCompany", related_name="orders"
    )

    class Meta:
        tenant_field = "company_id"


class TenantFkNamedOrder(Model):
    """Meta.tenant_field names the ForeignKeyField itself, not its shadow column."""

    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=32)
    company: fields.ForeignKeyRelation[TenantFkCompany] = fields.ForeignKeyField(
        "models.TenantFkCompany", related_name="named_orders"
    )

    class Meta:
        tenant_field = "company"


class UpsertTarget(Model):
    """bulk_create() upsert target: a unique field and a plain field both mapped through
    source_field, a db_default column and Meta.optimistic_lock_field."""

    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=20, unique=True, source_field="code_column")
    note = fields.CharField(max_length=20, default="", source_field="note_column")
    counter = fields.IntField(db_default=SqlDefault("7"))
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"


class BulkUpdateValuesAliasClash(Model):
    """Its table is named like bulk_update()'s VALUES alias and its columns like the VALUES
    table's own c<N> columns."""

    id = fields.IntField(primary_key=True)
    c0 = fields.IntField(default=0)
    c1 = fields.IntField(default=0)
    version = fields.IntField(default=0)

    class Meta:
        table = "hare_bulk_update_values"
        optimistic_lock_field = "version"


class OwnerScopedManager(Manager):
    """A default scope whose filter value is read from a runtime context value at query time."""

    current_owner: ClassVar[ContextVar[str | None]] = ContextVar("owner_scoped_manager_current_owner", default=None)

    def get_queryset(self):
        return super().get_queryset().filter(owner=OwnerScopedManager.current_owner.get())


class OptionalOwnerScopedManager(Manager):
    """Filters by the current owner only while one is set - the scope's shape changes per call."""

    def get_queryset(self):
        queryset = super().get_queryset()
        owner = OwnerScopedManager.current_owner.get()
        return queryset if owner is None else queryset.filter(owner=owner)


class SharedOrOwnTenantManager(Manager):
    """Rows shared by every tenant (company_id NULL) plus the active tenant's own."""

    def get_queryset(self):
        return super().get_queryset().filter(Q(company_id__isnull=True) | Q(company_id=Tenancy.current.get()))


class OwnerScopedLabel(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class OwnerScopedSecret(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.CharField(max_length=50)
    text = fields.CharField(max_length=100)
    labels: fields.ManyToManyRelation[OwnerScopedLabel] = fields.ManyToManyField(
        "models.OwnerScopedLabel", related_name="secrets"
    )

    class Meta:
        manager = OwnerScopedManager()


class OptionalOwnerScopedSecret(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.CharField(max_length=50)
    text = fields.CharField(max_length=100)

    class Meta:
        manager = OptionalOwnerScopedManager()


class SharedOrOwnTemplate(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField(null=True)
    body = fields.CharField(max_length=100)

    class Meta:
        manager = SharedOrOwnTenantManager()


class OwnerScopedSecretRef(Model):
    """Unscoped rows pointing at runtime-scoped targets."""

    id = fields.IntField(primary_key=True)
    secret: fields.ForeignKeyNullableRelation[OwnerScopedSecret] = fields.ForeignKeyField(
        "models.OwnerScopedSecret", related_name="refs", null=True, on_delete=NO_ACTION
    )
    optional_secret: fields.ForeignKeyNullableRelation[OptionalOwnerScopedSecret] = fields.ForeignKeyField(
        "models.OptionalOwnerScopedSecret", related_name="refs", null=True, on_delete=NO_ACTION
    )
    template: fields.ForeignKeyNullableRelation[SharedOrOwnTemplate] = fields.ForeignKeyField(
        "models.SharedOrOwnTemplate", related_name="refs", null=True, on_delete=NO_ACTION
    )


class OwnerScopedTeam(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    players: fields.ManyToManyRelation["OwnerScopedPlayer"] = fields.ManyToManyField(
        "models.OwnerScopedPlayer", related_name="teams", through="models.OwnerScopedRoster"
    )


class OwnerScopedPlayer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class OwnerScopedRoster(Model):
    """A `through=` model whose custom manager scopes its rows by a runtime context value."""

    id = fields.IntField(primary_key=True)
    team = fields.ForeignKeyField(OwnerScopedTeam, related_name="roster_rows")
    player = fields.ForeignKeyField("models.OwnerScopedPlayer", related_name="roster_rows")
    owner = fields.CharField(max_length=50)

    class Meta:
        manager = OwnerScopedManager()


class SlugTenantAccount(Model):
    """Meta.tenant_field holding a caller-supplied string (a subdomain/slug) - bulk_create() upsert
    target scoped by it."""

    id = fields.IntField(primary_key=True)
    tenant = fields.CharField(max_length=100)
    sku = fields.CharField(max_length=50, unique=True)
    balance = fields.IntField(default=0)

    class Meta:
        tenant_field = "tenant"


class RowPinDepartment(Model):
    """Parent of two independent to-many relations - fixture for the aggregate fan-out check of a
    filter that narrows a to-many JOIN to one related row."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    employees: fields.ReverseRelation["RowPinEmployee"]
    projects: fields.ReverseRelation["RowPinProject"]


class RowPinLabel(Model):
    """Many-to-many target with a unique field, a nullable unique field and a unique pair."""

    id = fields.IntField(primary_key=True)
    slug = fields.CharField(max_length=20, unique=True)
    code = fields.CharField(max_length=20, unique=True, null=True)
    group = fields.IntField()
    rank = fields.IntField()
    name = fields.CharField(max_length=50)

    class Meta:
        constraints = (UniqueConstraint(fields=("group", "rank")),)


class RowPinEmployee(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    salary = fields.IntField()
    department: fields.ForeignKeyNullableRelation[RowPinDepartment] = fields.ForeignKeyField(
        "models.RowPinDepartment", related_name="employees", null=True
    )
    labels: fields.ManyToManyRelation[RowPinLabel] = fields.ManyToManyField(
        "models.RowPinLabel", related_name="employees"
    )
    loose_labels: fields.ManyToManyRelation[RowPinLabel] = fields.ManyToManyField(
        "models.RowPinLabel", related_name="loose_employees", through="models.RowPinLooseLink"
    )

    tasks: fields.ReverseRelation["RowPinTask"]


class RowPinLooseLink(Model):
    """A through model without a unique pair - it can link the same two rows twice."""

    id = fields.IntField(primary_key=True)
    employee = fields.ForeignKeyField("models.RowPinEmployee", related_name="loose_link_rows")
    label = fields.ForeignKeyField("models.RowPinLabel", related_name="loose_link_rows")


class RowPinTask(Model):
    id = fields.IntField(primary_key=True)
    employee = fields.ForeignKeyField("models.RowPinEmployee", related_name="tasks")
    hours = fields.IntField()


class RowPinProject(Model):
    id = fields.IntField(primary_key=True)
    department = fields.ForeignKeyField("models.RowPinDepartment", related_name="projects")
    cost = fields.IntField()


class RecursiveSoftNode(Model):
    """A soft-deleted tree - with_recursive() doesn't walk through a deleted row."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)
    parent: fields.ForeignKeyNullableRelation[RecursiveSoftNode] = fields.ForeignKeyField(
        "models.RecursiveSoftNode", related_name="children", null=True, on_delete=NO_ACTION
    )
    children: fields.ReverseRelation[RecursiveSoftNode]

    class Meta:
        soft_delete_field = "deleted_at"


class RecursiveTenantNode(Model):
    """A tree whose rows belong to tenants - a row may point at another tenant's row."""

    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    name = fields.CharField(max_length=20)
    parent: fields.ForeignKeyNullableRelation[RecursiveTenantNode] = fields.ForeignKeyField(
        "models.RecursiveTenantNode", related_name="children", null=True, on_delete=NO_ACTION
    )
    children: fields.ReverseRelation[RecursiveTenantNode]

    class Meta:
        tenant_field = "company_id"


class RecursiveCompositeNode(Model):
    """A tree of rows with a composite primary key."""

    a = fields.IntField()
    b = fields.IntField()
    name = fields.CharField(max_length=20)
    parent: fields.ForeignKeyNullableRelation[RecursiveCompositeNode] = fields.ForeignKeyField(
        "models.RecursiveCompositeNode", related_name="children", null=True, on_delete=NO_ACTION
    )
    children: fields.ReverseRelation[RecursiveCompositeNode]

    pk = fields.CompositePrimaryKey("a", "b")


class AuditedRestoreNote(Model):
    """Overrides restore() - QuerySet.restore() has to call it for each row."""

    name = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)
    restore_override_calls: ClassVar[list[Any]] = []

    class Meta:
        soft_delete_field = "deleted_at"

    async def restore(self, using: Any = None, *, cascade: bool = False) -> None:
        self.restore_override_calls.append((self.name, cascade))
        await super().restore(using=using, cascade=cascade)


class MergeSupplier(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)


class MergeStock(Model):
    """merge() target: a unique key, a relation, an auto_now field and an optimistic lock version."""

    sku = fields.CharField(max_length=20, unique=True)
    count = fields.IntField(db_default=SqlDefault("0"))
    supplier: fields.ForeignKeyNullableRelation[MergeSupplier] = fields.ForeignKeyField(
        "models.MergeSupplier", related_name="stocks", null=True, on_delete=NO_ACTION
    )
    updated_at = fields.DatetimeField(auto_now=True)
    version = fields.IntField(db_default=SqlDefault("0"))

    class Meta:
        optimistic_lock_field = "version"


class MergeDelivery(Model):
    """merge() source rows."""

    sku = fields.CharField(max_length=20)
    delivered = fields.IntField()

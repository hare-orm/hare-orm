"""Model._meta.get_lookup_info()/get_lookups()/get_ordering_info() and their QuerySet forms - what
a filter key or an ordering name refers to, described without building a query, and the check
.filter()/.exclude()/.order_by() run through them."""

import dataclasses
import datetime
import uuid

import pytest

from hare.contrib.test import requires_features
from hare.dialects.registry import DialectRegistry
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields import CharField
from hare.fields.generated import GeneratedField
from hare.query.enums import Lookup, LookupValueShape
from hare.query.expressions import Q
from hare.query.filters import FieldLookup
from hare.query.functions import Count
from hare.sql.terms import Term
from tests.testmodels import (
    DocumentRevisionNote,
    Event,
    JSONFields,
    Team,
    Tournament,
    VersionedArticle,
    VersionedDocument,
    VersionedDocumentWithComputedColumns,
    VersionedTag,
)

SQLITE = DialectRegistry.get_dialect("sqlite")
POSTGRESQL = DialectRegistry.get_dialect("postgresql")


def any_of_lookup(field: CharField | None) -> FieldLookup:
    def is_any_of(term: Term, value: list[str]) -> Term:
        return term.isin(value)

    return FieldLookup(is_any_of)


def postgresql_only_lookup(field: CharField | None) -> FieldLookup:
    def is_equal(term: Term, value: str) -> Term:
        return term == value

    return FieldLookup(is_equal)


CharField.register_lookup("lookup_info_any_of", any_of_lookup, value_shape=LookupValueShape.LIST, value_type=str)
CharField.register_lookup("lookup_info_postgresql_only", postgresql_only_lookup, dialects=["postgresql"])


def field_names(lookup_info) -> str | tuple[str, ...]:
    if isinstance(lookup_info.field, tuple):
        return tuple(field.model_field_name for field in lookup_info.field)
    return lookup_info.field.model_field_name


def relation_names(lookup_info) -> list[str]:
    return [relation.model_field_name for relation in lookup_info.relations]


@pytest.mark.asyncio
async def test_plain_field_lookups(db):
    exact = Event._meta.get_lookup_info("name")
    assert exact.key == "name"
    assert exact.model is Event
    assert (relation_names(exact), field_names(exact), exact.transforms) == ([], "name", ())
    assert (exact.lookup, exact.value_shape, exact.value_type) == (Lookup.EXACT, LookupValueShape.VALUE, str)
    assert not exact.crosses_to_many
    assert exact.requires_extension is None
    assert exact.dialects is None

    contains = Event._meta.get_lookup_info("name__icontains")
    assert (contains.lookup, contains.value_shape, contains.value_type) == (
        Lookup.ICONTAINS,
        LookupValueShape.VALUE,
        str,
    )
    members = Event._meta.get_lookup_info("event_id__in")
    assert (members.lookup, members.value_shape, members.value_type) == (Lookup.IN, LookupValueShape.LIST, int)
    between = Event._meta.get_lookup_info("event_id__range")
    assert (between.value_shape, between.value_type) == (LookupValueShape.RANGE, int)
    is_null = Event._meta.get_lookup_info("reporter_id__isnull")
    assert (is_null.lookup, is_null.value_shape, is_null.value_type) == (Lookup.ISNULL, LookupValueShape.VALUE, bool)
    primary_key = Event._meta.get_lookup_info("pk__in")
    assert (field_names(primary_key), primary_key.value_type) == ("event_id", int)


@pytest.mark.asyncio
async def test_descriptions_are_immutable_and_cached(db):
    lookup_info = Event._meta.get_lookup_info("tournament__name__icontains")
    assert Event._meta.get_lookup_info("tournament__name__icontains") is lookup_info
    with pytest.raises(dataclasses.FrozenInstanceError):
        lookup_info.lookup = Lookup.EXACT  # type: ignore[misc]


@pytest.mark.asyncio
async def test_forward_relation_lookups(db):
    relation = Event._meta.get_lookup_info("tournament")
    assert (relation_names(relation), field_names(relation)) == (["tournament"], "id")
    assert (relation.lookup, relation.value_shape, relation.value_type) == (Lookup.EXACT, LookupValueShape.VALUE, int)
    members = Event._meta.get_lookup_info("tournament__in")
    assert (members.lookup, members.value_shape, members.value_type) == (Lookup.IN, LookupValueShape.LIST, int)
    is_null = Event._meta.get_lookup_info("reporter__isnull")
    assert (relation_names(is_null), is_null.value_type) == (["reporter"], bool)
    key_column = Event._meta.get_lookup_info("tournament_id__gte")
    assert (relation_names(key_column), field_names(key_column), key_column.lookup) == (
        [],
        "tournament_id",
        Lookup.GTE,
    )
    through = Event._meta.get_lookup_info("tournament__name__startswith")
    assert (relation_names(through), field_names(through), through.lookup) == (
        ["tournament"],
        "name",
        Lookup.STARTSWITH,
    )
    for key in ("tournament__pk", "tournament__id", "tournament__pk__in", "tournament__id__in"):
        lookup_info = Event._meta.get_lookup_info(key)
        assert (relation_names(lookup_info), field_names(lookup_info), lookup_info.value_type) == (
            ["tournament"],
            "id",
            int,
        )
    assert not Event._meta.get_lookup_info("tournament__name").crosses_to_many


@pytest.mark.asyncio
async def test_forward_relation_filters_take_a_key_or_an_object(db):
    tournament = await Tournament.objects.create(name="T")
    other = await Tournament.objects.create(name="O")
    event = await Event.objects.create(name="E", tournament=tournament)
    await Event.objects.create(name="F", tournament=other)
    for key, value in (
        ("tournament", tournament),
        ("tournament", tournament.pk),
        ("tournament__in", [tournament]),
        ("tournament__in", [tournament.pk]),
        ("tournament__pk", tournament.pk),
        ("tournament__id__in", [tournament.pk]),
    ):
        Event._meta.get_lookup_info(key)
        assert await Event.objects.filter(**{key: value}).values_list("pk", flat=True) == [event.pk]


@pytest.mark.asyncio
async def test_composite_primary_key_lookups(db):
    exact = VersionedDocument._meta.get_lookup_info("pk")
    assert field_names(exact) == ("id", "version")
    assert (exact.lookup, exact.value_shape, exact.value_type) == (
        Lookup.EXACT,
        LookupValueShape.VALUE,
        (uuid.UUID, int),
    )
    members = VersionedDocument._meta.get_lookup_info("pk__in")
    assert (members.lookup, members.value_shape, members.value_type) == (
        Lookup.IN,
        LookupValueShape.LIST,
        (uuid.UUID, int),
    )
    with pytest.raises(FieldError, match="VersionedDocument.pk is a composite primary key - it takes pk= and pk__in="):
        VersionedDocument._meta.get_lookup_info("pk__gt")
    assert sorted(VersionedDocument._meta.get_lookups("pk", SQLITE)) == ["", "in"]


@pytest.mark.asyncio
async def test_forward_relation_to_a_composite_key(db):
    relation = DocumentRevisionNote._meta.get_lookup_info("document")
    assert (relation_names(relation), field_names(relation)) == (["document"], ("id", "version"))
    assert relation.value_type == (uuid.UUID, int)
    members = DocumentRevisionNote._meta.get_lookup_info("document__in")
    assert (members.value_shape, members.value_type) == (LookupValueShape.LIST, (uuid.UUID, int))
    for key in ("document__pk", "document__pk__in"):
        assert field_names(DocumentRevisionNote._meta.get_lookup_info(key)) == ("id", "version")
    assert DocumentRevisionNote._meta.get_lookup_info("document__isnull").value_type is bool
    key_columns = [DocumentRevisionNote._meta.get_lookup_info(key) for key in ("document_id", "document_version__gt")]
    assert [(field_names(lookup_info), lookup_info.value_type) for lookup_info in key_columns] == [
        ("document_id", uuid.UUID),
        ("document_version", int),
    ]
    with pytest.raises(QueryError, match="DocumentRevisionNote.document has a composite key"):
        DocumentRevisionNote._meta.get_lookup_info("document__gt")

    document = await VersionedDocument.objects.create(title="D")
    other = await VersionedDocument.objects.create(title="O")
    note = await DocumentRevisionNote.objects.create(document=document, note="N")
    await DocumentRevisionNote.objects.create(document=other, note="M")
    for key, value in (
        ("document", document),
        ("document", document.pk),
        ("document__in", [document]),
        ("document__pk", document.pk),
        ("document__pk__in", [document.pk]),
        ("document_id", document.id),
    ):
        assert await DocumentRevisionNote.objects.filter(**{key: value}).values_list("pk", flat=True) == [note.pk]


@pytest.mark.asyncio
async def test_reverse_and_many_to_many_relations(db):
    reverse = VersionedDocument._meta.get_lookup_info("revision_notes__isnull")
    assert (relation_names(reverse), reverse.lookup, reverse.value_type) == (["revision_notes"], Lookup.ISNULL, bool)
    assert reverse.crosses_to_many
    through_reverse = VersionedDocument._meta.get_lookup_info("revision_notes__note__icontains")
    assert (field_names(through_reverse), through_reverse.crosses_to_many) == ("note", True)

    reverse_one_to_one = Event._meta.get_lookup_info("address")
    assert relation_names(reverse_one_to_one) == ["address"]
    assert not reverse_one_to_one.crosses_to_many

    many_to_many = Event._meta.get_lookup_info("participants__in")
    assert (relation_names(many_to_many), field_names(many_to_many)) == (["participants"], "id")
    assert (many_to_many.value_shape, many_to_many.value_type, many_to_many.crosses_to_many) == (
        LookupValueShape.LIST,
        int,
        True,
    )
    reverse_many_to_many = Team._meta.get_lookup_info("events__name")
    assert (relation_names(reverse_many_to_many), reverse_many_to_many.crosses_to_many) == (["events"], True)
    chain = Tournament._meta.get_lookup_info("events__participants__name__icontains")
    assert (relation_names(chain), field_names(chain), chain.crosses_to_many) == (
        ["events", "participants"],
        "name",
        True,
    )


@pytest.mark.asyncio
async def test_many_to_many_to_a_composite_key(db):
    relation = VersionedArticle._meta.get_lookup_info("tags")
    assert (relation_names(relation), field_names(relation), relation.value_type) == (
        ["tags"],
        ("id", "version"),
        (uuid.UUID, int),
    )
    members = VersionedArticle._meta.get_lookup_info("tags__in")
    assert (members.value_shape, members.crosses_to_many) == (LookupValueShape.LIST, True)
    assert field_names(VersionedArticle._meta.get_lookup_info("tags__pk__in")) == ("id", "version")
    assert field_names(VersionedTag._meta.get_lookup_info("articles__title")) == "title"

    tag = await VersionedTag.objects.create(name="T")
    article = await VersionedArticle.objects.create(title="A")
    await VersionedArticle.objects.create(title="B")
    await article.tags.add(tag)
    for key, value in (("tags", tag), ("tags__in", [tag.pk]), ("tags__pk", tag.pk), ("tags__name", "T")):
        assert await VersionedArticle.objects.filter(**{key: value}).values_list("title", flat=True) == ["A"]
    assert await VersionedArticle.objects.filter(tags__isnull=True).values_list("title", flat=True) == ["B"]


@pytest.mark.asyncio
async def test_date_parts(db):
    year = Event._meta.get_lookup_info("modified__year__gte")
    assert (field_names(year), year.transforms, year.lookup, year.value_type) == (
        "modified",
        ("year",),
        Lookup.GTE,
        int,
    )
    week_days = Event._meta.get_lookup_info("modified__week_day__in")
    assert (week_days.value_shape, week_days.value_type) == (LookupValueShape.LIST, int)
    date = Event._meta.get_lookup_info("modified__date")
    assert (date.transforms, date.lookup, date.value_type) == (("date",), Lookup.EXACT, datetime.date)
    assert Event._meta.get_lookup_info("modified__time__lt").value_type is datetime.time
    with pytest.raises(FieldError, match="Event.name has no lookup 'year'"):
        Event._meta.get_lookup_info("name__year")


@pytest.mark.asyncio
async def test_json_values_and_paths(db):
    contains = JSONFields._meta.get_lookup_info("data__contains")
    assert (contains.transforms, contains.lookup, contains.value_type) == ((), Lookup.CONTAINS, object)
    keys = JSONFields._meta.get_lookup_info("data__has_keys")
    assert (keys.value_shape, keys.value_type) == (LookupValueShape.LIST, str)
    path = JSONFields._meta.get_lookup_info("data__owner__name__icontains")
    assert (field_names(path), path.transforms, path.lookup, path.value_type) == (
        "data",
        ("owner", "name"),
        Lookup.ICONTAINS,
        str,
    )
    path_value = JSONFields._meta.get_lookup_info("data__owner__rank")
    assert (path_value.transforms, path_value.lookup, path_value.value_type) == (
        ("owner", "rank"),
        Lookup.EXACT,
        object,
    )
    assert JSONFields._meta.get_lookup_info("data__owner__rank__in").value_shape == LookupValueShape.LIST
    with pytest.raises(FieldError, match="JSONFields.data has no lookup 'startswith'"):
        JSONFields._meta.get_lookup_info("data__startswith")


@pytest.mark.asyncio
async def test_generated_field_is_described_by_its_output_field(db):
    total = VersionedDocumentWithComputedColumns._meta.get_lookup_info("total__gte")
    assert isinstance(total.field, GeneratedField)
    assert (total.lookup, total.value_type) == (Lookup.GTE, int)


@pytest.mark.asyncio
async def test_custom_lookups_declare_their_value(db):
    any_of = Tournament._meta.get_lookup_info("name__lookup_info_any_of")
    assert (any_of.lookup, any_of.value_shape, any_of.value_type, any_of.dialects) == (
        "lookup_info_any_of",
        LookupValueShape.LIST,
        str,
        None,
    )
    postgresql_only = Tournament._meta.get_lookup_info("name__lookup_info_postgresql_only")
    assert (postgresql_only.value_shape, postgresql_only.value_type) == (LookupValueShape.VALUE, str)
    assert postgresql_only.dialects == frozenset({"postgresql"})
    assert not postgresql_only.is_supported(SQLITE)
    assert postgresql_only.is_supported(POSTGRESQL)
    assert "lookup_info_postgresql_only" not in Tournament._meta.get_lookups("name", SQLITE)
    assert "lookup_info_postgresql_only" in Tournament._meta.get_lookups("name", POSTGRESQL)

    await Tournament.objects.create(name="A")
    await Tournament.objects.create(name="B")
    await Tournament.objects.create(name="C")
    names = (
        await Tournament.objects.filter(name__lookup_info_any_of=["A", "C"])
        .order_by("name")
        .values_list("name", flat=True)
    )
    assert names == ["A", "C"]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_lookup_the_dialect_does_not_run_fails_before_sql_is_built(db):
    queryset = Tournament.objects.filter(name__lookup_info_postgresql_only="A")
    with pytest.raises(UnSupportedError, match="its field only exists on postgresql|doesn't implement"):
        await queryset
    with pytest.raises(
        UnSupportedError, match=r"Tournament.objects.filter\(name__trigram_similar=...\) can't run on sqlite"
    ):
        await Tournament.objects.filter(name__trigram_similar="A")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_dialect_only_lookup_runs_on_its_dialect(db):
    await Tournament.objects.create(name="A")
    assert await Tournament.objects.filter(name__lookup_info_postgresql_only="A").count() == 1


@pytest.mark.asyncio
async def test_dialect_support(db):
    search = Event._meta.get_lookup_info("name__search")
    assert (search.is_supported(SQLITE), search.is_supported(POSTGRESQL)) == (False, True)
    trigram = Event._meta.get_lookup_info("name__trigram_similar")
    assert trigram.requires_extension == "pg_trgm"
    assert (trigram.is_supported(SQLITE), trigram.is_supported(POSTGRESQL)) == (False, True)
    regex = Event._meta.get_lookup_info("name__posix_regex")
    assert (regex.is_supported(SQLITE), regex.is_supported(POSTGRESQL)) == (True, True)
    assert SQLITE.supports_lookup(regex)

    postgresql_only = set(Event._meta.get_lookups("name", POSTGRESQL)) - set(Event._meta.get_lookups("name", SQLITE))
    assert postgresql_only == {"search", "trigram_similar", "trigram_word_similar", "trigram_strict_word_similar"}
    date_lookups = Event._meta.get_lookups("modified", SQLITE)
    assert {"", "year", "year__gte", "date__in", "isnull"} <= set(date_lookups)
    assert date_lookups["year__gte"].value_type is int


@pytest.mark.asyncio
async def test_lookups_of_relations(db):
    assert {"", "in", "not", "not_in", "isnull", "not_isnull", "gte"} <= set(
        Event._meta.get_lookups("tournament", SQLITE)
    )
    assert sorted(Event._meta.get_lookups("participants", SQLITE)) == sorted(
        ["", "not", "in", "not_in", "isnull", "not_isnull"]
    )
    assert sorted(DocumentRevisionNote._meta.get_lookups("document__pk", SQLITE)) == ["", "in"]
    assert "icontains" in Event._meta.get_lookups("tournament__name", SQLITE)
    with pytest.raises(FieldError, match="Event has no field path 'tournament__nme'"):
        Event._meta.get_lookups("tournament__nme", SQLITE)


@pytest.mark.asyncio
async def test_unknown_paths_and_lookups(db):
    with pytest.raises(FieldError, match="Unknown filter param 'nme': Event has no field 'nme'"):
        Event._meta.get_lookup_info("nme")
    with pytest.raises(FieldError, match="Unknown filter param 'name__foo': Event.name has no lookup 'foo'"):
        Event._meta.get_lookup_info("name__foo")
    with pytest.raises(FieldError, match="Unknown filter param 'tournament__nme': Tournament has no field 'nme'"):
        Event._meta.get_lookup_info("tournament__nme")
    with pytest.raises(FieldError, match="Tournament.name has no lookup 'icontains__gte'"):
        Event._meta.get_lookup_info("tournament__name__icontains__gte")


@pytest.mark.asyncio
async def test_filter_exclude_and_order_by_check_every_key_when_called(db):
    with pytest.raises(
        FieldError, match=r"Event.objects.filter\(tournament__nme=...\): Tournament has no field 'nme'"
    ):
        Event.objects.filter(tournament__nme="T")
    with pytest.raises(
        FieldError, match=r"Event.objects.exclude\(tournament__name__foo=...\): Tournament.name has no"
    ):
        Event.objects.exclude(tournament__name__foo="T")
    with pytest.raises(FieldError, match=r"Event.objects.filter\(participants__nme=...\): Team has no field 'nme'"):
        Event.objects.filter(Q(name="E") | Q(participants__nme="T"))
    with pytest.raises(QueryError, match="DocumentRevisionNote.document has a composite key"):
        DocumentRevisionNote.objects.filter(document__gt=1)
    with pytest.raises(FieldError, match="Unknown field tournament__nme for ordering: Tournament has no field 'nme'"):
        Event.objects.all().order_by("-tournament__nme")


@pytest.mark.asyncio
async def test_ordering_info(db):
    descending = Event._meta.get_ordering_info("-name")
    assert (descending.paths, descending.descending, descending.crosses_to_many) == (("name",), True, False)
    relation = Event._meta.get_ordering_info("tournament")
    assert (relation.relations, relation.paths) == ((), ("tournament_id",))
    through = Event._meta.get_ordering_info("tournament__name")
    assert ([relation.model_field_name for relation in through.relations], through.paths) == (
        ["tournament"],
        ("tournament__name",),
    )
    assert VersionedDocument._meta.get_ordering_info("-pk").paths == ("id", "version")
    assert DocumentRevisionNote._meta.get_ordering_info("document").paths == ("document_id", "document_version")
    related_key = DocumentRevisionNote._meta.get_ordering_info("document__pk")
    assert related_key.paths == ("document__pk",)
    assert [field.model_field_name for field in related_key.fields] == ["id", "version"]
    assert Event._meta.get_ordering_info("participants").paths == ("participants",)
    to_many = Event._meta.get_ordering_info("participants__name")
    assert to_many.crosses_to_many
    assert JSONFields._meta.get_ordering_info("data__owner__rank").transforms == ("owner", "rank")
    with pytest.raises(FieldError, match="Unknown field nme for ordering: Event has no field 'nme'"):
        Event._meta.get_ordering_info("nme")

    first = await VersionedDocument.objects.create(title="A")
    second = await VersionedDocument.objects.create(title="B")
    await DocumentRevisionNote.objects.create(document=first, note="1")
    await DocumentRevisionNote.objects.create(document=second, note="2")
    expected = sorted([first.pk, second.pk], reverse=True)
    assert await VersionedDocument.objects.all().order_by("-pk").values_list("id", "version") == expected


@pytest.mark.asyncio
async def test_queryset_describes_its_annotations(db):
    queryset = Tournament.objects.annotate(event_count=Count("events"))
    count = queryset.get_lookup_info("event_count__gte")
    assert (count.relations, count.lookup, count.value_shape, count.value_type) == (
        (),
        Lookup.GTE,
        LookupValueShape.VALUE,
        int,
    )
    assert count.field is not None
    assert queryset.get_lookup_info("name__icontains") is Tournament._meta.get_lookup_info("name__icontains")
    assert {"", "gte", "in", "isnull"} <= set(queryset.get_lookups("event_count", SQLITE))
    ordering = queryset.get_ordering_info("-event_count")
    assert (ordering.paths, ordering.descending) == (("event_count",), True)
    with pytest.raises(FieldError, match=r"Tournament.objects.filter\(event_count__foo=...\)"):
        queryset.filter(event_count__foo=1)

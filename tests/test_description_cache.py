"""Filter and ordering descriptions are built once: per model for a model's own keys, per queryset
for a key starting with an annotation - shared with clones, dropped when the annotation changes,
and every one dropped when a model's fields or relations change. Errors are never cached."""

import datetime
import os
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test.helpers import hare_test_context
from hare.core.hare import Hare
from hare.exceptions import FieldError
from hare.models import Model
from hare.query.functions import Count, Lower, Max
from hare.query.lookup_info.lookup_info_builder import LookupInfoBuilder
from tests.primary_keyless_models import VisitLog
from tests.testmodels import DocumentRevisionNote, Tournament, VersionedDocument

BUILDER_METHOD_NAMES = ("get_lookup_info", "get_ordering_info", "get_lookups")


@contextmanager
def count_builder_calls() -> Generator[dict[str, Any]]:
    """Counts every call of the description builder's methods, by method name."""
    with (
        patch.object(LookupInfoBuilder, "get_lookup_info", wraps=LookupInfoBuilder.get_lookup_info) as lookup_info,
        patch.object(LookupInfoBuilder, "get_ordering_info", wraps=LookupInfoBuilder.get_ordering_info) as ordering,
        patch.object(LookupInfoBuilder, "get_lookups", wraps=LookupInfoBuilder.get_lookups) as lookups,
    ):
        yield {"get_lookup_info": lookup_info, "get_ordering_info": ordering, "get_lookups": lookups}


def call_counts(calls: dict[str, Any]) -> dict[str, int]:
    return {name: mock.call_count for name, mock in calls.items()}


@pytest.mark.asyncio
async def test_model_keys_are_described_once(db):
    dialect = db.db().dialect
    keys = ("name__icontains", "events__name", "pk")
    orderings = ("-name", "events__name", "pk")

    def describe() -> None:
        Tournament.objects.filter(**dict.fromkeys(keys, "x")).exclude(name="y").order_by(*orderings)
        for ordering in orderings:
            Tournament._meta.get_ordering_info(ordering)
        Tournament._meta.get_lookups("events__name", dialect)

    describe()
    with count_builder_calls() as calls:
        for __ in range(3):
            describe()
    assert call_counts(calls) == dict.fromkeys(BUILDER_METHOD_NAMES, 0)
    assert Tournament._meta.get_ordering_info("-name") is Tournament._meta.get_ordering_info("-name")
    assert Tournament._meta.get_ordering_info("-name").descending
    assert not Tournament._meta.get_ordering_info("name").descending


@pytest.mark.asyncio
async def test_get_lookups_is_cached_per_dialect_and_returns_a_copy(db):
    dialect = db.db().dialect
    lookups = Tournament._meta.get_lookups("name", dialect)
    lookups.clear()
    assert Tournament._meta.get_lookups("name", dialect)
    assert Tournament._meta.get_lookups("name", dialect) == Tournament._meta.get_lookups("name", dialect)


@pytest.mark.asyncio
async def test_composite_keys_and_relations_to_them_are_cached(db):
    VersionedDocument.objects.filter(pk=("x", 1)).order_by("-pk")
    DocumentRevisionNote.objects.filter(document__pk__in=[], document__isnull=False).order_by("document")
    with count_builder_calls() as calls:
        VersionedDocument.objects.filter(pk=("x", 1)).order_by("-pk")
        DocumentRevisionNote.objects.filter(document__pk__in=[], document__isnull=False).order_by("document")
    assert call_counts(calls) == dict.fromkeys(BUILDER_METHOD_NAMES, 0)
    assert VersionedDocument._meta.get_ordering_info("-pk").paths == ("id", "version")


@pytest.mark.asyncio
async def test_errors_are_raised_again_every_time(db):
    for __ in range(2):
        with pytest.raises(FieldError, match=r"Tournament.objects.filter\(no_such_field=...\)"):
            Tournament.objects.filter(no_such_field="x")
        with pytest.raises(FieldError, match=r"Tournament.objects.exclude\(no_such_field=...\)"):
            Tournament.objects.exclude(no_such_field="x")
        with pytest.raises(FieldError, match="Unknown field no_such_field for ordering"):
            Tournament.objects.all().order_by("no_such_field")
        with pytest.raises(FieldError, match=r"Tournament.objects.filter\(event_count__foo=...\)"):
            Tournament.objects.annotate(event_count=Count("events")).filter(event_count__foo=1)


@pytest.mark.asyncio
async def test_annotation_keys_are_described_once_per_queryset(db):
    dialect = db.db().dialect
    queryset = Tournament.objects.annotate(event_count=Count("events"))
    queryset.filter(event_count__gte=1).order_by("-event_count")
    queryset.get_lookup_info("event_count__gte")
    queryset.get_ordering_info("-event_count")
    queryset.get_lookups("event_count", dialect)
    with count_builder_calls() as calls:
        for __ in range(3):
            queryset.filter(event_count__gte=1).order_by("-event_count")
            queryset.get_lookup_info("event_count__gte")
            queryset.get_ordering_info("-event_count")
            queryset.get_lookups("event_count", dialect)
    assert call_counts(calls) == dict.fromkeys(BUILDER_METHOD_NAMES, 0)


@pytest.mark.asyncio
async def test_annotation_output_fields_are_resolved_once_per_queryset(db):
    queryset = Tournament.objects.annotate(event_count=Count("events"), lowered=Lower("name"))
    first = queryset._get_annotation_output_fields()
    with patch.object(type(queryset), "_get_probe_expression_context", side_effect=AssertionError) as probe:
        assert queryset._get_annotation_output_fields() == first
        assert queryset.filter(event_count__gte=1)._get_annotation_output_fields() == first
    assert not probe.called


@pytest.mark.asyncio
async def test_a_clone_shares_its_originals_descriptions_but_not_the_other_way(db):
    original = Tournament.objects.annotate(event_count=Count("events"))
    original.get_lookup_info("event_count__gte")
    clone = original.filter(name="x")
    with count_builder_calls() as calls:
        clone.get_lookup_info("event_count__gte")
    assert call_counts(calls)["get_lookup_info"] == 0

    extended = original.annotate(latest=Max("events__modified"))
    assert extended.get_lookup_info("latest__gte").field is not None
    assert extended.get_lookup_info("event_count__gte") is original.get_lookup_info("event_count__gte")
    with pytest.raises(FieldError, match=r"Tournament.objects.filter\(latest__gte=...\)"):
        original.filter(latest__gte=1)
    assert ("lookup_info", "latest__gte") not in {key[:2] for key in original._annotation_descriptions or {}}


@pytest.mark.asyncio
async def test_replacing_an_annotation_gives_a_new_description(db):
    counted = Tournament.objects.annotate(value=Count("events"))
    counted_value = counted.get_lookup_info("value__gte")
    counted.get_ordering_info("value")
    replaced = counted.annotate(value=Max("events__modified"))
    replaced_value = replaced.get_lookup_info("value__gte")
    assert replaced_value is not counted_value
    assert replaced_value.value_type is datetime.datetime
    assert counted_value.value_type is int
    assert counted.get_lookup_info("value__gte") is counted_value
    aliased = counted.alias(value=Max("events__modified"))
    assert aliased.get_lookup_info("value__gte").value_type is datetime.datetime


@pytest.mark.asyncio
async def test_a_model_without_a_primary_key_is_cached():
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(["tests.primary_keyless_models"], db_url=db_url):

        def describe() -> None:
            VisitLog.objects.filter(visitor="ann", venue__name="Hall").order_by("-duration", "venue__name")
            VisitLog._meta.get_ordering_info("-duration")

        describe()
        with count_builder_calls() as calls:
            describe()
        assert call_counts(calls) == dict.fromkeys(BUILDER_METHOD_NAMES, 0)
        for __ in range(2):
            with pytest.raises(FieldError, match="has no primary key"):
                VisitLog._meta.get_ordering_info("pk")


def build_live_model() -> type[Model]:
    return type(
        "CachedTournamentNote",
        (Model,),
        {
            "text": fields.CharField(max_length=20),
            "tournament": fields.ForeignKeyField(
                "models.Tournament", related_name="cached_notes", on_delete=fields.CASCADE
            ),
            "Meta": type("Meta", (), {"app": "models"}),
        },
    )


@pytest.mark.asyncio
async def test_a_live_model_change_drops_the_descriptions(db):
    with pytest.raises(FieldError):
        Tournament._meta.get_lookup_info("cached_notes__text")
    Tournament._meta.get_ordering_info("name")
    note_model = build_live_model()
    Hare.register_live_models([note_model], "models", connection_alias="models")
    try:
        assert Tournament._meta.get_lookup_info("cached_notes__text").crosses_to_many
        assert Tournament._meta.get_ordering_info("cached_notes__text").crosses_to_many
    finally:
        Hare.unregister_live_models([note_model])
    with pytest.raises(FieldError):
        Tournament._meta.get_lookup_info("cached_notes__text")
    with pytest.raises(FieldError):
        Tournament._meta.get_ordering_info("cached_notes__text")


@pytest_asyncio.fixture
async def tournament_rows(db) -> AsyncGenerator[Any]:
    for name in ("b", "a", "c"):
        await Tournament.objects.create(name=name)
    yield db


@pytest.mark.asyncio
async def test_cached_descriptions_give_the_same_rows(tournament_rows):
    for __ in range(2):
        assert await Tournament.objects.filter(name__in=["a", "b"]).order_by("-name").values_list(
            "name", flat=True
        ) == [
            "b",
            "a",
        ]
        queryset = Tournament.objects.annotate(lowered=Lower("name")).filter(lowered__gte="b").order_by("lowered")
        assert await queryset.values_list("name", flat=True) == ["b", "c"]

import pytest

from hare.contrib.pydantic import pydantic_model_creator
from hare.exceptions import NoValuesFetched
from hare.models.tenancy.tenancy import Tenancy
from tests.testmodels import Event, SoftDeleteVersioned, TenantScopedTag, Tournament


@pytest.mark.asyncio
async def test_only_partial_load_field_without_a_default_raises_validation_error(db):
    """A required field (no default=) with no schema fallback already raises pydantic's own
    ValidationError today - the baseline, non-buggy case _raise_if_relation_not_fetched's
    broadened check below must not disturb."""
    obj = await SoftDeleteVersioned.objects.create(name="secret-name", version=7)

    class PydanticMetaOverride:
        backward_relations = False

    Schema = pydantic_model_creator(SoftDeleteVersioned, meta_override=PydanticMetaOverride)

    partial = await SoftDeleteVersioned.objects.filter(pk=obj.pk).only("id", "deleted_at").get()

    with pytest.raises(AttributeError):
        partial.name

    with pytest.raises(NoValuesFetched, match="name"):
        Schema.model_validate(partial)


@pytest.mark.asyncio
async def test_only_partial_load_field_with_default_raises_instead_of_silently_substituting(db):
    """`version` has default=0 AND `Meta.optimistic_lock_field = "version"` - real DB value is 7, but
    .only() excludes it. Used to silently substitute the field's default=0 in place of the real,
    unloaded value (pydantic's from_attributes mode treats AttributeError as "not provided" and
    falls back to the schema default) - now raises NoValuesFetched instead, same as an unfetched
    relation."""
    obj = await SoftDeleteVersioned.objects.create(name="secret-name", version=7)

    class PydanticMetaOverride:
        backward_relations = False

    Schema = pydantic_model_creator(SoftDeleteVersioned, meta_override=PydanticMetaOverride)

    partial = await SoftDeleteVersioned.objects.filter(pk=obj.pk).only("id", "name", "deleted_at").get()

    with pytest.raises(AttributeError):
        partial.version

    with pytest.raises(NoValuesFetched, match="version"):
        Schema.model_validate(partial)


@pytest.mark.asyncio
async def test_only_partial_load_tenant_field_raises_instead_of_silently_none(db):
    """Meta.tenant_field's own is_tenant_field branch in creator.py's _process_orm_field gives it
    a schema default=None on every generated schema, not just a create-schema. Real company_id
    in DB is 42, but .only() excludes it - used to silently report company_id as None on the READ
    schema; now raises NoValuesFetched instead."""
    with Tenancy.scope(42):
        tag = await TenantScopedTag.objects.create(name="secret-tag", company_id=42)

    Schema = pydantic_model_creator(TenantScopedTag, exclude=("widgets",))

    with Tenancy.scope(42):
        partial = await TenantScopedTag.objects.filter(pk=tag.pk).only("id", "name").get()

    with pytest.raises(AttributeError):
        partial.company_id

    with pytest.raises(NoValuesFetched, match="company_id"):
        Schema.model_validate(partial)


@pytest.mark.asyncio
async def test_only_partial_load_recurses_into_select_related_submodel(db):
    """Same root cause, one level deeper: Event_Pydantic nests a Tournament submodel.
    Tournament.created is auto_now_add=True (read-only, schema default=None).
    select_related('tournament') + .only(..., 'tournament__id') loads only Tournament.id - used
    to silently report created=None on the nested submodel instead of raising; now raises
    NoValuesFetched, same as the top-level case."""
    tournament = await Tournament.objects.create(name="Real Tournament")
    event = await Event.objects.create(name="Test Event", tournament=tournament)
    assert tournament.created is not None

    class PydanticMetaOverride:
        exclude = ("participants", "address", "reporter", "tournament.name", "tournament.desc")

    Schema = pydantic_model_creator(Event, meta_override=PydanticMetaOverride)

    partial_event = (
        await Event.objects.filter(pk=event.pk)
        .select_related("tournament")
        .only("event_id", "name", "modified", "token", "alias", "tournament_id", "tournament__id", "reporter_id")
        .get()
    )

    with pytest.raises(AttributeError):
        partial_event.tournament.created

    with pytest.raises(NoValuesFetched, match="created"):
        Schema.model_validate(partial_event)

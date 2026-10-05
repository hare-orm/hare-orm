import os
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from hare import prefetch_related_objects
from hare.contrib.test import requires_features
from hare.exceptions import (
    DoesNotExist,
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    MultipleObjectsReturned,
    NoValuesFetched,
    QueryError,
    ValidationError,
)
from hare.fields.database_default import DatabaseDefault
from hare.models import NoneAwaitable
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import F, Q
from hare.time import Timezone
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    CallableDefault,
    CompositePkThing,
    DatetimeFields,
    DefaultModel,
    Dest_null,
    DirtyTrackedThing,
    Event,
    IntFields,
    JSONFields,
    LazyJoinedChild,
    LazyJoinedParent,
    Node,
    NoID,
    O2O_null,
    PkOnly,
    RequiredPKModel,
    SoftDeleteStandalone,
    SourceFields,
    Team,
    TenantScopedWidget,
    Tournament,
    UniqueName,
    UUIDFkRelatedNullModel,
    UUIDFkRelatedSourceModel,
    UUIDPkModel,
    UUIDPkSourceModel,
    VersionedThing,
)

# ============================================================================
# TestModelCreate
# ============================================================================


@pytest.mark.asyncio
async def test_save_generated(db):
    mdl = await Tournament.objects.create(name="Test")
    mdl2 = await Tournament.objects.get(id=mdl.id)
    assert mdl == mdl2


@pytest.mark.asyncio
async def test_save_non_generated(db):
    mdl = await UUIDFkRelatedNullModel.objects.create(name="Test")
    mdl2 = await UUIDFkRelatedNullModel.objects.get(id=mdl.id)
    assert mdl == mdl2


@pytest.mark.asyncio
async def test_save_generated_custom_id(db):
    cid = 12345
    mdl = await Tournament.objects.create(id=cid, name="Test")
    assert mdl.id == cid
    mdl2 = await Tournament.objects.get(id=cid)
    assert mdl == mdl2


@pytest.mark.asyncio
async def test_save_non_generated_custom_id(db):
    cid = uuid4()
    mdl = await UUIDFkRelatedNullModel.objects.create(id=cid, name="Test")
    assert mdl.id == cid
    mdl2 = await UUIDFkRelatedNullModel.objects.get(id=cid)
    assert mdl == mdl2


@pytest.mark.asyncio
async def test_save_generated_duplicate_custom_id(db):
    cid = 12345
    await Tournament.objects.create(id=cid, name="TestOriginal")
    with pytest.raises(IntegrityError):
        await Tournament.objects.create(id=cid, name="Test")


@pytest.mark.asyncio
async def test_save_non_generated_duplicate_custom_id(db):
    cid = uuid4()
    await UUIDFkRelatedNullModel.objects.create(id=cid, name="TestOriginal")
    with pytest.raises(IntegrityError):
        await UUIDFkRelatedNullModel.objects.create(id=cid, name="Test")


@pytest.mark.asyncio
async def test_clone_pk_required_error(db):
    mdl = await RequiredPKModel.objects.create(id="A", name="name_a")
    with pytest.raises(QueryError):
        mdl.clone()


@pytest.mark.asyncio
async def test_clone_pk_required(db):
    mdl = await RequiredPKModel.objects.create(id="A", name="name_a")
    mdl2 = mdl.clone(pk="B")
    await mdl2.save()
    mdls = list(await RequiredPKModel.objects.all())
    assert len(mdls) == 2


@pytest.mark.asyncio
async def test_implicit_clone_pk_required_none(db):
    mdl = await RequiredPKModel.objects.create(id="A", name="name_a")
    mdl.pk = None
    with pytest.raises(ValidationError):
        await mdl.save()


# ============================================================================
# TestModelMethods fixtures and tests
# ============================================================================


@pytest_asyncio.fixture
async def tournament_model(db):
    """Fixture that provides a saved Tournament model instance."""
    return await Tournament.objects.create(name="Test")


@pytest_asyncio.fixture
async def tournament_model_unsaved(db):
    """Fixture that provides an unsaved Tournament model instance."""
    return Tournament(name="Test")


@pytest.fixture(params=[Tournament, NoID], ids=["tournament", "noid"])
def model_class(request):
    """A model with a declared primary key, and one whose primary key hare adds."""
    return request.param


@pytest_asyncio.fixture
async def saved_model(db, model_class):
    """A saved instance of ``model_class``."""
    return await model_class.objects.create(name="Test")


@pytest_asyncio.fixture
async def unsaved_model(db, model_class):
    """An unsaved instance of ``model_class``."""
    return model_class(name="Test")


@pytest.mark.asyncio
async def test_save(saved_model):
    mdl = saved_model
    oldid = mdl.id
    await mdl.save()
    assert mdl.id == oldid


@pytest.mark.asyncio
async def test_save_f_expression(db):
    int_field = await IntFields.objects.create(intnum=1)
    int_field.intnum = F("intnum") + 1
    await int_field.save(update_fields=["intnum"])
    n_int = await IntFields.objects.get(pk=int_field.pk)
    assert n_int.intnum == 2


@pytest.mark.asyncio
async def test_save_full(tournament_model):
    tournament_model.name = "TestS"
    tournament_model.desc = "Something"
    await tournament_model.save()
    n_mdl = await Tournament.objects.get(id=tournament_model.id)
    assert n_mdl.name == "TestS"
    assert n_mdl.desc == "Something"


@pytest.mark.asyncio
async def test_save_partial(saved_model, model_class):
    saved_model.name = "TestS"
    saved_model.desc = "Something"
    await saved_model.save(update_fields=["desc"])
    n_mdl = await model_class.objects.get(id=saved_model.id)
    assert n_mdl.name == "Test"
    assert n_mdl.desc == "Something"


@pytest.mark.asyncio
async def test_save_partial_with_pk_update(tournament_model):
    # Not allow to update pk field only
    with pytest.raises(QueryError, match="Can't update pk field"):
        await tournament_model.save(update_fields=["id"])
    # So does update pk field with others
    with pytest.raises(QueryError, match=rf"use `{Tournament.__name__}.objects.create\(\)` instead"):
        await tournament_model.save(update_fields=["id", "desc"])


@pytest.mark.asyncio
async def test_create(db, model_class):
    mdl = model_class(name="Test2")
    assert mdl.id is None
    await mdl.save()
    assert mdl.id is not None


@pytest.mark.asyncio
async def test_create_pk_only_model_actually_inserts_a_row(db):
    """A model whose only field is the auto-generated PK had an empty `regular_columns` list -
    the precomputed INSERT statement's `.columns().insert()` call with no arguments rendered as
    an EMPTY STRING, not a valid INSERT statement at all, so the DB driver silently no-opped: no
    row was ever inserted, no exception was raised, and the instance kept whatever the PK's
    Python-side default was (0 for a plain int) instead of a real DB-assigned value."""
    obj = await PkOnly.objects.create()
    assert obj.id != 0

    count = await PkOnly.objects.all().count()
    assert count == 1

    fetched = await PkOnly.objects.get(id=obj.id)
    assert fetched.id == obj.id


def test_constructor_rejects_unknown_kwarg():
    """_set_kwargs()'s per-key dispatch (fk/o2o, direct field, backward fk, backward o2o, m2m)
    had no final `else: raise` - a typo'd kwarg (Tournament(nam="x") instead of name="x") fell
    through every branch and was silently dropped, with the field left at its default and no
    error pointing at the typo."""
    from hare.exceptions import FieldError

    with pytest.raises(FieldError, match="Unknown field 'nam'"):
        Tournament(nam="Test2")


@pytest.mark.asyncio
async def test_delete(saved_model, unsaved_model, model_class):
    fetched_mdl = await model_class.objects.get(name="Test")
    assert saved_model.id == fetched_mdl.id

    await saved_model.delete()

    with pytest.raises(DoesNotExist):
        await model_class.objects.get(name="Test")

    with pytest.raises(QueryError):
        await unsaved_model.delete()


@pytest.mark.asyncio
async def test_delete_on_only_partial_missing_pk_raises(db):
    """Without this guard, instance.pk silently falls back to None for a partial instance missing
    its own pk column - the DELETE's WHERE clause then matches zero rows with no exception, and
    the row is left behind untouched. Confirmed empirically before fixing."""
    created = await Tournament.objects.create(name="ToDelete")
    partial = await Tournament.objects.get(name="ToDelete").only("name")

    with pytest.raises(IncompleteInstanceError):
        await partial.delete()

    assert await Tournament.objects.filter(pk=created.pk).exists()


@pytest.mark.asyncio
async def test_delete_on_only_with_pk_included_still_works(db):
    created = await Tournament.objects.create(name="ToDelete2")
    partial = await Tournament.objects.get(name="ToDelete2").only("name", "id")

    await partial.delete()

    assert not await Tournament.objects.filter(pk=created.pk).exists()


@pytest.mark.asyncio
async def test_str(tournament_model):
    mdl = tournament_model
    assert str(mdl) == "Test"


@pytest.mark.asyncio
async def test_repr(saved_model, unsaved_model, model_class):
    assert repr(saved_model) == f"<{model_class.__name__}: {saved_model.id}>"
    assert repr(unsaved_model) == f"<{model_class.__name__}>"


@pytest.mark.asyncio
async def test_repr_pk_zero_shows_the_real_pk(db):
    """A falsy-but-genuinely-assigned pk value (0) must still be shown in repr(), not be mistaken
    for "unset" - mirrors test_hash_pk_zero_is_hashable's identical reasoning for __hash__."""
    obj = IntFields(intnum=1)
    obj.pk = 0
    assert repr(obj) == "<IntFields: 0>"


@pytest.mark.asyncio
async def test_to_dict(tournament_model):
    as_dict = tournament_model.to_dict()
    assert as_dict == dict(tournament_model)
    assert as_dict["id"] == tournament_model.id
    assert as_dict["name"] == "Test"
    assert as_dict["desc"] is None


@pytest.mark.asyncio
async def test_iter_and_to_dict_keyed_by_model_field_name_not_db_column(db):
    """__iter__ used to iterate self._meta.db_fields (actual DB COLUMN names) instead of
    self._meta.fields_db_projection (model FIELD names) and call getattr(self, field) on the
    result - for any field whose source_field differs from its model field name, that raised
    AttributeError. dict(instance)/to_dict() must be keyed by field name and must not crash on a
    model that makes heavy use of source_field, unloaded FK/O2O relations included (those are
    represented by their own shadow id field, e.g. "fk_id", not the relation attribute itself)."""
    obj = await SourceFields.objects.create(chars="hi")

    as_dict = obj.to_dict()

    assert as_dict == dict(obj)
    assert as_dict == {
        "eyedee": obj.eyedee,
        "chars": "hi",
        "blip": "BLIP",
        "nullable": None,
        "fk_id": None,
        "o2o_id": None,
    }


def test_eq_two_different_unsaved_instances_are_not_equal():
    """__eq__ compared self.pk == other.pk directly - two DIFFERENT never-saved instances both
    have pk None, so they compared equal to each other despite representing distinct rows,
    asymmetric with __hash__ (right above), which explicitly raises on an unset pk to avoid
    exactly this class of bug."""
    a = Tournament(name="A")
    b = Tournament(name="B")
    assert a != b
    assert a == a


@pytest.mark.asyncio
async def test_eq_saved_instances_with_same_pk_are_still_equal(db):
    created = await Tournament.objects.create(name="A")
    loaded = await Tournament.objects.get(pk=created.pk)
    assert created == loaded
    assert created is not loaded


@pytest.mark.asyncio
async def test_hash(saved_model, unsaved_model):
    assert hash(saved_model) == saved_model.id
    with pytest.raises(TypeError):
        hash(unsaved_model)


@pytest.mark.asyncio
async def test_hash_pk_zero_is_hashable(db):
    """A falsy-but-genuinely-assigned pk value (0) must hash fine, not be mistaken for "unset" -
    __hash__'s guard checks `is None`, not plain truthiness, for exactly this reason."""
    obj = IntFields(intnum=1)
    obj.pk = 0
    assert hash(obj) == 0


@pytest.mark.asyncio
async def test_eq(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.get(name="Test")
    assert mdl == fetched_mdl


@pytest.mark.asyncio
async def test_get_or_create(saved_model, model_class):
    mdl = saved_model
    fetched_mdl, created = await model_class.objects.get_or_create(name="Test")
    assert created is False
    assert mdl == fetched_mdl
    new_mdl, created = await model_class.objects.get_or_create(name="Test2")
    assert created is True
    assert mdl != new_mdl
    mdl2 = await model_class.objects.get(name="Test2")
    assert new_mdl == mdl2


@pytest.mark.asyncio
async def test_update_or_create(saved_model, model_class):
    mdl = saved_model
    fetched_mdl, created = await model_class.objects.update_or_create(name="Test")
    assert created is False
    assert mdl == fetched_mdl
    new_mdl, created = await model_class.objects.update_or_create(name="Test2")
    assert created is True
    assert mdl != new_mdl
    mdl2 = await model_class.objects.get(name="Test2")
    assert new_mdl == mdl2


@pytest.mark.asyncio
async def test_update_or_create_with_defaults(tournament_model):
    mdl = tournament_model
    fetched_mdl = await Tournament.objects.get(name=mdl.name)
    mdl_dict = dict(fetched_mdl)
    oldid = fetched_mdl.id
    fetched_mdl.id = 135
    with pytest.raises(QueryError, match="Conflict value with key='id':"):
        # Missing query: check conflict with kwargs and defaults before create
        await Tournament.objects.update_or_create(id=fetched_mdl.id, defaults=mdl_dict)
    desc = str(uuid4())
    # If there is no conflict with defaults and kwargs, it will be success to update or create
    defaults = dict(mdl_dict, desc=desc)
    kwargs = {"id": defaults["id"], "name": defaults["name"]}
    updated_mdl, created = await Tournament.objects.update_or_create(defaults, **kwargs)
    assert created is False
    assert defaults["desc"] == updated_mdl.desc
    assert mdl.desc != updated_mdl.desc
    # Hint query: use defaults to update without checking conflict
    mdl2, created = await Tournament.objects.update_or_create(
        id=oldid, desc=desc, defaults=dict(mdl_dict, desc="new desc")
    )
    assert created is False
    assert dict(updated_mdl) != dict(mdl2)
    # Missing query: success to create if no conflict
    not_exist_name = str(uuid4())
    no_conflict_defaults = {"name": not_exist_name, "desc": desc}
    no_conflict_kwargs = {"name": not_exist_name}
    created_mdl, created = await Tournament.objects.update_or_create(no_conflict_defaults, **no_conflict_kwargs)
    assert created is True
    assert not_exist_name == created_mdl.name


@pytest.mark.asyncio
async def test_create_rejects_expression_value(db):
    name = str(uuid4())
    with pytest.raises(QueryError, match="desc"):
        await Tournament.objects.create(name=name, desc=F("name"))
    assert await Tournament.objects.get(does_not_exist_exception=None, name=name) is None


@pytest.mark.asyncio
async def test_get_or_create_defaults_expression_on_create_rejected(db):
    name = str(uuid4())
    with pytest.raises(QueryError, match="desc"):
        await Tournament.objects.get_or_create(name=name, defaults={"desc": F("name")})
    assert await Tournament.objects.get(does_not_exist_exception=None, name=name) is None


@pytest.mark.asyncio
async def test_update_or_create_defaults_expression_on_create_rejected(db):
    name = str(uuid4())
    with pytest.raises(QueryError, match="desc"):
        await Tournament.objects.update_or_create(name=name, defaults={"desc": F("name")})
    assert await Tournament.objects.get(does_not_exist_exception=None, name=name) is None


@pytest.mark.asyncio
async def test_update_or_create_defaults_expression_on_update_still_resolves(tournament_model):
    mdl = tournament_model
    _, created = await Tournament.objects.update_or_create(name=mdl.name, defaults={"desc": F("name")})
    assert created is False
    # The in-memory instance can keep the raw F() object rather than the resolved value for a
    # plain unvalidated field - what matters here is that the DB row itself was correctly
    # resolved, not left holding a garbage F() repr.
    refreshed = await Tournament.objects.get(pk=mdl.pk)
    assert refreshed.desc == mdl.name


@pytest.mark.asyncio
async def test_first(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.first()
    assert mdl.id == fetched_mdl.id


@pytest.mark.asyncio
async def test_last(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.last()
    assert mdl.id == fetched_mdl.id


@pytest.mark.asyncio
async def test_latest(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.latest("name")
    assert mdl.id == fetched_mdl.id


@pytest.mark.asyncio
async def test_earliest(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.earliest("name")
    assert mdl.id == fetched_mdl.id


@pytest.mark.asyncio
async def test_filter(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.filter(name="Test").first()
    assert mdl.id == fetched_mdl.id
    fetched_mdl = await model_class.objects.filter(name="Test2").first()
    assert fetched_mdl is None


@pytest.mark.asyncio
async def test_all(saved_model, model_class):
    mdl = saved_model
    mdls = list(await model_class.objects.all())
    assert len(mdls) == 1
    assert mdls == [mdl]


@pytest.mark.asyncio
async def test_get(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.get(name="Test")
    assert mdl.id == fetched_mdl.id

    with pytest.raises(DoesNotExist):
        await model_class.objects.get(name="Test2")

    await model_class.objects.create(name="Test")

    with pytest.raises(MultipleObjectsReturned):
        await model_class.objects.get(name="Test")


@pytest.mark.asyncio
async def test_exists(db):
    await Tournament.objects.create(name="Test")
    ret = await Tournament.objects.filter(name="Test").exists()
    assert ret is True

    ret = await Tournament.objects.filter(name="XXX").exists()
    assert ret is False

    ret = await Tournament.objects.filter(Q(name="XXX") & Q(name="Test")).exists()
    assert ret is False


@pytest.mark.asyncio
async def test_get_or_none(saved_model, model_class):
    mdl = saved_model
    fetched_mdl = await model_class.objects.get(does_not_exist_exception=None, name="Test")
    assert mdl.id == fetched_mdl.id

    fetched_mdl = await model_class.objects.get(does_not_exist_exception=None, name="Test2")
    assert fetched_mdl is None

    await model_class.objects.create(name="Test")

    with pytest.raises(MultipleObjectsReturned):
        await model_class.objects.get(does_not_exist_exception=None, name="Test")


@pytest.mark.skipif(os.name == "nt", reason="timestamp issue on Windows")
@pytest.mark.asyncio
async def test_update_from_dict(db):
    evt1 = await Event.objects.create(name="a", tournament=await Tournament.objects.create(name="a"))
    orig_modified = evt1.modified
    await evt1.update_from_dict({"alias": "8", "name": "b", "bad_name": "foo"}).save()
    assert evt1.alias == 8
    assert evt1.name == "b"

    with pytest.raises(AttributeError):
        _ = evt1.bad_name

    evt2 = await Event.objects.get(name="b")
    assert evt1.pk == evt2.pk
    assert evt1.modified == evt2.modified
    assert orig_modified != evt1.modified

    with pytest.raises(QueryError, match="m2m"):
        await evt2.update_from_dict({"participants": []})

    with pytest.raises(ValidationError):
        await evt2.update_from_dict({"alias": "foo"})


@pytest.mark.asyncio
async def test_index_access(saved_model, model_class):
    obj = await model_class[saved_model.pk]
    assert obj == saved_model


@pytest.mark.asyncio
async def test_index_badval(db, model_class):
    with pytest.raises(DoesNotExist) as exc_info:
        await model_class[32767]
    the_exception = exc_info.value
    assert isinstance(the_exception, LookupError)
    assert the_exception.model is model_class
    assert str(the_exception) == f"{model_class.__name__} has no object with id=32767"


@pytest.mark.asyncio
async def test_index_badtype(db, model_class):
    with pytest.raises(DoesNotExist) as exc_info:
        await model_class["asdf"]
    the_exception = exc_info.value
    assert isinstance(the_exception, LookupError)
    assert the_exception.model is model_class
    assert str(the_exception) == f"{model_class.__name__} has no object with id=asdf"


@pytest.mark.asyncio
async def test_clone(saved_model, model_class):
    mdl = saved_model
    mdl2 = mdl.clone()
    assert mdl2.pk is None
    await mdl2.save()
    assert mdl2.pk != mdl.pk
    mdls = list(await model_class.objects.all())
    assert len(mdls) == 2


@pytest.mark.asyncio
async def test_clone_with_pk(tournament_model):
    mdl = tournament_model
    mdl2 = mdl.clone(pk=8888)
    assert mdl2.pk == 8888
    await mdl2.save()
    assert mdl2.pk == 8888
    assert await Tournament.objects.filter(pk=8888).exists()
    await mdl2.save()
    mdls = list(await Tournament.objects.all())
    assert len(mdls) == 2


@pytest.mark.asyncio
async def test_clone_with_pk_is_used_by_bulk_create(tournament_model):
    # Not a fixed number - an autoincrement counter isn't reset between tests, and could reach it.
    clone_pk = tournament_model.pk + 1
    cloned = tournament_model.clone(pk=clone_pk)
    await Tournament.objects.bulk_create([cloned])
    assert (await Tournament.objects.get(pk=clone_pk)).name == "Test"


@pytest.mark.asyncio
async def test_clone_of_explicit_pk_instance_gets_a_generated_pk(db):
    """The custom-pk state used to stay set on the clone while its pk was reset to None, so
    saving it failed validation instead of falling back to the database-generated pk."""
    explicit = await Tournament.objects.create(id=500, name="explicit")
    cloned = explicit.clone()
    await cloned.save()
    assert cloned.pk not in (None, 500)
    assert await Tournament.objects.all().count() == 2


@pytest.mark.asyncio
async def test_pk_assigned_after_construction_is_used_on_save(db):
    tournament = Tournament(name="late-pk")
    tournament.pk = 778
    await tournament.save()
    assert tournament.pk == 778
    assert (await Tournament.objects.get(pk=778)).name == "late-pk"


@pytest.mark.asyncio
async def test_id_assigned_after_construction_is_used_on_bulk_create(db):
    tournament = Tournament(name="late-id")
    tournament.id = 777
    await Tournament.objects.bulk_create([tournament])
    assert (await Tournament.objects.get(pk=777)).name == "late-id"


@pytest.mark.asyncio
async def test_bulk_create_mixes_explicit_late_and_generated_pks(db):
    constructor_pk = Tournament(id=610, name="constructor-pk")
    late_pk = Tournament(name="late-pk")
    late_pk.pk = 611
    generated_pk = Tournament(name="generated-pk")
    handed_back_pk = Tournament(id=612, name="handed-back-pk")
    handed_back_pk.pk = None

    await Tournament.objects.bulk_create([constructor_pk, late_pk, generated_pk, handed_back_pk])

    pk_by_name = {tournament.name: tournament.pk for tournament in await Tournament.objects.all()}
    assert pk_by_name["constructor-pk"] == 610
    assert pk_by_name["late-pk"] == 611
    assert len(set(pk_by_name.values())) == 4


@pytest.mark.asyncio
async def test_custom_generated_pk_flag_follows_pk_assignment(db):
    assert Tournament(name="a")._custom_generated_pk is False
    assert Tournament(id=5, name="a")._custom_generated_pk is True
    assert Tournament(pk=5, name="a")._custom_generated_pk is True

    tournament = Tournament(name="a")
    tournament.pk = 5
    assert tournament._custom_generated_pk is True
    tournament.pk = None
    assert tournament._custom_generated_pk is False

    created = await Tournament.objects.create(name="a")
    assert created._custom_generated_pk is False
    fetched = await Tournament.objects.get(pk=created.pk)
    assert fetched._custom_generated_pk is False
    await fetched.refresh_from_db()
    assert fetched._custom_generated_pk is False


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_explicit_pk_survives_a_rolled_back_save_and_retry(db):
    """The rollback restore assigns _saved_in_db and pk back one after another - the pk must end
    up counted as caller-supplied again, or the retry would silently get a different pk."""

    class BoomError(Exception):
        pass

    tournament = Tournament(id=4200, name="rolled-back")
    with pytest.raises(BoomError):
        async with Transactions.atomic():
            await tournament.save()
            raise BoomError("unrelated failure after save() returned")

    assert tournament._saved_in_db is False
    assert tournament.pk == 4200
    await tournament.save()
    assert (await Tournament.objects.get(pk=4200)).name == "rolled-back"


@pytest.mark.asyncio
async def test_clone_gets_its_own_auto_now_add_value_on_first_save(db):
    """clone() carried the original's auto_now_add value over, so the new row was stamped with
    the original's creation time instead of its own first-save time."""
    original_created = datetime(2020, 1, 1, tzinfo=Timezone.default())
    original = await DatetimeFields.objects.create(datetime=original_created, datetime_add=original_created)
    assert original.datetime_add == original_created

    cloned = original.clone()
    assert cloned.datetime_add is None
    await cloned.save()

    assert cloned.datetime_add > original_created
    assert (await DatetimeFields.objects.get(pk=original.pk)).datetime_add == original_created


@pytest.mark.asyncio
async def test_clone_respects_auto_now_add_value_assigned_before_save(db):
    original = await DatetimeFields.objects.create(datetime=datetime(2020, 1, 1, tzinfo=Timezone.default()))
    explicit_created = datetime(2021, 6, 1, tzinfo=Timezone.default())

    cloned = original.clone()
    cloned.datetime_add = explicit_created
    await cloned.save()

    assert (await DatetimeFields.objects.get(pk=cloned.pk)).datetime_add == explicit_created


@pytest.mark.asyncio
async def test_clone_deep_copies_mutable_field_values(db):
    """clone() is a shallow copy.copy() under the hood - without an explicit deepcopy step, a
    mutable direct-field value (JSONField's dict/list here) would be the SAME object on both the
    clone and the original, so mutating the clone would silently corrupt the original in memory
    too (and, if the original later gets saved for any other reason, in the DB). Confirmed
    empirically before fixing: cloned.data is original.data was True."""
    original = await JSONFields.objects.create(data={"a": 1})
    cloned = original.clone()

    assert cloned.data is not original.data
    cloned.data["a"] = 999

    assert original.data == {"a": 1}
    assert cloned.data == {"a": 999}


@pytest.mark.asyncio
async def test_clone_leaves_deferred_fields_unloaded(db):
    """A field never loaded by .only()/.defer() must stay unloaded on the clone too - the fix
    iterates over field values already present in __dict__, not every field the model has."""
    created = await JSONFields.objects.create(data={"a": 1})
    partial = await JSONFields.objects.get(pk=created.pk).only("id")

    cloned = partial.clone()

    assert "data" not in cloned.__dict__
    with pytest.raises(AttributeError):
        _ = cloned.data


@pytest.mark.asyncio
async def test_clone_recomputes_app_side_pk_default(db):
    """clone()'s pk_field.generated is False and pk_field.default is None check only guarded
    against the "no default at all" case - a generated=False PK with an app-side default (e.g.
    UUIDField(primary_key=True), which defaults to uuid4) fell into the same `obj.pk = None`
    branch as a DB-generated (SERIAL-style) PK, even though nothing on the DB side would ever
    assign it a fresh value: the clone would try to INSERT a NULL primary key."""
    from tests.testmodels import UUIDPkModel

    original = await UUIDPkModel.objects.create()
    cloned = original.clone()

    assert cloned.pk is not None
    assert cloned.pk != original.pk
    await cloned.save()


@pytest.mark.asyncio
async def test_clone_does_not_share_await_when_save_dict(db):
    """clone() is a shallow copy.copy() under the hood - without an explicit copy of
    _await_when_save, the clone and original started out pointing at the exact SAME dict.
    Model.__setattr__ pops a field's entry from that dict in place whenever the field is
    assigned, so explicitly setting a pending-async-default field on the ORIGINAL (never
    touching the clone at all) silently stripped the CLONE's own still-pending default too."""
    from tests.testmodels import CallableDefault

    original = CallableDefault(id=1)
    cloned = original.clone(pk=2)

    assert original._await_when_save is not cloned._await_when_save

    original.async_default = "manually-set-on-original"
    assert "async_default" in cloned._await_when_save

    await cloned.save()
    assert cloned.async_default == "async_callable_default"


@pytest.mark.asyncio
async def test_bare_copy_does_not_share_await_when_save_dict(db):
    """Same hazard as test_clone_does_not_share_await_when_save_dict, for a bare
    copy.copy(instance) - clone() already re-copied _await_when_save itself before this fix,
    but a plain copy.copy() (now implemented via Model.__copy__, which clone() also delegates
    to) did not, so setting a pending-async-default field on the ORIGINAL after copying it
    silently stripped the COPY's own still-pending default too, with no explicit setattr on the
    copy at all."""
    import copy as copy_module

    from tests.testmodels import CallableDefault

    original = CallableDefault(id=1)
    copied = copy_module.copy(original)
    copied.pk = 2

    assert original._await_when_save is not copied._await_when_save

    original.async_default = "manually-set-on-original"
    assert "async_default" in copied._await_when_save

    await copied.save()
    assert copied.async_default == "async_callable_default"


@pytest.mark.asyncio
async def test_clone_m2m_container_is_not_shared_with_the_original(db):
    """clone()/copy.copy() used to leave a cached ManyToManyRelation container (created by any
    PAST access to event.participants) shared as-is between the original and the clone. That
    container holds its own `.instance` back-reference to the OWNING model - sharing it meant
    clone.participants.add(...) wrote a through-table row keyed by the ORIGINAL's pk instead of
    the clone's own, even though the clone had already been saved under a different pk."""
    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    team_one = await Team.objects.create(name="one")
    team_two = await Team.objects.create(name="two")

    await event.participants.add(team_one)  # forces the _participants container to be created

    cloned_event = event.clone()
    await cloned_event.save()
    assert cloned_event.pk != event.pk

    await cloned_event.participants.add(team_two)

    linked_to_clone = await Event.objects.filter(participants=team_two).first()
    assert linked_to_clone is not None
    assert linked_to_clone.pk == cloned_event.pk

    linked_to_original = await Event.objects.filter(participants=team_one).first()
    assert linked_to_original is not None
    assert linked_to_original.pk == event.pk


@pytest.mark.asyncio
async def test_clone_reverse_fk_container_is_not_shared_with_the_original(db):
    """Same hazard as test_clone_m2m_container_is_not_shared_with_the_original, for a cached
    ReverseRelation container (event backward FK - tournament.events)."""
    tournament = await Tournament.objects.create(name="cup")
    _ = tournament.events  # forces the _events container to be created

    cloned_tournament = tournament.clone()
    await cloned_tournament.save()

    child_event = await cloned_tournament.events.create(name="child")
    assert child_event.tournament_id == cloned_tournament.pk


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_pickle_and_deepcopy_survive_a_cached_m2m_relation(db):
    """A save() (UPDATE) made inside an open transaction on a model with an auto_now field
    stashes a live DB client on the instance (_register_rollback_restore's own pending-restore
    bookkeeping) so a later rollback can undo the optimistic auto_now bump - and a cached M2M
    relation container stashes its own live client the same way, for its own rollback-triggered
    cache reset. Neither is picklable on any backend - pickling (or deepcopy, which goes through
    the same __getstate__ path) used to crash with e.g. "cannot pickle 'sqlite3.Connection'
    object" instead of just dropping that live, process-local, transaction-scoped state."""
    import copy as copy_module
    import pickle

    tournament = await Tournament.objects.create(name="cup")
    event = await Event.objects.create(name="final", tournament=tournament)
    team = await Team.objects.create(name="one")

    async with Transactions.atomic():
        event.name = "final (renamed)"
        await event.save()  # auto_now `modified` bump -> stashes a live rollback-restore client
        await event.participants.add(team)  # stashes a live client on the M2M container too

        pickled = pickle.loads(pickle.dumps(event))
        assert pickled.pk == event.pk
        assert pickled.name == "final (renamed)"

        deep_copied = copy_module.deepcopy(event)
        assert deep_copied.pk == event.pk
        assert deep_copied.name == "final (renamed)"


@pytest.mark.asyncio
async def test_clone_from_db(saved_model, model_class):
    mdl = saved_model
    mdl2 = await model_class.objects.get(pk=mdl.pk)
    mdl3 = mdl2.clone()
    mdl3.pk = None
    await mdl3.save()
    assert mdl3.pk != mdl2.pk
    mdls = list(await model_class.objects.all())
    assert len(mdls) == 2


@pytest.mark.asyncio
async def test_implicit_clone(saved_model, model_class):
    mdl = saved_model
    mdl.pk = None
    await mdl.save()
    mdls = list(await model_class.objects.all())
    assert len(mdls) == 2


@pytest.mark.asyncio
async def test_force_create(saved_model, model_class):
    obj = model_class(name="Test", id=saved_model.id)
    with pytest.raises(IntegrityError):
        await obj.save(force_create=True)


@pytest.mark.asyncio
async def test_force_update(saved_model, model_class):
    obj = model_class(name="Test3", id=saved_model.id)
    await obj.save(force_update=True)
    assert (await model_class.objects.get(id=saved_model.id)).name == "Test3"


@pytest.mark.asyncio
async def test_force_update_raise(saved_model, model_class):
    obj = model_class(name="Test3", id=saved_model.id + 100)
    with pytest.raises(IntegrityError):
        await obj.save(force_update=True)


@pytest.mark.asyncio
async def test_raw(db):
    await Node.objects.create(name="TestRaw")
    ret = await Node.objects.raw("select * from node where name='TestRaw'")
    assert len(ret) == 1
    ret = await Node.objects.raw("select * from node where name='111'")
    assert len(ret) == 0


@pytest.mark.asyncio
async def test_raw_with_bind_params(db):
    await Node.objects.create(name="TestRaw")
    ret = await Node.objects.raw("select * from node where name = %s", ["TestRaw"])
    assert len(ret) == 1

    ret = await Node.objects.raw("select * from node where name = %s", ["111"])
    assert len(ret) == 0

    # A value that would break out of the SQL string if naively interpolated must be safely
    # bound instead - if it leaked into the SQL text unescaped, "OR '1'='1'" would make the WHERE
    # always-true and return every row instead of zero.
    ret = await Node.objects.raw("select * from node where name = %s", ["nonexistent' OR '1'='1"])
    assert ret == []


@pytest.mark.asyncio
async def test_raw_with_bind_params_placeholder_count_mismatch_raises(db):
    with pytest.raises(ValueError, match="placeholder"):
        Node.objects.raw("select * from node where name = %s", ["a", "b"])
    with pytest.raises(ValueError, match="placeholder"):
        Node.objects.raw("select * from node where name = %s and id = %s", ["a"])


# ============================================================================
# TestModelMethodsNoID fixtures and tests
# ============================================================================


@pytest_asyncio.fixture
async def noid_model(db):
    """Fixture that provides a saved NoID model instance."""
    return await NoID.objects.create(name="Test")


@pytest_asyncio.fixture
async def noid_model_unsaved(db):
    """Fixture that provides an unsaved NoID model instance."""
    return NoID(name="Test")


@pytest.mark.asyncio
async def test_noid_save_full(noid_model):
    noid_model.name = "TestS"
    await noid_model.save()
    n_mdl = await NoID.objects.get(id=noid_model.id)
    assert n_mdl.name == "TestS"


@pytest.mark.asyncio
async def test_noid_save_partial_with_pk_update(noid_model):
    # Not allow to update pk field only
    with pytest.raises(QueryError, match="Can't update pk field"):
        await noid_model.save(update_fields=["id"])
    # So does update pk field with others
    with pytest.raises(QueryError, match=rf"use `{NoID.__name__}.objects.create\(\)` instead"):
        await noid_model.save(update_fields=["id", "name"])


@pytest.mark.asyncio
async def test_noid_str(noid_model, noid_model_unsaved):
    assert str(noid_model) == f"NoID object ({noid_model.id})"
    assert str(noid_model_unsaved) == "NoID object (None)"


@pytest.mark.asyncio
async def test_noid_exists(noid_model):
    await NoID.objects.create(name="Test")
    ret = await NoID.objects.filter(name="Test").exists()
    assert ret is True

    ret = await NoID.objects.filter(name="XXX").exists()
    assert ret is False

    ret = await NoID.objects.filter(Q(name="XXX") & Q(name="Test")).exists()
    assert ret is False


@pytest.mark.asyncio
async def test_noid_clone_with_pk(noid_model):
    mdl2 = noid_model.clone(pk=8888)
    assert mdl2.pk == 8888
    await mdl2.save()
    assert mdl2.pk != noid_model.pk
    await mdl2.save()
    mdls = list(await NoID.objects.all())
    assert len(mdls) == 2


# ============================================================================
# TestModelConstructor
# ============================================================================


def test_null_in_nonnull_field():
    with pytest.raises(ValueError, match="name is non nullable field, but null was passed"):
        Event(name=None)


@pytest.mark.asyncio
async def test_none_for_a_generated_pk_means_not_assigned_yet(db):
    """A DB-generated pk of None is "no pk yet" - what a fresh instance (and dict(instance)) already
    reports - not a null violation. It used to raise "id is non nullable field", so
    Model(**dict(instance)) couldn't round-trip an unsaved instance."""
    tournament = Tournament(id=None, name="a")
    assert tournament.pk is None
    assert not tournament._custom_generated_pk

    rebuilt = UniqueName(**dict(UniqueName(name="b")))
    assert rebuilt.pk is None
    assert rebuilt.name == "b"

    via_pk = Tournament(pk=None, name="c")
    assert via_pk.pk is None

    await tournament.save()
    created = await Tournament.objects.create(id=None, name="d")
    assert tournament.pk is not None
    assert created.pk is not None
    assert tournament.pk != created.pk


def test_none_for_a_non_generated_pk_without_default_still_raises():
    with pytest.raises(ValueError, match="non nullable"):
        RequiredPKModel(id=None, name="x")


def test_none_for_a_pk_with_default_applies_the_default():
    assert isinstance(UUIDPkModel(id=None).pk, UUID)


def test_rev_fk():
    with pytest.raises(
        QueryError,
        match="You can't set backward relations through init, change related model instead",
    ):
        Tournament(name="a", events=[])


def test_m2m():
    with pytest.raises(
        QueryError,
        match="You can't set m2m relations through init, use m2m_manager instead",
    ):
        Event(name="a", participants=[])


def test_rev_m2m():
    with pytest.raises(
        QueryError,
        match="You can't set m2m relations through init, use m2m_manager instead",
    ):
        Team(name="a", events=[])


@pytest.mark.asyncio
async def test_rev_o2o(db):
    with pytest.raises(
        QueryError,
        match="You can't set backward one to one relations through init, change related model instead",
    ):
        address = await O2O_null.objects.create(name="Ocean")
        await Dest_null(name="a", address_null=address)


def test_fk_unsaved():
    with pytest.raises(QueryError, match="You should first call .save()"):
        Event(name="a", tournament=Tournament(name="a"))


@pytest.mark.asyncio
async def test_fk_saved(db):
    tournament = await Tournament.objects.create(name="a")
    event = await Event.objects.create(name="a", tournament=tournament)
    assert (await Event.objects.get(event_id=event.event_id)).tournament_id == tournament.id


@pytest.mark.asyncio
async def test_noneawaitable(db):
    assert not NoneAwaitable
    assert await NoneAwaitable is None
    assert not NoneAwaitable
    assert await NoneAwaitable is None


@pytest.mark.asyncio
async def test_save_on_model_with_nothing_to_write_is_a_noop(db):
    pk_only = await PkOnly.objects.create()
    await pk_only.save()
    fetched_pk_only = await PkOnly.objects.get(pk=pk_only.pk)
    await fetched_pk_only.save()
    assert await PkOnly.objects.all().count() == 1

    uuid_pk_model = await UUIDPkModel.objects.create()
    await uuid_pk_model.save()
    assert await UUIDPkModel.objects.all().count() == 1


@pytest.mark.asyncio
async def test_save_with_empty_update_fields_is_a_noop(db):
    tournament = await Tournament.objects.create(name="original")
    tournament.name = "changed in memory only"
    await tournament.save(update_fields=[])
    assert (await Tournament.objects.get(pk=tournament.pk)).name == "original"

    versioned_thing = await VersionedThing.objects.create(name="original")
    await versioned_thing.save(update_fields=[])
    assert versioned_thing.version == 0
    assert (await VersionedThing.objects.get(pk=versioned_thing.pk)).version == 0


@pytest.mark.asyncio
async def test_save_with_only_database_default_update_fields_is_a_noop(db):
    default_model = await DefaultModel.objects.create()
    default_model.int_default = DatabaseDefault(DefaultModel._meta.fields_map["int_default"])
    await default_model.save(update_fields=["int_default"])
    assert (await DefaultModel.objects.get(pk=default_model.pk)).int_default == 1


@pytest.mark.asyncio
async def test_save_still_detects_a_vanished_row_when_there_is_something_to_write(db):
    tournament = await Tournament.objects.create(name="original")
    await Tournament.objects.filter(pk=tournament.pk).delete()
    tournament.name = "changed"
    with pytest.raises(IntegrityError, match="Can't update object that doesn't exist"):
        await tournament.save()
    with pytest.raises(IntegrityError, match="Can't update object that doesn't exist"):
        await tournament.save(update_fields=["name"])


@pytest.mark.asyncio
async def test_save_update_fields_on_unsaved_instance_with_client_pk_updates_by_pk(db):
    """save(update_fields=...) on an instance that has a pk but was never loaded/saved is an
    UPDATE by primary key (an update without a fetch), for every pk shape - a client-generated
    UUID and a composite pk included - not an INSERT."""
    uuid_row = await UUIDFkRelatedNullModel.objects.create(name="original")
    await UUIDFkRelatedNullModel(id=uuid_row.id, name="changed").save(update_fields=["name"])
    assert (await UUIDFkRelatedNullModel.objects.get(id=uuid_row.id)).name == "changed"

    composite_row = await CompositePkThing.objects.create(thing_id=1, revision=1, name="original")
    await CompositePkThing(thing_id=1, revision=1, name="changed").save(update_fields=["name"])
    assert (await CompositePkThing.objects.get(thing_id=1, revision=1)).name == "changed"
    assert composite_row.name == "original"


@pytest.mark.asyncio
async def test_save_update_fields_on_unsaved_instance_whose_row_is_missing_explains_it_never_creates(db):
    missing_id = uuid4()
    with pytest.raises(IntegrityError, match="never creates the row"):
        await UUIDFkRelatedNullModel(id=missing_id, name="ghost").save(update_fields=["name"])
    with pytest.raises(IntegrityError, match="never creates the row"):
        await CompositePkThing(thing_id=9, revision=9, name="ghost").save(update_fields=["name"])

    assert not await UUIDFkRelatedNullModel.objects.filter(id=missing_id).exists()
    assert not await CompositePkThing.objects.filter(thing_id=9, revision=9).exists()


@pytest.mark.asyncio
async def test_save_update_fields_is_ignored_when_the_pk_is_not_assigned_yet(db):
    """With no pk yet (DB-generated), there is nothing to UPDATE by - the row is inserted whole and
    update_fields is ignored, as documented."""
    tournament = Tournament(name="brand new", desc="kept")
    await tournament.save(update_fields=["name"])

    fetched = await Tournament.objects.get(id=tournament.id)
    assert fetched.name == "brand new"
    assert fetched.desc == "kept"


# ============================================================================
# One-shot iterables in update_fields=/fields=
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_from_db_accepts_a_generator_of_fields(db):
    """fields was typed Iterable[str], but .only(*fields) consumed a generator before the
    `fields or ...` truthiness check ran - the refresh then silently updated nothing at all."""
    tournament = await Tournament.objects.create(name="original")
    await Tournament.objects.filter(pk=tournament.pk).update(name="changed-in-db")

    await tournament.refresh_from_db(fields=(field for field in ["name"]))

    assert tournament.name == "changed-in-db"


@pytest.mark.asyncio
async def test_save_accepts_a_generator_of_update_fields_and_syncs_dirty_snapshot(db):
    """A generator update_fields was exhausted by the write itself, so the post-write dirty
    snapshot sync saw an empty list and get_dirty_fields() kept reporting the just-saved field
    as dirty."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"

    await thing.save(update_fields=(field for field in ["name"]))

    assert (await DirtyTrackedThing.objects.get(pk=thing.pk)).name == "B"
    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_save_accepts_a_generator_of_update_fields_on_a_tenant_scoped_model(db):
    """The tenant-scope check `tenant_field in update_fields` ate a generator before the executor
    ever saw it, so the UPDATE matched zero columns and save() raised a false IntegrityError."""
    with Tenancy.scope(1):
        widget = await TenantScopedWidget.objects.create(name="A1", company_id=1)
        widget.name = "A2"

        await widget.save(update_fields=(field for field in ["name"]))

        assert (await TenantScopedWidget.objects.get(pk=widget.pk)).name == "A2"


# ============================================================================
# Unknown/string/relation names in update_fields=/fields=
# ============================================================================


@pytest.mark.asyncio
async def test_save_rejects_unknown_update_field_with_field_error(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)

    with pytest.raises(FieldError, match="nope"):
        await event.save(update_fields=["nope"])


@pytest.mark.asyncio
async def test_save_rejects_a_bare_string_for_update_fields(db):
    """update_fields="name" iterated as characters and surfaced as KeyError: 'n'."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)

    with pytest.raises(QueryError, match=r"\['name'\]"):
        await event.save(update_fields="name")


@pytest.mark.asyncio
async def test_save_update_fields_accepts_a_relation_name(db):
    """update_fields=["tournament"] writes the relation's key column, like Django."""
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    event = await Event.objects.create(name="e", tournament=first)

    event.tournament = second
    event.name = "not saved"
    await event.save(update_fields=["tournament"])

    reloaded = await Event.objects.get(pk=event.pk)
    assert (reloaded.tournament_id, reloaded.name) == (second.pk, "e")


@pytest.mark.asyncio
async def test_save_update_fields_accepts_a_relations_source_field(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    event = await Event.objects.create(name="e", tournament=first)

    event.tournament = second
    await event.save(update_fields=["tournament_id"])

    assert (await Event.objects.get(pk=event.pk)).tournament_id == second.pk


@pytest.mark.asyncio
async def test_refresh_from_db_rejects_unknown_and_string_fields(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)

    with pytest.raises(FieldError, match="nope"):
        await event.refresh_from_db(fields=["nope"])
    with pytest.raises(QueryError):
        await event.refresh_from_db(fields="name")


@pytest.mark.asyncio
async def test_refresh_from_db_relation_name_refreshes_its_key_column(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    event = await Event.objects.create(name="e", tournament=first)
    await Event.objects.filter(pk=event.pk).update(tournament_id=second.pk, name="renamed")

    await event.refresh_from_db(fields=["tournament"])

    assert (event.tournament_id, event.name) == (second.pk, "e")
    assert (await event.tournament).name == "second"


@pytest.mark.asyncio
async def test_refresh_from_db_relation_source_field_refreshes_the_cached_relation(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    event = await Event.objects.create(name="e", tournament=first)
    assert (await event.tournament).name == "first"
    await Event.objects.filter(pk=event.pk).update(tournament_id=second.pk)

    await event.refresh_from_db(fields=["tournament_id"])

    assert (await event.tournament).name == "second"


# ============================================================================
# refresh_from_db() on partial / soft-deleted instances
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_from_db_on_partial_instance_without_pk_raises_incomplete_instance_error(db):
    """A .only("name") instance has no pk - get(pk=None) used to surface a misleading
    DoesNotExist, unlike delete()/restore() which raise IncompleteInstanceError."""
    tournament = await Tournament.objects.create(name="t")
    partial = await Tournament.objects.filter(pk=tournament.pk).only("name").first()
    assert partial is not None

    with pytest.raises(IncompleteInstanceError):
        await partial.refresh_from_db()


@pytest.mark.asyncio
async def test_refresh_from_db_refreshes_an_instance_after_its_own_soft_delete(db):
    obj = await SoftDeleteStandalone.objects.create(name="A")
    await obj.delete()
    await SoftDeleteStandalone.objects.include_deleted().filter(pk=obj.pk).update(name="renamed-in-db")

    await obj.refresh_from_db()

    assert obj.name == "renamed-in-db"
    assert obj.deleted_at is not None


@pytest.mark.asyncio
async def test_refresh_from_db_refreshes_an_instance_loaded_through_include_deleted(db):
    obj = await SoftDeleteStandalone.objects.create(name="A")
    await obj.delete()
    loaded = await SoftDeleteStandalone.objects.include_deleted().get(pk=obj.pk)
    await SoftDeleteStandalone.objects.include_deleted().filter(pk=obj.pk).update(name="renamed-in-db")

    await loaded.refresh_from_db(fields=["name"])

    assert loaded.name == "renamed-in-db"


@pytest.mark.asyncio
async def test_refresh_from_db_of_a_deleted_instance_still_respects_tenant_scope(db):
    from tests.testmodels import TenantScopedFactory

    with Tenancy.scope(1):
        factory = await TenantScopedFactory.objects.create(name="F", company_id=1)
        await factory.delete()
        await factory.refresh_from_db()

    with Tenancy.scope(2):
        with pytest.raises(DoesNotExist):
            await factory.refresh_from_db()

    with pytest.raises(QueryError):
        await factory.refresh_from_db()


# ============================================================================
# Constructor / assignment / update_from_dict validation
# ============================================================================


@pytest.mark.asyncio
async def test_constructor_and_assignment_reject_a_non_model_relation_value_cleanly(db):
    """Model(tournament=5) crashed with a raw AttributeError on `int._saved_in_db`, while the
    equivalent `event.tournament = 5` already raised a clean ValidationError."""
    with pytest.raises(ValidationError):
        Event(name="e", tournament=5)

    event = Event(name="e")
    with pytest.raises(ValidationError):
        event.tournament = 5
    with pytest.raises(ValidationError):
        event.update_from_dict({"tournament": 5})


@pytest.mark.asyncio
@pytest.mark.parametrize("relation_first", [True, False])
async def test_constructor_rejects_conflicting_relation_and_source_column(db, relation_first):
    """Event(tournament=t, tournament_id=999) silently resolved the conflict by kwargs order."""
    tournament = await Tournament.objects.create(name="t")
    conflicting = {"tournament": tournament, "tournament_id": tournament.pk + 1}
    if not relation_first:
        conflicting = dict(reversed(conflicting.items()))

    with pytest.raises(QueryError, match="tournament_id"):
        Event(name="e", **conflicting)


@pytest.mark.asyncio
@pytest.mark.parametrize("relation_first", [True, False])
async def test_constructor_accepts_matching_relation_and_source_column_in_any_order(db, relation_first):
    tournament = await Tournament.objects.create(name="t")
    matching = {"tournament": tournament, "tournament_id": tournament.pk}
    if not relation_first:
        matching = dict(reversed(matching.items()))

    event = Event(name="e", **matching)

    assert event.tournament_id == tournament.pk
    assert event.tournament is tournament


@pytest.mark.asyncio
async def test_constructor_rejects_explicit_none_for_a_non_null_relation(db):
    """name=None already raised ValueError for a non-nullable field, tournament=None was accepted."""
    with pytest.raises(ValueError, match="tournament is non nullable"):
        Event(name="e", tournament=None)
    assert Event(name="e", reporter=None).reporter_id is None


@pytest.mark.asyncio
async def test_assigning_an_unsaved_instance_to_a_relation_raises_like_the_constructor(db):
    """The constructor rejected an unsaved related instance, plain assignment silently stored a
    NULL foreign key (the unsaved instance has no pk yet)."""
    event = Event(name="e")
    with pytest.raises(QueryError, match="You should first call .save()"):
        event.tournament = Tournament(name="unsaved")


@pytest.mark.asyncio
async def test_related_manager_create_rejects_a_conflicting_foreign_key(db):
    """tournament.events.create(name=..., tournament=other) silently ignored `tournament=other`."""
    tournament = await Tournament.objects.create(name="t")
    other = await Tournament.objects.create(name="other")

    with pytest.raises(QueryError):
        await tournament.events.create(name="x", tournament=other)
    with pytest.raises(QueryError):
        await tournament.events.create(name="x", tournament_id=other.pk)
    assert await Event.objects.filter(name="x").count() == 0

    event = await tournament.events.create(name="x", tournament=tournament)
    assert event.tournament_id == tournament.pk


@pytest.mark.asyncio
async def test_update_from_dict_is_atomic_when_a_later_key_fails(db):
    """{"name": "changed", "alias": "abc"} applied name, then raised on alias."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="original", tournament=tournament, alias=1)

    with pytest.raises(ValidationError):
        event.update_from_dict({"name": "changed", "alias": "abc"})
    assert (event.name, event.alias) == ("original", 1)

    with pytest.raises(ValidationError):
        event.update_from_dict({"name": "changed", "tournament": 5})
    assert (event.name, event.tournament_id) == ("original", tournament.pk)

    with pytest.raises(ValueError):
        event.update_from_dict({"name": "changed", "tournament": None})
    assert (event.name, event.tournament_id) == ("original", tournament.pk)

    with pytest.raises(QueryError):
        event.update_from_dict({"name": "changed", "participants": []})
    assert event.name == "original"


@pytest.mark.asyncio
async def test_update_from_dict_is_atomic_against_the_soft_delete_write_guard(db):
    obj = await SoftDeleteStandalone.objects.create(name="A")

    with pytest.raises(QueryError):
        obj.update_from_dict({"name": "changed", "deleted_at": None})

    assert obj.name == "A"


@pytest.mark.asyncio
async def test_update_from_dict_still_applies_every_valid_key(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    event = await Event.objects.create(name="original", tournament=first, alias=1)

    event.update_from_dict({"name": "changed", "alias": "7", "tournament": second, "unknown": "ignored"})

    assert (event.name, event.alias, event.tournament_id) == ("changed", 7, second.pk)
    assert event.tournament is second


# ============================================================================
# to_dict()/dict()/iteration with a still-pending async default
# ============================================================================


@pytest.mark.asyncio
async def test_to_dict_of_an_unsaved_instance_reports_a_pending_async_default_as_none(db):
    """An unsaved instance never ran its async default(), so the attribute doesn't exist yet -
    to_dict()/dict()/iteration raised a raw AttributeError."""
    instance = CallableDefault()

    assert instance.to_dict()["async_default"] is None
    assert dict(instance)["async_default"] is None
    assert dict(instance)["callable_default"] == "callable_default"

    await instance.save()
    assert instance.to_dict()["async_default"] == "async_callable_default"


# ============================================================================
# Relation caches must not go stale across refresh_from_db()/M2M mutations
# ============================================================================


@pytest.mark.asyncio
async def test_full_refresh_from_db_invalidates_the_reverse_fk_cache(db):
    """Synchronous access (no re-``fetch_related()``) is the only way to prove the CACHE itself
    was invalidated - a second ``fetch_related()`` call always re-queries regardless of caching,
    so it would pass even without this fix."""
    tournament = await Tournament.objects.create(name="t")
    await Event.objects.create(name="e1", tournament=tournament)
    await prefetch_related_objects([tournament], "events")
    assert [event.name for event in tournament.events] == ["e1"]

    await Event.objects.create(name="e2", tournament=tournament)
    await tournament.refresh_from_db()

    with pytest.raises(NoValuesFetched):
        list(tournament.events)


@pytest.mark.asyncio
async def test_m2m_add_invalidates_an_already_loaded_cache(db):
    """Synchronous access (not another ``await``, which always re-queries regardless of caching)
    is the only way to prove the CACHE itself was invalidated by add()."""
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    team = await Team.objects.create(name="team1")
    await prefetch_related_objects([event], "participants")
    assert list(event.participants) == []

    await event.participants.add(team)

    with pytest.raises(NoValuesFetched):
        list(event.participants)
    assert await event.participants == [team]


@pytest.mark.asyncio
async def test_m2m_clear_and_set_invalidate_an_already_loaded_cache(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    team1 = await Team.objects.create(name="team1")
    team2 = await Team.objects.create(name="team2")
    await event.participants.add(team1, team2)
    await prefetch_related_objects([event], "participants")
    assert sorted(team.name for team in event.participants) == ["team1", "team2"]

    await event.participants.clear()
    with pytest.raises(NoValuesFetched):
        list(event.participants)
    assert await event.participants == []

    await prefetch_related_objects([event], "participants")
    await event.participants.set(team1)
    with pytest.raises(NoValuesFetched):
        list(event.participants)
    assert await event.participants == [team1]


@pytest.mark.asyncio
async def test_m2m_remove_invalidates_an_already_loaded_cache(db):
    tournament = await Tournament.objects.create(name="t")
    event = await Event.objects.create(name="e", tournament=tournament)
    team1 = await Team.objects.create(name="team1")
    team2 = await Team.objects.create(name="team2")
    await event.participants.add(team1, team2)
    await prefetch_related_objects([event], "participants")
    assert sorted(team.name for team in event.participants) == ["team1", "team2"]

    await event.participants.remove(team1)

    with pytest.raises(NoValuesFetched):
        list(event.participants)
    assert [team.name for team in await event.participants] == ["team2"]


@pytest.mark.asyncio
async def test_refresh_from_db_reloads_a_lazy_joined_fk(db):
    """lazy="joined" is meant for synchronous, already-resolved access - a plain refresh_from_db()
    used to leave the cached related object cleared, so the next synchronous `.parent.name`
    access got an unawaited QuerySetSingle instead of the related instance."""
    parent = await LazyJoinedParent.objects.create(name="original")
    child = await LazyJoinedChild.objects.create(name="c", parent=parent)
    assert child.parent.name == "original"

    await LazyJoinedParent.objects.filter(pk=parent.pk).update(name="renamed")
    await child.refresh_from_db()

    assert child.parent.name == "renamed"


@pytest.mark.asyncio
async def test_refresh_from_db_partial_fields_reloads_a_lazy_joined_relation_cache(db):
    """A partial refresh_from_db(fields=[...]) that includes a lazy="joined" relation's own
    shadow column (e.g. "parent_id") must resolve the JOIN again and repopulate the cache - it
    used to silently DROP the cache instead, leaving `.parent` an unawaited QuerySet."""
    parent1 = await LazyJoinedParent.objects.create(name="P1")
    parent2 = await LazyJoinedParent.objects.create(name="P2")
    child = await LazyJoinedChild.objects.create(name="C", parent=parent1)
    fetched = await LazyJoinedChild.objects.filter(pk=child.pk).first()
    assert fetched.parent.name == "P1"

    await LazyJoinedChild.objects.filter(pk=child.pk).update(parent=parent2)
    await fetched.refresh_from_db(fields=["parent_id"])

    assert fetched.parent.name == "P2"


@pytest.mark.asyncio
async def test_refresh_from_db_partial_fields_without_shadow_column_leaves_lazy_joined_cache_untouched(db):
    """A partial refresh_from_db(fields=[...]) that does NOT touch a lazy="joined" relation's own
    shadow column must leave that relation's already-cached value exactly as it was."""
    parent1 = await LazyJoinedParent.objects.create(name="P1")
    parent2 = await LazyJoinedParent.objects.create(name="P2")
    child = await LazyJoinedChild.objects.create(name="C", parent=parent1)
    fetched = await LazyJoinedChild.objects.filter(pk=child.pk).first()
    assert fetched.parent.name == "P1"

    await LazyJoinedChild.objects.filter(pk=child.pk).update(name="C2", parent=parent2)
    await fetched.refresh_from_db(fields=["name"])

    assert fetched.name == "C2"
    assert fetched.parent.name == "P1"


@pytest.mark.asyncio
async def test_refresh_from_db_partial_fields_after_explicit_select_related_still_reloads_cache(db):
    """An instance originally fetched via an explicit .select_related(...) (not just the field-
    level lazy="joined" default) must still get its cache correctly reloaded by a later partial
    refresh_from_db(fields=[shadow_column])."""
    parent1 = await LazyJoinedParent.objects.create(name="P1")
    parent2 = await LazyJoinedParent.objects.create(name="P2")
    child = await LazyJoinedChild.objects.create(name="C", parent=parent1)
    fetched = await LazyJoinedChild.objects.filter(pk=child.pk).select_related("parent").first()
    assert fetched.parent.name == "P1"

    await LazyJoinedChild.objects.filter(pk=child.pk).update(parent=parent2)
    await fetched.refresh_from_db(fields=["parent_id"])

    assert fetched.parent.name == "P2"


@pytest.mark.asyncio
async def test_refresh_from_db_partial_fields_after_defer_related_still_populates_cache(db):
    """.defer_related(...) opts a relation out of its lazy="joined" default for the ONE query it
    was called on - it isn't recorded anywhere on the instance itself, so a later
    refresh_from_db(fields=[shadow_column]) has no way to know the instance was ever fetched with
    it deferred, and correctly runs its own default (lazy-honoring) query instead."""
    parent1 = await LazyJoinedParent.objects.create(name="P1")
    parent2 = await LazyJoinedParent.objects.create(name="P2")
    child = await LazyJoinedChild.objects.create(name="C", parent=parent1)
    fetched = await LazyJoinedChild.objects.filter(pk=child.pk).defer_related("parent").first()
    assert "_parent" not in fetched.__dict__

    await LazyJoinedChild.objects.filter(pk=child.pk).update(parent=parent2)
    await fetched.refresh_from_db(fields=["parent_id"])

    assert fetched.parent.name == "P2"


@pytest.mark.asyncio
async def test_refresh_from_db_works_on_model_with_soft_delete_field(db):
    """refresh_from_db()'s default (no explicit `fields`) branch unconditionally setattr'd every
    DB field, including the soft-delete field - __setattr__ rejects any direct write to
    soft_delete_field on a persisted instance (use .delete()/.restore() instead), so ANY model
    with Meta.soft_delete_field crashed on a plain refresh_from_db(), even one that wasn't
    changing the soft-delete state at all."""
    obj = await SoftDeleteStandalone.objects.create(name="A")
    await obj.refresh_from_db()
    assert obj.deleted_at is None


@pytest.mark.asyncio
async def test_refresh_from_db_uses_model_field_names_not_db_columns(db):
    """The default branch iterated self._meta.db_fields (actual DB COLUMN names) and setattr'd
    them directly - for a field whose source_field differs from its model field name, this
    created a bogus new attribute under the raw column name instead of updating the real one,
    leaving the model field itself stale."""
    parent = await UUIDPkSourceModel.objects.create()
    obj = await UUIDFkRelatedSourceModel.objects.create(name="original", model=parent)

    await UUIDFkRelatedSourceModel.objects.filter(pk=obj.pk).update(name="changed-in-db")
    await obj.refresh_from_db()

    assert obj.name == "changed-in-db"
    assert not hasattr(obj, "c")  # "c" is the DB column name for the "name" field


@pytest.mark.asyncio
async def test_refresh_from_db_resyncs_the_dirty_tracking_snapshot(db):
    """refresh_from_db() does its own setattr loop directly, bypassing both save() (which always
    re-snapshots after a write) and hydration (which always re-snapshots after a fetch) - it had
    no equivalent, so a field changed by refresh_from_db() itself kept reporting its PRE-refresh
    value as "dirty" even though the whole point of refresh_from_db() is to make the instance
    clean again, matching what the database now actually holds."""
    obj = await DirtyTrackedThing.objects.create(name="A", count=0)
    await DirtyTrackedThing.objects.filter(pk=obj.pk).update(count=5)

    await obj.refresh_from_db()

    assert obj.count == 5
    assert obj.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_refresh_from_db_partial_resyncs_only_the_refreshed_fields(db):
    """A partial refresh_from_db(fields=[...]) must only re-sync the snapshot for the fields it
    actually refreshed - mirrors save(update_fields=...)'s own partial-sync behavior, so an
    unrelated local edit on a field NOT named in `fields` stays correctly reported as dirty."""
    obj = await DirtyTrackedThing.objects.create(name="A", count=0)
    await DirtyTrackedThing.objects.filter(pk=obj.pk).update(count=5)
    obj.nullable = "unsaved local edit"

    await obj.refresh_from_db(fields=["count"])

    assert obj.count == 5
    assert obj.get_dirty_fields() == {"nullable": (None, "unsaved local edit")}


@pytest.mark.asyncio
async def test_refresh_from_db_with_no_fields_clears_partial_flag(db):
    """A call with no fields= (the documented "all db fields... are updated" behavior) loads
    every db-backed field, same as a fresh .get() would - the instance is genuinely complete
    afterward, but _partial (save()'s sole gate for IncompleteInstanceError) was never reset, so
    a .only(...)-fetched instance stayed stuck "partial" forever even after this full refresh,
    and a later bare .save() incorrectly kept raising IncompleteInstanceError."""
    obj = await DirtyTrackedThing.objects.create(name="A", count=0, nullable="x")

    partial = await DirtyTrackedThing.objects.all().only("id", "name").get(pk=obj.pk)
    assert partial._partial is True

    await partial.refresh_from_db()

    assert partial._partial is False
    assert partial.count == 0
    assert partial.nullable == "x"
    partial.name = "B"
    await partial.save()  # must not raise IncompleteInstanceError


@pytest.mark.asyncio
async def test_refresh_from_db_with_explicit_fields_leaves_partial_flag_untouched(db):
    """The targeted fields=[...] branch above deliberately does NOT clear _partial - it never
    claimed to complete anything beyond the fields it was asked to refresh, so a .only(...)
    instance refreshing just one field must still require update_fields on its next save()."""
    obj = await DirtyTrackedThing.objects.create(name="A", count=0)
    await DirtyTrackedThing.objects.filter(pk=obj.pk).update(count=5)

    partial = await DirtyTrackedThing.objects.all().only("id", "count").get(pk=obj.pk)
    await partial.refresh_from_db(fields=["count"])

    assert partial._partial is True


@pytest.mark.asyncio
async def test_getbypk_missing_row_still_raises_object_does_not_exist(db):
    await IntFields.objects.create(intnum=1)
    with pytest.raises(DoesNotExist):
        await IntFields[999999]


@pytest.mark.asyncio
async def test_getbypk_invalid_key_type_keeps_real_cause_discoverable(db):
    """`except DoesNotExist, ValueError:` used to remap a malformed pk value (one that can't be
    coerced to the pk field's own type, e.g. "not-a-valid-int" against an IntField pk) to the
    exact same DoesNotExist as a genuinely missing row, with the original ValueError
    completely gone - masking a real type error as plain "not found". Model[key] stays
    KeyError-compatible (existing, deliberate dict-subscript ergonomics - see
    test_index_badtype/test_noid_index_badtype), but the original cause is now chained instead
    of discarded.

    The cause is a ValidationError, not a bare ValueError - Field.to_db_value's own
    `self.field_type(value)` coercion failure is now wrapped as ValidationError (see
    hare/fields/base.py, this session's base-Field fix, matching the same fix already applied to
    Int/CharEnumFieldInstance/UUIDField's own constructor calls), and _getbypk's except clause
    (hare/models/model.py) was updated to catch both."""
    await IntFields.objects.create(intnum=1)
    with pytest.raises(DoesNotExist) as exc_info:
        await IntFields["not-a-valid-int"]
    assert isinstance(exc_info.value.__cause__, ValidationError)
    assert "invalid literal for int" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_delete_of_a_bulk_created_object_without_a_primary_key_says_why(db):
    """bulk_create() without returning=True leaves a generated pk unset - delete() used to fail
    validating the None primary key instead."""
    tournament = Tournament(name="bulk")
    await Tournament.objects.bulk_create([tournament])
    if tournament.pk is not None:
        pytest.skip("this backend fills the generated primary key in")

    with pytest.raises(QueryError, match="primary key isn't set"):
        await tournament.delete()


def test_update_from_dict_sets_the_primary_key_through_pk():
    """`pk` is the key Model(pk=...) takes - update_from_dict() used to drop it as unknown."""
    from tests.testmodels import CompositePkThing

    tournament = Tournament(id=1, name="t")
    assert tournament.update_from_dict({"pk": 5, "nope": 1}).pk == 5

    thing = CompositePkThing(thing_id=1, revision=2, name="x")
    thing.update_from_dict({"pk": (7, 8)})
    assert (thing.pk, thing.thing_id, thing.revision) == ((7, 8), 7, 8)

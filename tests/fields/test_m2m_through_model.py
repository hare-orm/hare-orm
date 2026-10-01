import asyncio
import datetime

import pytest

from hare.exceptions import (
    ConfigurationError,
    QueryError,
)
from hare.fields import SET_DEFAULT, SET_NULL, ManyToManyField
from hare.inspectdb.introspector import SchemaIntrospector
from tests import testmodels


@pytest.mark.asyncio
async def test_add_creates_through_row_with_default_extra_field(db):
    """`.add()` creates a real Membership row, leaving `joined_date` at its declared default."""
    person = await testmodels.Person.objects.create(name="Alice")
    group = await testmodels.Group.objects.create(name="Admins")
    await person.groups.add(group)

    assert await person.groups == [group]
    assert await group.members == [person]

    rows = await testmodels.Membership.objects.filter(person=person, group=group)
    assert len(rows) == 1
    assert rows[0].joined_date is None


@pytest.mark.asyncio
async def test_add_through_defaults_sets_extra_field(db):
    """`.add(..., through_defaults={...})` sets the through model's own extra field on the row
    it inserts - previously ignored entirely (Finding 1)."""
    person = await testmodels.Person.objects.create(name="Nora")
    group = await testmodels.Group.objects.create(name="Admins")
    await person.groups.add(group, through_defaults={"joined_date": datetime.date(2024, 5, 1)})

    membership = await testmodels.Membership.objects.get(person=person, group=group)
    assert membership.joined_date == datetime.date(2024, 5, 1)


@pytest.mark.asyncio
async def test_add_through_defaults_overrides_db_default(db):
    """`through_defaults` also overrides a `db_default=`-backed extra field (WidgetTagMembership's
    own `weight`, normally defaulted to 1 by the database itself when `.add()` leaves it out)."""
    widget = await testmodels.Widget.objects.create(name="Gizmo")
    tag = await testmodels.Tag.objects.create(name="rare")
    await widget.tags.add(tag, through_defaults={"weight": 42})

    row = await testmodels.WidgetTagMembership.objects.get(widget=widget, tag=tag)
    assert row.weight == 42


@pytest.mark.asyncio
async def test_add_through_defaults_only_applies_to_newly_inserted_rows(db):
    """Mirrors Django's own through_defaults semantics: a pair that already exists is left
    untouched, even if a later .add() call for the same pair passes through_defaults."""
    person = await testmodels.Person.objects.create(name="Oscar")
    group = await testmodels.Group.objects.create(name="Staff")
    await person.groups.add(group)
    await person.groups.add(group, through_defaults={"joined_date": datetime.date(2024, 6, 1)})

    membership = await testmodels.Membership.objects.get(person=person, group=group)
    assert membership.joined_date is None


@pytest.mark.asyncio
async def test_add_through_defaults_rejected_without_real_through_model(db):
    """through_defaults only makes sense for a real through=Model - hare's own auto-managed
    2-column through table has no extra fields for it to set."""
    m1 = await testmodels.Flavor.objects.create(name="Vanilla")
    m2 = await testmodels.Drink.objects.create(name="Soda")
    with pytest.raises(QueryError, match="through_defaults isn't supported"):
        await m2.flavors.add(m1, through_defaults={"anything": 1})


@pytest.mark.asyncio
async def test_add_through_defaults_rejected_for_unknown_field(db):
    person = await testmodels.Person.objects.create(name="Priya")
    group = await testmodels.Group.objects.create(name="Admins")
    with pytest.raises(QueryError, match="isn't a field on through model"):
        await person.groups.add(group, through_defaults={"nonexistent_field": 1})


@pytest.mark.asyncio
async def test_add_through_defaults_rejected_for_relation_own_fk_field(db):
    person = await testmodels.Person.objects.create(name="Quinn")
    group = await testmodels.Group.objects.create(name="Admins")
    with pytest.raises(QueryError, match="own FK fields making up the relation itself"):
        await person.groups.add(group, through_defaults={"person": person})


@pytest.mark.asyncio
async def test_add_through_defaults_rejected_for_generated_field(db):
    """bulk_create()/bulk_update()/QuerySet.update() all reject a GeneratedField target with a
    clean ConfigurationError/IntegrityError before ever reaching the database -
    through_defaults built its own INSERT column list directly and had no equivalent guard,
    letting the same mistake reach the database and surface as a raw, dialect-leaking driver
    error (e.g. "cannot insert a value into generated column") instead."""
    widget = await testmodels.Widget.objects.create(name="Gizmo")
    tag = await testmodels.Tag.objects.create(name="rare")
    with pytest.raises(QueryError, match="generated field"):
        await widget.tags.add(tag, through_defaults={"weight_doubled": 42})


@pytest.mark.asyncio
async def test_no_separate_auto_through_table_created(db):
    """Only the real "membership"/"person"/"group" tables exist - hare doesn't also create its
    own opaque 2-column through table for a ManyToManyField(through=Model). Uses
    SchemaIntrospector (the same dialect-agnostic introspection `hare inspectdb` uses) rather than
    a raw SQL query against a dialect-specific system catalog, so this runs unchanged on both
    SQLite and Postgres."""
    db_client = testmodels.Membership._meta.db
    table_names = await SchemaIntrospector.get_table_names(db_client)
    assert {"person", "group", "membership"} <= set(table_names)

    membership_table = await SchemaIntrospector.inspect_table(db_client, "membership")
    column_names = {column.name for column in membership_table.columns}
    assert {"joined_date", "person_id", "group_id"} <= column_names


@pytest.mark.asyncio
async def test_through_model_independently_queryable(db):
    """The through model is a normal model: filterable, and its extra field can be read/written
    directly, with the write reflected through the M2M relation's own through-table lookups."""
    person = await testmodels.Person.objects.create(name="Bob")
    group = await testmodels.Group.objects.create(name="Staff")
    await person.groups.add(group)

    membership = await testmodels.Membership.objects.get(person=person, group=group)
    assert membership.joined_date is None
    membership.joined_date = datetime.date(2024, 5, 1)
    await membership.save()

    reloaded = await testmodels.Membership.objects.get(person=person, group=group)
    assert reloaded.joined_date == datetime.date(2024, 5, 1)
    # The M2M relation itself still reflects the same (person, group) pair.
    assert await person.groups == [group]


@pytest.mark.asyncio
async def test_add_idempotent_without_db_unique_constraint(db):
    """.add() called twice for the same pair creates only one through row, via the
    SELECT+INSERT dedup path (no DB-level unique constraint exists on a user-declared through
    table, unlike hare's own auto-managed through table)."""
    person = await testmodels.Person.objects.create(name="Carol")
    group = await testmodels.Group.objects.create(name="Admins")
    await person.groups.add(group)
    await person.groups.add(group)

    rows = await testmodels.Membership.objects.filter(person=person, group=group)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_add_many(db):
    person = await testmodels.Person.objects.create(name="Dan")
    group1 = await testmodels.Group.objects.create(name="G1")
    group2 = await testmodels.Group.objects.create(name="G2")
    await person.groups.add(group1, group2)
    assert sorted(g.name for g in await person.groups) == ["G1", "G2"]


@pytest.mark.asyncio
async def test_remove(db):
    person = await testmodels.Person.objects.create(name="Erin")
    group1 = await testmodels.Group.objects.create(name="G1")
    group2 = await testmodels.Group.objects.create(name="G2")
    await person.groups.add(group1, group2)
    await person.groups.remove(group1)

    assert await person.groups == [group2]
    assert await testmodels.Membership.objects.filter(person=person, group=group1) == []


@pytest.mark.asyncio
async def test_clear(db):
    person = await testmodels.Person.objects.create(name="Frank")
    group1 = await testmodels.Group.objects.create(name="G1")
    group2 = await testmodels.Group.objects.create(name="G2")
    await person.groups.add(group1, group2)
    await person.groups.clear()

    assert await person.groups == []
    assert await testmodels.Membership.objects.filter(person=person) == []


@pytest.mark.asyncio
async def test_reverse_add(db):
    """Adding from the auto-generated backward relation (Group.members) works the same way."""
    person = await testmodels.Person.objects.create(name="Grace")
    group = await testmodels.Group.objects.create(name="Admins")
    await group.members.add(person)

    assert await person.groups == [group]
    assert await group.members == [person]
    assert await testmodels.Membership.objects.filter(person=person, group=group) != []


@pytest.mark.asyncio
async def test_prefetch_related(db):
    person = await testmodels.Person.objects.create(name="Hank")
    group1 = await testmodels.Group.objects.create(name="G1")
    group2 = await testmodels.Group.objects.create(name="G2")
    await person.groups.add(group1, group2)

    fetched = await testmodels.Person.objects.filter(id=person.id).prefetch_related("groups").first()
    assert sorted(g.name for g in fetched.groups) == ["G1", "G2"]


@pytest.mark.asyncio
async def test_filter_by_m2m_relation(db):
    person = await testmodels.Person.objects.create(name="Ivy")
    group = await testmodels.Group.objects.create(name="Admins")
    await person.groups.add(group)

    found = await testmodels.Person.objects.filter(groups=group)
    assert found == [person]


@pytest.mark.asyncio
async def test_through_model_delete_removes_m2m_link(db):
    """Deleting the through row directly (not via .remove()) also drops the M2M link, since the
    relation reads the through model's own table."""
    person = await testmodels.Person.objects.create(name="Jack")
    group = await testmodels.Group.objects.create(name="Admins")
    await person.groups.add(group)

    membership = await testmodels.Membership.objects.get(person=person, group=group)
    await membership.delete()

    assert await person.groups == []


@pytest.mark.asyncio
async def test_class_reference_through_model_add_remove_clear(db):
    """`through=` also accepts a live Model class directly (not just an "app.Model" string) -
    Widget/Tag/WidgetTagMembership covers that form end-to-end, including a db_default= extra
    field surviving .add()'s raw INSERT."""
    widget = await testmodels.Widget.objects.create(name="Gadget")
    tag1 = await testmodels.Tag.objects.create(name="cool")
    tag2 = await testmodels.Tag.objects.create(name="new")

    await widget.tags.add(tag1, tag2)
    assert sorted(t.name for t in await widget.tags) == ["cool", "new"]

    rows = await testmodels.WidgetTagMembership.objects.filter(widget=widget, tag=tag1)
    assert len(rows) == 1
    assert rows[0].weight == 1

    await widget.tags.remove(tag1)
    assert [t.name for t in await widget.tags] == ["new"]

    await widget.tags.clear()
    assert await widget.tags == []
    assert await testmodels.WidgetTagMembership.objects.filter(widget=widget) == []


@pytest.mark.asyncio
async def test_through_model_extra_field_updatable_via_direct_write(db):
    widget = await testmodels.Widget.objects.create(name="Thing")
    tag = await testmodels.Tag.objects.create(name="shiny")
    await widget.tags.add(tag)

    row = await testmodels.WidgetTagMembership.objects.get(widget=widget, tag=tag)
    assert row.weight == 1
    row.weight = 5
    await row.save()

    reloaded = await testmodels.WidgetTagMembership.objects.get(widget=widget, tag=tag)
    assert reloaded.weight == 5


@pytest.mark.asyncio
async def test_through_model_deconstruct_keeps_the_through_model_reference(db):
    # `db` forces Apps._init_relations() to have run for this process/worker before reading
    # field.through - that's what resolves through=Model to the real table name string
    # ("membership") in the first place. Without it, this test's result depends on some OTHER
    # test in the same xdist worker happening to have already triggered init first - passing
    # or failing depending on worker/collection order, not on this test's own logic.
    field = testmodels.Person._meta.fields_map["groups"]
    assert field.deconstruct()[2]["through"] == "models.Membership"
    assert field.through == "membership"


@pytest.mark.asyncio
async def test_class_reference_through_model_deconstruct(db):
    field = testmodels.Widget._meta.fields_map["tags"]
    assert field.deconstruct()[2]["through"] == "models.WidgetTagMembership"
    assert field.through == "widgettagmembership"


def test_through_and_through_table_string_both_supported():
    """A plain opaque table-name string for `through=` (hare's pre-existing behavior) still
    works unchanged alongside the new `through=Model` support - a field's `through_model`
    attribute simply stays None for that case."""
    field = ManyToManyField("models.Foo", through="a_through_table")
    assert field.through_model is None
    assert field.through == "a_through_table"


def test_through_model_class_reference_stored_directly():
    field = ManyToManyField("models.Foo", through=testmodels.Membership)
    assert field.through_model is testmodels.Membership
    # Not resolved to a table name yet - that only happens once Apps._init_relations runs.
    assert field.through == ""


def test_on_delete_set_default_rejected_for_auto_through_table_but_allowed_for_through_model():
    # SET_NULL is implemented for hare's own auto-managed through table (a separate fix), so it
    # no longer raises here - only SET_DEFAULT still does, since there's no way to declare a
    # default target row for the auto-managed table's columns.
    with pytest.raises(ConfigurationError, match="on_delete=SET_DEFAULT is not supported"):
        ManyToManyField("models.Foo", on_delete=SET_DEFAULT)
    # No ConfigurationError either way - on_delete is only meaningful for hare's own auto-managed
    # through table; a through=Model's own FK fields carry their own (independently validated)
    # on_delete.
    field = ManyToManyField("models.Foo", through=testmodels.Membership, on_delete=SET_NULL)
    assert field.on_delete == SET_NULL
    field_default = ManyToManyField("models.Foo", through=testmodels.Membership, on_delete=SET_DEFAULT)
    assert field_default.on_delete == SET_DEFAULT


@pytest.mark.asyncio
async def test_remove_soft_deletes_through_row_when_meta_soft_delete_field_is_set(db):
    """ManyToManyRelation.remove()/clear() used to always hard-delete the through row regardless
    of Meta.soft_delete_field - unlike Model.delete()/QuerySet.delete(), which already respect it
    for every other model. The row must survive .remove() with its soft_delete_field set, not
    disappear from the table.

    Scoped to the through row's own state (queried directly on the through model, which DOES
    apply the ambient soft-delete filter normally) - NOT to `crew.mates` itself, which has a
    separate, wider, NOT-yet-fixed gap: see test_m2m_relation_traversal_does_not_yet_exclude_
    soft_deleted_through_rows below for why."""
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Alpha")
    mate = await testmodels.SoftDeleteThroughMate.objects.create(name="Bob")
    await crew.mates.add(mate)

    await crew.mates.remove(mate)

    assert await testmodels.SoftDeleteThroughMembership.objects.filter(crew=crew, mate=mate) == []
    row = await testmodels.SoftDeleteThroughMembership.objects.include_deleted().get(crew=crew, mate=mate)
    assert row.deleted_at is not None


@pytest.mark.asyncio
async def test_clear_soft_deletes_through_rows_when_meta_soft_delete_field_is_set(db):
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Bravo")
    mate1 = await testmodels.SoftDeleteThroughMate.objects.create(name="Cid")
    mate2 = await testmodels.SoftDeleteThroughMate.objects.create(name="Dee")
    await crew.mates.add(mate1, mate2)

    await crew.mates.clear()

    assert await testmodels.SoftDeleteThroughMembership.objects.filter(crew=crew) == []
    rows = await testmodels.SoftDeleteThroughMembership.objects.include_deleted().filter(crew=crew)
    assert len(rows) == 2
    assert all(row.deleted_at is not None for row in rows)


@pytest.mark.asyncio
async def test_remove_and_clear_do_not_restamp_an_already_soft_deleted_through_row(db):
    """remove()/clear() overwrote deleted_at of a through row that was ALREADY soft-deleted with a
    fresh timestamp - erasing the moment it was actually removed (the audit trail
    Meta.soft_delete_field exists to keep)."""
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Foxtrot")
    mate1 = await testmodels.SoftDeleteThroughMate.objects.create(name="Fay")
    mate2 = await testmodels.SoftDeleteThroughMate.objects.create(name="Gil")
    await crew.mates.add(mate1, mate2)

    await crew.mates.remove(mate1)
    first_removed_at = (
        await testmodels.SoftDeleteThroughMembership.objects.include_deleted().get(crew=crew, mate=mate1)
    ).deleted_at
    await asyncio.sleep(0.01)

    await crew.mates.remove(mate1)
    await crew.mates.clear()

    restamped = await testmodels.SoftDeleteThroughMembership.objects.include_deleted().get(crew=crew, mate=mate1)
    assert restamped.deleted_at == first_removed_at
    other = await testmodels.SoftDeleteThroughMembership.objects.include_deleted().get(crew=crew, mate=mate2)
    assert other.deleted_at is not None
    assert other.deleted_at != first_removed_at


@pytest.mark.asyncio
async def test_soft_removed_through_row_bumps_version_and_auto_now(db):
    """remove()/clear() soft-deleted a through row with a bare SET deleted_at=..., skipping the
    optimistic_lock_field/auto_now bump every other write path (.update(), bulk_update(), save(), the bulk
    soft-delete fast path) applies - the row kept its old version (a concurrent writer's
    optimistic-lock check still passed) and a stale updated_at."""
    owner = await testmodels.VersionedThroughOwner.objects.create(name="Hotel")
    target1 = await testmodels.VersionedThroughTarget.objects.create(name="India")
    target2 = await testmodels.VersionedThroughTarget.objects.create(name="Juliet")
    await owner.targets.add(target1, target2)

    await owner.targets.remove(target1)
    removed = await testmodels.VersionedThroughLink.objects.include_deleted().get(owner=owner, target=target1)
    assert removed.deleted_at is not None
    assert removed.version == 1
    assert removed.updated_at is not None

    await owner.targets.clear()
    cleared = await testmodels.VersionedThroughLink.objects.include_deleted().get(owner=owner, target=target2)
    assert cleared.version == 1
    # The already-removed row is not touched again by clear().
    untouched = await testmodels.VersionedThroughLink.objects.include_deleted().get(owner=owner, target=target1)
    assert untouched.version == 1


@pytest.mark.asyncio
async def test_add_revives_a_soft_deleted_pair_instead_of_treating_it_as_already_active(db):
    """A pair .remove()d (now soft-deleted, per the fix above) must not silently look "already
    there" to a later .add() for the exact same pair - it needs to be treated as absent, the same
    way an ordinary soft-deleted row is invisible to a default (non-.include_deleted()) query."""
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Charlie")
    mate = await testmodels.SoftDeleteThroughMate.objects.create(name="Eve")
    await crew.mates.add(mate)
    await crew.mates.remove(mate)
    assert await testmodels.SoftDeleteThroughMembership.objects.filter(crew=crew, mate=mate) == []

    await crew.mates.add(mate)

    active_rows = await testmodels.SoftDeleteThroughMembership.objects.filter(crew=crew, mate=mate)
    assert len(active_rows) == 1
    assert active_rows[0].deleted_at is None


@pytest.mark.asyncio
async def test_m2m_nested_lookup_excludes_soft_deleted_through_rows(db):
    """A JOIN built via a NESTED lookup crossing the M2M relation (`.filter(mates__name=...)`,
    `.filter(crews__name=...)`, or an equivalent multi-hop select_related()/order_by()/annotate()
    path - anything that goes through LookupPaths.get_joins_for_related_field() +
    get_nested_field()/_join_table_by_field()/Q._get_nested_filter()) folds the through model's
    own Meta.soft_delete_field/Meta.tenant_field ambient scope into the through-table join
    itself, the same way a JOIN straight to a soft-delete/tenant-scoped MODEL already was -
    previously invisible entirely, so a soft-deleted through row's pair kept surfacing through
    any of these paths regardless."""
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Delta")
    mate = await testmodels.SoftDeleteThroughMate.objects.create(name="Frank")
    await crew.mates.add(mate)
    await crew.mates.remove(mate)

    assert await testmodels.SoftDeleteThroughMate.objects.filter(crews__name="Delta") == []
    assert await testmodels.SoftDeleteThroughCrew.objects.filter(mates__name="Frank") == []


@pytest.mark.asyncio
async def test_m2m_bare_relation_and_prefetch_exclude_soft_deleted_through_rows(db):
    """The remaining 2 read paths beyond the nested-lookup one above, each resolving through its
    own, entirely separate mechanism: a BARE relation-equality filter/traversal
    (`.filter(mates=some_mate)`, `crew.mates`) via Q._process_filter_kwarg()'s
    `model._meta.filters[key]` join descriptor (never touching
    LookupPaths.get_joins_for_related_field() at all), and `prefetch_related()` via its own
    dedicated raw through-table SELECT (`PrefetchExecutorMixin._prefetch_m2m_relation()`, never
    going through `Manager.get_queryset()`/any JOIN-building code at all). Both independently
    fixed to fold the through model's own Meta.soft_delete_field/Meta.tenant_field ambient scope
    in too, closing the gap the nested-lookup fix above didn't reach."""
    crew = await testmodels.SoftDeleteThroughCrew.objects.create(name="Golf")
    mate = await testmodels.SoftDeleteThroughMate.objects.create(name="Hank")
    await crew.mates.add(mate)
    await crew.mates.remove(mate)

    assert await crew.mates == []
    assert await testmodels.SoftDeleteThroughMate.objects.filter(crews=crew) == []
    assert await testmodels.SoftDeleteThroughCrew.objects.filter(mates=mate) == []
    reloaded_crew = await testmodels.SoftDeleteThroughCrew.objects.filter(id=crew.id).prefetch_related("mates").first()
    assert reloaded_crew is not None
    assert reloaded_crew.mates.related_objects == []

    # An active pair (never removed) must still surface correctly on all 4 read paths - the
    # ambient-scope fold must not turn into a blanket "always exclude" filter.
    mate2 = await testmodels.SoftDeleteThroughMate.objects.create(name="Ivy")
    await crew.mates.add(mate2)

    assert await crew.mates == [mate2]
    assert await testmodels.SoftDeleteThroughMate.objects.filter(crews=crew) == [mate2]
    assert await testmodels.SoftDeleteThroughCrew.objects.filter(mates=mate2) == [crew]
    reloaded_crew2 = (
        await testmodels.SoftDeleteThroughCrew.objects.filter(id=crew.id).prefetch_related("mates").first()
    )
    assert reloaded_crew2 is not None
    assert reloaded_crew2.mates.related_objects == [mate2]


@pytest.mark.asyncio
async def test_add_applies_python_side_defaults_of_the_through_model(db):
    """A through model whose fields fill themselves from Python-side defaults (UUID primary key,
    auto_now_add, a plain default=) used to make .add() violate NOT NULL, since the raw INSERT
    carried only the FK columns."""
    owner = await testmodels.DefaultsThroughOwner.objects.create(name="o")
    first = await testmodels.DefaultsThroughTarget.objects.create(name="t1")
    second = await testmodels.DefaultsThroughTarget.objects.create(name="t2")

    await owner.targets.add(first, second)

    rows = await testmodels.DefaultsThroughLink.objects.filter(owner=owner)
    assert len(rows) == 2
    assert len({row.id for row in rows}) == 2
    assert all(row.note == "linked" for row in rows)
    assert all(row.created_at is not None for row in rows)


@pytest.mark.asyncio
async def test_add_through_defaults_override_the_python_side_default(db):
    owner = await testmodels.DefaultsThroughOwner.objects.create(name="o")
    target = await testmodels.DefaultsThroughTarget.objects.create(name="t")

    await owner.targets.add(target, through_defaults={"note": "custom"})

    (row,) = await testmodels.DefaultsThroughLink.objects.filter(owner=owner)
    assert row.note == "custom"


@pytest.mark.asyncio
async def test_m2m_isnull_ignores_a_soft_deleted_through_row(db):
    removed = await testmodels.SoftDeleteThroughCrew.objects.create(name="removed")
    linked = await testmodels.SoftDeleteThroughCrew.objects.create(name="linked")
    await testmodels.SoftDeleteThroughCrew.objects.create(name="empty")
    mate = await testmodels.SoftDeleteThroughMate.objects.create(name="Hal")
    await removed.mates.add(mate)
    await linked.mates.add(mate)
    await removed.mates.remove(mate)

    empty_crews = await testmodels.SoftDeleteThroughCrew.objects.filter(mates__isnull=True).values_list(
        "name", flat=True
    )
    assert sorted(empty_crews) == ["empty", "removed"]
    assert await testmodels.SoftDeleteThroughCrew.objects.exclude(mates__isnull=True).values_list(
        "name", flat=True
    ) == ["linked"]
    assert await testmodels.SoftDeleteThroughMate.objects.filter(crews__isnull=False).count() == 1

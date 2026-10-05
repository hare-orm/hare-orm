import pytest

from hare.contrib import test as hare_test
from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import ConfigurationError, IntegrityError, QueryError
from hare.query.expressions import Q
from hare.query.functions import Count
from tests.testmodels import CompositePkThing, CompositePkTriple


@pytest.mark.asyncio
async def test_q_pk_and_pk_in_work_for_composite_primary_key(db):
    """ "pk"/"pk__in" were only special-cased inside PendingFilterCalls.build_filter_q(QuerySet) (.filter()/.get()
    kwargs) - an explicit Q(pk=...)/Q(pk__in=...) object (needed to combine a composite pk
    condition with anything else via |/&, since a bare kwarg only ever gives AND) bypassed that
    entirely and raised FieldError: Unknown filter param 'pk'."""
    a = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    b = await CompositePkThing.objects.create(thing_id=2, revision=1, name="B")
    await CompositePkThing.objects.create(thing_id=3, revision=1, name="C")

    assert await CompositePkThing.objects.filter(Q(pk=a.pk)).values_list("name", flat=True) == ["A"]
    assert sorted(await CompositePkThing.objects.filter(Q(pk=a.pk) | Q(name="C")).values_list("name", flat=True)) == [
        "A",
        "C",
    ]
    assert sorted(await CompositePkThing.objects.filter(Q(pk__in=[a.pk, b.pk])).values_list("name", flat=True)) == [
        "A",
        "B",
    ]


@pytest.mark.asyncio
async def test_pk_not_and_pk_not_in_of_a_composite_primary_key(db):
    """A composite primary key took pk= and pk__in= alone - pk__not= and pk__not_in=, which a relation
    to such a key takes, raised FieldError."""
    a = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    b = await CompositePkThing.objects.create(thing_id=2, revision=1, name="B")
    await CompositePkThing.objects.create(thing_id=3, revision=1, name="C")

    def names(queryset):
        return queryset.order_by("name").values_list("name", flat=True)

    assert await names(CompositePkThing.objects.filter(pk__not=a.pk)) == ["B", "C"]
    assert await names(CompositePkThing.objects.filter(pk__not_in=[a.pk, b.pk])) == ["C"]
    assert await names(CompositePkThing.objects.filter(pk__not_in=[])) == ["A", "B", "C"]
    assert await names(CompositePkThing.objects.filter(Q(pk__not=a.pk) & Q(name__in=["A", "B"]))) == ["B"]
    assert await names(CompositePkThing.objects.exclude(pk__not_in=[b.pk])) == ["B"]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_ddl_generates_composite_primary_key_clause(db):
    alias = next(iter(Connections.current().db_config))
    sql = Connections.get(alias).get_schema_sql(safe=True)

    # the table's own creation statement, isolated from the rest of the full-schema dump
    table_start = sql.index('"compositepkthing"')
    table_end = sql.index(";", table_start)
    table_sql = sql[table_start:table_end]

    assert 'PRIMARY KEY ("thing_id", "revision")' in table_sql
    # neither member column gets its own inline PRIMARY KEY marker
    assert '"thing_id" INT NOT NULL PRIMARY KEY' not in table_sql
    assert '"revision" INT NOT NULL PRIMARY KEY' not in table_sql


@pytest.mark.asyncio
async def test_create_and_get_by_composite_pk(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")

    fetched = await CompositePkThing.objects.get(thing_id=1, revision=1)
    assert fetched.name == "A"
    assert fetched.pk == (1, 1)


@pytest.mark.asyncio
async def test_pk_returns_tuple(db):
    thing = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    assert thing.pk == (1, 1)


def test_default_custom_generated_pk_is_false_for_composite_pk():
    """A composite pk's own component fields can never be generated=True
    (ModelMeta._parse_composite_pk forbids it), so there's no such thing as a DB-auto-generated
    composite pk to distinguish a "custom" value from - unlike a single-column pk, where
    default_custom_generated_pk tells DB-hydrated/cloned instances apart by whether their pk
    column is in generated_db_fields. Before this fix, db_pk_column stayed "" (never a real
    column name) for a composite pk, so `"" not in generated_db_fields` evaluated True
    unconditionally - inconsistent with a freshly-constructed composite-pk instance, whose
    _custom_generated_pk starts and stays False (the pk=True-and-generated=True condition that
    would ever flip it True can never be satisfied by a composite pk member either)."""
    assert CompositePkThing._meta.default_custom_generated_pk is False


@pytest.mark.asyncio
async def test_fetched_composite_pk_instance_matches_fresh_instance_custom_generated_pk(db):
    """BulkCreateQuery partitions objects by _custom_generated_pk to decide whether to populate
    RETURNING-backfilled fields - a DB-hydrated (or cloned) composite-pk instance must agree with
    a freshly-built one on this flag, or it silently opts out of that population for no reason
    tied to its actual pk-generation semantics (composite pks are never DB-generated, so this
    flag should be False for every composite-pk instance regardless of how it was constructed)."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    fetched = await CompositePkThing.objects.get(thing_id=1, revision=1)
    fresh = CompositePkThing(thing_id=2, revision=1, name="B")

    assert fetched._custom_generated_pk is False
    assert fresh._custom_generated_pk is False


@pytest.mark.asyncio
async def test_composite_pk_assignment_never_marks_custom_generated_pk(db):
    thing = CompositePkThing(thing_id=1, revision=1, name="A")
    thing.pk = (2, 2)
    cloned = thing.clone(pk=(3, 3))

    assert CompositePkThing._meta.generated_pk_field_name is None
    assert thing._custom_generated_pk is False
    assert cloned._custom_generated_pk is False
    await cloned.save()
    assert (await CompositePkThing.objects.get(thing_id=3, revision=3)).name == "A"


@pytest.mark.asyncio
async def test_same_thing_id_different_revision_are_distinct_rows(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="rev1")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="rev2")

    rev1 = await CompositePkThing.objects.get(thing_id=1, revision=1)
    rev2 = await CompositePkThing.objects.get(thing_id=1, revision=2)
    assert rev1.name == "rev1"
    assert rev2.name == "rev2"
    assert await CompositePkThing.objects.filter(thing_id=1).count() == 2


@pytest.mark.asyncio
async def test_update_by_composite_pk(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    thing = await CompositePkThing.objects.get(thing_id=1, revision=1)
    thing.name = "B"
    await thing.save()

    refreshed = await CompositePkThing.objects.get(thing_id=1, revision=1)
    assert refreshed.name == "B"


@pytest.mark.asyncio
async def test_update_only_touches_the_matching_composite_row(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="rev1")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="rev2")

    thing = await CompositePkThing.objects.get(thing_id=1, revision=1)
    thing.name = "changed"
    await thing.save()

    assert (await CompositePkThing.objects.get(thing_id=1, revision=1)).name == "changed"
    assert (await CompositePkThing.objects.get(thing_id=1, revision=2)).name == "rev2"


@pytest.mark.asyncio
async def test_delete_by_composite_pk(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    thing = await CompositePkThing.objects.get(thing_id=1, revision=1)
    await thing.delete()

    assert not await CompositePkThing.objects.filter(thing_id=1, revision=1).exists()
    assert await CompositePkThing.objects.filter(thing_id=1, revision=2).exists()


@pytest.mark.asyncio
async def test_bulk_delete_by_composite_pk_queryset(db):
    """QuerySet.delete() - not the instance-level .delete() above - exercises _execute()'s
    _has_protected_relations() check, which reads self.model._meta.primary_key_attribute as a bare string
    whenever the model has incoming relations. A composite-PK model can never have incoming FK/M2M
    relations (see the rejection tests above), so that string-typed branch is unreachable here by
    construction - this confirms the no-relations fast path actually works end to end for a
    composite PK, not just that the unreachable branch is theoretically safe."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    deleted = await CompositePkThing.objects.filter(thing_id=1).delete()

    assert deleted == 2
    assert not await CompositePkThing.objects.filter(thing_id=1).exists()
    assert await CompositePkThing.objects.filter(thing_id=2).exists()


@pytest.mark.asyncio
async def test_duplicate_composite_pk_raises_integrity_error(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    with pytest.raises(IntegrityError):
        await CompositePkThing.objects.create(thing_id=1, revision=1, name="duplicate")


@pytest.mark.asyncio
async def test_three_column_composite_pk(db):
    """Not hardcoded to exactly 2 columns."""
    await CompositePkTriple.objects.create(a=1, b=2, c=3, name="X")
    fetched = await CompositePkTriple.objects.get(a=1, b=2, c=3)
    assert fetched.name == "X"
    assert fetched.pk == (1, 2, 3)

    # a different combination of the same values in different "slots" is a different row
    await CompositePkTriple.objects.create(a=2, b=1, c=3, name="Y")
    assert await CompositePkTriple.objects.filter().count() == 2


@pytest.mark.asyncio
async def test_clone_composite_pk_explicit_value(db):
    """Regression anchor - the explicit-pk path is unaffected by extending the implicit path."""
    original = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    cloned = original.clone(pk=(2, 1))
    assert cloned.pk == (2, 1)
    await cloned.save()
    assert await CompositePkThing.objects.filter().count() == 2


@pytest.mark.asyncio
async def test_clone_composite_pk_raises_for_component_without_default(db):
    """Only the component genuinely lacking a default/db_default blocks the implicit clone -
    not the whole composite PK unconditionally."""
    from tests.testmodels import CompositePkVersioned

    original = await CompositePkVersioned.objects.create(id=1, version=1, name="A")
    with pytest.raises(QueryError, match="id"):
        original.clone()


@pytest.mark.asyncio
async def test_clone_composite_pk_uses_per_component_sync_defaults(db):
    from tests.testmodels import CompositePkBothDefaulted

    original = await CompositePkBothDefaulted.objects.create(a=5, b=6, name="A")
    cloned = original.clone()

    assert cloned.a == 1
    assert cloned.b == 2
    assert cloned.pk == (1, 2)
    await cloned.save()


@pytest.mark.asyncio
async def test_clone_composite_pk_resolves_async_default_component(db):
    from tests.testmodels import CompositePkAsyncDefault

    original = await CompositePkAsyncDefault.objects.create(id=5, version=5, name="A")
    cloned = original.clone()

    assert cloned.id == 1
    assert "version" in cloned._await_when_save
    await cloned.save()
    assert cloned.version == 7


@pytest.mark.asyncio
async def test_clone_composite_pk_resolves_db_default_component(db):
    from hare.fields.database_default import DatabaseDefault
    from tests.testmodels import CompositePkDbDefault

    original = await CompositePkDbDefault.objects.create(id=5, version=5, name="A")
    cloned = original.clone()

    assert cloned.id == 1
    assert isinstance(cloned.version, DatabaseDefault)
    await cloned.save()
    assert cloned.version == 1


@pytest.mark.asyncio
async def test_cannot_update_pk_field_directly(db):
    thing = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    with pytest.raises(QueryError):
        await thing.save(update_fields=["thing_id"])


@pytest.mark.asyncio
async def test_regular_single_pk_model_unaffected(db):
    """Regression anchor - the whole point of scoping this behind isinstance(primary_key_attribute, tuple)
    checks throughout is that a normal single-PK model's behavior doesn't change at all."""
    from tests.testmodels import IntFields

    obj = await IntFields.objects.create(intnum=1)
    assert not isinstance(obj.pk, tuple)
    obj.intnum = 2
    await obj.save()
    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 2
    await obj.delete()
    assert not await IntFields.objects.filter(pk=obj.pk).exists()


def test_composite_pk_needs_at_least_two_fields():
    from hare.fields.composite_primary_key import CompositePrimaryKey

    with pytest.raises(ConfigurationError):
        CompositePrimaryKey("only_one")


def test_composite_pk_rejects_duplicate_field_names():
    from hare.fields.composite_primary_key import CompositePrimaryKey

    with pytest.raises(ConfigurationError):
        CompositePrimaryKey("a", "a")


def test_composite_pk_member_cannot_be_marked_primary_key():
    from hare import fields
    from hare.models import Model

    with pytest.raises(ConfigurationError):

        class BadComposite(Model):
            a = fields.IntField(primary_key=True)
            b = fields.IntField()

            pk = fields.CompositePrimaryKey("a", "b")


def test_composite_pk_member_cannot_be_generated():
    from hare import fields
    from hare.models import Model

    with pytest.raises(ConfigurationError):

        class BadComposite2(Model):
            a = fields.IntField(generated=True)
            b = fields.IntField()

            pk = fields.CompositePrimaryKey("a", "b")


def test_composite_pk_member_cannot_be_a_relation_field():
    """A ForeignKeyField/OneToOneField/ManyToManyField has no single DB column of its own (only
    its generated shadow column(s), e.g. 'parent_id') - MetaInfo.fields_db_projection deliberately
    excludes it (has_db_field=False). Passing one as a CompositePrimaryKey component used to sail
    through this validation (field.pk and field.generated are both False on an un-configured FK)
    and only fail much later, deep in schema generation, with a raw, unguarded
    `KeyError: 'parent'` from fields_db_projection[name] - not a clear error naming the real
    problem."""
    from hare import fields
    from hare.models import Model

    with pytest.raises(ConfigurationError, match="relation field"):

        class FkPkParent(Model):
            id = fields.IntField(primary_key=True)

        class FkPkChild(Model):
            parent = fields.ForeignKeyField("models.FkPkParent")
            revision = fields.IntField()

            pk = fields.CompositePrimaryKey("parent", "revision")


def test_composite_pk_field_must_exist():
    from hare import fields
    from hare.models import Model

    with pytest.raises(ConfigurationError):

        class BadComposite3(Model):
            a = fields.IntField()

            pk = fields.CompositePrimaryKey("a", "not_a_real_field")


def test_diamond_inheritance_from_two_different_composite_pk_bases_raises():
    """A concrete model inheriting from two UNRELATED abstract bases, each with its OWN
    composite PK, used to silently keep only the first base's PK - the second base's PK fields
    were demoted to plain non-PK fields with no error at all, since _search_for_field_attributes
    re-synthesizes each abstract base's composite PK marker under the same fixed dict key and
    only ever populated it once."""
    from hare import fields
    from hare.models import Model

    class AbstractDiamondA(Model):
        a = fields.IntField()
        b = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "b")

        class Meta:
            abstract = True

    class AbstractDiamondB(Model):
        c = fields.IntField()
        d = fields.IntField()
        pk = fields.CompositePrimaryKey("c", "d")

        class Meta:
            abstract = True

    with pytest.raises(ConfigurationError, match="two CompositePrimaryKey declarations"):

        class ConcreteDiamond(AbstractDiamondA, AbstractDiamondB):
            pass


def test_diamond_inheritance_from_same_composite_pk_ancestor_via_two_paths_is_fine():
    """The genuine diamond case - both branches lead back to the SAME abstract ancestor and its
    SAME composite PK - is not a conflict (the field is inherited exactly once, just reachable
    through two paths) and must not raise, matching how a plain .pk=True field inherited via two
    paths already causes no error either."""
    from hare import fields
    from hare.models import Model

    class AbstractDiamondBase(Model):
        a = fields.IntField()
        b = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "b")

        class Meta:
            abstract = True

    class AbstractDiamondLeft(AbstractDiamondBase):
        class Meta:
            abstract = True

    class AbstractDiamondRight(AbstractDiamondBase):
        class Meta:
            abstract = True

    class ConcreteDiamondSamePk(AbstractDiamondLeft, AbstractDiamondRight):
        pass

    assert ConcreteDiamondSamePk._meta.primary_key_attribute == ("a", "b")
    assert set(ConcreteDiamondSamePk._meta.fields_map) >= {"a", "b"}


@pytest.mark.asyncio
async def test_fk_to_composite_pk_model_generates_one_shadow_column_per_pk_component():
    """A ForeignKeyField with no explicit to_field= targeting a composite-PK model defaults to
    the whole PK, in order, and gets one shadow column per component instead of the usual single
    "<field>_id" - resolved at Hare.init() time (FK setup runs over registered models), not at
    bare class definition, hence the separate module."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_default_constraint"]):
        from tests.model_setup.models_fk_composite_pk_target_default_constraint import CompositeTarget, FkTarget

        fk_object = FkTarget._meta.fields_map["other"]
        assert fk_object.to_field_names == ("a", "b")
        assert fk_object.to_field_instances == (
            CompositeTarget._meta.fields_map["a"],
            CompositeTarget._meta.fields_map["b"],
        )
        assert fk_object.source_fields == ("other_a", "other_b")
        assert fk_object.source_field == "other_a"
        assert "other_a" in FkTarget._meta.fields_map
        assert "other_b" in FkTarget._meta.fields_map
        assert FkTarget._meta.fields_map["other_a"].reference is fk_object
        assert FkTarget._meta.fields_map["other_b"].reference is fk_object


@pytest.mark.asyncio
async def test_fk_to_composite_pk_model_setter_getter_round_trip():
    """Assigning a composite-target FK via the model kwarg (not the raw shadow columns)
    populates every shadow column, and reading it back resolves the actual related row -
    exercises the lazy property get/set wired up in
    MetaInfo._generate_lazy_forward_relation_fields / Model._fk_setter/_fk_getter for
    the composite case. Built in memory (not FkTarget.objects.create()) - a real DB-level composite FK
    constraint on fktarget's own shadow columns is a later, DDL-layer commit; this only
    exercises the ORM-level property plumbing, querying CompositeTarget (already fully correct)
    for the getter half."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_default_constraint"]):
        from tests.model_setup.models_fk_composite_pk_target_default_constraint import CompositeTarget, FkTarget

        target = await CompositeTarget.objects.create(a=1, b=2, name="target-row")
        child = FkTarget(other=target)

        assert child.other_a == 1
        assert child.other_b == 2

        related = await child.other
        assert related.pk == (1, 2)
        assert related.name == "target-row"


@pytest.mark.asyncio
async def test_fk_to_composite_pk_model_setter_clears_all_shadow_columns_on_none():
    """Assigning None to a composite-target FK clears every shadow column, not just the first."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_default_constraint"]):
        from tests.model_setup.models_fk_composite_pk_target_default_constraint import CompositeTarget, FkTarget

        target = await CompositeTarget.objects.create(a=3, b=4, name="target-row-2")
        child = FkTarget(other=target)
        child.other = None
        assert child.other_a is None
        assert child.other_b is None


@pytest.mark.asyncio
async def test_filter_by_composite_target_fk_equality():
    """A bare `.filter(other=instance)` against a composite-target FK resolves to an AND of
    per-component equalities on the shadow columns directly (no JOIN needed) - exercises
    Q._get_composite_relation_kwarg()."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=1, b=2, name="target-row")
        other_target = await CompositeTarget.objects.create(a=9, b=9, name="other-row")
        matching = await FkTargetNoConstraint.objects.create(other=target, name="matching")
        await FkTargetNoConstraint.objects.create(other=other_target, name="not-matching")

        results = await FkTargetNoConstraint.objects.filter(other=target)
        assert [r.id for r in results] == [matching.id]


@pytest.mark.asyncio
async def test_filter_by_composite_target_fk_none_matches_isnull():
    """`.filter(other=None)` against a composite-target FK matches rows where every shadow
    column is NULL, not just the first."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import FkTargetNoConstraint

        unset = await FkTargetNoConstraint.objects.create(other=None, name="unset")

        results = await FkTargetNoConstraint.objects.filter(other=None)
        assert [r.id for r in results] == [unset.id]


@pytest.mark.asyncio
async def test_select_related_on_composite_target_fk_uses_row_value_join():
    """.select_related() over a composite-target FK joins on every shadow column at once (a
    `(a,b) = (x,y)` row-value comparison via hare.sql.terms.Tuple), not just the first - exercises
    LookupPaths.get_joins_for_related_field's forward-relation branch."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=5, b=6, name="joined-target")
        child = await FkTargetNoConstraint.objects.create(other=target, name="joined-child")

        qs = FkTargetNoConstraint.objects.filter(id=child.id).select_related("other")
        sql = qs.sql()
        expected_join = (
            '"a","fktargetnoconstraint__other"."b")='
            '("fktargetnoconstraint"."other_a","fktargetnoconstraint"."other_b")'
        )
        assert expected_join in sql, sql

        fetched = await qs.first()
        assert fetched.other.pk == (5, 6)
        assert fetched.other.name == "joined-target"


@pytest.mark.asyncio
async def test_nested_filter_through_backward_relation_to_composite_target_fk():
    """Filtering the composite-PK-target model through its backward relation
    (`no_constraint_children__name=...`) joins back via the same row-value comparison, from the
    other direction - exercises get_joins_for_related_field's BackwardForeignKeyRelation branch."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=7, b=8, name="backward-target")
        other_target = await CompositeTarget.objects.create(a=10, b=11, name="other-backward-target")
        await FkTargetNoConstraint.objects.create(other=target, name="find-me")
        await FkTargetNoConstraint.objects.create(other=other_target, name="not-me")

        results = await CompositeTarget.objects.filter(no_constraint_children__name="find-me")
        assert [r.pk for r in results] == [(7, 8)]


@pytest.mark.asyncio
async def test_prefetch_related_direct_relation_composite_target():
    """.prefetch_related() over a composite-target forward FK - exercises
    PrefetchExecutorMixin._prefetch_direct_relation's composite OR-of-ANDs path, including an
    unset (None) relation staying None rather than matching some accidental row."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=40, b=41, name="prefetch-target")
        other_target = await CompositeTarget.objects.create(a=42, b=43, name="prefetch-other-target")
        child = await FkTargetNoConstraint.objects.create(other=target, name="prefetch-child")
        other_child = await FkTargetNoConstraint.objects.create(other=other_target, name="prefetch-other-child")
        unset_child = await FkTargetNoConstraint.objects.create(other=None, name="prefetch-unset-child")

        qs = FkTargetNoConstraint.objects.filter(id__in=[child.id, other_child.id, unset_child.id])
        results = {c.id: c for c in await qs.prefetch_related("other")}

        assert results[child.id].other.pk == (40, 41)
        assert results[other_child.id].other.pk == (42, 43)
        assert results[unset_child.id].other is None


@pytest.mark.asyncio
async def test_prefetch_related_backward_relation_composite_target():
    """.prefetch_related() over the backward relation to a composite-target FK - exercises
    PrefetchExecutorMixin._prefetch_reverse_relation's composite OR-of-ANDs path."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=44, b=45, name="prefetch-backward-target")
        other_target = await CompositeTarget.objects.create(a=46, b=47, name="prefetch-backward-other-target")
        await FkTargetNoConstraint.objects.create(other=target, name="child-1")
        await FkTargetNoConstraint.objects.create(other=target, name="child-2")
        await FkTargetNoConstraint.objects.create(other=other_target, name="other-child")

        results = {
            r.pk: r
            for r in await CompositeTarget.objects.filter(pk__in=[(44, 45), (46, 47)]).prefetch_related(
                "no_constraint_children"
            )
        }

        names_for_target = sorted(c.name for c in results[(44, 45)].no_constraint_children)
        names_for_other = sorted(c.name for c in results[(46, 47)].no_constraint_children)
        assert names_for_target == ["child-1", "child-2"]
        assert names_for_other == ["other-child"]


@pytest.mark.asyncio
async def test_prefetch_related_backward_o2o_relation_composite_target():
    """.prefetch_related() over the backward O2O relation to a composite-target FK - exercises
    PrefetchExecutorMixin._prefetch_reverse_o2o_relation's composite OR-of-ANDs path."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, O2OTargetNoConstraint

        target = await CompositeTarget.objects.create(a=48, b=49, name="prefetch-o2o-target")
        other_target = await CompositeTarget.objects.create(a=50, b=51, name="prefetch-o2o-other-target")
        await O2OTargetNoConstraint.objects.create(other=target, name="o2o-child")
        await O2OTargetNoConstraint.objects.create(other=other_target, name="o2o-other-child")

        results = {
            r.pk: r
            for r in await CompositeTarget.objects.filter(pk__in=[(48, 49), (50, 51)]).prefetch_related(
                "no_constraint_o2o_child"
            )
        }

        assert results[(48, 49)].no_constraint_o2o_child.name == "o2o-child"
        assert results[(50, 51)].no_constraint_o2o_child.name == "o2o-other-child"


@pytest.mark.asyncio
async def test_cascade_delete_through_composite_target_fk():
    """Deleting a composite-PK-target row cascades to a row pointing at it (the default
    on_delete=CASCADE) - exercises ReverseRelationCascade._related_query's plural
    relation_fields/to_field_instances handling. Uses the soft-delete fixture module: a hard
    delete's CASCADE relies on a real DB-level FK constraint the composite target doesn't have
    yet (a later, DDL-layer commit), but Meta.soft_delete_field's cascade is pure Python and
    exercises the exact same ReverseRelationCascade code path regardless."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_soft_delete"]):
        from tests.model_setup.models_fk_composite_pk_target_soft_delete import CompositeTarget, FkTargetCascade

        target = await CompositeTarget.objects.create(a=20, b=21, name="cascade-target")
        other_target = await CompositeTarget.objects.create(a=22, b=23, name="unrelated-target")
        child = await FkTargetCascade.objects.create(other=target)
        survivor = await FkTargetCascade.objects.create(other=other_target)

        await target.hard_delete()

        assert await FkTargetCascade.objects.filter(id=child.id).exists() is False
        assert await FkTargetCascade.objects.filter(id=survivor.id).exists() is True


@pytest.mark.asyncio
async def test_set_null_on_delete_clears_every_shadow_column():
    """on_delete=SET_NULL against a composite target clears every shadow column, not just the
    first, when the target row is deleted (via the soft-delete fixture module - see
    test_cascade_delete_through_composite_target_fk for why)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_soft_delete"]):
        from tests.model_setup.models_fk_composite_pk_target_soft_delete import CompositeTarget, FkTargetSetNull

        target = await CompositeTarget.objects.create(a=24, b=25, name="set-null-target")
        child = await FkTargetSetNull.objects.create(other=target)

        await target.delete()

        refreshed = await FkTargetSetNull.objects.get(id=child.id)
        assert refreshed.other_a is None
        assert refreshed.other_b is None


@pytest.mark.asyncio
async def test_set_default_on_delete_applies_tuple_default_to_every_shadow_column():
    """on_delete=SET_DEFAULT against a composite target applies the tuple default= to every
    shadow column at once, in to_field order (via the soft-delete fixture module - see
    test_cascade_delete_through_composite_target_fk for why)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_soft_delete"]):
        from tests.model_setup.models_fk_composite_pk_target_soft_delete import CompositeTarget, FkTargetSetDefault

        target = await CompositeTarget.objects.create(a=26, b=27, name="set-default-target")
        child = await FkTargetSetDefault.objects.create(other=target)

        await target.delete()

        refreshed = await FkTargetSetDefault.objects.get(id=child.id)
        assert refreshed.other_a == 0
        assert refreshed.other_b == 0


@pytest.mark.asyncio
async def test_protect_on_delete_blocks_deleting_a_composite_target():
    """on_delete=PROTECT against a composite target blocks Model.delete() (single-row) and
    QuerySet.delete() (bulk) alike while a protecting row exists - the bulk path exercises
    check_protected_bulk's OR-of-ANDs expansion and the DeleteQuery.pks-as-tuples fix."""
    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.exceptions import ProtectedError

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetProtect

        target = await CompositeTarget.objects.create(a=28, b=29, name="protect-target")
        await CompositeTarget.objects.create(a=30, b=31, name="protect-other-target")
        await FkTargetProtect.objects.create(other=target)

        with pytest.raises(ProtectedError):
            await target.delete()

        # Bulk QuerySet.delete() over BOTH rows (one protected, one not) - the not-yet-protected
        # target must not be silently skipped just because its sibling is blocked.
        with pytest.raises(ProtectedError):
            await CompositeTarget.objects.filter(pk__in=[(28, 29), (30, 31)]).delete()

        assert await CompositeTarget.objects.filter(pk=(28, 29)).exists() is True


@pytest.mark.asyncio
async def test_delete_query_composite_pk_owner_with_join_regression():
    """Pre-existing, independent-of-composite-target-FK bug: DeleteQuery._make_query() hardcoded
    model._meta.db_pk_column (empty string for a composite-PK model) whenever the query needed a
    subquery (a JOIN forces one) - this exercises a composite-PK model (no composite-target FK at
    all) whose own delete filter traverses a relation, forcing that subquery path."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=32, b=33, name="join-delete-target")
        await CompositeTarget.objects.create(a=34, b=35, name="join-delete-survivor")
        await FkTargetNoConstraint.objects.create(other=target, name="joined-row")

        await CompositeTarget.objects.filter(no_constraint_children__name="joined-row").delete()

        assert await CompositeTarget.objects.filter(pk=(32, 33)).exists() is False
        assert await CompositeTarget.objects.filter(pk=(34, 35)).exists() is True


@pytest.mark.asyncio
async def test_o2o_to_composite_pk_model_generates_composite_unique_constraint():
    """OneToOneField shares init_foreign_key_or_one_to_one_field with ForeignKeyField but is checked separately here
    - a regression test locks this in independently so a future refactor that only touches one of
    the two field types can't silently drop support for the other. The N shadow columns can't
    each get their own per-column UNIQUE (that would enforce "each half is unique" rather than
    "this pair is unique") - a composite UniqueConstraint across all of them is registered
    instead, and the schema actually has to build (regression-gates the SQLite
    generate_schemas() ALTER TABLE ADD CONSTRAINT gap fixed alongside this)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_o2o_composite_pk_target"]):
        from hare.ddl.constraints import UniqueConstraint
        from tests.model_setup.models_o2o_composite_pk_target import O2OTarget

        fk_object = O2OTarget._meta.fields_map["other"]
        assert fk_object.to_field_names == ("a", "b")
        assert fk_object.source_fields == ("other_a", "other_b")
        assert not O2OTarget._meta.fields_map["other_a"].unique
        assert not O2OTarget._meta.fields_map["other_b"].unique
        assert UniqueConstraint(fields=("other_a", "other_b")) in O2OTarget._meta.constraints


@pytest.mark.asyncio
async def test_generate_schemas_composite_target_fk_ddl_enforces_real_constraint():
    """generate_schemas() for a composite-target FK (default db_constraint=True) renders a real
    table-level FOREIGN KEY constraint - a valid insert succeeds, and an orphan insert (values
    that don't match any real CompositeTarget row) is rejected by the database itself, not just
    an app-level check. Uses models_fk_composite_pk_target_default_constraint - the one fixture
    module that still uses the default db_constraint=True (see its own docstring)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target_default_constraint"]):
        from hare.exceptions import IntegrityError
        from tests.model_setup.models_fk_composite_pk_target_default_constraint import CompositeTarget, FkTarget

        target = await CompositeTarget.objects.create(a=100, b=101, name="ddl-target")
        child = await FkTarget.objects.create(other=target)
        assert child.other_a == 100
        assert child.other_b == 101

        orphan = FkTarget(other_a=999, other_b=888)
        with pytest.raises(IntegrityError):
            await orphan.save(force_create=True)


@pytest.mark.asyncio
async def test_generate_schemas_composite_target_o2o_ddl_enforces_real_constraint():
    """Same DB-level enforcement check as the FK test above, for a composite-target O2O field -
    both the FOREIGN KEY constraint and the composite UniqueConstraint (see the test above this
    one) come from the schema this same generate_schemas() call produces."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_o2o_composite_pk_target"]):
        from hare.exceptions import IntegrityError
        from tests.model_setup.models_o2o_composite_pk_target import CompositeTarget, O2OTarget

        target = await CompositeTarget.objects.create(a=102, b=103, name="ddl-o2o-target")
        child = await O2OTarget.objects.create(other=target)
        assert child.other_a == 102
        assert child.other_b == 103

        orphan = O2OTarget(other_a=997, other_b=996)
        with pytest.raises(IntegrityError):
            await orphan.save(force_create=True)


@pytest.mark.asyncio
async def test_generate_schemas_check_constraint_on_sqlite():
    """generate_schemas()'s SQLite path had a UniqueConstraint-only special case for "SQLite has
    no ALTER TABLE ADD CONSTRAINT" (see the two tests above) but no equivalent one for
    CheckConstraint - it fell through to the generic ADD_CONSTRAINT_TEMPLATE, which SQLite can't
    execute at all, so generate_schemas() itself used to fail outright for any model declaring
    one."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_check_constraint"]):
        from hare.exceptions import IntegrityError, OperationalError
        from tests.model_setup.models_check_constraint import WidgetWithCheckConstraint

        await WidgetWithCheckConstraint.objects.create(age=5)
        with pytest.raises((IntegrityError, OperationalError)):
            await WidgetWithCheckConstraint.objects.create(age=-1)


@pytest.mark.asyncio
async def test_m2m_owned_by_composite_pk_model_gets_one_through_column_per_pk_component():
    """A composite-PK model declaring its own ManyToManyField gets one through-table column per
    PK component on its (backward) side, prefixed by its own db_table since backward_key wasn't
    given explicitly - the forward (plain single-pk target) side is unaffected."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_owner"]):
        from tests.model_setup.models_m2m_composite_pk_owner import CompositePkM2MOwner

        field = CompositePkM2MOwner._meta.fields_map["others"]
        assert field.forward_keys == ("plaintarget_id",)
        assert field.backward_keys == ("compositepkm2mowner_a", "compositepkm2mowner_b")
        assert field.forward_key == "plaintarget_id"
        assert field.backward_key == "compositepkm2mowner_a"


@pytest.mark.asyncio
async def test_m2m_targeting_composite_pk_model_gets_one_through_column_per_pk_component():
    """A plain model's ManyToManyField targeting a composite-PK model gets one through-table
    column per PK component on its (forward) side - the backward (plain single-pk owner) side is
    unaffected."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_target"]):
        from tests.model_setup.models_m2m_composite_pk_target import CompositeTarget, M2MTargetingCompositePk

        field = M2MTargetingCompositePk._meta.fields_map["others"]
        assert field.forward_keys == ("compositetarget_a", "compositetarget_b")
        assert field.backward_keys == ("m2mtargetingcompositepk_id",)

        backward_field = CompositeTarget._meta.fields_map["m2mtargetingcompositepks"]
        assert backward_field.forward_keys == field.backward_keys
        assert backward_field.backward_keys == field.forward_keys


@pytest.mark.asyncio
async def test_m2m_both_sides_composite_pk_get_independent_column_counts():
    """Both sides of a ManyToManyField having a composite PK gets each side its own independent
    column count - 2 for the owner's PK, 3 for the target's, not tied to each other."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_both_sides"]):
        from tests.model_setup.models_m2m_composite_pk_both_sides import BothSidesCompositeOwner

        field = BothSidesCompositeOwner._meta.fields_map["others"]
        assert field.backward_keys == ("bothsidescompositeowner_a", "bothsidescompositeowner_b")
        assert field.forward_keys == (
            "bothsidescompositetarget_x",
            "bothsidescompositetarget_y",
            "bothsidescompositetarget_z",
        )


@pytest.mark.asyncio
async def test_m2m_self_referential_composite_pk_avoids_column_collision():
    """A self-referential ManyToManyField on a composite-PK model still gets the `_rel` suffix
    disambiguation on its auto-defaulted backward side, generalized to every PK component."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_self_referential"]):
        from tests.model_setup.models_m2m_composite_pk_self_referential import CompositePkPerson

        field = CompositePkPerson._meta.fields_map["friends"]
        assert field.forward_keys == ("compositepkperson_org_id", "compositepkperson_person_id")
        assert field.backward_keys == ("compositepkperson_rel_org_id", "compositepkperson_rel_person_id")
        assert field.forward_keys != field.backward_keys


@pytest.mark.asyncio
async def test_m2m_both_sides_composite_pk_add_remove_clear_and_orphan_reject():
    """End-to-end M2M between two composite-PK models, both sides composite at once: real
    through-table DDL (a table-level FOREIGN KEY constraint per side, plus the default
    unique=True's composite UNIQUE index across all 5 columns), `.add()`'s ON CONFLICT DO
    NOTHING fast path, `.remove()`, `.clear()`, and a real orphan-insert rejection."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_both_sides"]) as ctx:
        from tests.model_setup.models_m2m_composite_pk_both_sides import (
            BothSidesCompositeOwner,
            BothSidesCompositeTarget,
        )

        owner = await BothSidesCompositeOwner.objects.create(a=1, b=2, name="owner")
        t1 = await BothSidesCompositeTarget.objects.create(x=10, y=20, z=30, name="t1")
        t2 = await BothSidesCompositeTarget.objects.create(x=11, y=21, z=31, name="t2")

        await owner.others.add(t1, t2)
        assert sorted(t.name for t in await owner.others.filter()) == ["t1", "t2"]

        await owner.others.add(t1)  # idempotent re-add through the unique=True fast path
        assert sorted(t.name for t in await owner.others.filter()) == ["t1", "t2"]

        await owner.others.remove(t1)
        assert [t.name for t in await owner.others.filter()] == ["t2"]

        await owner.others.clear()
        assert list(await owner.others.filter()) == []

        conn = ctx.get_connection()
        through_table = BothSidesCompositeOwner._meta.fields_map["others"].through
        with pytest.raises(IntegrityError):
            await conn.execute_script(
                f'INSERT INTO "{through_table}" ("bothsidescompositeowner_a","bothsidescompositeowner_b",'
                f'"bothsidescompositetarget_x","bothsidescompositetarget_y","bothsidescompositetarget_z") '
                f"VALUES (99,99,10,20,30)"
            )


@pytest.mark.asyncio
async def test_m2m_both_sides_composite_pk_prefetch_related():
    """`.prefetch_related()` over a composite-PK M2M field resolves correctly from both the
    forward (declared) side and the auto-generated backward side."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_both_sides"]):
        from tests.model_setup.models_m2m_composite_pk_both_sides import (
            BothSidesCompositeOwner,
            BothSidesCompositeTarget,
        )

        owner1 = await BothSidesCompositeOwner.objects.create(a=1, b=2, name="owner1")
        owner2 = await BothSidesCompositeOwner.objects.create(a=3, b=4, name="owner2")
        t1 = await BothSidesCompositeTarget.objects.create(x=10, y=20, z=30, name="t1")
        t2 = await BothSidesCompositeTarget.objects.create(x=11, y=21, z=31, name="t2")
        await owner1.others.add(t1, t2)
        await owner2.others.add(t2)

        owners = await BothSidesCompositeOwner.objects.filter().prefetch_related("others")
        by_name = {o.name: sorted(t.name for t in o.others) for o in owners}
        assert by_name == {"owner1": ["t1", "t2"], "owner2": ["t2"]}

        targets = await BothSidesCompositeTarget.objects.filter().prefetch_related("bothsidescompositeowners")
        by_target_name = {t.name: sorted(o.name for o in t.bothsidescompositeowners) for t in targets}
        assert by_target_name == {"t1": ["owner1"], "t2": ["owner1", "owner2"]}


@pytest.mark.asyncio
async def test_m2m_composite_pk_target_bare_equality_and_pk_filter():
    """`.filter(many_to_many_field=instance)` and `.filter(m2m_field__pk=(...))` both resolve
    against a composite-PK M2M target through the JOIN - `__in`/`__not`/`__not_in` also work
    (get_m2m_filters() builds them from the target's tuple of PK fields; see
    tests/test_m2m_composite_pk_filters.py for dedicated coverage of those)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_m2m_composite_pk_target"]):
        from tests.model_setup.models_m2m_composite_pk_target import CompositeTarget, M2MTargetingCompositePk

        target = await CompositeTarget.objects.create(a=1, b=2, name="target")
        other_target = await CompositeTarget.objects.create(a=9, b=9, name="other")
        matching = await M2MTargetingCompositePk.objects.create(name="matching")
        await matching.others.add(target)
        not_matching = await M2MTargetingCompositePk.objects.create(name="not-matching")
        await not_matching.others.add(other_target)

        results = await M2MTargetingCompositePk.objects.filter(others=target)
        assert [r.name for r in results] == ["matching"]

        results = await M2MTargetingCompositePk.objects.filter(others__pk=(1, 2))
        assert [r.name for r in results] == ["matching"]

        results = await M2MTargetingCompositePk.objects.filter(others__in=[target])
        assert [r.name for r in results] == ["matching"]

        results = await M2MTargetingCompositePk.objects.filter(others__not_in=[target])
        assert [r.name for r in results] == ["not-matching"]


# ============================================================================
# pk=/pk__in= for composite PK - "pk" was never registered as a filter alias for a composite-PK
# model (member fields aren't individually marked .pk=True), so this used to raise a plain
# FieldError instead of resolving. Matches Django 5.2's own CompositePrimaryKey, where pk= does
# accept a tuple.
# ============================================================================


@pytest.mark.asyncio
async def test_get_by_pk_tuple(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    fetched = await CompositePkThing.objects.get(pk=(1, 2))
    assert fetched.name == "B"


@pytest.mark.asyncio
async def test_filter_by_pk_tuple(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    result = await CompositePkThing.objects.filter(pk=(1, 1))
    assert [obj.name for obj in result] == ["A"]


@pytest.mark.asyncio
async def test_exclude_by_pk_tuple(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    result = await CompositePkThing.objects.filter().exclude(pk=(1, 1))
    assert [obj.name for obj in result] == ["B"]


@pytest.mark.asyncio
async def test_filter_by_pk_in_tuples(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    result = await CompositePkThing.objects.filter(pk__in=[(1, 1), (2, 1)])
    assert sorted(obj.name for obj in result) == ["A", "C"]


@pytest.mark.asyncio
async def test_filter_by_pk_in_empty_list_matches_nothing(db):
    """Mirrors the existing single-column field__in=[] convention (SQL has no literal False, the
    library falls back to an always-false 1=0) - an empty pk__in must behave the same way, not
    silently match every row (the naive Q.with_connector(OR, *[]) approach did exactly that - an empty
    Q is falsy, and gets skipped as a no-op by AND-combination instead of excluding everything)."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")

    result = await CompositePkThing.objects.filter(pk__in=[])
    assert result == []


@pytest.mark.asyncio
async def test_filter_by_relation_pk_tuple():
    """`relation__pk=(...)` is a one-hop shorthand for a relation targeting a composite
    primary key - equivalent to writing out `Q(relation__a=x, relation__b=y)` by hand."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=1, b=2, name="target-row")
        other_target = await CompositeTarget.objects.create(a=9, b=9, name="other-row")
        matching = await FkTargetNoConstraint.objects.create(other=target, name="matching")
        await FkTargetNoConstraint.objects.create(other=other_target, name="not-matching")

        results = await FkTargetNoConstraint.objects.filter(other__pk=(1, 2))
        assert [r.id for r in results] == [matching.id]


@pytest.mark.asyncio
async def test_filter_by_relation_pk_in_tuples():
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target_a = await CompositeTarget.objects.create(a=1, b=2, name="a")
        target_b = await CompositeTarget.objects.create(a=3, b=4, name="b")
        target_c = await CompositeTarget.objects.create(a=9, b=9, name="c")
        child_a = await FkTargetNoConstraint.objects.create(other=target_a, name="child-a")
        child_b = await FkTargetNoConstraint.objects.create(other=target_b, name="child-b")
        await FkTargetNoConstraint.objects.create(other=target_c, name="child-c")

        results = await FkTargetNoConstraint.objects.filter(other__pk__in=[(1, 2), (3, 4)])
        assert sorted(r.id for r in results) == sorted([child_a.id, child_b.id])


@pytest.mark.asyncio
async def test_pk_tuple_wrong_length_raises(db):
    with pytest.raises(QueryError):
        await CompositePkThing.objects.get(pk=(1,))

    with pytest.raises(QueryError):
        await CompositePkThing.objects.get(pk=(1, 2, 3))


@pytest.mark.asyncio
async def test_pk_in_item_wrong_length_raises(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")

    with pytest.raises(QueryError):
        await CompositePkThing.objects.filter(pk__in=[(1, 1), (2,)])


@pytest.mark.asyncio
async def test_pk_tuple_three_column_composite(db):
    """Not hardcoded to exactly 2 columns."""
    await CompositePkTriple.objects.create(a=1, b=2, c=3, name="X")

    fetched = await CompositePkTriple.objects.get(pk=(1, 2, 3))
    assert fetched.name == "X"


@pytest.mark.asyncio
async def test_regular_single_pk_model_pk_kwarg_unaffected(db):
    """Regression: pk=/pk__in= for an ordinary single-PK model must behave exactly as before -
    this whole feature is scoped behind isinstance(primary_key_attribute, tuple)."""
    from tests.testmodels import IntFields

    obj = await IntFields.objects.create(intnum=1)
    other = await IntFields.objects.create(intnum=2)

    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 1
    assert {o.intnum for o in await IntFields.objects.filter(pk__in=[obj.pk, other.pk])} == {1, 2}


# ============================================================================
# A composite-PK model owning a forward FK to a normal model - unlike an FK/M2M *targeting* a
# composite-PK model (rejected above), Django allows this, and hare-orm's own get_backward_fk_filters
# used to crash at Hare.init() time for every model with an incoming FK from a composite-PK owner,
# since it unconditionally read `field.related_model._meta.pk.to_db_value` - always None for a
# composite-PK owner (only `.pk_fields` is set).
# ============================================================================


@pytest.mark.asyncio
async def test_composite_pk_model_can_own_a_forward_fk(db):
    from tests.testmodels import CompositePkOwningFK, Tournament

    tournament = await Tournament.objects.create(name="T1")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="X", tournament=tournament)
    await CompositePkOwningFK.objects.create(a=1, b=2, name="Y", tournament=tournament)

    owned = await CompositePkOwningFK.objects.filter(tournament=tournament).order_by("b")
    assert [o.name for o in owned] == ["X", "Y"]


@pytest.mark.asyncio
async def test_backward_relation_to_composite_pk_owner_accessor(db):
    """The auto-generated backward ReverseRelation accessor works normally - it was never the
    crashing part (the crash was in filter *registration*, not the relation itself)."""
    from tests.testmodels import CompositePkOwningFK, Tournament

    tournament = await Tournament.objects.create(name="T1")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="X", tournament=tournament)

    owners = await tournament.composite_owners.all()
    assert [o.name for o in owners] == ["X"]


@pytest.mark.asyncio
async def test_filter_by_backward_relation_to_composite_pk_owner_tuple(db):
    """.filter(<backward_relation>=<tuple>) - the bare equality alias get_backward_fk_filters()
    can't register for a composite-PK owner (no single Field for the scalar-shaped filter dict),
    expanded instead by PendingFilterCalls.build_filter_q(QuerySet) into a nested Q over the owner's pk columns."""
    from tests.testmodels import CompositePkOwningFK, Tournament

    t1 = await Tournament.objects.create(name="T1")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="X", tournament=t1)

    match = await Tournament.objects.filter(composite_owners=(1, 1)).first()
    assert match.pk == t1.pk

    no_match = await Tournament.objects.filter(composite_owners=(99, 99)).first()
    assert no_match is None


@pytest.mark.asyncio
async def test_filter_by_backward_relation_to_composite_pk_owner_wrong_length_raises(db):
    from tests.testmodels import Tournament

    with pytest.raises(QueryError):
        await Tournament.objects.filter(composite_owners=(1,)).first()


@pytest.mark.asyncio
async def test_filter_by_backward_relation_to_composite_pk_owner_isnull(db):
    """__isnull checks that the LEFT JOIN found any matching row - only needs one of the owner's
    pk columns, not all of them (a LEFT JOIN with no match nulls out every joined column)."""
    from tests.testmodels import CompositePkOwningFK, Tournament

    t1 = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="X", tournament=t1)

    assert [t.name for t in await Tournament.objects.filter(composite_owners__isnull=False).order_by("id")] == ["T1"]
    assert [t.name for t in await Tournament.objects.filter(composite_owners__isnull=True).order_by("id")] == ["T2"]


@pytest.mark.asyncio
async def test_filter_by_backward_relation_to_composite_pk_owner_nested_field_path(db):
    """The generic nested-path traversal (owners__name=...) was never affected by the bug - it
    never goes through get_backward_fk_filters()'s scalar-shaped dict at all."""
    from tests.testmodels import CompositePkOwningFK, Tournament

    tournament = await Tournament.objects.create(name="T1")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="X", tournament=tournament)

    match = await Tournament.objects.filter(composite_owners__name="X").first()
    assert match.pk == tournament.pk


# ============================================================================
# Composite PK x bulk_create/select_for_update/values/values_list/annotate+aggregate/distinct/
# distinct_on/refresh_from_db
# ============================================================================


@pytest.mark.asyncio
async def test_composite_pk_bulk_create(db):
    await CompositePkThing.objects.bulk_create(
        [
            CompositePkThing(thing_id=1, revision=1, name="A"),
            CompositePkThing(thing_id=1, revision=2, name="B"),
        ]
    )

    assert await CompositePkThing.objects.filter(thing_id=1).count() == 2
    assert (await CompositePkThing.objects.get(thing_id=1, revision=2)).name == "B"


@hare_test.requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_composite_pk_select_for_update_sql_includes_both_pk_columns(db):
    """select_for_update()'s WHERE clause (built from .get()/.filter(pk=...)) must reference both
    pk columns, not just one - only meaningfully asserts anything against a real Postgres
    connection (requires_features skips it on SQLite, which has no FOR UPDATE at all; same
    fixture/decorator shape as test_select_for_update in test_queryset.py). Previously ran with
    neither `db` nor `@pytest.mark.asyncio`, relying on ambient global state some earlier test
    happened to leave behind instead of its own connection - confirmed to fail
    (RuntimeError: No HareContext is currently active) when run without that ambient setup,
    e.g. under a different pytest-xdist worker distribution."""
    qs = CompositePkThing.objects.filter(pk=(1, 1)).select_for_update()
    sql = qs.sql()

    assert "FOR UPDATE" in sql
    assert '"thing_id"' in sql
    assert '"revision"' in sql


@pytest.mark.asyncio
async def test_composite_pk_values(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    rows = (
        await CompositePkThing.objects.filter(thing_id=1).order_by("revision").values("thing_id", "revision", "name")
    )
    assert rows == [
        {"thing_id": 1, "revision": 1, "name": "A"},
        {"thing_id": 1, "revision": 2, "name": "B"},
    ]


@pytest.mark.asyncio
async def test_composite_pk_values_list(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    rows = (
        await CompositePkThing.objects.filter(thing_id=1)
        .order_by("revision")
        .values_list("thing_id", "revision", "name")
    )
    assert rows == [(1, 1, "A"), (1, 2, "B")]


@pytest.mark.asyncio
async def test_composite_pk_selected_in_values_is_a_tuple(db):
    """A composite "pk" selected by .values()/.values_list() is returned as one tuple, like Django."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")

    assert await CompositePkThing.objects.all().order_by("revision").values("pk", "name") == [
        {"pk": (1, 1), "name": "A"},
        {"pk": (1, 2), "name": "B"},
    ]
    assert await CompositePkThing.objects.all().order_by("revision").values_list("pk", flat=True) == [(1, 1), (1, 2)]


@pytest.mark.asyncio
async def test_composite_pk_annotate_aggregate_auto_group_by(db):
    """The automatic GROUP BY added for an aggregate annotation groups by every db field
    (self.model._meta.db_fields), not just self.model._meta.primary_key_attribute - so it was never actually
    single-column-shaped for a composite PK to begin with. This test locks that in rather than
    fixing a real bug."""
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="A")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="B")

    rows = (
        await CompositePkThing.objects.annotate(name_count=Count("name"))
        .group_by("name")
        .order_by("name")
        .values("name", "name_count")
    )
    assert rows == [{"name": "A", "name_count": 2}, {"name": "B", "name_count": 1}]


@pytest.mark.asyncio
async def test_composite_pk_distinct(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="dup")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="dup")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="unique")

    # DISTINCT applies to the selected columns, not the full underlying row - selecting just
    # "name" collapses the two "dup" rows into one, same as it would for a single-PK model.
    names = await CompositePkThing.objects.all().distinct().values_list("name", flat=True)
    assert sorted(names) == ["dup", "unique"]


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_composite_pk_distinct_on(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="first")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="second")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="third")

    rows = await CompositePkThing.objects.all().distinct("thing_id").order_by("thing_id", "revision")
    assert sorted(row.name for row in rows) == ["first", "third"]


@pytest.mark.asyncio
async def test_composite_pk_refresh_from_db(db):
    """refresh_from_db() re-fetches via .get(pk=self.pk) - relies on pk= tuple support for a
    composite PK, added earlier in this same fix pass."""
    thing = await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.filter(thing_id=1, revision=1).update(name="B")

    assert thing.name == "A"
    await thing.refresh_from_db()
    assert thing.name == "B"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_composite_pk_in_transaction(db):
    from hare.transactions.transactions import Transactions

    async with Transactions.atomic():
        await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")

    assert await CompositePkThing.objects.filter(thing_id=1, revision=1).exists()


# ============================================================================
# __hash__ - self.pk is always a non-empty (thus truthy) tuple for a composite PK, even before
# every component is assigned, so a plain truthiness check on self.pk never catches "not really
# set yet" the way it does for a single-column pk's plain None.
# ============================================================================


@pytest.mark.asyncio
async def test_composite_pk_unset_instance_is_unhashable(db):
    """Two different, never-saved composite-PK instances used to silently hash identically
    (hash((None, None)) both times) and compare equal via __eq__, colliding into one element in a
    set - confirmed empirically before fixing. Now raises the same TypeError a single-pk unsaved
    instance already raised."""
    unset = CompositePkThing(name="C")

    with pytest.raises(TypeError):
        hash(unset)


@pytest.mark.asyncio
async def test_composite_pk_saved_instance_is_hashable(db):
    obj = await CompositePkThing.objects.create(thing_id=1, revision=1, name="X")
    assert isinstance(hash(obj), int)


def test_composite_pk_unset_instance_repr_does_not_print_a_fake_pk():
    """__repr__ used the same naive `if self.pk:` truthiness check __hash__ was already fixed
    to avoid right next to it - self.pk is a non-empty tuple ((None, None)) for an unsaved
    composite-PK instance, always truthy regardless of its contents, so __repr__ printed
    "<CompositePkThing: (None, None)>" as if that were a real, assigned pk, even though hash()
    on the exact same instance correctly raises TypeError for being unset."""
    unset = CompositePkThing(name="C")
    assert repr(unset) == "<CompositePkThing>"


@pytest.mark.asyncio
async def test_composite_pk_saved_instance_repr_shows_the_real_pk(db):
    obj = await CompositePkThing.objects.create(thing_id=1, revision=1, name="X")
    assert repr(obj) == f"<CompositePkThing: {obj.pk}>"


@pytest.mark.asyncio
async def test_composite_pk_partially_set_instance_is_unhashable(db):
    """Only one of the two pk components assigned - still not a real row identity yet."""
    partial = CompositePkThing(thing_id=1, name="C")

    with pytest.raises(TypeError):
        hash(partial)


@pytest.mark.asyncio
async def test_bulk_update_composite_target_fk_updates_every_shadow_column():
    """bulk_update() of a composite-target FK field used to remap it to only its FIRST shadow
    column (BulkUpdateQuery._make_queries used field_obj.source_field, singular), silently
    leaving every other shadow column at its old value - a mismatched shadow-column pair that no
    longer corresponds to any real target row. Every shadow column must move together."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        old_target = await CompositeTarget.objects.create(a=1, b=2, name="old")
        new_target = await CompositeTarget.objects.create(a=3, b=4, name="new")
        child = await FkTargetNoConstraint.objects.create(other=old_target, name="child")

        child.other = new_target
        await FkTargetNoConstraint.objects.bulk_update([child], fields=["other"])

        refetched = await FkTargetNoConstraint.objects.get(id=child.id)
        assert (refetched.other_a, refetched.other_b) == (3, 4)


@pytest.mark.asyncio
async def test_filter_composite_pk_parent_by_backward_fk_bare_equality():
    """A bare `.filter(<backward relation>=<child pk value>)` FROM the composite-PK side (the
    parent being filtered, not the child being pointed at) used to build
    `table[""] == child_table[<first shadow column only>]` - Q._process_filter_kwarg read
    `model._meta.db_pk_column` (empty for a composite PK) for this side, and
    get_backward_fk_filters() only ever populated the singular `backward_key` (the child's
    first shadow column), never the plural `relation_fields` needed for every shadow column.
    `<backward relation>=<value>` takes the target's raw pk value, not an instance - matching
    the existing composite-child convention (test_filter_by_backward_relation_to_composite_pk_
    owner_tuple)."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=20, b=21, name="parent-with-child")
        other_target = await CompositeTarget.objects.create(a=22, b=23, name="parent-without-this-child")
        child = await FkTargetNoConstraint.objects.create(other=target, name="the-child")

        results = await CompositeTarget.objects.filter(no_constraint_children=child.id)
        assert [r.pk for r in results] == [target.pk]

        no_results = await CompositeTarget.objects.filter(no_constraint_children=child.id, pk=other_target.pk)
        assert no_results == []


@pytest.mark.asyncio
async def test_filter_composite_pk_parent_by_backward_fk_isnull():
    """`.filter(<backward relation>__isnull=...)` FROM the composite-PK side - same root cause
    as the bare-equality case above, exercised via the always-registered __isnull/__not_isnull
    filters."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        with_child = await CompositeTarget.objects.create(a=24, b=25, name="has-child")
        without_child = await CompositeTarget.objects.create(a=26, b=27, name="no-child")
        await FkTargetNoConstraint.objects.create(other=with_child, name="only-child")

        has_children = await CompositeTarget.objects.filter(no_constraint_children__isnull=False)
        assert [r.pk for r in has_children] == [with_child.pk]

        no_children = await CompositeTarget.objects.filter(no_constraint_children__isnull=True)
        assert [r.pk for r in no_children] == [without_child.pk]


@pytest.mark.asyncio
async def test_filter_composite_pk_parent_by_backward_o2o_isnull():
    """Same as the backward-FK case above, but through a BackwardOneToOneRelation - confirms the
    fix (which only special-cases BackwardForeignKeyRelation) also covers O2O via subclassing, since
    BackwardOneToOneRelation(BackwardForeignKeyRelation) shares the same isinstance check."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, O2OTargetNoConstraint

        with_child = await CompositeTarget.objects.create(a=30, b=31, name="has-o2o-child")
        without_child = await CompositeTarget.objects.create(a=32, b=33, name="no-o2o-child")
        await O2OTargetNoConstraint.objects.create(other=with_child, name="only-o2o-child")

        has_child = await CompositeTarget.objects.filter(no_constraint_o2o_child__isnull=False)
        assert [r.pk for r in has_child] == [with_child.pk]

        no_child = await CompositeTarget.objects.filter(no_constraint_o2o_child__isnull=True)
        assert [r.pk for r in no_child] == [without_child.pk]


@pytest.mark.asyncio
async def test_f_expression_terminal_relation_to_composite_pk_target_raises():
    """F("<relation>") (and any other resolve_nested_field caller - order_by, annotate) used to
    build `related_table[""]` when the relation's *last* segment targets a composite-PK model -
    a composite PK has no single column, so there's no well-defined scalar term to return here
    (unlike a JOIN condition, every caller of resolve_nested_field - F() arithmetic, annotate/
    aggregate, cursor pagination - consumes the result as ONE scalar Term). Raises a clear
    ConfigurationError instead of building broken SQL."""
    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.query.expressions import F

    async with hare_test_context(["tests.model_setup.models_fk_composite_pk_target"]):
        from tests.model_setup.models_fk_composite_pk_target import CompositeTarget, FkTargetNoConstraint

        target = await CompositeTarget.objects.create(a=70, b=71, name="terminal-relation-target")
        child = await FkTargetNoConstraint.objects.create(other=target, name="terminal-relation-child")

        with pytest.raises(QueryError):
            await (
                FkTargetNoConstraint.objects.filter(id=child.id)
                .annotate(other_val=F("other"))
                .values("id", "other_val")
            )


@pytest.mark.asyncio
async def test_pk_compares_with_the_outer_row_key(db):
    """pk=OuterReference("pk") of a composite key names the outer row's key fields one by one - in a
    filter, a negated Q and exclude()."""
    from hare.query.expressions import Exists, OuterReference, Q

    for thing_id, revision in ((1, 1), (1, 2), (2, 1)):
        await CompositePkThing.objects.create(thing_id=thing_id, revision=revision, name=f"{thing_id}.{revision}")

    def names(queryset):
        return queryset.order_by("thing_id", "revision").values_list("name", flat=True)

    later = CompositePkThing.objects.filter(pk=OuterReference("pk"), revision__gt=1)
    assert await names(CompositePkThing.objects.filter(Exists(later))) == ["1.2"]
    other_revisions = CompositePkThing.objects.filter(~Q(pk=OuterReference("pk")), thing_id=OuterReference("thing_id"))
    assert await names(CompositePkThing.objects.filter(Exists(other_revisions))) == ["1.1", "1.2"]
    excluded = CompositePkThing.objects.exclude(pk=OuterReference("pk")).filter(thing_id=OuterReference("thing_id"))
    assert await names(CompositePkThing.objects.filter(Exists(excluded))) == ["1.1", "1.2"]

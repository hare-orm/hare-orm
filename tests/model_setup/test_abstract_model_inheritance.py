import pytest

from hare.contrib.test.helpers import hare_test_context
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.models.deletion.deletion_graph import DeletionGraph


@pytest.mark.asyncio
async def test_mutual_cascade_cycle_across_two_siblings_of_abstract_base():
    """CrossLinkNodeA and CrossLinkNodeB (tests/model_setup/model_abstract_mutual_cascade_cycle.py)
    share an abstract ancestor and CASCADE onto each other (A -> B -> A) via their own
    directly-declared FK fields. Checks that has_self_cascading_constrained_relations() detects
    this cross-model cycle (not just the single-model self-referential case)."""
    # _generate_schemas=False: a genuinely cyclic pair of non-deferred FK constraints can't be
    # created as real DDL at all (SqliteSchemaGenerator._get_create_schema_sql's topological sort
    # raises "Can't create schema due to cyclic fk references" - a separate, general limitation
    # unrelated to abstract-base inheritance specifically). The question here is only whether the
    # Python-side cascade-cycle DETECTION itself (has_self_cascading_constrained_relations)
    # correctly sees the cross-model cycle - that's a pure function of the registered relation
    # graph, computable without any real table existing.
    async with hare_test_context(
        modules=["tests.model_setup.model_abstract_mutual_cascade_cycle"],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label="mutual_cascade_cycle",
        _generate_schemas=False,
    ):
        from tests.model_setup.model_abstract_mutual_cascade_cycle import CrossLinkNodeA, CrossLinkNodeB

        assert DeletionGraph.has_self_cascading_constrained_relations(CrossLinkNodeA) is True
        assert DeletionGraph.has_self_cascading_constrained_relations(CrossLinkNodeB) is True


@pytest.mark.asyncio
async def test_tenant_field_from_abstract_base_scopes_both_siblings_independently():
    """TenantSiblingX/TenantSiblingY (tests/model_setup/model_abstract_tenant_field.py) both
    inherit Meta.tenant_field = "company_id" from the same abstract base. Tenancy.scope() is a
    single process-wide contextvar shared by design across every tenant-scoped model - checks
    this still filters BOTH siblings correctly and simultaneously, and that per-subclass
    MetaInfo.tenant_field doesn't get corrupted/aliased in a way that makes one sibling see the
    other's rows."""
    async with hare_test_context(
        modules=["tests.model_setup.model_abstract_tenant_field"],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label="tenant_field",
    ):
        from hare.models.tenancy import Tenancy
        from tests.model_setup.model_abstract_tenant_field import TenantSiblingX, TenantSiblingY

        assert TenantSiblingX._meta.tenant_field == "company_id"
        assert TenantSiblingY._meta.tenant_field == "company_id"
        # Independent MetaInfo instances, not the same object aliased across siblings.
        assert TenantSiblingX._meta is not TenantSiblingY._meta

        await TenantSiblingX.objects.all_tenants().bulk_create(
            [TenantSiblingX(company_id=1, name="x1a"), TenantSiblingX(company_id=2, name="x2a")]
        )
        await TenantSiblingY.objects.all_tenants().bulk_create(
            [TenantSiblingY(company_id=1, name="y1a"), TenantSiblingY(company_id=2, name="y2a")]
        )

        with Tenancy.scope(1):
            assert [row.name for row in await TenantSiblingX.objects.all()] == ["x1a"]
            assert [row.name for row in await TenantSiblingY.objects.all()] == ["y1a"]
            with Tenancy.scope(2):
                assert [row.name for row in await TenantSiblingX.objects.all()] == ["x2a"]
                assert [row.name for row in await TenantSiblingY.objects.all()] == ["y2a"]
            # Outer scope restored for BOTH siblings after the inner block exits.
            assert [row.name for row in await TenantSiblingX.objects.all()] == ["x1a"]
            assert [row.name for row in await TenantSiblingY.objects.all()] == ["y1a"]


@pytest.mark.asyncio
async def test_generated_field_from_abstract_base_resolves_independently_per_sibling():
    """GeneratedSiblingA/GeneratedSiblingB (tests/model_setup/model_abstract_generated_field.py)
    both inherit a GeneratedField from the same abstract base. Checks the generated column
    actually computes correctly (and independently) on both siblings' own real tables via SQLite
    schema generation."""
    async with hare_test_context(
        modules=["tests.model_setup.model_abstract_generated_field"],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label="abstract_generated_field",
    ):
        from tests.model_setup.model_abstract_generated_field import GeneratedSiblingA, GeneratedSiblingB

        a = await GeneratedSiblingA.objects.create(quantity=3, unit_price=10)
        b = await GeneratedSiblingB.objects.create(quantity=7, unit_price=2)
        await a.refresh_from_db()
        await b.refresh_from_db()
        assert a.total == 30
        assert b.total == 14


@pytest.mark.asyncio
async def test_ordering_unique_together_check_constraint_from_abstract_base():
    """MetaOptionsSiblingA/MetaOptionsSiblingB (tests/model_setup/model_abstract_meta_options.py)
    both inherit Meta.ordering/unique_together/constraints (a CheckConstraint) from the same
    abstract base, all aliased (not deep-copied) by ModelMeta.__new__ - round M's own fix only
    deep-copies Meta.indexes specifically. Checks each still works correctly and independently on
    both siblings' own tables."""
    async with hare_test_context(
        modules=["tests.model_setup.model_abstract_meta_options"],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label="abstract_meta_options",
    ):
        from tests.model_setup.model_abstract_meta_options import MetaOptionsSiblingA, MetaOptionsSiblingB

        await MetaOptionsSiblingA.objects.bulk_create(
            [
                MetaOptionsSiblingA(name="n1", category="c", quantity=5),
                MetaOptionsSiblingA(name="n2", category="c", quantity=9),
            ]
        )
        await MetaOptionsSiblingB.objects.bulk_create(
            [
                MetaOptionsSiblingB(name="n1", category="d", quantity=1),
                MetaOptionsSiblingB(name="n2", category="d", quantity=4),
            ]
        )
        # Default ordering ("-quantity", "name") applied independently to each sibling's own rows.
        assert [row.name for row in await MetaOptionsSiblingA.objects.all()] == ["n2", "n1"]
        assert [row.name for row in await MetaOptionsSiblingB.objects.all()] == ["n2", "n1"]

        # unique_together on (category, name) enforced independently per sibling's own table.
        from hare.exceptions import IntegrityError

        with pytest.raises(IntegrityError):
            await MetaOptionsSiblingA.objects.create(name="n1", category="c", quantity=99)
        # The SAME (category, name) pair ("c", "n1") that's a duplicate on A is fine on B - B has
        # no row with category="c" at all, separate tables/constraints, not shared/aliased.
        await MetaOptionsSiblingB.objects.create(name="n1", category="c", quantity=99)

        # CheckConstraint (quantity >= 0), same literal constraint name on both siblings' tables.
        with pytest.raises(IntegrityError):
            await MetaOptionsSiblingA.objects.create(name="n3", category="d", quantity=-1)
        with pytest.raises(IntegrityError):
            await MetaOptionsSiblingB.objects.create(name="n3", category="d", quantity=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("companion_module", "model_name"),
    [
        ("tests.model_setup.model_abstract_multiple_inheritance_meta_collision_a_first", "QuantityThenPriceProduct"),
        ("tests.model_setup.model_abstract_multiple_inheritance_meta_collision_b_first", "PriceThenQuantityProduct"),
    ],
    ids=["A-then-B order", "B-then-A order"],
)
async def test_multiple_inheritance_from_two_unrelated_abstract_bases_merges_both_meta_collections(
    companion_module, model_name
):
    """QuantityThenPriceProduct/PriceThenQuantityProduct (one per companion module, both built
    from the shared abstract mixins in model_abstract_multiple_inheritance_meta_collision.py)
    each inherit from TWO otherwise-unrelated abstract bases (AbstractQuantityMixin,
    AbstractPriceMixin), each declaring its own Meta.constraints/indexes, in the
    two possible base orders. ModelMeta.__new__ used to merge these by plain last-one-wins
    overwrite - only ONE base's declarations survived, the other silently vanished with no
    error, regardless of which base was listed first. Checks both bases' declarations survive,
    and that the real DDL for both actually applies (both unique constraints and both
    CheckConstraints enforced)."""
    async with hare_test_context(
        modules=[companion_module],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label=f"abstract_multi_inherit_meta_collision_{model_name}",
    ):
        import importlib

        model = getattr(importlib.import_module(companion_module), model_name)

        unique_constraints = [c for c in model._meta.constraints if isinstance(c, UniqueConstraint)]
        assert {tuple(c.fields) for c in unique_constraints} == {("qty",), ("price",)}
        assert {c.name for c in model._meta.constraints if isinstance(c, CheckConstraint)} == {
            "qty_nonneg",
            "price_nonneg",
        }
        assert {index.fields[0] for index in model._meta.indexes} == {"qty", "price"}

        from hare.exceptions import IntegrityError

        await model.objects.create(qty=1, price=1)
        with pytest.raises(IntegrityError):
            # unique_together on ("qty",) from AbstractQuantityMixin - still enforced.
            await model.objects.create(qty=1, price=2)
        with pytest.raises(IntegrityError):
            # CheckConstraint from AbstractPriceMixin - still enforced too, regardless of which
            # base this concrete model lists first.
            await model.objects.create(qty=2, price=-1)


@pytest.mark.asyncio
async def test_diamond_inheritance_non_overriding_base_does_not_shadow_overriding_sibling():
    """DiamondConcrete (tests/model_setup/model_abstract_diamond_mro.py) inherits from Mixin1
    (listed first, passively inherits `shared`/Meta.table from the shared abstract ancestor
    AbstractA without overriding either) and Mixin2 (listed second, genuinely overrides both).
    DiamondConcrete's real Python MRO is [DiamondConcrete, Mixin1, Mixin2, AbstractA, ...], so
    Mixin2's own override is the nearer declaration and must win - ModelMeta used to resolve both
    the field and Meta.table to AbstractA's original value instead, "protracted" through Mixin1
    simply because Mixin1 was walked first, regardless of which base actually declared the value
    closer to the diamond point. Checks both the resolved _meta AND the real DDL generate_schemas()
    produces on sqlite."""
    from hare.core.connections import Connections

    async with hare_test_context(
        modules=["tests.model_setup.model_abstract_diamond_mro"],
        db_url="sqlite://:memory:",
        app_label="models",
        connection_label="abstract_diamond_mro",
    ):
        from tests.model_setup.model_abstract_diamond_mro import DiamondConcrete

        assert DiamondConcrete._meta.db_table == "from_mixin2"
        assert DiamondConcrete._meta.fields_map["shared"].max_length == 99

        alias = next(iter(Connections.current().db_config))
        sql = Connections.get(alias).get_schema_sql(safe=True)

        assert '"from_mixin2"' in sql
        assert '"from_abstract_a"' not in sql
        table_start = sql.index('"from_mixin2"')
        table_end = sql.index(";", table_start)
        table_sql = sql[table_start:table_end]
        assert '"shared" VARCHAR(99)' in table_sql


@pytest.mark.asyncio
async def test_fk_model_name_as_abstract_class_object_is_rejected():
    """Apps._get_related_model() used to validate a class-object model_name differently from a
    string one - a string reference to an abstract model ("models.AbstractAttemptBase") is
    caught (abstract models are never registered in self.apps), but a class-object reference
    bypassed the app registry lookup entirely, going on to crash with a raw AttributeError deep
    inside relation setup instead of a clear ConfigurationError at the point of the mistake."""
    with pytest.raises(ConfigurationError, match="abstract"):
        async with hare_test_context(
            modules=["tests.model_setup.model_abstract_fk_class_object"],
            db_url="sqlite://:memory:",
            app_label="models",
            connection_label="abstract_fk_class_object",
        ):
            pass


@pytest.mark.asyncio
async def test_trigger_name_inherited_unchanged_from_abstract_base_raises():
    """TriggeredSiblingM/TriggeredSiblingN (tests/model_setup/model_abstract_trigger_collision.py)
    both inherit the SAME explicitly-named Trigger unchanged from one abstract base - the
    UniqueConstraint/ExclusionConstraint/Index collision guard in Apps._init_relations() never
    checked Meta.triggers at all, so this used to pass Hare.init() silently and only surface as a
    raw "trigger already exists" DB error once a real migration tried to create both. Trigger
    names occupy the same per-(connection, schema) namespace as indexes/constraints, not a
    per-table one - _generate_schemas=False since the guard fires at registration time, before
    any real DDL runs."""
    with pytest.raises(ConfigurationError, match='constraint/index named "trg_shared_touch"'):
        async with hare_test_context(
            modules=["tests.model_setup.model_abstract_trigger_collision"],
            db_url="sqlite://:memory:",
            app_label="models",
            connection_label="abstract_trigger_collision",
            _generate_schemas=False,
        ):
            pass

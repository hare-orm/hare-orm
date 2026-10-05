import pytest

from hare import Hare
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from tests.utils.context_apps_reset import ContextAppsReset

# Save the original classproperty before any test can shadow it
_original_apps_prop = Hare.__dict__["apps"]


async def _reset_hare():
    """Helper to reset Hare state before each test.

    Note: We MUST NOT set Hare.apps = None
    because it is a classproperty and setting it shadows the property
    with a class attribute, breaking future access.
    """
    # Restore the original classproperty if it was shadowed
    if not isinstance(Hare.__dict__.get("apps"), type(_original_apps_prop)):
        type.__setattr__(Hare, "apps", _original_apps_prop)

    # Get the current context and properly reset it
    ctx = HareContext.get_current()
    if ctx is not None:
        # Clear db_config first to prevent close_all from trying to import bad backends
        if ctx._connections is not None:
            # Clear storage without closing (to avoid importing bad backends)
            ctx._connections._storage.clear()
            ctx._connections._db_config = None
            ctx._connections = None
        ctx._apps = None
        ctx._inited = False
        ctx._default_connection = None
    else:
        # No context exists - create one for the test
        ctx = HareContext()
        ctx.__enter__()


async def _teardown_hare():
    """Helper to teardown Hare state after each test."""
    ContextAppsReset.reset_apps()


@pytest.mark.parametrize(
    ("models_module", "error_match"),
    [
        pytest.param("tests.model_setup.model_bad_rel1", "No app with name 'app' registered.", id="wrong_app_init"),
        pytest.param(
            "tests.model_setup.model_bad_rel2",
            "No model with name 'Tour' registered in app 'models'.",
            id="wrong_model_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel3",
            'ForeignKeyField accepts model name in format "app.Model"',
            id="no_app_in_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel4",
            'ForeignKeyField accepts model name in format "app.Model"',
            id="more_than_two_dots_in_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel5",
            'OneToOneField accepts model name in format "app.Model"',
            id="no_app_in_o2o_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel6",
            'field "uuid" in model "Tournament" is not unique',
            id="non_unique_field_in_fk_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel7",
            'there is no field named "uuids" in model "Tournament"',
            id="non_exist_field_in_fk_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel8",
            'field "uuid" in model "Tournament" is not unique',
            id="non_unique_field_in_o2o_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_bad_rel9",
            'there is no field named "uuids" in model "Tournament"',
            id="non_exist_field_in_o2o_reference_init",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_through_self_referential",
            "is self-referential through",
            id="self_referential_m2m_through_model_init",
        ),
        # A ManyToManyField(through=Model)'s own on_delete and the through model's own FK field's
        # explicitly-declared on_delete can't both be customized to different values - hare has no way
        # to know which one the user actually meant (Finding 2's reconciliation is only safe to apply
        # when at most one side was actually customized away from the shared CASCADE default).
        pytest.param(
            "tests.model_setup.model_m2m_through_on_delete_conflict",
            "set on_delete only once",
            id="m2m_through_on_delete_conflict_init",
        ),
        # Propagating a ManyToManyField(through=Model, on_delete=SET_NULL) onto the through model's
        # own FK field (left at its own CASCADE default) must still honor that field's own null=True
        # requirement for SET_NULL - the same requirement ForeignKeyField itself already enforces when
        # on_delete=SET_NULL is declared directly on it.
        pytest.param(
            "tests.model_setup.model_m2m_through_on_delete_set_null_not_nullable",
            "isn't null=True",
            id="m2m_through_on_delete_set_null_requires_nullable_fk_init",
        ),
        # Propagating a ManyToManyField(through=Model, on_delete=SET_DEFAULT) onto the through
        # model's own FK field must honor the same requirement ForeignKeyField enforces when
        # on_delete=SET_DEFAULT is declared directly on it: with a real FK constraint, db_default is
        # what the database's own ON DELETE SET DEFAULT resets the column to - default= alone isn't.
        pytest.param(
            "tests.model_setup.model_m2m_through_on_delete_set_default_only_default",
            "db_default set when db_constraint=True",
            id="m2m_through_on_delete_set_default_requires_db_default_init",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_through_ambiguous_fk",
            'must have exactly one ForeignKeyField pointing to "Person"',
            id="ambiguous_fk_m2m_through_model_init",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_through_missing_fk",
            'must have exactly one ForeignKeyField pointing to "Group"',
            id="missing_fk_m2m_through_model_init",
        ),
        pytest.param(
            "tests.model_setup.model_related_name_reserved_fk",
            'related_name "save" of "Book.author" would shadow',
            id="related_name_reserved_fk",
        ),
        pytest.param(
            "tests.model_setup.model_related_name_reserved_m2m",
            'related_name "display_name" of "Post.tags" would shadow',
            id="related_name_reserved_m2m",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_duplicate_auto_through",
            r'auto-generated through table "post_tag", already used by "Post\.(featured_)?tags"',
            id="m2m_duplicate_auto_through",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_auto_through_model_table",
            'auto-generated through table "post_tag", which is the table of model PostTag',
            id="m2m_auto_through_model_table",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_through_related_to_field",
            'through model "Seat" field "player" must reference "Player"',
            id="m2m_through_related_to_field",
        ),
        pytest.param(
            "tests.model_setup.model_m2m_through_owner_to_field",
            'through model "Seat" field "club" must reference "Club"',
            id="m2m_through_owner_to_field",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bad_relation_reference_fails_init(models_module, error_match):
    await _reset_hare()
    try:
        with pytest.raises(ConfigurationError, match=error_match):
            await Hare.init(
                {
                    "connections": {
                        "default": {
                            "engine": "sqlite+aiosqlite",
                            "credentials": {"file_path": ":memory:"},
                        }
                    },
                    "apps": {
                        "models": {
                            "models": [models_module],
                            "default_connection": "default",
                        }
                    },
                }
            )
    finally:
        await _teardown_hare()


@pytest.mark.asyncio
async def test_m2m_through_on_delete_set_default_with_db_default_init():
    await _reset_hare()
    try:
        await Hare.init(
            {
                "connections": {
                    "default": {
                        "engine": "sqlite+aiosqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {
                    "models": {
                        "models": ["tests.model_setup.model_m2m_through_on_delete_set_default_db_default"],
                        "default_connection": "default",
                    }
                },
            }
        )
        membership = Hare.apps.get_model("models", "DbDefaultMembership")
        assert membership._meta.fields_map["parent"].on_delete == "SET DEFAULT"
    finally:
        await _teardown_hare()

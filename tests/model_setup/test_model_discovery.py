"""A models module's app gets the models the module declares - not the ones it imports."""

import types

from hare.core.apps import Apps
from hare.fields import CharField, IntField
from hare.models import Model
from tests.model_setup.models_importing_other_models import ImportingWidget
from tests.testmodels import Author, Book


def test_imported_models_stay_in_their_own_app():
    """Discovery used to take every model in the module's namespace and set its _meta.app for the
    whole process - a module importing Author and Book moved them into its own app."""
    author_app, book_app = Author._meta.app, Book._meta.app

    discovered = Apps._discover_models("tests.model_setup.models_importing_other_models", "importing_app")

    assert discovered == [ImportingWidget]
    assert ImportingWidget._meta.app == "importing_app"
    assert (Author._meta.app, Book._meta.app) == (author_app, book_app)


def test_a_models_package_keeps_the_models_of_its_submodules():
    assert Apps._is_declared_in_module(Author, "tests")
    assert Apps._is_declared_in_module(Author, "tests.testmodels")
    assert not Apps._is_declared_in_module(Author, "tests.test")
    assert not Apps._is_declared_in_module(Author, "tests.model_setup")


def test_a_module_assembled_at_runtime_takes_every_model_given_to_it():
    class RuntimeGadget(Model):
        id = IntField(primary_key=True)
        label = CharField(max_length=20)

        class Meta:
            app = None

    module = types.ModuleType("tests.model_setup._runtime_gadget_models")
    module.RuntimeGadget = RuntimeGadget  # type: ignore[attr-defined]

    assert Apps._discover_models(module, "runtime_app") == [RuntimeGadget]
    assert RuntimeGadget._meta.app == "runtime_app"

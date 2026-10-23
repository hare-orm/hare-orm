"""A relation names its model by a label, the model class or a swappable reference: deleting and renaming the
model find it whichever way; the backward relation a relation adds is named by one rule - for the linking of
foreign keys and many-to-many relations and for makemigrations alike."""

import pytest

from hare import fields
from hare.core.apps.relation_linking import RelationLinking
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import DeleteModel, RenameModel
from hare.migrations.state.state import State
from hare.models import Model

pytestmark = pytest.mark.database_independent


class ReferencedAuthor(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "models"


class BookNamingItsAuthorByClass(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField(ReferencedAuthor, related_name="books")

    class Meta:
        app = "models"


def get_state() -> State:
    return State.from_models([ReferencedAuthor, BookNamingItsAuthorByClass])


def test_a_model_referenced_by_its_class_is_not_deleted_while_referenced():
    with pytest.raises(IncompatibleStateError, match="still referenced from models.BookNamingItsAuthorByClass"):
        DeleteModel("ReferencedAuthor").state_forward("models", get_state())


def test_renaming_a_model_renames_a_reference_to_its_class():
    state = get_state()
    RenameModel("ReferencedAuthor", "Writer").state_forward("models", state)

    author_field = state.models[("models", "BookNamingItsAuthorByClass")].fields["author"]
    assert author_field.model_name == "models.Writer"


@pytest.mark.parametrize(
    ("related_name", "expected"),
    [
        pytest.param(None, "books", id="none_declared"),
        pytest.param("", "books", id="empty"),
        pytest.param("written", "written", id="declared"),
        pytest.param("%(app_label)s_%(class)s_set", "library_book_set", id="template"),
        pytest.param(False, None, id="none_added"),
    ],
)
def test_the_backward_relation_name_rule(related_name, expected):
    assert RelationLinking.get_backward_relation_name(related_name, "book", "library", "Book") == expected


def test_the_linking_names_a_backward_relation_by_the_rule():
    rendered_author = get_state().apps.get_model("models.ReferencedAuthor")
    assert "books" in rendered_author._meta.fields_map

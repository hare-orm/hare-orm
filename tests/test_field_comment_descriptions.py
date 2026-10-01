"""A field's description comes from the "#:" comment lines right above it - only whole "#:" lines,
never a "#:" that merely appears inside an ordinary "#" comment's text."""

from hare import fields
from hare.models import Model


class CommentedWidget(Model):
    #: The widget's display name.
    #: Shown in listings.
    name = fields.TextField()
    # An ordinary note mentioning "#:" in passing - not a description.
    code = fields.TextField()
    # Internal note.
    size = fields.IntField()  #: trailing comment after code isn't one either
    color = fields.TextField()

    class Meta:
        abstract = True


def keep_class(model_class: type[Model]) -> type[Model]:
    return model_class


@keep_class
class DecoratedWidget(Model):
    #: Read from a decorated class too.
    label = fields.TextField()

    class Meta:
        abstract = True


def test_decorated_class_reads_its_comments():
    assert DecoratedWidget._meta.fields_map["label"].description == "Read from a decorated class too."


def test_class_defined_in_a_function_reads_only_its_own_comments():
    def make_widget() -> type[Model]:
        class LocalWidget(Model):
            #: The local widget's title.
            title = fields.TextField()
            name = fields.TextField()

            class Meta:
                abstract = True

        return LocalWidget

    local_widget = make_widget()
    assert local_widget._meta.fields_map["title"].description == "The local widget's title."
    # CommentedWidget's "name" comment is in the same file - not this class's.
    assert local_widget._meta.fields_map["name"].description is None


def test_description_is_the_first_line_of_the_hash_colon_block():
    name_field = CommentedWidget._meta.fields_map["name"]
    assert name_field.description == "The widget's display name."
    assert name_field.docstring == "The widget's display name.\nShown in listings."


def test_hash_colon_inside_an_ordinary_comment_is_not_a_description():
    """Bug: FIELD_COMMENT_RE let a "#:" block start mid-line, so the tail of an ordinary comment
    ('"#:" in passing - not a description.') became the next field's DB column comment."""
    assert CommentedWidget._meta.fields_map["code"].description is None
    assert CommentedWidget._meta.fields_map["color"].description is None

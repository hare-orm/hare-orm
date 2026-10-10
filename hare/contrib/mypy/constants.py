"""The names the mypy plugin looks up in mypy's types, and the texts of the errors it reports."""

from __future__ import annotations

from mypy.errorcodes import ErrorCode

from hare.contrib.mypy.enums import AnnotationTypeRule

#: The code of every error the plugin reports - ``# type: ignore[hare-query]`` silences one.
HARE_QUERY_ERROR = ErrorCode("hare-query", "Check the names and values of hare queries", "Hare")

QUERYSET_FULLNAME = "hare.query.queryset.queryset.QuerySet"
QUERY_SPECIFICATION_FULLNAME = "hare.query.queryset.query_specification.QuerySpecification"
MODEL_FULLNAME = "hare.models.model.Model"
FIELD_FULLNAME = "hare.fields.field.Field"
TYPED_DICT_FALLBACK_FULLNAME = "typing._TypedDict"
ITERABLE_FULLNAME = "typing.Iterable"
LIST_FULLNAME = "builtins.list"
TUPLE_FULLNAME = "builtins.tuple"
STR_FULLNAME = "builtins.str"
INT_FULLNAME = "builtins.int"
BOOL_FULLNAME = "builtins.bool"
TRUE_FULLNAME = "builtins.True"
FALSE_FULLNAME = "builtins.False"

#: The queryset methods whose keyword arguments are filter keys.
FILTER_METHOD_NAMES = frozenset({"filter", "exclude", "get", "get_or_create", "update_or_create"})
#: The queryset methods whose keyword arguments are field values.
WRITE_METHOD_NAMES = frozenset({"create", "update"})
ANNOTATION_METHOD_NAMES = frozenset({"annotate", "alias"})
VALUES_METHOD_NAMES = frozenset({"values", "values_list"})
#: The queryset methods taking names - of an ordering, a field to load or skip, a relation to join or
#: a field to write.
NAME_METHOD_NAMES = frozenset({"order_by", "only", "defer", "select_related", "bulk_update"})

#: Where the plugin keeps the annotations of a queryset whose class doesn't pass the annotations type
#: parameter on - a key no attribute can be named.
HIDDEN_ANNOTATIONS_ATTRIBUTE = "hare annotations"

#: The type of an annotation by the class of its expression.
ANNOTATION_TYPE_RULES = {
    "hare.query.functions.aggregates.count.Count": AnnotationTypeRule.INT,
    "hare.query.functions.text.length.Length": AnnotationTypeRule.INT,
    "hare.query.expressions.subqueries.exists.Exists": AnnotationTypeRule.BOOL,
    "hare.query.functions.aggregates.declarations.Sum": AnnotationTypeRule.OPTIONAL_PATH_VALUE,
    "hare.query.functions.aggregates.declarations.Min": AnnotationTypeRule.OPTIONAL_PATH_VALUE,
    "hare.query.functions.aggregates.declarations.Max": AnnotationTypeRule.OPTIONAL_PATH_VALUE,
    "hare.query.expressions.f.F": AnnotationTypeRule.PATH_VALUE,
    "hare.query.expressions.value.Value": AnnotationTypeRule.ARGUMENT_TYPE,
}

#: The ``[tool.hare]`` setting naming the modules imported before the models are bound.
MYPY_IMPORTS_SETTING = "mypy_imports"
#: What separates the parts of a filter key or a field path.
LOOKUP_SEPARATOR = "__"
PYPROJECT_FILE_NAME = "pyproject.toml"

MODELS_NOT_LOADED_MESSAGE = "hare: the models can't be loaded for type checking - {error}"
CONFIG_NOT_SET_MESSAGE = (
    "[tool.hare] hare_orm in pyproject.toml (or the HARE_ORM environment variable) must name the "
    "configuration as 'module.VARIABLE'"
)
MYPY_IMPORTS_TYPE_MESSAGE = "[tool.hare] mypy_imports in {file} must be a list of module names, got {value!r}"
UNSUPPORTED_LOOKUP_MESSAGE = "Filter param {key!r}: {model}.{path} has no lookup {lookup!r} on the {dialect} database"
UNKNOWN_NAME_MESSAGE = "{method}(): {model} has no field {name!r}"
MULTI_VALUED_RELATION_MESSAGE = (
    "select_related() can't follow {name!r} on {model} - it can hold many related rows (a reverse ForeignKey or a "
    "ManyToManyField); use prefetch_related() instead"
)
NOT_RELATION_MESSAGE = "select_related() field {name!r} on {model} is not a relation"
RELATION_NOT_FOUND_MESSAGE = "select_related() relation {name!r} for {model} not found"
RELATION_FIELD_NAME_MESSAGE = "{method}(): {model}.{name} is a relation holding many rows - it can't be {action}"
NOT_DIRECT_FIELD_MESSAGE = "defer(): {model}.{name} is a relation - only direct fields can be deferred"
UNKNOWN_WRITE_NAME_MESSAGE = "{model} has no field {name!r} to set"
MANY_ROWS_RELATION_WRITE_MESSAGE = "{model}.{name} is a relation holding many rows - it can't be set"

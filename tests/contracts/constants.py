import re

from tests.matrix.constants import REPOSITORY_DIRECTORY

#: The Meta options no sample of the Meta option matrix covers - each with why.
META_OPTIONS_NOT_SAMPLED = {
    "abstract": "never inherited, so nothing to compare - every sample asserts the concrete model isn't abstract",
    "table_is_explicit": "computed from table by the class building - a test checks it follows an inherited table",
    "primary_key_attribute": "computed from the key fields, not declared in Meta - it is migration state",
    "pk_without_overlaps": "declared by CompositePrimaryKey(without_overlaps=True), not in Meta - migration state",
}
#: What the messages of the exceptions the exception matrix raises hold - a database's, naming a table.
EXCEPTION_SECRET = "SELECT * FROM secret_table WHERE secret_column"
#: The arguments of exceptions besides their message, by name.
EXCEPTION_ARGUMENT_VALUES = {
    "protected_objects": [],
    "errors": [{"loc": ["name"], "msg": "refused", "type": "value_error"}],
    "participant_aliases": [],
    "pending_participant_aliases": [],
    "pk": 1,
    "expected_version": 1,
}
#: The exceptions the integrations answer with their own message - a value a field refuses, a parameter a request
#: may not ask for - their base classes.
EXCEPTION_BASES_ANSWERED_WITH_MESSAGE = (
    "hare.exceptions.ValidationError",
    "hare.contrib.request_query.exceptions.RequestQueryForbidden",
)
#: What a framework answers an error with that an integration leaves to it.
SERVER_ERROR_STATUS = 500
#: The status the integrations answer an exception with, by the path of its class - its subclasses too; one under
#: none of them is left to the framework.
EXCEPTION_STATUS_CODES = {
    "hare.exceptions.DoesNotExist": 404,
    "hare.exceptions.IntegrityError": 409,
    "hare.exceptions.TransactionRetryError": 409,
    "hare.exceptions.ValidationError": 422,
    "hare.contrib.request_query.exceptions.InvalidRequestQuery": 422,
    "hare.contrib.request_query.exceptions.RequestQueryForbidden": 403,
    "hare.contrib.request_query.exceptions.ApiGetterError": 502,
}
#: The documentation of hare, in English and Russian.
DOCUMENTATION_DIRECTORY = REPOSITORY_DIRECTORY / "docs"
#: A python code block of a Markdown document.
DOCUMENTATION_PYTHON_BLOCK_PATTERN = re.compile(r"^```python[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)

#: The packages whose ``__all__`` lists the field classes a model declares.
FIELD_PACKAGES = (
    "hare.fields",
    "hare.dialects.postgresql.fields",
    "hare.dialects.clickhouse.fields",
    "hare.gis",
    "hare.vectors",
)
#: The field classes of hare no model declares - each with why: bases of other fields, the fields of
#: expressions' values, the other side of a relation. Every other field class is exported from FIELD_PACKAGES
#: and has a sample.
FIELD_CLASSES_NOT_DECLARED = {
    "hare.fields.field.Field": "the base of every field",
    "hare.fields.data.containers.container_field.ContainerField": "the base of the container fields",
    "hare.fields.relations.fields.relational_field.RelationalField": "the base of the relation fields",
    "hare.fields.encrypted.encrypted_field_base.EncryptedFieldBase": "the base of the encrypted fields",
    "hare.fields.relations.fields.backward_foreign_key_relation.BackwardForeignKeyRelation": (
        "the other side of a foreign key, made by the relation"
    ),
    "hare.fields.relations.fields.declarations.BackwardOneToOneRelation": (
        "the other side of a one-to-one relation, made by the relation"
    ),
    "hare.fields.data.json.json_path_field.JSONPathField": "the value of a JSON path expression",
    "hare.dialects.postgresql.fields.network.network_field.NetworkField": "the base of the network fields",
    "hare.dialects.postgresql.fields.ranges.range_field.RangeField": "the base of the range fields",
    "hare.dialects.postgresql.fields.multiranges.multi_range_field.MultiRangeField": (
        "the base of the multirange fields"
    ),
    "hare.dialects.postgresql.functions.aggregates.jsonb_agg_field.JSONBAggField": "the value of JSONBAgg",
    "hare.query.expressions.numeric.unrounded_decimal_field.UnroundedDecimalField": (
        "the value of a decimal expression, not rounded to a column's places"
    ),
    "hare.gis.fields.made_line_field.MadeLineField": "the value of MakeLine",
    "hare.gis.fields.extent_field.ExtentField": "the value of Extent",
}
#: The dialects a field sample is checked on - each where the field has a column.
CONTRACT_DIALECTS = ("sqlite", "postgresql", "clickhouse")
#: The app of the contract tests' models.
CONTRACT_APP = "contracts"
#: The connection of the contract tests' database.
CONTRACT_CONNECTION = "models"
#: The field of a sample's model the sample is of.
SAMPLED_FIELD_NAME = "value"
#: The module of the pydantic model the JSON field sample declares - imported only where pydantic is.
PYDANTIC_DOCUMENT_MODULE = "tests.contracts.pydantic_document"
#: The request query class of each dialect that has one - its module and name; a dialect with none reads the base.
DIALECT_REQUEST_QUERIES = {
    "sqlite": ("hare.dialects.sqlite.request_query", "SqliteRequestQuery"),
    "postgresql": ("hare.dialects.postgresql.request_query", "PostgresqlRequestQuery"),
    "clickhouse": ("hare.dialects.clickhouse.request_query", "ClickhouseRequestQuery"),
}

#: The samples whose field has no column in its model's table - a many-to-many keeps its links in another table.
FIELD_SAMPLES_WITHOUT_ROW_COLUMN = ("many_to_many",)
#: The samples with an expression the database rewrites (a generated column's, a check's) - their
#: statements are compared without quotes, parentheses and spaces.
FIELD_SAMPLES_WITH_DATABASE_EXPRESSION = ("generated", "generic_foreign_key")
#: The types ClickHouse stores as one String - a table read back holds the text type for each.
CLICKHOUSE_STRING_PATTERN = re.compile(r"VARCHAR\(\d+\)|\bTEXT\b|\bString\b")
#: What the change capture of the write matrix adds to every event: a key of its payload and its headers.
WRITE_EXTENSION_PAYLOAD = {"source": "matrix"}
WRITE_EXTENSION_HEADERS = {"author": "matrix"}

#: The names of the methods that might write rows - what a method of a model, a queryset or a relation is called to
#: write.
WRITE_METHOD_PATTERN = re.compile(
    r"^(create|save|update|delete|hard_delete|restore|bulk_|get_or_create|insert|merge|add|remove|set|clear|upsert|"
    r"truncate|increment|touch|append|push|replace|write)"
)
#: The methods named like a write that write no row - each with why.
NOT_WRITE_METHODS = {
    "Model.update_from_dict": "sets the fields of the instance in memory",
    "Model.delete_preview": "reports what delete() would do",
}

#: The DB_URL schemes every environment has.
ALWAYS_AVAILABLE_URL_SCHEMES = ("sqlite+aiosqlite",)
#: The DB_URL scheme of each driver needing a package, with the package.
DRIVER_PACKAGES_BY_SCHEME = {
    "postgresql+asyncpg": "asyncpg",
    "clickhouse+clickhouse-connect": "clickhouse_connect",
    "clickhouse+clickhouse-driver": "clickhouse_driver",
}
#: The exceptions an integration answers somewhere else than its exception handler - Robyn answers
#: the parameters a request query refused where the request is known.
EXCEPTIONS_NOT_HANDLED_BY_INTEGRATION = {
    "robyn": ("hare.contrib.request_query.exceptions.InvalidRequestQuery",),
}
#: The statuses an integration answers with where it differs from the others, by the path of the exception - Litestar
#: answers invalid parameters with 400.
INTEGRATION_STATUS_CODES = {
    "litestar": {"hare.contrib.request_query.exceptions.InvalidRequestQuery": 400},
}
#: The applications of the application matrix, as text - each with the function writing a note through it.
APPLICATION_SOURCES_DIRECTORY = REPOSITORY_DIRECTORY / "tests" / "contracts" / "applications" / "sources"
#: The tests of an application matrix cell, in their order - its write, the rollback after it, the stop.
APPLICATION_TEST_NAMES = (
    "test_1_a_write_through_the_application_is_seen_by_the_test",
    "test_2_the_write_is_gone_in_the_next_test",
    "test_3_stopping_the_application_leaves_the_tests_context_open",
)
#: A test's outcome as ``pytest -v`` prints it: the module, the test, the outcome.
PRINTED_TEST_OUTCOME_PATTERN = re.compile(r"^(\w+)\.py::(\w+) (PASSED|FAILED|ERROR|SKIPPED)", re.MULTILINE)

#: The identity-generated copy of a sampled field and the operations compared with its source.
GENERATED_VALUE_FIELD_NAME = "generated_value"
GENERATED_FIELD_OPERATIONS = (
    "json_object",
    "json",
    "values",
    "annotation",
    "ordering",
    "annotation_ordering",
    "filter",
    "distinct",
    "aggregate",
    "aggregate_empty",
    "greatest_least",
    "coalesce_fractional",
    "lag_fractional",
)

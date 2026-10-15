from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField
from hare.fields.generated_field import GeneratedField


class TSVectorEntry(Model):
    id = fields.IntField(primary_key=True)
    title = fields.TextField()
    body = fields.TextField(null=True)
    search_vector = TSVectorField(
        source_fields=("title", "body"),
        config="english",
        weights=("A", "B"),
    )

    class Meta:
        table = "tsvector_entry"


class TSVectorNumberEntry(Model):
    """A generated TSVectorField over a non-text source column, next to a JSON column for an
    ad-hoc SearchVector over non-text sources."""

    id = fields.IntField(primary_key=True)
    title = fields.TextField()
    number = fields.IntField()
    data = fields.JSONField(null=True)
    search_vector = TSVectorField(source_fields=("title", "number"), config="simple")

    class Meta:
        table = "tsvector_number_entry"


class TSVectorAuthor(Model):
    """The RELATED side of a relation-crossing SearchRank("author__bio_vector", ...) test - its
    own stored TSVectorField, reached through TSVectorBook's forward FK."""

    id = fields.IntField(primary_key=True)
    bio = fields.TextField()
    bio_vector = TSVectorField(source_fields=("bio",), config="english")

    class Meta:
        table = "tsvector_author"


class TSVectorBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.TextField()
    author: fields.ForeignKeyRelation[TSVectorAuthor] = fields.ForeignKeyField(
        "models.TSVectorAuthor", related_name="books"
    )

    class Meta:
        table = "tsvector_book"


class GeneratedTSVectorEntry(Model):
    """A tsvector column built via GeneratedField(output_field=TSVectorField(...)) rather than
    TSVectorField's own source_fields=/weights= - a stored column whose expression is written
    out by hand, e.g. because it needs per-field weights TSVectorField's own generated-DDL
    shape doesn't support. SearchRank/`__search` must recognize this as an already-stored
    tsvector too, not double-wrap it in TO_TSVECTOR(...)."""

    id = fields.IntField(primary_key=True)
    title = fields.TextField()
    body = fields.TextField()
    search_vector = GeneratedField(
        expression=RawSQLTerm(
            "setweight(to_tsvector('english', coalesce(title,'')),'A') || "
            "setweight(to_tsvector('english', coalesce(body,'')),'B')"
        ),
        output_field=TSVectorField(),
    )

    class Meta:
        table = "generated_tsvector_entry"

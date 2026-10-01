from hare import Model, fields
from hare.ddl.indexes import Index as PlainIndex
from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.postgresql.indexes import (
    BloomIndex,
    BrinIndex,
    GinIndex,
    GistIndex,
    HashIndex,
    PostgresqlIndex,
    SpGistIndex,
)
from hare.dialects.postgresql.search import SearchVector
from hare.query.expressions import Q


class Index(Model):
    bloom = fields.CharField(max_length=200)
    brin = fields.CharField(max_length=200)
    gin = TSVectorField()
    gist = TSVectorField()
    sp_gist = fields.CharField(max_length=200)
    hash = fields.CharField(max_length=200)
    partial = fields.CharField(max_length=200)
    title = fields.TextField()
    body = fields.TextField()
    path = fields.CharField(max_length=200)

    class Meta:
        indexes = [
            BloomIndex(fields=("bloom",)),
            BrinIndex(fields=("brin",)),
            GinIndex(fields=("gin",)),
            GistIndex(fields=("gist",)),
            SpGistIndex(fields=("sp_gist",)),
            HashIndex(fields=("hash",)),
            PostgresqlIndex(fields=("partial",), condition=Q(id=1)),
            GinIndex(SearchVector("title", "body", config="english")),
            PlainIndex(fields=("path",), opclasses=("varchar_pattern_ops",)),
        ]

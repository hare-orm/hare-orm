import pytest

from hare.ddl import RawSQLTerm
from hare.ddl.indexes import Index, PartialIndex
from hare.dialects.postgresql.indexes import GinIndex
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import Q
from hare.sql.terms import Field as HareSqlField


def test_opclasses_requires_fields():
    with pytest.raises(ConfigurationError):
        Index(HareSqlField("path"), opclasses=("varchar_pattern_ops",))


def test_opclasses_length_must_match_fields():
    with pytest.raises(ConfigurationError):
        Index(fields=("a", "b"), opclasses=("varchar_pattern_ops",))


def test_opclasses_stored():
    index = Index(fields=("path",), opclasses=("varchar_pattern_ops",))
    assert index.opclasses == ["varchar_pattern_ops"]


def test_opclasses_in_deconstruct():
    index = Index(fields=("path",), opclasses=("varchar_pattern_ops",))
    _, _, kwargs = index.deconstruct()
    assert kwargs["opclasses"] == ["varchar_pattern_ops"]


def test_no_opclasses_defaults_to_empty_list():
    index = Index(fields=("path",))
    assert index.opclasses == []
    _, _, kwargs = index.deconstruct()
    assert "opclasses" not in kwargs


def test_partial_index_accepts_opclasses():
    """PartialIndex.__init__ used to not declare/forward opclasses at all, making it
    unreachable on every Postgres-specific index type (Gin/Gist/Brin/Hash/SpGist all
    subclass PartialIndex) - the exact mechanism needed to pick an operator class like
    gin_trgm_ops/jsonb_path_ops."""
    index = PartialIndex(fields=("data",), opclasses=("jsonb_path_ops",))
    assert index.opclasses == ["jsonb_path_ops"]


def test_gin_index_accepts_opclasses():
    index = GinIndex(fields=("data",), opclasses=("jsonb_path_ops",))
    assert index.opclasses == ["jsonb_path_ops"]


@pytest.mark.parametrize("condition", [{"status": "active"}, "status = 'active'"])
def test_partial_index_condition_is_a_q_or_raw_sql_term(condition):
    """A condition is a Q over the model's fields or RawSQLTerm of raw SQL - plain text and a
    dict of column values are rejected."""
    with pytest.raises(ConfigurationError, match="RawSQLTerm"):
        PartialIndex(fields=("status",), condition=condition)


def test_partial_index_condition_accepts_raw_sql_non_equality_predicate():
    """A partial-index predicate is often not an AND of equalities - raw SQL is written into
    the WHERE clause as it is."""
    index = PartialIndex(fields=("status",), condition=RawSQLTerm("status != 'archived'"))
    assert index.condition == RawSQLTerm("status != 'archived'")
    assert index.extra == " WHERE (status != 'archived')"


def test_partial_index_condition_accepts_raw_sql_or_predicate():
    index = PartialIndex(fields=("price",), condition=RawSQLTerm("price > 100 OR is_featured"))
    assert index.extra == " WHERE (price > 100 OR is_featured)"


def test_partial_index_raw_sql_condition_in_deconstruct():
    index = PartialIndex(fields=("status",), condition=RawSQLTerm("status != 'archived'"))
    _, _, kwargs = index.deconstruct()
    assert kwargs["condition"] == RawSQLTerm("status != 'archived'")


def test_partial_index_q_condition_is_rendered_against_the_model():
    """A Q condition has no SQL of its own until the index is created for a model."""
    index = PartialIndex(fields=("status",), condition=Q(status="active"))
    assert index.condition == Q(status="active")
    assert index.extra == ""


def test_unique_stored():
    index = Index(fields=("email",), unique=True)
    assert index.unique is True


def test_unique_defaults_to_false():
    index = Index(fields=("email",))
    assert index.unique is False


def test_unique_in_deconstruct_only_when_true():
    _, _, kwargs = Index(fields=("email",), unique=True).deconstruct()
    assert kwargs["unique"] is True
    _, _, kwargs = Index(fields=("email",)).deconstruct()
    assert "unique" not in kwargs


def test_unique_in_repr():
    assert repr(Index(fields=("email",), unique=True)) == "Index(fields=['email'], unique=True)"
    assert "unique" not in repr(Index(fields=("email",)))


def test_unique_affects_equality_and_hash():
    assert Index(fields=("email",), unique=True) != Index(fields=("email",))
    assert hash(Index(fields=("email",), unique=True)) != hash(Index(fields=("email",)))


def test_unique_partial_index_accepted():
    """PartialIndex must forward unique to its base __init__ like it already forwards
    fields/name/opclasses."""
    index = PartialIndex(fields=("email",), unique=True, condition=Q(active=True))
    assert index.unique is True


def test_unique_rejected_on_non_btree_index_type():
    """Postgres only supports UNIQUE on a plain btree index - GIN/GiST/BRIN/Hash never accept
    it, so this is caught at construction time instead of surfacing as a confusing DB error."""
    with pytest.raises(UnSupportedError):
        GinIndex(fields=("data",), unique=True)

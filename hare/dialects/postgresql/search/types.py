from collections.abc import Sequence

from hare.query.expressions import Expression
from hare.sql.terms.base.term import Term

ScalarValue = str | int | float | bool
VectorInput = Expression | Term | str
QueryInput = Expression | Term | str
ConfigInput = Expression | Term | str
WeightInput = Expression | Term | str
RankWeightInput = Expression | Term | Sequence[float] | Sequence[int] | str
NormalizationInput = Expression | Term | int
HeadlineExpressionInput = Expression | Term | str
HeadlineOptionValue = str | int | bool

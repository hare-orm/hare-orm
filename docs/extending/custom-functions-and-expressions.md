# Custom functions and expressions

A project adds a SQL function hare doesn't have as a `Function` subclass, and anything else a query can
compute as an `Expression` subclass; both then work in `annotate()`, `filter()`, `order_by()`, aggregates
and `update()` like the built-in ones.

## <a id="writing-a-custom-function"></a>Writing a custom function

Every class of [Database functions](../querying/functions.md) is a subclass of `Function` (or, for an
aggregate, `Aggregate`), both from `hare.query.expressions` — write your own the same way when the
built-in set doesn't cover a database function you need. A function written as `NAME(arguments...)` only
needs its SQL name in `function_name`; the arguments are the field (a name, `F()` or another expression)
followed by the values given after it:

```python
from hare.query.expressions import F, Function

class JsonSet(Function):
    function_name = "JSON_SET"
```

A function with a SQL shape of its own (`EXTRACT(part FROM value)`, `x::type`) leaves `function_name`
unset and sets `database_function` to a `hare.sql.terms.Function` subclass building that shape. Where one
database writes the function differently, its dialect registers a renderer for the name (see [Functions
and lookups](writing-a-dialect/sql.md#functions-and-lookups)).

Works both as an assigned attribute value before `.save()` and directly in `.update()` — either way it's
a real SQL-side expression, not a Python-computed value:

```python
book.metadata = JsonSet(F("metadata"), "$.edition", 2)
await book.save()

await Book.objects.filter(pk=book.pk).update(metadata=JsonSet(F("metadata"), "$.edition", 3))
```

A `Function` subclass inherits how a function describes its [statement plan](../querying/query-plan-cache.md):
its class, its field and the type of each literal argument (the values themselves are bound per query).
An attribute of your own that is written into the SQL text rather than bound is declared in `plan_parts`
after the inherited parts — as a `PlanPartType.KEY`, so two calls with different values of it never share a
plan. An attribute left out of `plan_parts` is left out of the key: two calls differing only in it would
share one plan, and with it one SQL text.

```python
from typing import ClassVar

from hare.query.expressions import Function
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms import Function as SqlFunction, LiteralValue


class Prefix(Function):
    plan_parts: ClassVar[DeclaredPlanParts] = (*Function.plan_parts, ("length", PlanPartType.KEY))

    def __init__(self, field: str, length: int) -> None:
        super().__init__(field)
        self.length = int(length)

    def get_function_term(self, term, *default_values):
        return SqlFunction("SUBSTR", term, LiteralValue("1"), LiteralValue(str(self.length)))
```

`get_function_term(term, *default_values)` builds the SQL term of the call from the terms of the
arguments — override it, as here, when the function writes its SQL itself. A `Function` subclass setting
neither `function_name` nor `database_function`, nor overriding `get_function_term()`, names no SQL
function: a query with it raises `ConfigurationError` before any SQL.

For adaptation that needs the completed result, override
`_finalize_result(result, function_arg, expression_context)`. It runs after the output field has been
selected and coerced: `result` carries the final term, joins and output field, while `function_arg`
retains the first argument's original field. Call `super()._finalize_result(...)` before adapting its
returned result, particularly in an `Aggregate` subclass, to preserve keyed comparisons and NULL
metadata. Return the adapted `ExpressionResult`; keep per-build information on that result or in local
variables, rather than changing the reusable function declaration.

## <a id="writing-a-custom-expression"></a>Writing a custom expression

An `Expression` subclass that isn't a `Function` — its own `get_result()` building the SQL term — also
says how a query holding it keeps a [statement plan](../querying/query-plan-cache.md#plan-descriptions):
it declares how each of its attributes meets the plan in `plan_parts`, and its description is generated
from them; or it declares `plannable = False`. A class doing neither is rejected when it is defined
(`TypeError`), so a new expression never reuses a plan built for another one by accident.

Each part is an attribute (or a method, for the `*_METHOD` types) with its `PlanPartType`, in the order
of the key:

| Part type | The attribute is |
|---|---|
| `KEY` | written into the SQL text as it is — an operator, a field or annotation name, an option |
| `KEYS` | a sequence written into the SQL text — field names, orderings |
| `KEY_METHOD` | a method returning what is written into the SQL text |
| `LITERAL` | a literal bound as a parameter (as `Value` binds one) |
| `ARGUMENT` / `ARGUMENTS` | an argument (a sequence of them): an expression by its own description, `None` as `NULL`, a `hare.sql` term by its text and values, any other literal by its type, bound; a list or tuple literal keeps no plan |
| `ENCODED_ARGUMENT` | an argument whose literal the build encodes into one bound value — a point as its text, a vector: a list or tuple literal is bound whole |
| `ARGUMENT_METHOD` | a method giving an argument, described as an `ARGUMENT` |
| `VALUE_METHOD` | a method giving a value bound as a parameter |
| `PARAMETERS` | the `ValueWrapper` parameters the object made when it was made (`RawSQL`'s) |
| `FIELD` / `FIELDS` | what a function reads (a sequence of them) — an expression, a field or annotation name, a `hare.sql` term |
| `EXPRESSION` / `EXPRESSIONS` | an expression or condition (a sequence of them) by its own description, `None` as absent |
| `EXPRESSION_METHOD` | a method giving an expression or condition |
| `JOIN_CONDITION_METHOD` | a method giving a condition the build folds into JOINs — its values are bound into each JOIN it is folded into, into none when there is none |
| `FILTERS` | the keyword filters of a condition |
| `QUERY` / `QUERY_METHOD` | a query built into the statement (a method giving one) |
| `PLAN_KEY` | an object giving its own plan key (`get_plan_key()`) |
| `KEEPS_PLAN_METHOD` | a method telling whether the object keeps a plan at all — `False` keeps none |
| `NONE` | described through another part (a method reading it), or no part of the SQL text at all — a cache, bookkeeping |

Every attribute of the class is classified: a slot of a class with `__slots__` that no part names is
rejected when the class is defined, and a pytest run with `--verify-plans` checks the attributes of every
object it describes. A base class lists the bookkeeping attributes of all its objects in
`unplanned_attributes` (`Expression`'s record of its constructor's arguments).

The build resolves each argument the way its part describes it —
`ExpressionArguments.get_result(self, attribute, value, expression_context)` for an `ARGUMENT`
(`binds_whole=True` for an `ENCODED_ARGUMENT`, `encoder=` converting a literal into its bound form
first), `ExpressionArguments.get_field_result(...)` for a `FIELD`: an expression is resolved into its
result, a SQL term records its values, and a literal is bound as a parameter recorded under the attribute
holding it while the query records its plan. A plan binds a value into every reference recorded under it,
however many times the build resolved it.

```python
from typing import ClassVar

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure


class Clamp(Expression):
    structure = ExpressionStructure(positions=((0, ArgumentRole.FIELD),), keywords=(("field", ArgumentRole.FIELD),))

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.FIELD),
        ("low", PlanPartType.ARGUMENT),
        ("high", PlanPartType.ARGUMENT),
    )

    def __init__(self, field: str, low: int, high: int) -> None:
        self.field, self.low, self.high = field, low, high

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field = ExpressionArguments.get_field_result(self, "field", self.field, expression_context)
        low = ExpressionArguments.get_result(self, "low", self.low, expression_context)
        high = ExpressionArguments.get_result(self, "high", self.high, expression_context)
        ...  # LEAST(GREATEST(field.term, low.term), high.term)


class Unplanned(Expression):
    plannable = False  # a query holding it is built in full every time

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult: ...
```

A class whose description no part type covers writes `get_plan_description(context)` itself and returns a
`PlanDescription(structure, values, origins)`, `None` when an instance keeps no plan; while a query
records its plan (`PlanOrigins.records`) `origins` lists where each value comes from —
`PlanOrigins.get_value_origin(self, attribute)` — and it is `None` otherwise.

An expression whose SQL text takes no value at all (`PI()`, the current moment) subclasses
`hare.query.expressions.ConstantExpression`, which describes it by its class alone.

## <a id="bound-paths-ordering"></a>Bound paths and ordering

`BoundExpression` prepares arguments whose literal encoding depends on their siblings.
It retains a literal's declaration and parameter origin until the containing operation chooses
its conversion. A non-literal child retains its complete `ExpressionResult`, including its value
field and required joins. `Case` binds each condition once, chooses the common branch type, then
encodes its literals. Functions prepare their children before encoding a literal first argument;
for example, a literal beside a JSON column in `Coalesce` uses the column's JSON conversion.
These prepared arguments belong to one build and must not be retained on shared expression objects.

When referencing another annotation, return its required joins together with its term and value
field. The input field of a function is not necessarily its result type: `Length("title")` is an
integer even when `title` is a text column. Unknown custom SQL keeps an unknown output type.

`ExpressionContext` carries the source model and table, connection, visibility and
extra conditions for relation paths. `F` and functions bind model paths through
`BoundLookupPath`. Its result contains the SQL value, output field, physical input
columns, crossed relations and already scoped joins. Consumers attach those joins;
they do not apply a second visibility or extra-condition policy. An annotation's
bound result belongs to the statement build, including its output field and joins.

`BoundOrdering` applies the field's ordering key to both the column and cursor
boundary. The cursor first encodes the Python value with the field's read-column
codec; NULL remains NULL. The comparison and equality used for a subsequent
tie-breaker use the same key, including JSON and custom type representations.
NULL placement comes from an explicit ordering option or the effective connection
features. Parameter recording retains the boundary's explicit source before key
transformation, so repeated occurrences bind through one parameter slot.

## <a id="structural-references"></a>Structural references and rebuilding

Expressions declare which constructor inputs are field references. This is independent of
`plan_parts`, which describes cache keys and bound parameters. Equal strings can have different
meanings: in `Replace("title", "title", "heading")`, only the first argument names a field.

```python
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure

class Suffix(Function):
    structure = ExpressionStructure(
        positions=((0, ArgumentRole.FIELD),),
        keywords=(("field", ArgumentRole.FIELD),),
    )

    def __init__(self, field, suffix):
        super().__init__(field, suffix)
```

Positions are zero-based, excluding `self`. Declare both positional and keyword forms accepted
by the constructor. Unlisted arguments have the `VALUE` role: child expressions are visited,
but plain strings remain literals. `FIELD` marks a field path, `ORDERING` also preserves a leading
minus sign, and `OUTER_FIELD` identifies a reference into the enclosing query.
`FILTERS` marks a mapping of lookup keys to values; `FIELD_MAPPING` marks a mapping keyed by field
names. Lists and tuples retain the role of their constructor argument.

`get_expression_children()` returns immediate `ExpressionChild` entries.
`rebuild_expression(children)` reconstructs the declaration through its constructor so normalized
attributes agree with the changed inputs. Override both methods when constructor metadata cannot
describe the declaration, as for condition keyword arguments mixed with result options.
The traversal preserves unchanged objects and does not mutate the original declaration. It uses an
explicit stack and rejects ancestor cycles while allowing repeated references to shared children.

`Value` and raw SQL are opaque. `structure = ExpressionStructure(query_scope=True)` establishes a
separate query scope: renaming the enclosing model does not rename local fields inside the query.
Ordinary dependency inspection reports outer references separately. A constraint's structural
check rejects nested queries, aggregates, windows and related columns before rendering; scalar
functions are checked against the actual DDL dialect.

SQL terms expose direct children through `get_children()`; `nodes_()` uses the shared iterative
traversal. Custom terms must yield immediate children, rather than recursively yielding descendants.
Grouping and aggregate analysis apply explicit query/window boundaries to this tree.

"""hare's core names no dialect: outside the dialects' own packages (``hare/dialects/`` except
its shared ``base``), no module imports a dialect's package, compares with a dialect's name or
points at a dialect's module, writes SQL only one database has or picks anything by a dialect's name -
everything a dialect decides goes through the ``Dialect``/``Driver`` objects and their registries, so a
new dialect is added without touching hare's core. The syntax several databases write alike beyond ISO SQL
is a class of ``base`` named after what it holds, which the dialect of each such database extends."""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.database_independent

HARE_DIRECTORY = Path(__file__).resolve().parents[2] / "hare"
DIALECTS_DIRECTORY = HARE_DIRECTORY / "dialects"
SHARED_DIALECT_DIRECTORY = DIALECTS_DIRECTORY / "base"
BUILTIN_DIALECT_PACKAGES = ("hare.dialects.sqlite", "hare.dialects.postgresql")
BUILTIN_DIALECT_NAMES = frozenset({"sqlite", "postgres", "postgresql"})
BUILTIN_DIALECT_NAME_MEMBERS = frozenset({"SQLITE", "POSTGRESQL"})
#: Keywords, catalog names and functions only one of the built-in databases has.
DIALECT_ONLY_SQL = re.compile(
    r"\bCONCURRENTLY\b|\bNOT VALID\b|\bVALIDATE CONSTRAINT\b|\bEXCLUDE\b|\bCREATE EXTENSION\b|"
    r"\bCREATE COLLATION\b|\bSET SCHEMA\b|NULLS NOT DISTINCT|\bINCLUDE \(|\bDISTINCT ON\b|\bILIKE\b|"
    r"\bUSING (?:GIST|GIN|BRIN|SPGIST|HNSW|IVFFLAT)\b|\bpg_[a-z_]+|::[a-z]+\b|\bjsonb\b|\bjsonb_[a-z_]+|"
    r"\bunnest\(|\b(?:BIG|SMALL)?SERIAL\b|\bTABLESPACE\b|\bUNLOGGED\b|\bLISTEN\b|\bNOTIFY\b|\bxmax\b|"
    r"\bNO KEY UPDATE\b|\bto_tsvector\b|\btsquery\b|\bATTACH\b|\bPRAGMA\b|sqlite_(?:master|schema|sequence)|"
    r"\bjson_each\b|\bAUTOINCREMENT\b|WITHOUT ROWID|\bstrftime\b|\bjulianday\b|\bGLOB\b|"
    r"::\{|\$\w*\$|EXECUTE FUNCTION|CONSTRAINT TRIGGER|\bALTER INDEX\b|\bLANGUAGE\b|"
    r"\bDATE_TRUNC\b|\bJSONB?_BUILD_OBJECT\b|\bSTATEMENT_TIMESTAMP\b|\bANY\(|['\)]::|"
    r"(?:^|\s)LIMIT\s*$|\bLIMIT (?:\d|-1)|\bFROM STDIN\b|\bFORMAT Native\b|^BEGIN;?$|"
    r"\bbtree_gist\b|\bbloom\b|\bsearch_path\b|\bON CLUSTER\b|\bKeeper\b"
)
#: The modules of ``base`` holding syntax several databases write alike beyond ISO SQL - their SQL
#: isn't one database's.
SEVERAL_DATABASES_SYNTAX_MODULES = frozenset({"hare/dialects/base/clauses/limit_returning_conflict_query_clauses.py"})
#: The name of a function only one of the built-in databases has, given to a ``Function``.
DIALECT_ONLY_FUNCTION_NAMES = frozenset({"ANY", "POW", "NOW", "DATE_TRUNC", "JSONB_BUILD_OBJECT", "JSON_EACH"})
#: The modules a dialect is found by its name in - the dialect itself and the registry of dialects.
DIALECT_NAME_MODULES = frozenset({"hare/dialects/base/dialect.py", "hare/dialects/dialect_registry.py"})
#: The calls taking a key first.
KEYED_CALL_NAMES = frozenset({"get", "pop", "setdefault"})


def get_core_modules() -> Iterator[Path]:
    for path in sorted(HARE_DIRECTORY.rglob("*.py")):
        if DIALECTS_DIRECTORY not in path.parents or SHARED_DIALECT_DIRECTORY in path.parents:
            yield path


def get_docstring_nodes(tree: ast.AST) -> set[int]:
    docstring_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_node_ids.add(id(first.value))
    return docstring_node_ids


def names_builtin_dialect(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lower() in BUILTIN_DIALECT_NAMES
    return isinstance(node, ast.Attribute) and node.attr in BUILTIN_DIALECT_NAME_MEMBERS


def get_last_identifier(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return get_last_identifier(node.func)
    return None


def is_dialect_name(node: ast.AST) -> bool:
    """Whether an expression is a dialect's name: ``dialect.name``, ``connection.dialect.name``,
    ``entry.dialect_name``, a ``dialect_name`` variable."""
    if isinstance(node, ast.Attribute) and node.attr == "dialect_name":
        return True
    if isinstance(node, ast.Name) and (node.id == "dialect_name" or node.id.endswith("_dialect_name")):
        return True
    if isinstance(node, ast.Attribute) and node.attr == "name":
        owner = get_last_identifier(node.value)
        return owner is not None and "dialect" in owner.lower()
    return False


def holds_dialect_name(node: ast.AST) -> bool:
    """Whether an expression is a dialect's name or a tuple holding one - a message isn't looked into."""
    if isinstance(node, ast.Tuple):
        return any(holds_dialect_name(element) for element in node.elts)
    return is_dialect_name(node)


def picks_by_dialect_name(node: ast.AST) -> bool:
    """Whether an expression picks something by a dialect's name - compares with it, reads by it, or makes
    a key of it. A description labelling something with the name picks nothing."""
    if isinstance(node, ast.Compare):
        return any(holds_dialect_name(operand) for operand in (node.left, *node.comparators))
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return holds_dialect_name(node.slice)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in KEYED_CALL_NAMES:
        return bool(node.args) and holds_dialect_name(node.args[0])
    if isinstance(node, ast.Tuple):
        return any(is_dialect_name(element) for element in node.elts)
    return False


def get_violations(path: Path) -> Iterator[str]:
    return get_violations_of(path, HARE_DIRECTORY.parent)


def get_violations_of(path: Path, root: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_node_ids = get_docstring_nodes(tree)
    location = path.relative_to(root).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(BUILTIN_DIALECT_PACKAGES):
            yield f"{location}:{node.lineno} imports {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(BUILTIN_DIALECT_PACKAGES):
                    yield f"{location}:{node.lineno} imports {alias.name}"
        elif isinstance(node, ast.Compare):
            if any(names_builtin_dialect(operand) for operand in (node.left, *node.comparators)):
                yield f"{location}:{node.lineno} compares with a dialect's name"
        elif isinstance(node, ast.Attribute) and node.attr in BUILTIN_DIALECT_NAME_MEMBERS:
            yield f"{location}:{node.lineno} names a dialect"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("startswith", "endswith")
            and any(names_builtin_dialect(argument) for argument in node.args)
        ):
            yield f"{location}:{node.lineno} matches a dialect's name"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_node_ids
            and any(package in node.value for package in BUILTIN_DIALECT_PACKAGES)
        ):
            yield f"{location}:{node.lineno} names a dialect's module"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_node_ids
            and location not in SEVERAL_DATABASES_SYNTAX_MODULES
            and (DIALECT_ONLY_SQL.search(node.value) or node.value in DIALECT_ONLY_FUNCTION_NAMES)
        ):
            yield f"{location}:{node.lineno} writes a dialect's SQL"
        if location not in DIALECT_NAME_MODULES and picks_by_dialect_name(node):
            yield f"{location}:{node.lineno} picks by a dialect's name"


def test_core_names_no_dialect():
    violations = [violation for path in get_core_modules() for violation in get_violations(path)]
    assert violations == []


def test_each_type_of_violation_is_found(tmp_path):
    module = tmp_path / "hare" / "query" / "offending.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        '"""Mentions hare.dialects.sqlite in a docstring, which is fine."""\n'
        "from hare.dialects.sqlite.constants import SQLITE_DIALECT\n"
        "import hare.dialects.postgresql.fields\n"
        "def f(dialect, url):\n"
        "    if dialect.name == 'postgresql' or dialect.name == DialectName.SQLITE:\n"
        "        return url.startswith('sqlite')\n"
        "    if dialect.features.supports_concurrent_indexes:\n"
        "        return 'CREATE INDEX CONCURRENTLY name_idx ON book (name)'\n"
        "    if dialect.features.supports_extensions:\n"
        "        return 'SELECT 1 FROM pg_extension'\n"
        "    if dialect.features.supports_returning:\n"
        "        return Function('DATE_TRUNC', 'day')\n"
        "    if dialect.features.supports_spatial:\n"
        "        return frozenset({DialectName.POSTGRESQL})\n"
        "    if entry.dialect_name != connection.dialect.name:\n"
        "        return implementations.get(query.dialect.name)\n"
        "    if dialect.features.supports_json_type:\n"
        "        return expression[dialect.name], cache[(path, dialect.name)]\n"
        "    raise ValueError(f'no {dialect.name} implementation')\n"
        "    return 'hare.dialects.postgresql.fields.array_field.ArrayField'\n",
        encoding="utf-8",
    )
    violations = sorted(violation.split(" ", 1)[1] for violation in get_violations_of(module, tmp_path))
    assert violations == sorted(
        [
            "imports hare.dialects.sqlite.constants",
            "imports hare.dialects.postgresql.fields",
            "compares with a dialect's name",
            "compares with a dialect's name",
            "matches a dialect's name",
            "names a dialect",
            "names a dialect",
            "names a dialect's module",
            "writes a dialect's SQL",
            "writes a dialect's SQL",
            "writes a dialect's SQL",
            # dialect.name == ... twice, entry.dialect_name != ..., .get(...), expression[...], cache[(...)]
            # and the (path, dialect.name) key itself - the message isn't one.
            *["picks by a dialect's name"] * 7,
        ]
    )
    assert list(get_core_modules())


def test_no_module_outside_the_clickhouse_dialect_names_it():
    """The ClickHouse dialect reaches hare only through its hooks and ``Features`` - no other module of
    hare names it but the list of hare's own dialects and drivers, which imports it by its name."""
    clickhouse_directory = DIALECTS_DIRECTORY / "clickhouse"
    naming_modules = [
        path.relative_to(HARE_DIRECTORY).as_posix()
        for path in sorted(HARE_DIRECTORY.rglob("*.py"))
        if clickhouse_directory not in path.parents and "clickhouse" in path.read_text(encoding="utf-8").lower()
    ]
    assert naming_modules == ["dialects/constants.py"]

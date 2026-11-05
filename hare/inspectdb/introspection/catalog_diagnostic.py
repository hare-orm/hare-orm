from dataclasses import dataclass

from hare.inspectdb.introspection.enums import CatalogDiagnosticCode


@dataclass(frozen=True, slots=True)
class CatalogDiagnostic:
    """A mapping limitation, retaining the object's identity and available source text.

    Diagnostics belong to a ``TableInfo``; presentation as source-code comments is
    a separate step. The original catalog snapshot retains the complete raw records,
    including properties for which the database exposes no standalone SQL definition.
    """

    code: CatalogDiagnosticCode
    name: str
    definition: str = ""
    columns: tuple[str, ...] = ()

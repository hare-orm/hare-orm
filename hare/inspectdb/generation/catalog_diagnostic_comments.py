from hare.inspectdb.introspection.catalog_diagnostic import CatalogDiagnostic
from hare.inspectdb.messages import CATALOG_DIAGNOSTIC_COMMENTS


class CatalogDiagnosticComments:
    """Present catalog mapping limitations in generated source without changing the records."""

    @staticmethod
    def get(diagnostic: CatalogDiagnostic) -> str:
        """Render a typed diagnostic as a source comment."""
        return CATALOG_DIAGNOSTIC_COMMENTS[diagnostic.code].format(
            name=diagnostic.name,
            definition=diagnostic.definition,
            columns=", ".join(diagnostic.columns),
        )

"""Finding repeated ("N+1") queries: ``RepeatedQueryDetector`` reports a query shape issued too
often in one unit of work; ``RepeatedQueryDetector.collect()`` counts the shapes of one block."""

from hare.contrib.repeated_queries.repeated_query_collection import RepeatedQueryCollection
from hare.contrib.repeated_queries.repeated_query_detector import RepeatedQueryDetector
from hare.contrib.repeated_queries.repeated_query_entry import RepeatedQueryEntry
from hare.contrib.repeated_queries.repeated_query_report import RepeatedQueryReport, ReportAction
from hare.contrib.repeated_queries.shape_stats import ShapeStats

__all__ = [
    "RepeatedQueryCollection",
    "RepeatedQueryDetector",
    "RepeatedQueryEntry",
    "RepeatedQueryReport",
    "ReportAction",
    "ShapeStats",
]

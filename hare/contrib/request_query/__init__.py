"""Request queries: the rows a request asks for - filters, search, ordering and pagination -
declared on a class and checked against the models when the application starts. A framework
adapter (``hare.contrib.frameworks``) builds them from a request's parameters."""

from __future__ import annotations

from hare.contrib.request_query.descriptions.parameter_choice import ParameterChoice
from hare.contrib.request_query.descriptions.parameter_description import ParameterDescription
from hare.contrib.request_query.descriptions.relation_description import RelationDescription
from hare.contrib.request_query.enums import BoundSide, ParameterType
from hare.contrib.request_query.exceptions import InvalidRequestQuery, RequestQueryForbidden
from hare.contrib.request_query.options.deleted_config import DeletedConfig
from hare.contrib.request_query.options.fields_config import FieldsConfig
from hare.contrib.request_query.options.filter import Filter
from hare.contrib.request_query.options.filter_field import FilterField
from hare.contrib.request_query.options.in_path import InPath
from hare.contrib.request_query.options.include_config import IncludeConfig
from hare.contrib.request_query.options.latest_versions import LatestVersions
from hare.contrib.request_query.options.no_filter import NoFilter
from hare.contrib.request_query.options.ordering_config import OrderingConfig
from hare.contrib.request_query.options.parameter_field import ParameterField
from hare.contrib.request_query.options.request_option import RequestOption
from hare.contrib.request_query.options.search_config import SearchConfig
from hare.contrib.request_query.paginations.base_offset_pagination import BaseOffsetPagination
from hare.contrib.request_query.paginations.cursor_pagination import CursorPagination
from hare.contrib.request_query.paginations.offset_pagination import OffsetPagination
from hare.contrib.request_query.paginations.pagination import Pagination
from hare.contrib.request_query.paginations.scroll_pagination import ScrollPagination
from hare.contrib.request_query.request_query import RequestQuery
from hare.contrib.request_query.results.cursor_page import CursorPage
from hare.contrib.request_query.results.page import Page
from hare.contrib.request_query.results.scroll_page import ScrollPage
from hare.contrib.request_query.types.comma_separated import CommaSeparated
from hare.contrib.request_query.types.generic_target import GenericTarget
from hare.contrib.request_query.types.key_columns import KeyColumns
from hare.contrib.request_query.types.text_parameter import TextParameter

__all__ = (
    "RelationDescription",
    "ParameterType",
    "ParameterDescription",
    "ParameterChoice",
    "BoundSide",
    "BaseOffsetPagination",
    "CommaSeparated",
    "GenericTarget",
    "KeyColumns",
    "CursorPage",
    "CursorPagination",
    "DeletedConfig",
    "FieldsConfig",
    "Filter",
    "FilterField",
    "InPath",
    "IncludeConfig",
    "InvalidRequestQuery",
    "LatestVersions",
    "NoFilter",
    "OffsetPagination",
    "OrderingConfig",
    "Page",
    "Pagination",
    "ParameterField",
    "RequestOption",
    "RequestQuery",
    "RequestQueryForbidden",
    "ScrollPage",
    "ScrollPagination",
    "SearchConfig",
    "TextParameter",
)

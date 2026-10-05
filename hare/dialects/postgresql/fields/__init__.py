from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.dialects.postgresql.fields.constants import EXPORTED_MODULES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.fields.citext_field import CitextField
    from hare.dialects.postgresql.fields.hstore import HStoreField
    from hare.dialects.postgresql.fields.ltree_field import LtreeField
    from hare.dialects.postgresql.fields.multiranges import (
        BigIntMultiRangeField,
        DateMultiRangeField,
        DateTimeMultiRangeField,
        DecimalMultiRangeField,
        IntMultiRangeField,
        MultiRangeField,
    )
    from hare.dialects.postgresql.fields.native_enum import NativeEnumField
    from hare.dialects.postgresql.fields.network import CidrField, InetField, MacAddressField
    from hare.dialects.postgresql.fields.postgis_field import PostGISField
    from hare.dialects.postgresql.fields.ranges import (
        BigIntRangeField,
        DateRangeField,
        DateTimeRangeField,
        DecimalRangeField,
        IntRangeField,
        Range,
        RangeField,
    )
    from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField

__all__ = [
    "BigIntMultiRangeField",
    "BigIntRangeField",
    "CidrField",
    "CitextField",
    "DateMultiRangeField",
    "DateRangeField",
    "DateTimeMultiRangeField",
    "DateTimeRangeField",
    "DecimalMultiRangeField",
    "DecimalRangeField",
    "HStoreField",
    "InetField",
    "IntMultiRangeField",
    "IntRangeField",
    "LtreeField",
    "MacAddressField",
    "MultiRangeField",
    "NativeEnumField",
    "PostGISField",
    "Range",
    "RangeField",
    "TSVectorField",
]


__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get

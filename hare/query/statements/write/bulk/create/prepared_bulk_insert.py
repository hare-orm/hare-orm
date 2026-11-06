from __future__ import annotations

from itertools import groupby
from operator import itemgetter
from typing import TYPE_CHECKING, Any

from hare.fields.database_default import DatabaseDefault
from hare.query.execution.bound_write import BoundWrite
from hare.query.statements.write.bulk.create.bulk_create_reports import BulkCreateReports
from hare.query.statements.write.bulk.create.copy_insert import CopyInsert
from hare.query.statements.write.bulk.create.declarations import BoundInsertBatch
from hare.query.statements.write.bulk.create.default_rows_insert import DefaultRowsInsert
from hare.query.statements.write.bulk.create.multi_row_insert import MultiRowInsert
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.query.statements.write.bulk.create.template_insert import TemplateInsert

if TYPE_CHECKING:
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class PreparedBulkInsert:
    """Prepare ordered bulk-insert batches before choosing their transaction boundary.

    Consecutive key shapes stay in input order, including bulk loads. Each strategy
    binds its values and computes its actual chunks once. Returned-row matching uses
    the metadata retained for that exact chunk rather than rebuilding the grouping.
    Calls without database-default candidates share one empty omission shape.
    """

    def __init__(self, bulk_create: BulkCreateQuery[Any], *, parameters_inline: bool = False) -> None:
        self.batches: list[BoundInsertBatch] = []
        self.omitted_fields: set[str] = set()
        default_fields = tuple(
            name
            for name in bulk_create._writer.templates.explicit_key_fields
            if bulk_create.model._meta.fields_map[name].has_db_default()
        )
        multi_row = (
            bulk_create._returning
            or BulkCreateReports.reports_returned_rows(bulk_create)
            or bulk_create._connection.features.execute_many_scales_poorly
        )
        empty_omitted_fields: frozenset[str] = frozenset()
        shapes = (
            (
                obj,
                (
                    obj._custom_generated_pk,
                    (
                        frozenset(name for name in default_fields if isinstance(getattr(obj, name), DatabaseDefault))
                        if default_fields
                        else empty_omitted_fields
                    ),
                ),
            )
            for obj in bulk_create._objects
        )
        for (custom_key, omitted), grouped_objects in groupby(shapes, key=itemgetter(1)):
            objects = [obj for obj, _shape in grouped_objects]
            omit_fields = set(omitted)
            self.omitted_fields.update(omit_fields)
            fields = bulk_create._filtered_field_names(omit_fields, include_generated=custom_key)
            columns = bulk_create._filter_columns(omit_fields, include_generated=custom_key)
            if not columns:
                templates = TemplateInsert.make_template_statements(bulk_create, omit_fields)
                batches = DefaultRowsInsert.bind_batches(
                    bulk_create, templates[int(custom_key)][0], objects, omit_fields, bulk_create._returning
                )
            elif bulk_create._use_copy:
                batches = CopyInsert.bind_batches(bulk_create, columns, fields, objects)
            elif multi_row:
                batches = MultiRowInsert.bind_batches(
                    bulk_create,
                    columns,
                    fields,
                    objects,
                    omit_fields,
                    populate_returned_fields=bulk_create._returning,
                    column_arrays_source_sql=MultiRowInsert.get_column_arrays_rows_source_sql(bulk_create, fields),
                    parameters_inline=parameters_inline,
                )
            else:
                batches = TemplateInsert.bind_batches(
                    bulk_create, columns, fields, objects, parameters_inline=parameters_inline
                )
            self.batches.extend(batches)
        self.bound = BoundWrite(batch.command for batch in self.batches)

    async def execute(self, bulk_create: BulkCreateQuery[Any]) -> None:
        """Apply results as their commands finish; stop at the first execution failure."""
        index = 0
        async for result in self.bound.execute(bulk_create._connection):
            batch = self.batches[index]
            index += 1
            if batch.populate_returned_fields:
                ReturnedRowsMatching.populate_returned_fields_from_returning_rows(
                    bulk_create,
                    list(batch.objects),
                    result.rows,
                    batch.conflict_fields,
                    batch.written_fields,
                    batch.applied_columns,
                )
            if bulk_create._reported_rows is not None:
                bulk_create._reported_rows.extend(dict(row) for row in result.rows)

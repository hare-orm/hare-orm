"""The scenario groups of the benchmark's databases, as the charts and pages show them."""

from __future__ import annotations

from orm_benchmark.definitions.scenario import Scenario
from orm_benchmark.definitions.scenario_group import ScenarioGroup

#: PostgreSQL's and SQLite's groups: a table of widgets, each with a gadget, a tag and a JSON document.
ROW_STORE_GROUPS = (
    ScenarioGroup(
        "reads",
        "Reads",
        "Чтение",
        (
            Scenario("fetch_all", "All {rows} rows", "Все {rows} строк"),
            Scenario("get_by_pk", "{batch} × get() by primary key", "{batch} × get() по ключу"),
            Scenario("first_row", "{batch} × first()", "{batch} × first()"),
            Scenario("paginated_fetch", "A page, LIMIT/OFFSET", "Страница, LIMIT/OFFSET"),
            Scenario("top_ten", "order_by() + the first 10", "order_by() + первые 10"),
            Scenario("filter", "Filter on two fields", "Фильтр по двум полям"),
            Scenario("icontains_search", "icontains search", "Поиск icontains"),
            Scenario("exists_check", "exists()", "exists()"),
            Scenario("values_list_flat", "values_list(flat=True)", "values_list(flat=True)"),
            Scenario("distinct_values", "distinct() values of a field", "distinct() значений поля"),
            Scenario("only_fields", 'only("id", "name")', 'only("id", "name")'),
            Scenario("complex_filter", "OR + JOIN + distinct", "OR + JOIN + distinct"),
            Scenario("large_in_filter", "id IN ({in_count} values)", "id IN ({in_count} значений)"),
            Scenario("iterate_chunks", "Iterating all rows in chunks", "Обход всех строк порциями"),
            Scenario("json_read", "Reading a JSON field", "Чтение JSON-поля"),
        ),
    ),
    ScenarioGroup(
        "relations",
        "Relations",
        "Связи",
        (
            Scenario("select_related_join", "select_related, a JOIN", "select_related, JOIN"),
            Scenario("related_filter", "Filter across a relation", "Фильтр через связь"),
            Scenario("prefetch_related", "prefetch_related", "prefetch_related"),
            Scenario("prefetch_many_to_many", "prefetch_related, many-to-many", "prefetch_related многие-ко-многим"),
            Scenario("many_to_many_filter", "Filter across many-to-many", "Фильтр через многие-ко-многим"),
            Scenario("annotate_count", "annotate(Count) of a relation", "annotate(Count) по связи"),
            Scenario("exists_annotation", "Exists() in annotate", "Exists() в annotate"),
            Scenario("n_plus_one", "N+1: {batch} separate queries", "N+1: {batch} отдельных запросов"),
            Scenario("many_to_many_add", "{batch} × many-to-many add()", "{batch} × add() многие-ко-многим"),
        ),
    ),
    ScenarioGroup(
        "aggregates",
        "Aggregates",
        "Агрегаты",
        (
            Scenario("count", "count()", "count()"),
            Scenario("aggregate", "Sum, Avg, Max, Min", "Sum, Avg, Max, Min"),
            Scenario("conditional_count", "Count with a condition", "Count с условием"),
            Scenario("group_by", "GROUP BY", "GROUP BY"),
            Scenario("having", "HAVING on an aggregate", "HAVING по агрегату"),
            Scenario("annotate_values", "annotate + values", "annotate + values"),
            Scenario("case_when_conditional", "Case / When", "Case / When"),
            Scenario("window_rank", "Window: Rank() per category", "Окно: Rank() по категории"),
        ),
    ),
    ScenarioGroup(
        "writes",
        "Writes",
        "Запись",
        (
            Scenario("bulk_create", "bulk_create, {rows} rows", "bulk_create, {rows} строк"),
            Scenario("bulk_create_large", "bulk_create, {large_rows} rows", "bulk_create, {large_rows} строк"),
            Scenario("single_insert", "{batch} × create()", "{batch} × create()"),
            Scenario("update_bulk", "update() by a filter", "update() по фильтру"),
            Scenario("update_expression", "update() with F(): value + 1", "update() с F(): value + 1"),
            Scenario("update_by_pk", "{batch} × update() by primary key", "{batch} × update() по ключу"),
            Scenario("update_loop", "{batch} × get() + save()", "{batch} × get() + save()"),
            Scenario("bulk_update", "bulk_update, {batch} rows", "bulk_update, {batch} строк"),
            Scenario("upsert", "Upsert, {batch} rows", "Upsert, {batch} строк"),
            Scenario("get_or_create", "{batch} × get_or_create()", "{batch} × get_or_create()"),
            Scenario("delete_bulk", "delete() by a filter", "delete() по фильтру"),
            Scenario("delete_loop", "{batch} × get() + delete()", "{batch} × get() + delete()"),
            Scenario("json_write", "Writing a JSON field", "Запись JSON-поля"),
        ),
    ),
    ScenarioGroup(
        "transactions",
        "Transactions, concurrency, start",
        "Транзакции, параллельность, запуск",
        (
            Scenario(
                "atomic_update_loop", "{batch} × a transaction: get() + save()", "{batch} × транзакция: get() + save()"
            ),
            Scenario(
                "nested_transaction_loop",
                "{batch} × a nested transaction (savepoint)",
                "{batch} × вложенная транзакция (savepoint)",
            ),
            Scenario(
                "select_for_update_loop",
                "{batch} × select_for_update() in a transaction",
                "{batch} × select_for_update() в транзакции",
            ),
            Scenario("concurrent_get", "{batch} × get() at once", "{batch} × get() одновременно"),
            Scenario("cold_start", "Start: init and the first connection", "Запуск: init и первое подключение"),
        ),
    ),
)

#: ClickHouse's groups: an analytical table of events, read by aggregates and written in bulk.
CLICKHOUSE_GROUPS = (
    ScenarioGroup(
        "reads",
        "Reads",
        "Чтение",
        (
            Scenario("count_filter", "count() with a filter", "count() с фильтром"),
            Scenario("group_sum", "GROUP BY + Sum, Avg", "GROUP BY + Sum, Avg"),
            Scenario("top_n", "The top 10 by a sum", "Топ-10 по сумме"),
            Scenario("by_day", "Grouped by day", "Группировка по дню"),
            Scenario("distinct_count", "Count of distinct values", "Число уникальных значений"),
            Scenario("page_values", "A page of values()", "Страница values()"),
            Scenario("get_by_key", "{batch} × get() by key", "{batch} × get() по ключу"),
            Scenario("concurrent_reads", "{batch} × get() at once", "{batch} × get() одновременно"),
        ),
    ),
    ScenarioGroup(
        "writes",
        "Writes and start",
        "Запись и запуск",
        (
            Scenario("bulk_insert", "Inserting {insert_rows} rows", "Вставка {insert_rows} строк"),
            Scenario("mutation_update", "update() by a filter (a mutation)", "update() по фильтру (мутация)"),
            Scenario("mutation_delete", "delete() by a filter (a mutation)", "delete() по фильтру (мутация)"),
            Scenario("cold_start", "Start: init and the first connection", "Запуск: init и первое подключение"),
        ),
    ),
)

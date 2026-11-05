import json


class ExplainPlanChecks:
    """Check each server's documented plan structure without fixing its optimizer choices."""

    @staticmethod
    def assert_read_plan(plan, dialect_name, table):
        assert plan
        assert all(type(row) is dict and row for row in plan), plan
        if dialect_name == "sqlite":
            for row in plan:
                assert {"id", "parent", "notused", "detail"} <= row.keys()
                assert all(isinstance(row[name], int) for name in ("id", "parent", "notused"))
                assert isinstance(row["detail"], str) and row["detail"]
            assert any(row["detail"].startswith(("SCAN ", "SEARCH ")) and table in row["detail"] for row in plan)
        elif dialect_name == "postgresql":
            value = plan[0]["QUERY PLAN"]
            documents = json.loads(value) if isinstance(value, str) else value
            assert isinstance(documents, list) and len(documents) == 1
            nodes = [documents[0]["Plan"]]
            for node in nodes:
                assert isinstance(node["Node Type"], str) and node["Node Type"]
                nodes.extend(node.get("Plans", ()))
            assert any(node.get("Relation Name") == table for node in nodes)
        elif dialect_name == "clickhouse":
            assert all(isinstance(row["explain"], str) and row["explain"].strip() for row in plan)
        else:
            raise AssertionError(f"No plan contract declared for {dialect_name}")

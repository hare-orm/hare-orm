"""Independent expected names checked through the public shapes of a relation query."""


class RelationQueryChecks:
    @staticmethod
    async def check(queryset, expected_names):
        expected = sorted(expected_names)
        ordered = queryset.order_by("name")
        assert [obj.name for obj in await ordered] == expected
        assert [obj.name for obj in await ordered.only("name")] == expected
        assert await ordered.values("name") == [{"name": name} for name in expected]
        assert await ordered.values_list("name") == [(name,) for name in expected]
        assert await ordered.values_list("name", flat=True) == expected
        assert await queryset.count() == len(expected)
        assert await queryset.exists() == bool(expected)
        assert await ordered[1:2].values_list("name", flat=True) == expected[1:2]
        assert await ordered[1:2].count() == len(expected[1:2])
        assert await ordered[1:2].exists() == bool(expected[1:2])
        first = await ordered.first()
        last = await ordered.last()
        assert (first.name if first else None) == (expected[0] if expected else None)
        assert (last.name if last else None) == (expected[-1] if expected else None)

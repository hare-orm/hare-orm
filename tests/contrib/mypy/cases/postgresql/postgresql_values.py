"""The values of the lookups of ranges and geometries as the mypy plugin types them - a range lookup
takes a ``Range`` or its bounds, a path reads inside the range, a distance lookup takes
``(geometry, distance)`` - as the docs write them."""

import ipaddress
from datetime import UTC, date, datetime

from hare.dialects.postgresql.fields import Range
from hare.gis import Point, Polygon
from tests.contrib.mypy.postgresql_models import Booking


async def postgresql_values() -> None:
    start, end = datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
    await Booking.objects.filter(seats__overlap=Range(1, 5), seats__contains=Range(2, 3), seats=(1, 5))
    await Booking.objects.filter(seats__contains=2, during__fully_lt=Range(start, end), during__adjacent_to=(start, end))
    await Booking.objects.filter(days__overlap=Range(date(2024, 1, 1), date(2024, 1, 6)))
    await Booking.objects.filter(days=[Range(date(2024, 1, 1), date(2024, 1, 6))], days__contains=date(2024, 1, 2))
    await Booking.objects.filter(during__startswith__year=2024, during__isempty=False, days__span__upper_inf=True)
    await Booking.objects.filter(place__dwithin=(Point(37.62, 55.75), 10_000), place__distance_lt=(Point(1, 2), 0.5))
    await Booking.objects.filter(place__relate=(Point(1, 2), "T********"), place__x__gte=1.5)
    await Booking.objects.filter(seats__overlap="1-5")  # E: Argument "seats__overlap" to "filter" of "QuerySet" has incompatible type "str"
    await Booking.objects.filter(place__dwithin=Point(1, 2))  # E: Argument "place__dwithin" to "filter" of "QuerySet" has incompatible type "Point"
    await Booking.objects.filter(during__startswith__year="2024")  # E: Argument "during__startswith__year" to "filter" of "QuerySet" has incompatible type "str"


async def other_values() -> None:
    address = ipaddress.ip_address("10.0.0.1")
    await Booking.objects.filter(address=address, address__in=[address, "10.0.0.2"], address__gt=address)
    await Booking.objects.filter(network__net_contained=ipaddress.ip_network("10.0.0.0/8"), network=address)
    await Booking.objects.filter(id=1).update(address=address, network=ipaddress.ip_interface("10.0.0.1/24"))
    await Booking.objects.filter(section__matches_any=["top.*", "*.news"])
    await Booking.objects.filter(area__contains=Point(1, 2), place__within=Polygon([(0, 0), (0, 3), (3, 3), (0, 0)]))
    await Booking.objects.filter(seats__contains=(1, 5), days__contains=(date(2024, 1, 1), date(2024, 1, 6)))
    await Booking.objects.filter(days__overlap=[(date(2024, 1, 1), date(2024, 1, 6))], days__contains=[Range(date(2024, 1, 1), date(2024, 1, 2))])
    await Booking.objects.filter(id=1).update(seats=(1, 5), details={"a": 1})
    await Booking.objects.filter(id=1).update(details=[1, "a"])

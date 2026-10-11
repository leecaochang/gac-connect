from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from gac_connect.client import GacClient
from gac_connect.vehicle import window_operation

ZONE = ZoneInfo("Asia/Bangkok")


def stamp(value):
    return datetime(**{k: v for k, v in value.items() if k != "millisecond"}, tzinfo=ZONE)


@pytest.mark.parametrize("hour", range(24))
def test_overnight_dates_are_ordered_at_every_hour(hour):
    now = datetime(2026, 10, 11, hour, tzinfo=ZONE)
    op = window_operation("23:00", "05:00", ZONE, now=now)
    start, stop = stamp(op["startTime"]), stamp(op["stopTime"])
    assert now < start < stop
    assert (stop - start).total_seconds() == 6 * 3600
    assert op["dailyReservationStartTime"] == 23 * 3600000


def test_next_start_respects_weekday_mask():
    now = datetime(2026, 10, 11, 2, tzinfo=ZONE)  # Sunday
    op = window_operation("23:00", "05:00", ZONE, weekly=1, now=now)
    assert stamp(op["startTime"]).isoformat() == "2026-10-12T23:00:00+07:00"
    assert stamp(op["stopTime"]).isoformat() == "2026-10-13T05:00:00+07:00"


@pytest.mark.parametrize("kw", [
    {"start": "25:00"}, {"stop": "1:00"}, {"stop": "23:00"}, {"start": None},
    *[{"weekly": v} for v in (-1, 128, 1.5, True, "1", None, float("nan"), float("inf"), 10**400)],
    {"until_soc": "false"},
])
def test_invalid_window_never_sends(kw):
    import asyncio
    client = GacClient("AU", Mock())
    client._reservation = AsyncMock()
    with pytest.raises(ValueError):
        asyncio.run(client.set_charge_window("VIN", **{"start": "23:00", "stop": "05:00", **kw}))
    client._reservation.assert_not_called()

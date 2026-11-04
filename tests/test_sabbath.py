from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from app import sabbath


@pytest.mark.parametrize('day,reference',[(date(2026,11,6),'18:08'),(date(2026,11,7),'18:08')])
def test_dusk_matches_independent_published_jerusalem_table(day,reference):
    # Source-owned calculation table, retrieved 2026-11-04:
    # https://www.timeanddate.com/sun/israel/jerusalem?month=11&year=2026
    result=sabbath.astronomical_dusk(day).astimezone(ZoneInfo('Asia/Jerusalem'))
    published=datetime.combine(day,datetime.strptime(reference,'%H:%M').time(),ZoneInfo('Asia/Jerusalem'))
    assert abs((result-published).total_seconds())<90
    assert abs(sabbath._solar_altitude(result)+18)<.00001


def test_seasonal_dates_dst_and_weekly_rounding():
    zone=ZoneInfo('Asia/Jerusalem')
    winter=sabbath.astronomical_dusk(date(2026,11,6))
    summer=sabbath.astronomical_dusk(date(2026,6,19))
    assert winter.astimezone(zone).utcoffset()==timedelta(hours=2)
    assert summer.astimezone(zone).utcoffset()==timedelta(hours=3)
    assert summer.astimezone(zone).hour>winter.astimezone(zone).hour
    start,end=sabbath.window_for_week(date(2026,11,6))
    assert start<=winter<start+timedelta(minutes=1)
    assert end-timedelta(minutes=1)<sabbath.astronomical_dusk(date(2026,11,7))<=end
    assert sabbath.window_for_week(date(2026,11,7))==(start,end)
    assert not sabbath.blackout(datetime(2026,11,8,12,tzinfo=timezone.utc))


def test_missing_timezone_or_unsupported_date_is_fail_closed(monkeypatch):
    monkeypatch.delenv('SABBATH_BLACKOUT',raising=False)
    assert sabbath.blackout(datetime(1900,1,1))
    monkeypatch.setattr(sabbath,'ZoneInfo',lambda key:(_ for _ in ()).throw(ValueError('missing tzdata')))
    assert sabbath.blackout(datetime(2026,11,4,tzinfo=timezone.utc))

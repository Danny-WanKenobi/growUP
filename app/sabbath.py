"""Offline Jerusalem astronomical-dusk guard; no provider or calendar API calls.

Solar geometry: NOAA/Meeus equations (geometric centre, level horizon),
https://gml.noaa.gov/grad/solcalc/calcdetails.html . Astronomical dusk is
18 degrees below the horizon: https://aa.usno.navy.mil/faq/RST_defs .
This implements the owner's timing definition, not a religious ruling or a
measurement of weather, terrain, artificial light or observed sky brightness.
"""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import math
import os
from zoneinfo import ZoneInfo

SABBATH_TZ = 'Asia/Jerusalem'
LATITUDE = 31.778
LONGITUDE = 35.235


def _solar_altitude(instant):
    """Geometric solar-centre altitude in degrees; no sunset refraction term."""
    utc=instant.astimezone(timezone.utc)
    t=(utc.timestamp()/86400+2440587.5-2451545)/36525
    rad=math.radians
    lon=(280.46646+t*(36000.76983+t*.0003032))%360
    anomaly=357.52911+t*(35999.05029-.0001537*t)
    eccentricity=.016708634-t*(.000042037+.0000001267*t)
    centre=(math.sin(rad(anomaly))*(1.914602-t*(.004817+.000014*t))
        +math.sin(rad(2*anomaly))*(.019993-.000101*t)+math.sin(rad(3*anomaly))*.000289)
    omega=125.04-1934.136*t
    apparent=lon+centre-.00569-.00478*math.sin(rad(omega))
    obliquity=23+(26+(21.448-t*(46.815+t*(.00059-t*.001813)))/60)/60+.00256*math.cos(rad(omega))
    declination=math.asin(math.sin(rad(obliquity))*math.sin(rad(apparent)))
    y=math.tan(rad(obliquity)/2)**2
    equation=4*math.degrees(y*math.sin(2*rad(lon))-2*eccentricity*math.sin(rad(anomaly))
        +4*eccentricity*y*math.sin(rad(anomaly))*math.cos(2*rad(lon))
        -.5*y*y*math.sin(4*rad(lon))-1.25*eccentricity**2*math.sin(2*rad(anomaly)))
    minutes=utc.hour*60+utc.minute+utc.second/60+utc.microsecond/60000000
    hour_angle=rad(((minutes+equation+4*LONGITUDE)%1440)/4-180)
    sine=math.sin(rad(LATITUDE))*math.sin(declination)+math.cos(rad(LATITUDE))*math.cos(declination)*math.cos(hour_angle)
    return math.degrees(math.asin(max(-1,min(1,sine))))


@lru_cache(maxsize=128)
def astronomical_dusk(day):
    """Return the evening downward -18 degree crossing, UTC (model estimate)."""
    if not isinstance(day,date) or isinstance(day,datetime) or not 2000<=day.year<=2100:
        raise ValueError('Jerusalem dusk supports calendar dates from 2000 through 2100')
    zone=ZoneInfo(SABBATH_TZ)
    low=datetime.combine(day,time(12),zone).astimezone(timezone.utc)
    high=datetime.combine(day,time(23,59),zone).astimezone(timezone.utc)
    if not (_solar_altitude(low)>-18 and _solar_altitude(high)<-18):
        raise ValueError('Jerusalem astronomical-dusk crossing unavailable')
    for _ in range(32):
        mid=low+(high-low)/2
        if _solar_altitude(mid)>-18: low=mid
        else: high=mid
    return high


def window_for_week(day):
    """Friday dusk inclusive to Saturday dusk exclusive, conservatively rounded."""
    friday=day-timedelta(days=(day.weekday()-4)%7)
    start=astronomical_dusk(friday).replace(second=0,microsecond=0)
    raw_end=astronomical_dusk(friday+timedelta(days=1))
    end=raw_end.replace(second=0,microsecond=0)
    if raw_end!=end: end+=timedelta(minutes=1)
    return start,end


def blackout(now=None):
    """Shared discovery/monitor guard; invalid clocks/calculations fail closed.

    Naive datetimes mean Jerusalem local time. Only explicit BLACKOUT=0 disables.
    Retired Chicago START/END settings never override the astronomical rule.
    """
    if os.getenv('SABBATH_BLACKOUT','1').strip()=='0': return False
    try:
        zone=ZoneInfo(SABBATH_TZ)
        current=now if now is not None else datetime.now(timezone.utc)
        if not isinstance(current,datetime): return True
        current=current.replace(tzinfo=zone) if current.tzinfo is None else current.astimezone(zone)
        start,end=window_for_week(current.date())
        return start<=current.astimezone(timezone.utc)<end
    except Exception:
        return True


if __name__=='__main__':
    import json
    local=datetime.now(timezone.utc).astimezone(ZoneInfo(SABBATH_TZ))
    start,end=window_for_week(local.date())
    if local.astimezone(timezone.utc)>=end:
        start,end=window_for_week(local.date()+timedelta(days=7))
    print(json.dumps(dict(timezone=SABBATH_TZ,coordinates=dict(latitude=LATITUDE,longitude=LONGITUDE),
        definition='Friday to Saturday astronomical dusk; solar centre -18 degrees',
        blackout_now=blackout(local),window='current_or_upcoming',friday_start=start.astimezone(ZoneInfo(SABBATH_TZ)).isoformat(),
        saturday_end=end.astimezone(ZoneInfo(SABBATH_TZ)).isoformat(),
        rounding='start down / end up to whole minute; calculated times, not observed sky brightness'),indent=2))

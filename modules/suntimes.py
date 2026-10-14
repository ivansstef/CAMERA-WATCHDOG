"""
Sunrise/sunset calculator (NOAA spreadsheet algorithm, ±3 min accuracy).
No external dependencies — used by night_profile mode=sun.
"""
import datetime
import math


def _equation_of_time(day_of_year: int) -> float:
    """Хвилини."""
    b = 2 * math.pi * (day_of_year - 81) / 364
    return 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)


def _solar_declination(day_of_year: int) -> float:
    """Градуси."""
    return 23.44 * math.sin(math.radians(360 / 365 * (day_of_year - 81)))


def sun_times_utc(lat: float, lon: float, date: datetime.date | None = None,
                  zenith_deg: float = 90.833) -> tuple[datetime.time, datetime.time]:
    """
    Sunrise/sunset (UTC time objects) for given lat/lon and date.
    zenith 90.833° = official (with refraction).
    """
    day = date or datetime.date.today()
    n = day.timetuple().tm_yday
    lat_rad = math.radians(lat)
    decl = math.radians(_solar_declination(n))

    cos_h = (math.cos(math.radians(zenith_deg)) - math.sin(lat_rad) * math.sin(decl)) / \
            (math.cos(lat_rad) * math.cos(decl))
    cos_h = max(-1.0, min(1.0, cos_h))
    h_deg = math.degrees(math.acos(cos_h))    # годинний кут у градусах

    eot = _equation_of_time(n)

    def _utc_time(solar_noon_deg_offset: float) -> datetime.time | None:
        # local solar noon у хвилинах UTC
        noon_utc_min = 720 - solar_noon_deg_offset * 4 - eot - lon * 4
        total = noon_utc_min + solar_noon_deg_offset * 4  # ± H/15*60 хв... див нижче
        return None

    # sunrise: 12 - H/15 (local solar) → UTC: - lon/15*60 - eot
    def _to_utc(solar_hour: float) -> datetime.time:
        minutes_utc = solar_hour * 60 - eot - lon * 4
        minutes_utc %= 1440
        return datetime.time(int(minutes_utc // 60), int(minutes_utc % 60))

    sunrise = _to_utc(12 - h_deg / 15)
    sunset = _to_utc(12 + h_deg / 15)
    return sunrise, sunset


def is_night(lat: float, lon: float, now_local: datetime.datetime,
             morning_offset_min: int = 30, evening_offset_min: int = 30) -> bool:
    """
    Ніч = від (захід - evening_offset) до (схід + morning_offset). Локальний час.
    Полярний день/ніч (acos clamp) — обробляється: якщо cos_h > 1 (сонце не сходить)
    → вся доба ніч; < -1 → вся доба день (тут широти України не загрожують, але коректно).
    """
    day = now_local.date()
    n = day.timetuple().tm_yday
    lat_rad = math.radians(lat)
    decl = math.radians(_solar_declination(n))
    cos_h = (math.cos(math.radians(90.833)) - math.sin(lat_rad) * math.sin(decl)) / \
            (math.cos(lat_rad) * math.cos(decl))
    if cos_h > 1:      # полярна ніч
        return True
    if cos_h < -1:     # полярний день
        return False

    sunrise_utc, sunset_utc = sun_times_utc(lat, lon, day)
    tz = now_local.utcoffset() or datetime.timedelta(0)
    sunrise_local = (datetime.datetime.combine(day, sunrise_utc, tzinfo=datetime.timezone.utc) + tz).replace(tzinfo=None)
    sunset_local = (datetime.datetime.combine(day, sunset_utc, tzinfo=datetime.timezone.utc) + tz).replace(tzinfo=None)

    night_start = sunset_local - datetime.timedelta(minutes=evening_offset_min)
    night_end = sunrise_local + datetime.timedelta(minutes=morning_offset_min)
    now_naive = now_local.replace(tzinfo=None) if now_local.tzinfo else now_local
    return now_naive < night_end or now_naive >= night_start
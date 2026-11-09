#!/data/data/com.termux/files/usr/bin/env python
"""Print a Persian (Jalali) calendar with the current day highlighted.
Converts the Gregorian date with its own Jalali arithmetic, prints the month grid with Persian month names, Persian digits and right-to-left alignment (through ``faprint``) and marks today.
Usage: facal Examples: $ facal Runs as a script (``python facal.py``) in the directory it should act on; the module can also be imported."""

from datetime import date
import sys

from faprint import faprint


_PERSIAN_DIGITS: dict[int, str] = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_JALALI_MONTHS_FA: list[str] = [
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]
_WEEKDAYS_FA_BY_PY_WEEKDAY: list[str] = [
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنج‌شنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
]
_WEEKDAY_ABBR_FA: list[str] = ["شن", "یک", "دو", "سه", "چه", "پن", "جم"]


def to_persian_digits(s: str) -> str:
    return s.translate(_PERSIAN_DIGITS)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_days: list[int] = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2: int = gy - 1600
    gm2: int = gm - 1
    gd2: int = gd - 1
    g_day_no: int = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_days[gm2] + gd2
    j_day_no: int = g_day_no - 79
    j_np: int = j_day_no // 12053
    j_day_no %= 12053
    jy: int = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    jm: int
    jd: int
    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        j_day_no -= 186
        jm = 7 + j_day_no // 30
        jd = 1 + j_day_no % 30
    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy2: int = jy + 1595
    days: int = -355668 + 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33) + 3) // 4 + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186
    gy: int = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd: int = days + 1
    leap: bool = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    sal_a: list[int] = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm: int = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return gy, gm, gd


def is_jalali_leap(jy: int) -> bool:
    return ((jy + 12) % 33) % 4 == 1


def jalali_days_in_month(jy: int, jm: int) -> int:
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_jalali_leap(jy) else 29


def jalali_weekday_index(jy: int, jm: int, jd: int) -> int:
    gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
    return (date(gy, gm, gd).weekday() + 2) % 7


def format_full_date(jy: int, jm: int, jd: int) -> str:
    gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
    weekday_name: str = _WEEKDAYS_FA_BY_PY_WEEKDAY[date(gy, gm, gd).weekday()]
    month_name: str = _JALALI_MONTHS_FA[jm - 1]
    return f"{weekday_name} {to_persian_digits(str(jd))} {month_name} {to_persian_digits(str(jy))}"


def format_calendar(jy: int, jm: int) -> list[str]:
    first_weekday: int = jalali_weekday_index(jy, jm, 1)
    dim: int = jalali_days_in_month(jy, jm)
    lines: list[str] = [" ".join(_WEEKDAY_ABBR_FA)]
    cells: list[str] = [""] * first_weekday
    for d in range(1, dim + 1):
        cells.append(to_persian_digits(str(d)))
        if len(cells) == 7:
            lines.append(" ".join(cells))
            cells = []
    if cells:
        cells.extend([""] * (7 - len(cells)))
        lines.append(" ".join(cells))
    return lines


def main() -> None:
    args: list[str] = sys.argv[1:]
    show_only_date: bool = any(a in ("-d", "--date") for a in args)
    today: date = date.today()
    jy: int
    jm: int
    jd: int
    jy, jm, jd = gregorian_to_jalali(today.year, today.month, today.day)
    faprint(format_full_date(jy, jm, jd))
    if not show_only_date:
        for line in format_calendar(jy, jm):
            faprint(line)


if __name__ == "__main__":
    main()

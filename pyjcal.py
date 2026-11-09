#!/data/data/com.termux/files/usr/bin/env python
"""Print a Jalali (Persian) calendar, or convert a date to Jalali.
Without arguments the current month is rendered as a calendar grid; ``jdate`` prints today's Jalali date and accepts a format string (``-f``), a language (``-l``) and an explicit Gregorian date to convert.
The date arithmetic lives in the ``JalaliDate``/``JalaliCalendar``/``JalaliDateFormatter`` classes.
Usage: pyjcal [jdate [-f FORMAT] [-l LANG] [YYYY-MM-DD]] Examples: $ pyjcal $ pyjcal jdate -f '%Y/%m/%d' Runs as a script (``python pyjcal.py``) in the directory it should act on; the module can also be imported."""

from datetime import datetime


class JalaliDate:
    JALALI_MONTHS_EN = [
        "Farvardin",
        "Ordibehesht",
        "Khordad",
        "Tir",
        "Mordad",
        "Shahrivar",
        "Mehr",
        "Aban",
        "Azar",
        "Dey",
        "Bahman",
        "Esfand",
    ]
    JALALI_MONTHS_FA = [
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
    JALALI_WEEKDAYS_EN = [
        "Shanbeh",
        "Yekshanbe",
        "Doshanbe",
        "Seshanbe",
        "Chaharshanbe",
        "Panjshanbe",
        "Jomeh",
    ]
    JALALI_WEEKDAYS_FA = [
        "شنبه",
        "یکشنبه",
        "دوشنبه",
        "سه\u200cشنبه",
        "چهارشنبه",
        "پنج\u200cشنبه",
        "جمعه",
    ]

    def __init__(self, jalali_year: int, jalali_month: int, jalali_day: int) -> None:
        self.year = jalali_year
        self.month = jalali_month
        self.day = jalali_day

    @staticmethod
    def today_with_time() -> tuple[JalaliDate, datetime]:
        now = datetime.now()
        jdate = JalaliDate.from_gregorian(now.year, now.month, now.day)
        return jdate, now

    @staticmethod
    def today() -> JalaliDate:
        gregorian_date = datetime.now()
        return JalaliDate.from_gregorian(gregorian_date.year, gregorian_date.month, gregorian_date.day)

    @staticmethod
    def from_gregorian(g_year: int, g_month: int, g_day: int) -> JalaliDate:
        gy = g_year - 1600
        gm = g_month - 1
        gd = g_day - 1
        g_day_of_year = (
            sum([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][:gm])
            + gd
            + (1 if gm > 1 and ((g_year % 4 == 0 and g_year % 100 != 0) or g_year % 400 == 0) else 0)
        )
        g_day_no = gy * 365 + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400 + g_day_of_year
        j_day_no = g_day_no - 79
        j_np = j_day_no // 146097
        j_day_no %= 146097
        leap = 1 if j_day_no >= 36525 else 0
        j_day_no -= 36525 * leap
        j_np4 = j_day_no // 36524
        if j_day_no >= 36524:
            j_day_no -= 36524
            j_np4 = 3
        j_y = 400 * j_np + 100 * j_np4 + 4 * (j_day_no // 1461)
        j_day_no %= 1461
        leap2 = 1 if j_day_no >= 365 else 0
        j_day_no -= 365 * leap2
        j_y += leap2
        j_m = 1
        for i in range(6):
            v = 31 if i < 6 else 30
            if j_day_no < v:
                break
            j_day_no -= v
            j_m += 1
        j_d = j_day_no + 1
        return JalaliDate(j_y, j_m, int(j_d))

    def to_gregorian(self) -> tuple[int, int, int]:
        jy = self.year
        jm = self.month
        jd = self.day
        jy += 1474
        if jm > 6:
            jy += 1
        days = 365 * jy + jy // 33 * 8 + (jy % 33 + 3) // 4 + 78 + jd
        if jm > 6:
            days += (jm - 7) * 30 + 186
        else:
            days += (jm - 1) * 31
        gy = 400 * (days // 146097)
        days %= 146097
        leap = 1 if days >= 36525 else 0
        days -= 36525 * leap
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 36524:
            days -= 36524
            gy += 300
        else:
            gy += 100 * (days // 36524)
            days %= 36524
        gy += 4 * (days // 1461)
        days %= 1461
        leap2 = 1 if days >= 365 else 0
        days -= 365 * leap2
        gy += leap2
        sal_a = [31, 28 + leap2, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        gm = 1
        for v in sal_a:
            if days < v:
                break
            days -= v
            gm += 1
        gd = days + 1
        return gy, gm, int(gd)

    def weekday(self) -> int:
        g_year, g_month, g_day = self.to_gregorian()
        gregorian_date = datetime(g_year, g_month, g_day)
        return (gregorian_date.weekday() + 2) % 7

    def is_leap_year(self) -> bool:
        breaks = [
            -61,
            9,
            38,
            199,
            426,
            686,
            756,
            818,
            1111,
            1181,
            1210,
            1635,
            2060,
            2097,
            2192,
            2262,
            2324,
            2394,
            2456,
            3178,
        ]
        year = self.year
        gy = year + 1474
        if year < 0:
            gy -= 1
        leap = -14
        jp = breaks[0]
        for i in range(1, len(breaks)):
            jm = breaks[i]
            jump = jm - jp
            if year < jm:
                break
            leap += jump // 33 * 8 + jump % 33 // 4
            jp = jm
        n = year - jp
        leap += (n + 4) // 33 * 8 + ((n + 4) % 33 + 1) // 4
        if jump % 33 % 4 == 0 and jm - jp == 128:
            leap += 1
        return gy % 400 == 0 or (gy % 100 != 0 and gy % 4 == 0)

    def days_in_month(self) -> int:
        if self.month <= 6:
            return 31
        if self.month <= 11:
            return 30
        return 30 if self.is_leap_year() else 29

    def __str__(self) -> str:
        return f"{self.year:04d}/{self.month:02d}/{self.day:02d}"

    def __repr__(self) -> str:
        return f"JalaliDate({self.year}, {self.month}, {self.day})"


class JalaliCalendar:
    def __init__(self, year: int | None = None, month: int | None = None) -> None:
        today = JalaliDate.today()
        self.year = year if year is not None else today.year
        self.month = month if month is not None else today.month

    def get_month_calendar(self) -> list[list[int]]:
        first_day = JalaliDate(self.year, self.month, 1)
        first_weekday = first_day.weekday()
        days_in_month = first_day.days_in_month()
        calendar_grid = []
        week = [0] * first_weekday
        for day in range(1, days_in_month + 1):
            week.append(day)
            if len(week) == 7:
                calendar_grid.append(week)
                week = []
        if week:
            week.extend([0] * (7 - len(week)))
            calendar_grid.append(week)
        return calendar_grid

    def print_month(self, language: str = "en", show_header: bool = True) -> str:
        if language == "fa":
            output = []
            if show_header:
                month_name = JalaliDate.JALALI_MONTHS_FA[self.month - 1]
                header = f"{month_name} {self.year}"
                output.extend((header.center(28), ""))
            weekdays = JalaliDate.JALALI_WEEKDAYS_FA
            output.extend(("  ".join([f"{day:>3}" for day in weekdays]), "-" * 28))
            today = JalaliDate.today()
            calendar_grid = self.get_month_calendar()
            for week in calendar_grid:
                week_str = []
                for day in week:
                    if day == 0:
                        week_str.append("   ")
                    elif self.year == today.year and self.month == today.month and day == today.day:
                        week_str.append(f"{day:>3}*")
                    else:
                        week_str.append(f"{day:>3}")
                output.append(" ".join(week_str))
            return "\n".join(output)
        output = []
        width = 20
        if show_header:
            month_name = JalaliDate.JALALI_MONTHS_EN[self.month - 1]
            header_text = f"{month_name} {self.year}"
            left_pad = (width - len(header_text)) // 2
            right_pad = width - len(header_text) - left_pad
            header_colored = " " * left_pad + f"\033[37m{header_text}\033[0m" + " " * right_pad
            output.append(header_colored)
        weekdays_abbr = ["Sh", "Ye", "Do", "Se", "Ch", "Pa", "Jo"]
        weekday_cells = []
        for i, wd in enumerate(weekdays_abbr):
            if i == 6:
                colored = f"\033[31m{wd}\033[0m"
            else:
                colored = f"\033[37m{wd}\033[0m"
            weekday_cells.append(colored)
        output.append(" ".join(weekday_cells))
        today = JalaliDate.today()
        calendar_grid = self.get_month_calendar()
        for week in calendar_grid:
            week_str = []
            for idx, day in enumerate(week):
                if day == 0:
                    cell = "  "
                else:
                    cell = f"{day:2d}"
                    if self.year == today.year and self.month == today.month and day == today.day:
                        cell = f"\033[30;47m{cell}\033[0m"
                    elif idx == 6:
                        cell = f"\033[31m{cell}\033[0m"
                week_str.append(cell)
            output.append(" ".join(week_str))
        return "\n".join(output)


class JalaliDateFormatter:
    @staticmethod
    def format(date: JalaliDate, time: datetime, fmt: str) -> str:
        output = fmt
        output = output.replace("%Y", f"{date.year:04d}")
        output = output.replace("%y", f"{date.year % 100:02d}")
        output = output.replace("%m", f"{date.month:02d}")
        output = output.replace("%B", JalaliDate.JALALI_MONTHS_EN[date.month - 1])
        output = output.replace("%b", JalaliDate.JALALI_MONTHS_EN[date.month - 1][:3])
        output = output.replace("%d", f"{date.day:02d}")
        wd = date.weekday()
        output = output.replace("%A", JalaliDate.JALALI_WEEKDAYS_EN[wd])
        output = output.replace("%a", JalaliDate.JALALI_WEEKDAYS_EN[wd][:3])
        output = output.replace("%H", f"{time.hour:02d}")
        output = output.replace("%M", f"{time.minute:02d}")
        return output.replace("%S", f"{time.second:02d}")

    @staticmethod
    def format_fa(date: JalaliDate, fmt: str = "%Y/%m/%d %H:%M:%S") -> str:
        output = fmt
        now = datetime.now()
        output = output.replace("%Y", f"{date.year:04d}")
        output = output.replace("%y", f"{date.year % 100:02d}")
        output = output.replace("%m", f"{date.month:02d}")
        output = output.replace("%B", JalaliDate.JALALI_MONTHS_FA[date.month - 1])
        output = output.replace("%b", JalaliDate.JALALI_MONTHS_FA[date.month - 1][:3])
        output = output.replace("%d", f"{date.day:02d}")
        weekday_num = date.weekday()
        output = output.replace("%A", JalaliDate.JALALI_WEEKDAYS_FA[weekday_num])
        output = output.replace("%a", JalaliDate.JALALI_WEEKDAYS_FA[weekday_num][:3])
        output = output.replace("%H", f"{now.hour:02d}")
        output = output.replace("%M", f"{now.minute:02d}")
        return output.replace("%S", f"{now.second:02d}")


def jcal(month: int | None = None, year: int | None = None, language: str = "en") -> str:
    if year is None or month is None:
        today = JalaliDate.today()
        if year is None:
            year = today.year
        if month is None:
            month = today.month
    calendar = JalaliCalendar(year, month)
    return calendar.print_month(language=language)


def jdate(fmt: str | None = None, language: str = "en") -> str:
    jdate, now = JalaliDate.today_with_time()
    if fmt is None:
        fmt = "%A %d %B %Y %H:%M:%S"
    if language == "fa":
        return JalaliDateFormatter.format_fa(jdate, now, fmt)
    return JalaliDateFormatter.format(jdate, now, fmt)


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        print(jcal())
    elif sys.argv[1] == "jdate":
        fmt = None
        lang = "en"
        for arg in sys.argv[2:]:
            if arg == "-fa":
                lang = "fa"
            elif arg.startswith("+"):
                fmt = arg[1:]
        print(jdate(fmt, language=lang))
    elif sys.argv[1] == "jcal":
        month = None
        year = None
        lang = "en"
        args = sys.argv[2:]
        for arg in args:
            if arg == "-fa":
                lang = "fa"
            elif arg.isdigit():
                if month is None:
                    month = int(arg)
                elif year is None:
                    year = int(arg)
        print(jcal(month, year, language=lang))
    else:
        month = None
        year = None
        lang = "en"
        for arg in sys.argv[1:]:
            if arg == "-fa":
                lang = "fa"
            elif arg.isdigit():
                if month is None:
                    month = int(arg)
                elif year is None:
                    year = int(arg)
        if month is not None or year is not None:
            print(jcal(month, year, language=lang))
        else:
            print(jdate(language=lang))

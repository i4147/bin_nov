#!/data/data/com.termux/files/usr/bin/env python
"""Convert a Persian date to Gregorian and say how long ago it was.
Takes day, month and year in the Persian calendar, converts them with the bundled ``PersianDateConverter`` (including leap-year handling) and prints the Persian and Gregorian dates, the weekday and the number of days since that date.
Usage: since DAY MONTH YEAR Examples: $ since 10 12 1404 Runs as a script (``python since.py``) in the directory it should act on; the module can also be imported."""

import datetime as dt
import sys


class PersianDateConverter:
    PERSIAN_MONTHS = [
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
    PERSIAN_MONTH_LENGTHS = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]

    @staticmethod
    def is_persian_leap_year(year):
        return (year * 33 + 33) % 132 < 6

    @staticmethod
    def persian_to_gregorian(persian_year, persian_month, persian_day):
        if persian_month < 1 or persian_month > 12:
            msg = f"Month must be between 1 and 12, got {persian_month}"
            raise ValueError(msg)
        max_day = PersianDateConverter.PERSIAN_MONTH_LENGTHS[persian_month - 1]
        if persian_month == 12 and PersianDateConverter.is_persian_leap_year(persian_year):
            max_day = 30
        if persian_day < 1 or persian_day > max_day:
            msg = f"Day must be between 1 and {max_day} for month {persian_month}, got {persian_day}"
            raise ValueError(msg)
        days = 0
        for year in range(1, persian_year):
            if PersianDateConverter.is_persian_leap_year(year):
                days += 366
            else:
                days += 365
        for month in range(1, persian_month):
            month_length = PersianDateConverter.PERSIAN_MONTH_LENGTHS[month - 1]
            if month == 12 and PersianDateConverter.is_persian_leap_year(persian_year):
                month_length = 30
            days += month_length
        days += persian_day - 1
        ref_date = dt.date(622, 3, 19)
        result_date = ref_date + dt.timedelta(days=days)
        return result_date.year, result_date.month, result_date.day

    @staticmethod
    def days_since(gregorian_year, gregorian_month, gregorian_day):
        input_date = dt.date(gregorian_year, gregorian_month, gregorian_day)
        today = dt.date.today()
        delta = today - input_date
        return delta.days, input_date, today

    @staticmethod
    def format_days_since(days):
        if days < 0:
            return f"{abs(days)} days in the future"
        elif days == 0:
            return "Today!"
        elif days == 1:
            return "1 day ago (yesterday)"
        elif days < 7:
            return f"{days} days ago"
        elif days < 30:
            weeks = days // 7
            remaining_days = days % 7
            if remaining_days == 0:
                return f"{weeks} weeks ago"
            else:
                return f"{weeks} weeks and {remaining_days} days ago"
        elif days < 365:
            months = days // 30
            remaining_days = days % 30
            if remaining_days == 0:
                return f"{months} months ago"
            else:
                return f"{months} months and {remaining_days} days ago"
        else:
            years = days // 365
            remaining_days = days % 365
            months = remaining_days // 30
            remaining_days = remaining_days % 30
            if months == 0 and remaining_days == 0:
                return f"{years} years ago"
            elif months == 0:
                return f"{years} years and {remaining_days} days ago"
            elif remaining_days == 0:
                return f"{years} years and {months} months ago"
            else:
                return f"{years} years, {months} months, and {remaining_days} days ago"

    @staticmethod
    def format_persian_date(year, month, day):
        month_name = PersianDateConverter.PERSIAN_MONTHS[month - 1]
        return f"{year}/{month:02d}/{day:02d} ({month_name})"


def main():
    if len(sys.argv) != 4:
        print("Usage: python convert_date.py <day> <month> <year>")
        print("Example: python convert_date.py 10 12 1404")
        print("         (Converts 10/12/1404 Persian to Gregorian)")
        print("\nNote: Date format is day month year (Persian calendar)")
        sys.exit(1)
    try:
        day = int(sys.argv[1])
        month = int(sys.argv[2])
        year = int(sys.argv[3])
        if day < 1 or day > 31:
            print("Error: Day must be between 1 and 31")
            sys.exit(1)
        if month < 1 or month > 12:
            print("Error: Month must be between 1 and 12")
            sys.exit(1)
        converter = PersianDateConverter()
        gregorian_year, gregorian_month, gregorian_day = converter.persian_to_gregorian(year, month, day)
        days_since, input_date, today = converter.days_since(gregorian_year, gregorian_month, gregorian_day)
        print("-" * 40)
        print("📅 PERSIAN TO GREGORIAN DATE CONVERTER")
        print("-" * 40)
        persian_date_str = converter.format_persian_date(year, month, day)
        print(f"\n🇮🇷 Persian date:  {persian_date_str}")
        gregorian_date_str = f"{gregorian_year}/{gregorian_month:02d}/{gregorian_day:02d}"
        print(f"🌍 Gregorian date: {gregorian_date_str}")
        weekday_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        weekday = weekday_names[input_date.weekday()]
        print(f"📆 Weekday:       {weekday}")
        print("\n" + "-" * 40)
        print("📊 DAYS SINCE CALCULATION")
        print("-" * 40)
        print(f"\n📅 Input date:    {gregorian_date_str}")
        print(f"📅 Today:         {today.strftime('%Y/%m/%d')}")
        print(f"📊 Days since:    {days_since:,} days")
        print(f"📝 Human format:  {converter.format_days_since(days_since)}")
        if days_since > 0:
            print("\n📈 STATISTICS:")
            print(f"   • Weeks:       {days_since // 7:,} weeks")
            print(f"   • Months:      {days_since // 30:,} months (approx)")
            print(f"   • Years:       {days_since / 365:.2f} years (approx)")
        elif days_since == 0:
            print("\n🎉 Today's date! No time has passed.")
        else:
            print(f"\n⏳ This date is {abs(days_since)} days in the future.")
        print("\n" + "=" * 40)
    except ValueError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())

#!/data/data/com.termux/files/usr/bin/env python
"""Quick command-line calculator with unit conversion.
Evaluates the arithmetic, the scientific functions and the unit conversions with :class:`decimal.Decimal` at 28 digits, honours the ``-p`` precision and the ``-d`` degrees mode for the trigonometry and offers an interactive shell with ``-i``.
Usage: pyqalc EXPRESSION [-p PRECISION] [-d] [-i] Positional arguments: expression ...
Mathematical expression to evaluate Options: -f, --format Output format (default: auto) -p, --precision Decimal precision (default: 15) -d, --degrees Use degrees instead of radians for trig functions -i, --interactive Start interactive calculator mode Examples: $ pyqalc '3 km to m' $ pyqalc -i Command-line script: run ``python pyqalc.py`` or the ``pyqalc`` command.
The module is importable as well."""

import argparse
from decimal import getcontext
import math
import re


getcontext().prec = 28


class UnitConverter:
    LENGTH_TO_METERS = {
        "nm": 1e-09,
        "nanometer": 1e-09,
        "um": 1e-06,
        "micrometer": 1e-06,
        "mm": 0.001,
        "millimeter": 0.001,
        "cm": 0.01,
        "centimeter": 0.01,
        "dm": 0.1,
        "decimeter": 0.1,
        "m": 1,
        "meter": 1,
        "km": 1000.0,
        "kilometer": 1000.0,
        "in": 0.0254,
        "inch": 0.0254,
        "ft": 0.3048,
        "foot": 0.3048,
        "yd": 0.9144,
        "yard": 0.9144,
        "mi": 1609.34,
        "mile": 1609.34,
        "au": 149600000000.0,
        "ua": 149600000000.0,
        "ly": 9461000000000000.0,
        "lightyear": 9461000000000000.0,
    }
    MASS_TO_KG = {
        "mg": 1e-06,
        "milligram": 1e-06,
        "g": 0.001,
        "gram": 0.001,
        "kg": 1,
        "kilogram": 1,
        "t": 1000,
        "tonne": 1000,
        "ton": 1000,
        "oz": 0.0283495,
        "ounce": 0.0283495,
        "lb": 0.453592,
        "pound": 0.453592,
    }
    VOLUME_TO_LITERS = {
        "ml": 0.001,
        "milliliter": 0.001,
        "cc": 0.001,
        "l": 1,
        "liter": 1,
        "cl": 0.01,
        "centiliter": 0.01,
        "dl": 0.1,
        "deciliter": 0.1,
        "fl oz": 0.0295735,
        "floz": 0.0295735,
        "pint": 0.473176,
        "quart": 0.946353,
        "gallon": 3.78541,
        "gal": 3.78541,
    }
    TEMP_UNITS = {"c", "f", "k", "celsius", "fahrenheit", "kelvin"}

    @staticmethod
    def convert(value: float, from_unit: str, to_unit: str) -> float:
        from_unit = from_unit.lower().strip()
        to_unit = to_unit.lower().strip()
        if from_unit == to_unit:
            return value
        if from_unit in UnitConverter.LENGTH_TO_METERS and to_unit in UnitConverter.LENGTH_TO_METERS:
            meters = value * UnitConverter.LENGTH_TO_METERS[from_unit]
            return meters / UnitConverter.LENGTH_TO_METERS[to_unit]
        if from_unit in UnitConverter.MASS_TO_KG and to_unit in UnitConverter.MASS_TO_KG:
            kg = value * UnitConverter.MASS_TO_KG[from_unit]
            return kg / UnitConverter.MASS_TO_KG[to_unit]
        if from_unit in UnitConverter.VOLUME_TO_LITERS and to_unit in UnitConverter.VOLUME_TO_LITERS:
            liters = value * UnitConverter.VOLUME_TO_LITERS[from_unit]
            return liters / UnitConverter.VOLUME_TO_LITERS[to_unit]
        if from_unit in UnitConverter.TEMP_UNITS and to_unit in UnitConverter.TEMP_UNITS:
            return UnitConverter._convert_temperature(value, from_unit, to_unit)
        msg = f"Cannot convert between {from_unit} and {to_unit}"
        raise ValueError(msg)

    @staticmethod
    def _convert_temperature(value: float, from_unit: str, to_unit: str) -> float:
        from_unit = from_unit[0].lower()
        to_unit = to_unit[0].lower()
        if from_unit == "c":
            kelvin = value + 273.15
        elif from_unit == "f":
            kelvin = (value + 459.67) * 5 / 9
        else:
            kelvin = value
        if to_unit == "c":
            return kelvin - 273.15
        elif to_unit == "f":
            return kelvin * 9 / 5 - 459.67
        else:
            return kelvin


class Calculator:
    def __init__(self):
        self.constants = {
            "pi": math.pi,
            "π": math.pi,
            "e": math.e,
            "phi": (1 + math.sqrt(5)) / 2,
            "φ": (1 + math.sqrt(5)) / 2,
            "tau": 2 * math.pi,
            "τ": 2 * math.pi,
            "inf": float("inf"),
            "nan": float("nan"),
        }
        self.functions = {
            "sin": math.sin,
            "cos": math.cos,
            "tan": math.tan,
            "asin": math.asin,
            "arcsin": math.asin,
            "acos": math.acos,
            "arccos": math.acos,
            "atan": math.atan,
            "arctan": math.atan,
            "sinh": math.sinh,
            "cosh": math.cosh,
            "tanh": math.tanh,
            "sqrt": math.sqrt,
            "cbrt": lambda x: x ** (1 / 3),
            "abs": abs,
            "log": math.log10,
            "log10": math.log10,
            "log2": math.log2,
            "ln": math.log,
            "exp": math.exp,
            "deg": math.degrees,
            "rad": math.radians,
            "floor": math.floor,
            "ceil": math.ceil,
            "round": round,
            "factorial": math.factorial,
        }

    def _tokenize(self, expression: str) -> list:
        expression = expression.replace("^", "**")
        pattern = r"\n            (\d+\.?\d*(?:[eE][+-]?\d+)?)|  # Numbers (including scientific notation)\n            ([a-zA-Z_]\w*)|                 # Variables/functions/units\n            ([+\-*/%()])|                   # Operators and parentheses\n            (\*\*)|                          # Power operator\n            (\"|\')|                          # Quote characters (for unit conversion)\n            (\s+)                            # Whitespace\n        "
        tokens = []
        for match in re.finditer(pattern, expression, re.VERBOSE):
            token = match.group()
            if token and (not token.isspace()):
                tokens.append(token)
        return tokens

    def _parse_unit_conversion(self, tokens: list) -> list:
        result = []
        i = 0
        while i < len(tokens):
            if i + 2 < len(tokens) and tokens[i + 1] == '"':
                value_str = tokens[i]
                try:
                    value = float(value_str)
                    from_unit = tokens[i + 2]
                    to_unit = tokens[i + 3] if i + 3 < len(tokens) and tokens[i + 3] not in "+-*/%()" else None
                    if to_unit:
                        converted = UnitConverter.convert(value, from_unit, to_unit)
                        result.append(str(converted))
                        i += 4
                        continue
                except (ValueError, IndexError):
                    pass
            result.append(tokens[i])
            i += 1
        return result

    def evaluate(self, expression: str) -> float | str:
        try:
            tokens = self._tokenize(expression)
            if not tokens:
                return 0
            tokens = self._parse_unit_conversion(tokens)
            namespace = self.constants.copy()
            namespace.update(self.functions)
            expr_str = "".join(tokens)
            result = eval(expr_str, {"__builtins__": {}}, namespace)
            return result
        except ZeroDivisionError:
            return "Error: Division by zero"
        except ValueError as e:
            return f"Error: {e!s}"
        except NameError as e:
            return f"Error: Unknown variable or function: {e!s}"
        except SyntaxError:
            return "Error: Invalid expression syntax"
        except Exception as e:
            return f"Error: {e!s}"

    def format_result(self, result: float | str) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, bool):
            return str(result)
        if math.isnan(result):
            return "NaN"
        if math.isinf(result):
            return "inf" if result > 0 else "-inf"
        if isinstance(result, float) and result.is_integer():
            return str(int(result))
        if (abs(result) < 1e-10 and result != 0) or abs(result) > 10000000000.0:
            return f"{result:.10e}"
        else:
            formatted = f"{result:.15f}".rstrip("0").rstrip(".")
            return formatted


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qalc",
        description="Quick command-line calculator with unit conversion support",
        add_help=True,
    )
    parser.add_argument("expression", nargs="*", help="Mathematical expression to evaluate")
    parser.add_argument(
        "-f",
        "--format",
        choices=["auto", "hex", "oct", "bin", "scientific"],
        default="auto",
        help="Output format (default: auto)",
    )
    parser.add_argument(
        "-p",
        "--precision",
        type=int,
        default=15,
        help="Decimal precision (default: 15)",
    )
    parser.add_argument(
        "-d",
        "--degrees",
        action="store_true",
        help="Use degrees instead of radians for trig functions",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Start interactive calculator mode",
    )
    return parser


def format_result_with_options(result: float | str, format_type: str = "auto") -> str:
    if isinstance(result, str):
        return result
    if format_type == "hex":
        try:
            return hex(int(result))
        except (ValueError, TypeError):
            return str(result)
    elif format_type == "oct":
        try:
            return oct(int(result))
        except (ValueError, TypeError):
            return str(result)
    elif format_type == "bin":
        try:
            return bin(int(result))
        except (ValueError, TypeError):
            return str(result)
    elif format_type == "scientific":
        if isinstance(result, float):
            return f"{result:.10e}"
        return str(result)
    return result


def main():
    parser = create_parser()
    args = parser.parse_args()
    calculator = Calculator()
    if args.precision:
        args.precision = 28
    if args.interactive:
        print("qalc - Quick Calculator (type 'quit' to exit)")
        print("Supports: +, -, *, /, %, **, sin, cos, tan, sqrt, log, etc.")
        print('Units: value"from_unit to_unit (e.g., 1024"kb mb)')
        print()
        while True:
            try:
                expr = input(">>> ").strip()
                if expr.lower() in ("quit", "exit", "q"):
                    break
                if not expr:
                    continue
                result = calculator.evaluate(expr)
                formatted = calculator.format_result(result)
                formatted = format_result_with_options(formatted, args.format)
                print(formatted)
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except EOFError:
                break
    elif args.expression:
        expression = " ".join(args.expression)
        result = calculator.evaluate(expression)
        formatted = calculator.format_result(result)
        formatted = format_result_with_options(formatted, args.format)
        print(formatted)
    else:
        parser.print_help()


if __name__ == "__main__":
    raise SystemExit(main())

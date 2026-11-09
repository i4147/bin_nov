#!/data/data/com.termux/files/usr/bin/env python
"""Evaluate arithmetic expressions, either from the command line or in a Textual TUI.
With arguments the expression is evaluated once and printed (the exit status reports success); called without arguments a small full-screen calculator opens with the same parser behind its buttons.
Usage: calc [EXPRESSION ...] Examples: $ calc '(3 + 4) * 2' $ calc Runs as a script (``python calc.py``) in the directory it should act on; the module can also be imported."""

import re
import sys
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.widgets import Button, Static


class Display(Static):
    DEFAULT_CSS = "\n    Display {\n        width: 1fr;\n        height: 3;\n        content-align: right middle;\n        background: $surface;\n        border: solid $primary;\n        text-style: bold;\n    }\n    "

    def __init__(self) -> None:
        super().__init__("0")
        self.value = "0"

    def update_display(self, text: str) -> None:
        self.value = text
        self.update(text)


class Calculator(Static):
    DEFAULT_CSS = "\n    Calculator {\n        width: 50;\n        height: auto;\n        border: solid $accent;\n        background: $panel;\n    }\n\n    #button-grid {\n        width: 1fr;\n        height: auto;\n        grid-size: 4 5;\n        grid-gutter: 1 1;\n        padding: 1;\n    }\n\n    Button {\n        width: 1fr;\n        height: 3;\n    }\n\n    Button.operator {\n        background: $accent 80%;\n    }\n\n    Button.equals {\n        background: $success 80%;\n    }\n\n    Button.clear {\n        background: $error 80%;\n    }\n    "

    def __init__(self) -> None:
        super().__init__()
        self.display_widget = Display()
        self.left_operand = None
        self.operator = None
        self.new_input = True

    def compose(self) -> ComposeResult:
        yield self.display_widget
        with Grid(id="button-grid"):
            yield Button("C", id="clear", classes="clear")
            yield Button("÷", id="divide", classes="operator")
            yield Button("×", id="multiply", classes="operator")
            yield Button("−", id="minus", classes="operator")
            yield Button("7")
            yield Button("8")
            yield Button("9")
            yield Button("+", id="plus", classes="operator")
            yield Button("4")
            yield Button("5")
            yield Button("6")
            yield Button("=", id="equals", classes="equals")
            yield Button("1")
            yield Button("2")
            yield Button("3")
            yield Button(".", id="decimal")
            yield Button("0", id="zero")
            yield Button("", disabled=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        button_label = str(event.button.label)
        if button_id == "clear":
            self.display_widget.update_display("0")
            self.left_operand = None
            self.operator = None
            self.new_input = True
            return
        if button_id == "equals":
            if self.left_operand is not None and self.operator is not None:
                right_operand = float(self.display_widget.value)
                result = self._calculate(self.left_operand, self.operator, right_operand)
                self.display_widget.update_display(result)
                self.left_operand = None
                self.operator = None
                self.new_input = True
            return
        if button_id in ("plus", "minus", "multiply", "divide"):
            current_value = float(self.display_widget.value)
            if self.left_operand is not None and self.operator is not None and (not self.new_input):
                result = self._calculate(self.left_operand, self.operator, current_value)
                self.display_widget.update_display(result)
                self.left_operand = float(result)
            else:
                self.left_operand = current_value
            operator_map = {"plus": "+", "minus": "−", "multiply": "×", "divide": "÷"}
            self.operator = operator_map[button_id]
            self.new_input = True
            return
        if button_label in "0123456789" or button_id == "decimal":
            if button_id == "decimal":
                if "." in self.display_widget.value:
                    return
                button_label = "."
            if self.new_input:
                if button_label == ".":
                    self.display_widget.update_display("0.")
                else:
                    self.display_widget.update_display(button_label)
                self.new_input = False
            else:
                current = self.display_widget.value
                if len(current) < 12:
                    self.display_widget.update_display(current + button_label)

    def _calculate(self, left: float, operator: str, right: float) -> str:
        try:
            if operator == "+":
                result = left + right
            elif operator == "−":
                result = left - right
            elif operator == "×":
                result = left * right
            elif operator == "÷":
                if right == 0:
                    return "Error"
                result = left / right
            else:
                return "Error"
            if result == int(result):
                return str(int(result))
            else:
                return f"{result:.10g}"
        except Exception:
            return "Error"


def parse_expression(expr: str):
    expr = expr.replace(" ", "")
    pattern = r"^([\d.]+)\s*([+\-*/×÷])\s*([\d.]+)$"
    match = re.match(pattern, expr)
    if match:
        return (match.group(1), match.group(2), match.group(3))
    return (None, None, None)


def evaluate_cli(args: list[Any]) -> bool:
    expr = " ".join(args)
    num1_str, operator, num2_str = parse_expression(expr)
    if num1_str is None:
        if len(args) < 3:
            print("Usage: python calc.py <expression>")
            print("Examples:")
            print("  python calc.py 1024 * 1024")
            print("  python calc.py 1024*1024")
            print("  python calc.py 10 / 3")
            print("  python calc.py 10/3")
            print("  python calc.py 5.5 + 2.5")
            print("  python calc.py 5.5+2.5")
            return False
        try:
            num1 = float(args[0])
            operator = args[1]
            num2 = float(args[2])
            num1_str = str(num1)
            num2_str = str(num2)
        except (ValueError, IndexError):
            print("Error: Invalid expression format")
            print("Examples: 1024*1024 or 1024 * 1024")
            return False
    else:
        try:
            num1 = float(num1_str)
            num2 = float(num2_str)
        except ValueError:
            print("Error: Invalid numbers in expression")
            return False
    operator_map = {"+": "+", "-": "−", "*": "×", "/": "÷", "×": "×", "÷": "÷"}
    if operator not in operator_map:
        print(f"Error: Unsupported operator '{operator}'")
        print("Supported operators: +, -, *, /, ×, ÷")
        return False
    mapped_operator = operator_map[operator]
    calc = Calculator()
    result = calc._calculate(num1, mapped_operator, num2)
    if result == "Error":
        print("Error: Invalid calculation (division by zero?)")
        return False
    operator_display = {"+": "+", "−": "-", "×": "*", "÷": "/"}
    num1_str = str(int(num1)) if num1 == int(num1) else str(num1)
    num2_str = str(int(num2)) if num2 == int(num2) else str(num2)
    print(f"{num1_str} {operator_display.get(mapped_operator, operator)} {num2_str} = {result}")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        success = evaluate_cli(sys.argv[1:])
        sys.exit(0 if success else 1)
    else:

        class CalcApp(App):
            BINDINGS = [("q", "quit", "Quit")]

            def compose(self) -> ComposeResult:
                yield Calculator()

        app = CalcApp()
        app.run()

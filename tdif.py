#!/data/data/com.termux/files/usr/bin/env python
"""Compare two files and show their differences in a textual TUI."""

import argparse
import difflib
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal, ScrollableContainer
from textual.widgets import Footer, Header, Label, Static


class DiffLine(Static):
    def __init__(self, text: str, line_type: str, line_num: int | None = None) -> None:
        self.raw_text = text
        self.line_type = line_type
        self.line_num = line_num
        display_text = self._create_display_text()
        super().__init__(display_text)
        self._apply_styling()

    def _create_display_text(self) -> str:
        prefix = f"{self.line_num:4d}" if self.line_num is not None else "    "
        safe_text = self.raw_text.replace("[", "[]")
        if self.line_type == " ":
            return f"{prefix}  {safe_text}"
        if self.line_type == "-":
            return f"{prefix} - {safe_text}"
        if self.line_type == "+":
            return f"{prefix} + {safe_text}"
        if self.line_type == "?":
            return f"{prefix} ? {safe_text}"
        return f"{prefix}   {safe_text}"

    def _apply_styling(self) -> None:
        if self.line_type == " ":
            self.styles.background = Color(30, 30, 30)
            self.styles.color = Color(200, 200, 200)
        elif self.line_type == "-":
            self.styles.background = Color(80, 30, 30)
            self.styles.color = Color(255, 150, 150)
        elif self.line_type == "+":
            self.styles.background = Color(30, 80, 30)
            self.styles.color = Color(150, 255, 150)
        elif self.line_type == "?":
            self.styles.background = Color(60, 60, 30)
            self.styles.color = Color(255, 255, 150)


class DiffPanel(ScrollableContainer):
    def __init__(self, title: str, lines: list[tuple[str, str, int]]) -> None:
        super().__init__()
        self.panel_title = title
        self.lines = lines

    def compose(self) -> ComposeResult:
        yield Label(f"[bold]{self.panel_title}[/bold]", classes="panel-title")
        for text, line_type, line_num in self.lines:
            yield DiffLine(text, line_type, line_num)

    def on_mount(self) -> None:
        self.can_focus = True
        self.can_focus_children = True


class DiffViewerApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    .panel-title {
        padding: 1;
        text-align: center;
        background: $primary;
        color: $text;
        text-style: bold;
        width: 100%;
    }
    DiffPanel {
        border: solid $primary;
        height: 100%;
        width: 50%;
        overflow-y: auto;
    }
    DiffPanel:focus {
        border: double $secondary;
    }
    DiffLine {
        padding: 0 1;
        width: 100%;
        height: 1;
    }
    Horizontal {
        height: 1fr;
    }
    Header {
        background: $primary-lighten-1;
    }
    Footer {
        background: $primary-darken-1;
    }
    """
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("f1", "toggle_panel", "Focus Next Panel"),
        ("ctrl+c", "quit", "Quit"),
        ("/", "search", "Search"),
        ("n", "next_search", "Next Result"),
    ]

    def __init__(self, file1: str, file2: str) -> None:
        super().__init__()
        self.file1 = Path(file1)
        self.file2 = Path(file2)
        self.left_lines = []
        self.right_lines = []
        self.search_term = ""
        self.search_results = []

    def read_file(self, path: Path) -> list[str]:
        try:
            with Path(path).open(encoding="utf-8") as f:
                return f.readlines()
        except UnicodeDecodeError:
            try:
                with Path(path).open(encoding="latin-1") as f:
                    return f.readlines()
            except Exception as e:
                self.notify(f"Error reading {path}: {e}", severity="error")
                return []
        except Exception as e:
            self.notify(f"Error reading {path}: {e}", severity="error")
            return []

    def compute_diff(self) -> None:
        lines1 = self.read_file(self.file1)
        lines2 = self.read_file(self.file2)
        lines1 = [line.rstrip("\n") for line in lines1]
        lines2 = [line.rstrip("\n") for line in lines2]
        differ = difflib.Differ()
        diff = list(differ.compare(lines1, lines2))
        left_line_num = 0
        right_line_num = 0
        for line in diff:
            line_type = line[0] if line else " "
            content = line[2:] if len(line) > 2 else ""
            if line_type == " ":
                left_line_num += 1
                right_line_num += 1
                self.left_lines.append((content, line_type, left_line_num))
                self.right_lines.append((content, line_type, right_line_num))
            elif line_type == "-":
                left_line_num += 1
                self.left_lines.append((content, line_type, left_line_num))
                self.right_lines.append(("", " ", None))
            elif line_type == "+":
                right_line_num += 1
                self.left_lines.append(("", " ", None))
                self.right_lines.append((content, line_type, right_line_num))
            elif line_type == "?":
                self.left_lines.append((content, line_type, None))
                self.right_lines.append((content, line_type, None))

    def compose(self) -> ComposeResult:
        yield Header()
        self.compute_diff()
        with Horizontal():
            left_panel = DiffPanel(str(self.file1), self.left_lines)
            right_panel = DiffPanel(str(self.file2), self.right_lines)
            yield left_panel
            yield right_panel
        yield Footer()

    def on_mount(self) -> None:
        panels = self.query(DiffPanel)
        if panels:
            panels.first().focus()

    def action_toggle_panel(self) -> None:
        current = self.focused
        if current and isinstance(current, DiffPanel):
            panels = list(self.query(DiffPanel))
            for i, panel in enumerate(panels):
                if panel == current:
                    next_panel = panels[(i + 1) % len(panels)]
                    next_panel.focus()
                    break
        else:
            panels = self.query(DiffPanel)
            if panels:
                panels.first().focus()

    def action_search(self) -> None:
        def on_input(submitted_text: str) -> None:
            if submitted_text:
                self.search_term = submitted_text
                self.highlight_search_results()

        self.push_screen("input", on_input, title="Search", instructions="Enter text to search for:")

    def highlight_search_results(self) -> None:
        if not self.search_term:
            return
        for line in self.query(DiffLine):
            line.styles.background = None
        for line in self.query(DiffLine):
            if self.search_term.lower() in line.raw_text.lower():
                line.styles.background = Color(70, 70, 150)

    def action_next_search(self) -> None:
        self.notify("Next search result (feature not fully implemented)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two files and show their differences",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s file1.txt file2.txt
  %(prog)s --help
        """,
    )
    parser.add_argument("file1", help="First file to compare")
    parser.add_argument("file2", help="Second file to compare")
    args = parser.parse_args()
    file1 = Path(args.file1)
    file2 = Path(args.file2)
    if not file1.exists():
        print(f"Error: File '{file1}' does not exist")
        return 1
    if not file2.exists():
        print(f"Error: File '{file2}' does not exist")
        return 1
    app = DiffViewerApp(str(file1), str(file2))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

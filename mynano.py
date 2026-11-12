#!/data/data/com.termux/files/usr/bin/env python
"""A small Textual-based text editor with readline tab completion.
Opens the file given on the command line (or an empty buffer), provides quit, save and save-as actions plus a key binding list, and hooks ``readline`` with ``rlcompleter`` for the completion popup.
Usage: mynano [FILE] Examples: $ mynano notes.txt Runs as a script (``python mynano.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import readline
import rlcompleter
import sys

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.log import TextLog
from textual.widgets import Footer, Header, TextEditor


class BasicEditor(App):
    BINDINGS = [
        ("o", "open_file", "Open"),
        ("s", "save_file", "Save"),
        ("q", "app_quit", "Quit"),
    ]

    def __init__(self, filename: str | None = None) -> None:
        super().__init__()
        self.filename = filename
        self.is_dirty = False

    def setup_readline(self) -> None:
        readline.parse_and_bind("tab: complete")
        readline.set_completer(rlcompleter.Completer(namespace=sys.modules).complete)

    def compose(self) -> ComposeResult:
        self.setup_readline()
        yield Header()
        with Container():
            yield TextEditor(id="editor", name="editor")
            yield TextLog(id="log", height=2, panel=True, label="Status")
        yield Footer()

    def on_mount(self) -> None:
        editor = self.query_one(TextEditor)
        log = self.query_one(TextLog)
        if self.filename:
            try:
                with Path(self.filename).open("r", encoding="utf-8") as f:
                    editor.text = f.read()
                self.title = f"Basic Editor - {self.filename}"
                log.write(f"Opened file: {self.filename}")
            except FileNotFoundError:
                log.write(f"Error: File '{self.filename}' not found.")
                self.title = "Basic Editor - New File"
            except Exception as e:
                log.write(f"Error opening file: {e}")
                self.title = "Basic Editor - New File"
        else:
            log.write("New file. Use Ctrl+O to open or Ctrl+S to save.")

    def action_open_file(self) -> None:
        log = self.query_one(TextLog)
        editor = self.query_one(TextEditor)
        try:
            filename = input("Enter filename to open: ")
            if filename:
                self.filename = filename
                with Path(self.filename).open("r", encoding="utf-8") as f:
                    editor.text = f.read()
                self.title = f"Basic Editor - {self.filename}"
                log.write(f"Opened file: {self.filename}")
                self.is_dirty = False
            else:
                log.write("Open cancelled.")
        except FileNotFoundError:
            log.write(f"Error: File '{self.filename}' not found.")
        except Exception as e:
            log.write(f"Error opening file: {e}")

    def action_save_file(self) -> None:
        log = self.query_one(TextLog)
        editor = self.query_one(TextEditor)
        if not self.filename:
            try:
                filename = input("Enter filename to save as: ")
                if filename:
                    self.filename = filename
                else:
                    log.write("Save cancelled.")
                    return
            except Exception as e:
                log.write(f"Error getting filename: {e}")
                return
        try:
            Path(self.filename).write_text(editor.text, encoding="utf-8")
            self.title = f"Basic Editor - {self.filename}"
            log.write(f"Saved file: {self.filename}")
            self.is_dirty = False
        except Exception as e:
            log.write(f"Error saving file: {e}")

    def action_app_quit(self) -> None:
        log = self.query_one(TextLog)
        editor = self.query_one(TextEditor)
        if editor.text and self.is_dirty:
            try:
                confirm = input("You have unsaved changes. Are you sure you want to quit? (y/n): ")
                if confirm.lower() == "y":
                    self.exit()
                else:
                    log.write("Quit cancelled.")
            except Exception as e:
                log.write(f"Error during quit confirmation: {e}")
        else:
            self.exit()

    def on_text_editor_changed(self, event: TextEditor.Changed) -> None:
        self.is_dirty = True
        self.query_one(Footer).key_display = [
            ("o", "Open", "primary"),
            ("s", "Save", "primary"),
            ("q", "Quit", "primary"),
        ]
        if self.is_dirty:
            self.query_one(Footer).key_display.append(("Ctrl+S", "Save", "warning"))


if __name__ == "__main__":
    initial_filename = sys.argv[1] if len(sys.argv) > 1 else None
    app = BasicEditor(filename=initial_filename)
    app.run()

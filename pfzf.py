#!/data/data/com.termux/files/usr/bin/env python
"""A dependency-free fuzzy finder with an interactive prompt.
Scores and ranks the lines read from stdin (or a built-in demo list) while you type, rendering the matches with the matched characters highlighted; arrow keys move the selection and Enter prints it.
``--filter QUERY`` prints the ranked matches non-interactively.
Usage: pfzf [--filter QUERY] Examples: $ ls | pfzf $ pfzf --filter btn Runs as a script (``python pfzf.py``) in the directory it should act on; the module can also be imported.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from typing import Any, Iterable, Sequence


def fuzzy_score(query: str, text: str):
    if not query:
        return (0, [])
    query_lower = query.lower()
    text_lower = text.lower()
    positions = []
    q_idx = 0
    t_idx = 0
    while q_idx < len(query) and t_idx < len(text):
        qc = query_lower[q_idx]
        found = False
        for i in range(t_idx, len(text)):
            if text_lower[i] == qc:
                positions.append(i)
                t_idx = i + 1
                q_idx += 1
                found = True
                break
        if not found:
            return (float("-inf"), [])
    if q_idx < len(query):
        return (float("-inf"), [])
    score = 0
    score += len(positions) * 10
    consecutive = 0
    for i in range(1, len(positions)):
        if positions[i] == positions[i - 1] + 1:
            consecutive += 1
            score += 20 * consecutive
        else:
            consecutive = 0
    separators = set(" /.-_\\")
    for pos in positions:
        if pos == 0 or text[pos - 1] in separators:
            score += 15
        elif pos > 0 and text[pos].isupper() and text[pos - 1].islower():
            score += 10
    for i, pos in enumerate(positions):
        if query[i] == text[pos]:
            score += 5
    for i in range(1, len(positions)):
        score -= (positions[i] - positions[i - 1] - 1) * 2
    score -= len(text) * 0.5
    return (score, positions)


def render_match(text: str, positions: Iterable[Any], width: int, selected=False) -> str:
    RESET = "\033[0m"
    SELECTED = "\033[7m"
    MATCH = "\033[1;33m"
    SELECTED_MATCH = "\033[1;37m\033[45m"
    display = text
    if len(display) > width - 3:
        display = display[: width - 4] + "…"
    result = []
    if selected:
        result.append(SELECTED)
    pos_set = set(positions)
    for i, ch in enumerate(display):
        if i in pos_set:
            if selected:
                result.append(SELECTED_MATCH)
                result.append(ch)
                result.append(SELECTED)
            else:
                result.append(MATCH)
                result.append(ch)
                result.append(RESET)
                if selected:
                    result.append(SELECTED)
        else:
            result.append(ch)
    if selected:
        result.append(RESET)
    visible_len = len(display)
    result.append(" " * max(0, width - visible_len - 1))
    return "".join(result)


def get_terminal_size():
    try:
        import shutil

        return shutil.get_terminal_size()
    except:
        return os.terminal_size((80, 24))


def read_key() -> str | None:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            import select

            if select.select([sys.stdin], [], [], 0.01)[0]:
                seq = os.read(fd, 2)
                if seq == b"[A":
                    return "UP"
                elif seq == b"[B":
                    return "DOWN"
                elif seq == b"[C":
                    return "RIGHT"
                elif seq == b"[D":
                    return "LEFT"
                elif seq == b"[3~":
                    return "DELETE"
                elif seq == b"[H":
                    return "HOME"
                elif seq == b"[F":
                    return "END"
                elif seq.startswith(b"["):
                    extra = b""
                    while True:
                        b = os.read(fd, 1)
                        extra += b
                        if b.isalpha() or b == b"~":
                            break
                    return f"ESC[{seq[1:].decode()}{extra.decode()}"
                return f"ESC{seq.decode()}"
            return "ESC"
        elif ch in {b"\r", b"\n"}:
            return "ENTER"
        elif ch == b"\t":
            return "TAB"
        elif ch == b"\x7f":
            return "BACKSPACE"
        elif ch == b"\x03":
            return "CTRL_C"
        elif ch == b"\x04":
            return "CTRL_D"
        elif ch == b"\x15":
            return "CTRL_U"
        elif ch == b"\x17":
            return "CTRL_W"
        elif ch == b"\x01":
            return "HOME"
        elif ch == b"\x05":
            return "END"
        elif ch == b"\x0b":
            return "CTRL_K"
        elif ch == b"\x1c":
            return "CTRL_SLASH"
        elif ch in {b"\x00", b"\xe0"}:
            return None
        else:
            try:
                return ch.decode("utf-8")
            except:
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def move_cursor(row: int, col=0) -> None:
    sys.stdout.write(f"\033[{row + 1};{col + 1}H")
    sys.stdout.flush()


def clear_line() -> None:
    sys.stdout.write("\033[2K\r")
    sys.stdout.flush()


def fzf(items: list[Any], prompt="> ", multi=False, preview=None, preview_window="right:50%"):
    items = list(items)
    if not items:
        return [] if multi else None
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    query = ""
    cursor_pos = 0
    selected_idx = 0
    scroll_offset = 0
    selected_items = set()

    def get_matches(q: Any):
        if not q:
            return [(item, 0, []) for item in items]
        scored = []
        for item in items:
            score, positions = fuzzy_score(q, item)
            if score > float("-inf"):
                scored.append((score, item, positions))
        scored.sort(key=lambda x: (-x[0], items.index(x[1])))
        return [(item, score, positions) for score, item, positions in scored]

    def redraw() -> None:
        cols, rows = get_terminal_size()
        height = rows - 2
        matches = get_matches(query)
        total = len(matches)
        nonlocal scroll_offset, selected_idx
        if selected_idx < scroll_offset:
            scroll_offset = selected_idx
        elif selected_idx >= scroll_offset + height:
            scroll_offset = selected_idx - height + 1
        scroll_offset = max(0, min(scroll_offset, max(0, total - height)))
        selected_idx = max(0, min(selected_idx, total - 1)) if total > 0 else 0
        clear_screen()
        header = f" {total}/{len(items)} "
        if multi:
            header += f" [{len(selected_items)} selected] "
        header += f"  (Ctrl-C to cancel, Enter to select"
        if multi:
            header += ", Tab to multi-select"
        header += ")"
        if len(header) > cols:
            header = header[: cols - 1]
        sys.stdout.write(f"\033[90m{header}\033[0m\n")
        visible = matches[scroll_offset : scroll_offset + height]
        for i, (item, _score, positions) in enumerate(visible):
            actual_idx = scroll_offset + i
            is_selected = actual_idx == selected_idx
            in_multi = item in selected_items
            prefix = ""
            if multi:
                prefix = "[+] " if in_multi else "[ ] "
            line_width = cols - len(prefix) - 1
            line = render_match(item, positions, line_width, selected=is_selected)
            if is_selected and multi and in_multi:
                sys.stdout.write(f"\033[36m{prefix}\033[0m{line}\n")
            else:
                sys.stdout.write(f"{prefix}{line}\n")
        drawn = len(visible)
        for _ in range(height - drawn):
            sys.stdout.write("\033[2K\n")
        sys.stdout.write(f"\033[{rows};1H")
        sys.stdout.write("\033[2K")
        display_query = query
        sys.stdout.write(f"\033[1m{prompt}\033[0m{display_query}")
        cursor_col = len(prompt) + cursor_pos + 1
        sys.stdout.write(f"\033[{rows};{cursor_col}H")
        sys.stdout.flush()

    try:
        while True:
            redraw()
            key = read_key()
            if key is None:
                continue
            matches = get_matches(query)
            if key in {"CTRL_C", "CTRL_D", "ESC"}:
                return [] if multi else None
            elif key == "ENTER":
                if matches and selected_idx < len(matches):
                    item = matches[selected_idx][0]
                    if multi:
                        if item in selected_items:
                            selected_items.remove(item)
                        else:
                            selected_items.add(item)
                        if not selected_items:
                            return []
                        result = [it for it in items if it in selected_items]
                        return result
                    else:
                        return item
                return [] if multi else None
            elif key == "TAB" and multi:
                if matches and selected_idx < len(matches):
                    item = matches[selected_idx][0]
                    if item in selected_items:
                        selected_items.remove(item)
                    else:
                        selected_items.add(item)
            elif key == "UP":
                selected_idx = max(0, selected_idx - 1)
            elif key == "DOWN":
                selected_idx = min(len(matches) - 1, selected_idx + 1)
            elif key in {"HOME", "CTRL_A"}:
                cursor_pos = 0
            elif key in {"END", "CTRL_E"}:
                cursor_pos = len(query)
            elif key == "LEFT":
                cursor_pos = max(0, cursor_pos - 1)
            elif key == "RIGHT":
                cursor_pos = min(len(query), cursor_pos + 1)
            elif key == "BACKSPACE":
                if cursor_pos > 0:
                    query = query[: cursor_pos - 1] + query[cursor_pos:]
                    cursor_pos -= 1
                    selected_idx = 0
                    scroll_offset = 0
            elif key == "DELETE":
                if cursor_pos < len(query):
                    query = query[:cursor_pos] + query[cursor_pos + 1 :]
                    selected_idx = 0
            elif key == "CTRL_U":
                query = query[cursor_pos:]
                cursor_pos = 0
                selected_idx = 0
                scroll_offset = 0
            elif key == "CTRL_K":
                query = query[:cursor_pos]
                selected_idx = 0
            elif key == "CTRL_W":
                if cursor_pos > 0:
                    pos = cursor_pos - 1
                    while pos >= 0 and query[pos].isspace():
                        pos -= 1
                    while pos >= 0 and not query[pos].isspace():
                        pos -= 1
                    query = query[: pos + 1] + query[cursor_pos:]
                    cursor_pos = pos + 1
                    selected_idx = 0
                    scroll_offset = 0
            elif key == "CTRL_SLASH":
                pass
            elif len(key) == 1 and key.isprintable():
                query = query[:cursor_pos] + key + query[cursor_pos:]
                cursor_pos += 1
                selected_idx = 0
                scroll_offset = 0
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def fzf_filter(query: str, items: list[Any]):
    results = []
    for item in items:
        score, positions = fuzzy_score(query, item)
        if score > float("-inf"):
            results.append((score, item, positions))
    results.sort(key=lambda x: -x[0])
    return [item for _, item, _ in results]


if __name__ == "__main__":
    test_items = [
        "src/components/Button.tsx",
        "src/components/Modal.tsx",
        "src/utils/helpers.ts",
        "src/utils/api.ts",
        "tests/Button.test.tsx",
        "tests/Modal.test.tsx",
        "package.json",
        "tsconfig.json",
        "README.md",
        ".gitignore",
        "src/styles/main.css",
        "src/styles/theme.css",
        "docker-compose.yml",
        "Dockerfile",
        "scripts/build.sh",
        "scripts/deploy.sh",
        "src/pages/Home.tsx",
        "src/pages/About.tsx",
        "src/pages/Contact.tsx",
        "src/hooks/useAuth.ts",
        "src/hooks/useFetch.ts",
        "src/types/index.ts",
        "src/context/AppContext.tsx",
    ]
    if not sys.stdin.isatty():
        piped_items = [line.rstrip("\n") for line in sys.stdin]
        if piped_items:
            test_items = piped_items
    if len(sys.argv) > 1 and sys.argv[1] == "--filter":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        results = fzf_filter(query, test_items)
        for r in results:
            print(r)
    else:
        result = fzf(test_items, multi="--multi" in sys.argv)
        if result is None:
            sys.exit(1)
        elif isinstance(result, list):
            for r in result:
                print(r)
        else:
            print(result)

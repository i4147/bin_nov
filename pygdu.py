#!/data/data/com.termux/files/usr/bin/env python
"""Interactive disk-usage browser in the terminal.
Scans the given directory with a worker pool, shows the entries as a navigable list with size bars and lets you descend into folders or go back with the arrow keys and Enter/Escape.
Usage: pygdu [DIRECTORY] Examples: $ pygdu ~ Runs as a script (``python pygdu.py``) in the directory it should act on; the module can also be imported.
"""

from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
import sys
import termios
import tty
from typing import Final

from dh import fsz
from loguru import logger

POOL_SIZE: Final[int] = 8
BAR_WIDTH: Final[int] = 10
RESET: Final[str] = "\x1b[0m"
BOLD: Final[str] = "\x1b[1m"
REVERSE: Final[str] = "\x1b[7m"
GREEN: Final[str] = "\x1b[32m"
YELLOW: Final[str] = "\x1b[33m"
MAGENTA: Final[str] = "\x1b[35m"
CYAN: Final[str] = "\x1b[36m"
KEY_UP: Final[str] = "\x1b[A"
KEY_DOWN: Final[str] = "\x1b[B"
KEY_RIGHT: Final[str] = "\x1b[C"
KEY_LEFT: Final[str] = "\x1b[D"
KEY_ESC: Final[str] = "\x1b"


@dataclass(slots=True)
class FSItem:
    path: Path
    name: str
    is_dir: bool
    size: int = 0
    children: list[FSItem] = field(default_factory=list)
    parent: FSItem | None = None
    flag: str = " "


def _scan_recursive(path_str: str) -> FSItem:
    path = Path(path_str)
    try:
        if path.is_symlink():
            return FSItem(path=path, name=path.name, is_dir=False, size=0, flag="@")
        if path.is_file():
            try:
                return FSItem(
                    path=path,
                    name=path.name,
                    is_dir=False,
                    size=path.stat().st_size,
                )
            except OSError:
                return FSItem(path=path, name=path.name, is_dir=False, size=0, flag="!")
        dir_item = FSItem(path=path, name=path.name, is_dir=True)
        try:
            for child in path.iterdir():
                child_item = _scan_recursive(str(child))
                child_item.parent = dir_item
                dir_item.children.append(child_item)
                dir_item.size += child_item.size
        except OSError:
            dir_item.flag = "!"
        if not dir_item.children and dir_item.flag == " ":
            dir_item.flag = "e"
        dir_item.children.sort(key=lambda x: x.size, reverse=True)
        return dir_item
    except OSError:
        return FSItem(path=path, name=path.name, is_dir=False, size=0, flag="!")


class DiskAnalyzer:
    def __init__(self, root_path: Path) -> None:
        self.root_path: Path = root_path.resolve()

    def scan(self) -> FSItem:
        root_item = FSItem(path=self.root_path, name=str(self.root_path), is_dir=True)
        try:
            top_level: list[Path] = list(self.root_path.iterdir())
        except OSError:
            root_item.flag = "!"
            return root_item
        results: list[FSItem] = []
        with Pool(processes=POOL_SIZE) as pool:
            async_results = [pool.apply_async(_scan_recursive, (str(p),)) for p in top_level]
            for ar in async_results:
                try:
                    results.append(ar.get())
                except Exception as exc:
                    logger.warning("Worker failed: {}", exc)
        for child in results:
            child.parent = root_item
            root_item.children.append(child)
            root_item.size += child.size
        root_item.children.sort(key=lambda x: x.size, reverse=True)
        return root_item


def get_progress_bar(item_size: int, max_size: int) -> str:
    if max_size == 0:
        return f"[{' ' * BAR_WIDTH}]"
    ratio = item_size / max_size
    filled = int(ratio * BAR_WIDTH)
    filled = max(0, min(BAR_WIDTH, filled))
    return f"[{'#' * filled}{' ' * (BAR_WIDTH - filled)}]"


def get_key() -> str:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == KEY_ESC:
            ch += sys.stdin.read(2)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def clear_screen() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def draw_interface(current_node: FSItem, selected_idx: int) -> None:
    lines: list[str] = []
    lines.append(f"{BOLD}Directory: {current_node.path}{RESET}\n")
    max_size = max((c.size for c in current_node.children), default=1)
    for idx, item in enumerate(current_node.children):
        size_str = fsz(item.size)
        bar_str = get_progress_bar(item.size, max_size)
        flag_str = f"[{item.flag}]" if item.flag != " " else "   "
        name_str = f"{item.name}/" if item.is_dir else item.name
        if idx == selected_idx:
            line = f"{REVERSE}{size_str}  {bar_str}  {flag_str}  {name_str}{RESET}"
        else:
            line = (
                f"{GREEN}{size_str}{RESET}  "
                f"{YELLOW}{bar_str}{RESET}  "
                f"{MAGENTA}{flag_str}{RESET}  "
                f"{(CYAN if item.is_dir else RESET)}{name_str}{RESET}"
            )
        lines.append(line)
    clear_screen()
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def main() -> int:
    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path()
    if not target_dir.is_dir():
        logger.error("{} is not a valid directory.", target_dir)
        return 1
    print("Scanning {} targets efficiently...", target_dir.resolve())
    analyzer = DiskAnalyzer(target_dir)
    current_node: FSItem = analyzer.scan()
    selected_idx: int = 0
    while True:
        draw_interface(current_node, selected_idx)
        key = get_key()
        if key in ("q", "\x03"):
            clear_screen()
            break
        if key in (KEY_UP, "k"):
            if selected_idx > 0:
                selected_idx -= 1
        elif key in (KEY_DOWN, "j"):
            if selected_idx < len(current_node.children) - 1:
                selected_idx += 1
        elif key in (KEY_RIGHT, "l", "\r"):
            if current_node.children:
                target = current_node.children[selected_idx]
                if target.is_dir and target.children:
                    current_node = target
                    selected_idx = 0
        elif key in (KEY_LEFT, "h", KEY_ESC) and current_node.parent is not None:
            old_node = current_node
            current_node = current_node.parent
            try:
                selected_idx = current_node.children.index(old_node)
            except ValueError:
                selected_idx = 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

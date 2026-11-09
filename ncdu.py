#!/data/data/com.termux/files/usr/bin/env python
"""An ``ncdu``-style disk usage browser in Python.
Scans the directory in parallel (eight workers, hardlink dedup, optional one-file-system and apparent size), shows the tree in a curses browser sorted by size, name, item count or mtime, and can export or import the ncdu JSON format (``-o``/``-f``) without the UI.
Usage: ncdu [PATH] [-o export.json] [-f export.json] [-x] [--si] Positional arguments: path Directory to scan Options: -o, --output Export to ncdu JSON v2 and exit (no UI) -f, --file Load a previous export instead of scanning -x, --one-file-system Do not cross filesystem boundaries -L, --follow-symlinks Follow symlinks (dangerous: loops) --exclude Exclude entries with this exact name (repeatable) --apparent-size Show apparent size instead of disk usage --si Use powers of 1000 instead of 1024 -v, --verbose Increase verbosity (-v, -vv, -vvv) -q, --quiet Only warnings/errors --log Log file used while the TUI is active (default: ncdu2.log) ...
plus 3 more; run ``ncdu --help`` for the full list.
Examples: $ ncdu ~ -o usage.json Command-line script: run ``python ncdu.py`` or the ``ncdu`` command.
The module is importable as well."""

import argparse
import curses
from dataclasses import dataclass, field
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any, Iterator

from dh import MAX_WORKERS
from loguru import logger


__version__ = "2.0.0"
PROGNAME = "ncdu2.py"
LOG = logger


def setup_logging(verbosity: int, logfile: Path | None) -> None:
    level = {0: "WARNING", 1: "INFO"}.get(verbosity, "DEBUG")
    logger.remove()
    logger.add(logfile if logfile is not None else sys.stderr, level=level)
    LOG.info(
        "{} v{} starting, verbosity={}, pid={}, python={}",
        PROGNAME,
        __version__,
        verbosity,
        os.getpid(),
        sys.version.split()[0],
    )
    LOG.debug("logging destination: {}", logfile or "<stderr>")


def redirect_logging_to_file(path: Path) -> None:
    logger.remove()
    logger.add(path)
    LOG.info("--- logging redirected to {} (curses UI active) ---", path)


def fmt_size(n: int, si: bool = False) -> str:
    base = 1000.0 if si else 1024.0
    units = (
        ("  B", " kB", " MB", " GB", " TB", " PB", " EB") if si else ("  B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    )
    f = float(n)
    for u in units:
        if abs(f) < base or u is units[-1]:
            if u == "  B":
                return f"{int(f):5d} {u}"
            return f"{f:5.1f} {u}"
        f /= base
    return f"{f:5.1f} {units[-1]}"


def fmt_count(n: int) -> str:
    return f"{n:,}"


def _stat_to_node(p: Path, st: os.stat_result, is_dir: bool) -> dict[str, Any]:
    return {
        "n": p.name or str(p),
        "d": is_dir,
        "s": int(st.st_size),
        "b": int(getattr(st, "st_blocks", 0)) * 512,
        "e": False,
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
        "l": int(st.st_nlink),
        "m": float(st.st_mtime),
        "c": [] if is_dir else None,
    }


def scan_subtree(
    path_str: str,
    root_dev: int,
    one_filesystem: bool,
    follow_symlinks: bool,
    exclude: tuple[str, ...],
    verbosity: int,
) -> dict[str, Any]:
    log = logger
    root = Path(path_str)
    t0 = time.perf_counter()
    counter = {"files": 0, "dirs": 0, "errors": 0}

    def walk(p: Path) -> dict[str, Any]:
        try:
            st = p.lstat()
        except OSError as exc:
            counter["errors"] += 1
            log.debug("lstat failed on {}: {}", p, exc)
            return {
                "n": p.name,
                "d": False,
                "s": 0,
                "b": 0,
                "e": True,
                "dev": 0,
                "ino": 0,
                "l": 1,
                "m": 0.0,
                "c": None,
            }
        is_dir = stat.S_ISDIR(st.st_mode)
        if stat.S_ISLNK(st.st_mode) and follow_symlinks:
            try:
                st2 = p.stat()
                is_dir = stat.S_ISDIR(st2.st_mode)
                st = st2
            except OSError:
                pass
        node = _stat_to_node(p, st, is_dir)
        if not is_dir:
            counter["files"] += 1
            return node
        counter["dirs"] += 1
        if one_filesystem and st.st_dev != root_dev:
            log.debug("skipping {}: other filesystem (dev={} != {})", p, st.st_dev, root_dev)
            node["c"] = []
            return node
        try:
            entries = sorted(p.iterdir(), key=lambda q: q.name)
        except OSError as exc:
            counter["errors"] += 1
            node["e"] = True
            node["c"] = []
            log.debug("cannot list {}: {}", p, exc)
            return node
        children: list[dict[str, Any]] = []
        for child in entries:
            if child.name in exclude:
                log.debug("excluded by name: {}", child)
                continue
            if stat.S_ISLNK(child.lstat().st_mode) if False else False:
                continue
            children.append(walk(child))
        node["c"] = children
        return node

    result = walk(root)
    dt = time.perf_counter() - t0
    log.info(
        "scanned {} dirs={} files={} errors={} in {}s",
        str(root)[-60:],
        counter["dirs"],
        counter["files"],
        counter["errors"],
        dt,
    )
    result["_stats"] = counter | {"seconds": dt, "root": str(root)}
    return result


@dataclass(slots=True)
class Node:
    name: str
    is_dir: bool
    size: int = 0
    dsize: int = 0
    own_size: int = 0
    own_dsize: int = 0
    items: int = 1
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None
    err: bool = False
    dev: int = 0
    ino: int = 0
    nlink: int = 1
    dup: bool = False
    mtime: float = 0.0

    @property
    def path(self) -> Path:
        parts: list[str] = []
        n: Node | None = self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return Path(*reversed(parts))

    def iter_all(self) -> Iterator["Node"]:
        yield self
        for c in self.children:
            yield from c.iter_all()

    def __repr__(self) -> str:
        return f"<Node {self.name!r} dir={self.is_dir} size={self.size}>"


def build_tree(d: dict[str, Any], parent: Node | None = None) -> Node:
    n = Node(
        name=d["n"],
        is_dir=bool(d["d"]),
        own_size=int(d["s"]),
        own_dsize=int(d["b"]),
        err=bool(d["e"]),
        dev=int(d["dev"]),
        ino=int(d["ino"]),
        nlink=int(d["l"]),
        mtime=float(d["m"]),
        parent=parent,
    )
    for cd in d.get("c") or ():
        n.children.append(build_tree(cd, n))
    return n


def dedup_hardlinks(root: Node) -> int:
    seen: set[tuple[int, int]] = set()
    dups = 0
    for n in root.iter_all():
        if n.is_dir or n.nlink <= 1 or n.ino == 0:
            continue
        key = (n.dev, n.ino)
        if key in seen:
            n.dup = True
            dups += 1
        else:
            seen.add(key)
    LOG.info("hard-link de-duplication: {} duplicate entries neutralised", dups)
    return dups


def aggregate(node: Node) -> tuple[int, int, int]:
    if node.is_dir:
        s = d = 0
        it = 1
        for c in node.children:
            cs, cd, ci = aggregate(c)
            s += cs
            d += cd
            it += ci
        node.size, node.dsize, node.items = s, d, it
    else:
        if node.dup:
            node.size = node.dsize = 0
        else:
            node.size, node.dsize = node.own_size, node.own_dsize
        node.items = 1
    return node.size, node.dsize, node.items


def parallel_scan(
    root: Path,
    *,
    one_filesystem: bool,
    follow_symlinks: bool,
    exclude: tuple[str, ...],
    verbosity: int,
) -> Node:
    root = root.resolve()
    LOG.info("scan root      : {}", root)
    LOG.info("worker processes: {} (fixed)", MAX_WORKERS)
    LOG.info(
        "options        : one_filesystem={} follow_symlinks={} exclude={}",
        one_filesystem,
        follow_symlinks,
        exclude or "()",
    )
    try:
        rst = root.stat()
    except OSError as exc:
        msg = f"{PROGNAME}: cannot stat {root}: {exc}"
        raise SystemExit(msg)
    if not stat.S_ISDIR(rst.st_mode):
        msg = f"{PROGNAME}: {root} is not a directory"
        raise SystemExit(msg)
    root_dev = rst.st_dev
    t_start = time.perf_counter()
    try:
        top_entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        msg = f"{PROGNAME}: cannot read {root}: {exc}"
        raise SystemExit(msg)
    top_entries = [p for p in top_entries if p.name not in exclude]
    LOG.info("top-level entries to dispatch: {}", len(top_entries))
    root_node = build_tree(_stat_to_node(root, rst, True))
    root_node.name = str(root)
    ctx = mp.get_context("fork" if sys.platform != "win32" else "spawn")
    LOG.debug("multiprocessing start method: {}", ctx.get_start_method())
    results: list[tuple[Path, "mp.pool.AsyncResult[dict[str, Any]]"]] = []
    with ctx.Pool(processes=MAX_WORKERS) as pool:
        LOG.info("pool created with {} workers", MAX_WORKERS)
        for entry in top_entries:
            LOG.debug("apply_async -> {}", entry)
            ar = pool.apply_async(
                scan_subtree,
                args=(
                    str(entry),
                    root_dev,
                    one_filesystem,
                    follow_symlinks,
                    exclude,
                    verbosity,
                ),
            )
            results.append((entry, ar))
        pool.close()
        LOG.info("all {} jobs submitted, collecting results ...", len(results))
        done = 0
        totals = {"dirs": 0, "files": 0, "errors": 0}
        for entry, ar in results:
            try:
                node_dict = ar.get()
            except Exception as exc:
                LOG.error("worker failed on {}: {}", entry, exc)
                node_dict = {
                    "n": entry.name,
                    "d": entry.is_dir(),
                    "s": 0,
                    "b": 0,
                    "e": True,
                    "dev": 0,
                    "ino": 0,
                    "l": 1,
                    "m": 0.0,
                    "c": [] if entry.is_dir() else None,
                }
            st = node_dict.pop("_stats", None)
            if st:
                totals["dirs"] += st["dirs"]
                totals["files"] += st["files"]
                totals["errors"] += st["errors"]
            child = build_tree(node_dict, root_node)
            root_node.children.append(child)
            done += 1
            LOG.info(
                "[{}/{}] collected {} ({})",
                done,
                len(results),
                entry.name[:40],
                fmt_size(child.own_size if not child.is_dir else 0).strip(),
            )
        pool.join()
        LOG.info("pool joined")
    dedup_hardlinks(root_node)
    aggregate(root_node)
    elapsed = time.perf_counter() - t_start
    LOG.info("=" * 72)
    LOG.info("scan complete: {}", root)
    LOG.info("  directories : {}", fmt_count(totals["dirs"] + 1))
    LOG.info("  files       : {}", fmt_count(totals["files"]))
    LOG.info("  errors      : {}", fmt_count(totals["errors"]))
    LOG.info("  apparent    : {}", fmt_size(root_node.size))
    LOG.info("  disk usage  : {}", fmt_size(root_node.dsize))
    LOG.info(
        "  elapsed     : {}s ({} items/s)",
        elapsed,
        root_node.items / max(elapsed, 1e-9),
    )
    LOG.info("=" * 72)
    return root_node


def export_json(root: Node, out: Path) -> None:
    LOG.info("exporting JSON v2 to {}", out)

    def enc(n: Node) -> Any:
        d: dict[str, Any] = {
            "name": n.name,
            "asize": n.own_size,
            "dsize": n.own_dsize,
            "ino": n.ino,
            "dev": n.dev,
            "nlink": n.nlink,
            "mtime": int(n.mtime),
        }
        if n.err:
            d["read_error"] = True
        if n.dup:
            d["hlnkc"] = True
        if not n.is_dir:
            return d
        return [d, *(enc(c) for c in n.children)]

    payload = [
        1,
        2,
        {"progname": PROGNAME, "progver": __version__, "timestamp": int(time.time())},
        enc(root),
    ]
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    LOG.info("export finished: {} ({})", out, fmt_size(out.stat().st_size))


def import_json(path: Path) -> Node:
    LOG.info("importing JSON from {}", path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, list) and len(data) >= 4):
        msg = f"{PROGNAME}: {path}: not an ncdu export"
        raise SystemExit(msg)
    LOG.info("export header: major={} minor={} meta={}", data[0], data[1], data[2])

    def dec(item: Any, parent: Node | None) -> Node:
        if isinstance(item, list):
            info = item[0]
            n = _mk(info, True, parent)
            for sub in item[1:]:
                n.children.append(dec(sub, n))
            return n
        return _mk(item, False, parent)

    def _mk(info: dict[str, Any], is_dir: bool, parent: Node | None) -> Node:
        return Node(
            name=info.get("name", "?"),
            is_dir=is_dir,
            own_size=int(info.get("asize", 0)),
            own_dsize=int(info.get("dsize", 0)),
            err=bool(info.get("read_error", False)),
            dev=int(info.get("dev", 0)),
            ino=int(info.get("ino", 0)),
            nlink=int(info.get("nlink", 1)),
            dup=bool(info.get("hlnkc", False)),
            mtime=float(info.get("mtime", 0)),
            parent=parent,
        )

    root = dec(data[3], None)
    aggregate(root)
    LOG.info("import done: {} items, {}", fmt_count(root.items), fmt_size(root.dsize))
    return root


SORT_MODES = ("size", "name", "items", "mtime")


class Browser:
    def __init__(self, root: Node, *, apparent: bool = False, si: bool = False):
        self.root = root
        self.cur = root
        self.apparent = apparent
        self.si = si
        self.sort = "size"
        self.reverse = True
        self.show_graph = True
        self.show_percent = True
        self.cursor = 0
        self.offset = 0
        self.message = ""
        self.stack: list[tuple[Node, int, int]] = []
        LOG.info("browser initialised at {}", root.path)

    def val(self, n: Node) -> int:
        return n.size if self.apparent else n.dsize

    def listing(self) -> list[Node]:
        key = {
            "size": self.val,
            "name": lambda n: n.name.lower(),
            "items": lambda n: n.items,
            "mtime": lambda n: n.mtime,
        }[self.sort]
        rev = self.reverse if self.sort != "name" else not self.reverse
        return sorted(self.cur.children, key=key, reverse=rev)

    def run(self, scr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        scr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)
        while True:
            items = self.listing()
            self.draw(scr, items)
            ch = scr.getch()
            LOG.debug("key={} cursor={} dir={}", ch, self.cursor, self.cur.path)
            if not self.handle(scr, ch, items):
                LOG.info("quitting browser")
                return

    def draw(self, scr, items: list[Node]) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        body = max(h - 3, 1)
        if self.cursor >= len(items):
            self.cursor = max(len(items) - 1, 0)
        self.offset = min(self.offset, self.cursor)
        if self.cursor >= self.offset + body:
            self.offset = self.cursor - body + 1
        total = self.val(self.cur) or 1
        mode = "apparent size" if self.apparent else "disk usage"
        head = (
            f" {PROGNAME} {__version__} ~ {mode} ~ "
            f"{fmt_size(self.val(self.cur), self.si).strip()} in "
            f"{fmt_count(self.cur.items)} items "
        )
        scr.addnstr(0, 0, head.ljust(w)[:w], w, curses.color_pair(1) | curses.A_BOLD)
        scr.addnstr(1, 0, f"--- {self.cur.path} ".ljust(w, "-")[:w], w)
        for i in range(body):
            idx = self.offset + i
            if idx >= len(items):
                break
            n = items[idx]
            sel = idx == self.cursor
            attr = curses.color_pair(5) if sel else curses.A_NORMAL
            v = self.val(n)
            pct = 100.0 * v / total
            parts = [f"{fmt_size(v, self.si):>10}"]
            if self.show_percent:
                parts.append(f"{pct:5.1f}%")
            if self.show_graph:
                bars = round(pct / 100.0 * 10)
                parts.append("[" + "#" * bars + " " * (10 - bars) + "]")
            prefix = " ".join(parts)
            marker = "/" if n.is_dir else " "
            flag = "!" if n.err else ("H" if n.dup else " ")
            line = f"{flag}{prefix} {marker}{n.name}"
            scr.addnstr(
                2 + i,
                0,
                line.ljust(w)[:w],
                w,
                (
                    attr
                    if sel
                    else (curses.color_pair(4) if n.err else curses.color_pair(2) if n.is_dir else curses.A_NORMAL)
                ),
            )
        foot = (
            self.message
            or " q:quit  ↵/→:open  ←:up  d:delete  n/s/C/M:sort  a:apparent  g:graph  i:info  e:export  ?:help "
        )
        scr.addnstr(h - 1, 0, foot.ljust(w)[:w], w, curses.color_pair(1))
        self.message = ""
        scr.refresh()

    def handle(self, scr, ch: int, items: list[Node]) -> bool:
        h, _ = scr.getmaxyx()
        body = max(h - 3, 1)
        sel = items[self.cursor] if items else None
        match ch:
            case curses.KEY_UP | 107:
                self.cursor = max(0, self.cursor - 1)
            case curses.KEY_DOWN | 106:
                self.cursor = min(len(items) - 1, self.cursor + 1) if items else 0
            case curses.KEY_NPAGE | 4:
                self.cursor = min(len(items) - 1, self.cursor + body) if items else 0
            case curses.KEY_PPAGE | 21:
                self.cursor = max(0, self.cursor - body)
            case curses.KEY_HOME | 103:
                self.cursor = 0
            case curses.KEY_END | 71:
                self.cursor = max(len(items) - 1, 0)
            case curses.KEY_RIGHT | 10 | 13 | curses.KEY_ENTER:
                if sel is not None and sel.is_dir:
                    LOG.info("entering {}", sel.path)
                    self.stack.append((self.cur, self.cursor, self.offset))
                    self.cur, self.cursor, self.offset = sel, 0, 0
            case curses.KEY_LEFT | 127 | curses.KEY_BACKSPACE | 104:
                if self.stack:
                    self.cur, self.cursor, self.offset = self.stack.pop()
                    LOG.info("back to {}", self.cur.path)
            case 110:
                self.sort = "name"
                LOG.info("sort by name")
            case 115:
                self.sort = "size"
                LOG.info("sort by size")
            case 67:
                self.sort = "items"
                LOG.info("sort by item count")
            case 77:
                self.sort = "mtime"
                LOG.info("sort by mtime")
            case 101:
                self.do_export(scr)
            case 105:
                if sel:
                    self.info_popup(scr, sel)
            case 97:
                self.apparent = not self.apparent
                LOG.info("size mode -> {}", "apparent" if self.apparent else "disk")
            case 103 if False:
                pass
            case 71 if False:
                pass
            case 100:
                if sel:
                    self.do_delete(scr, sel)
            case 111:
                self.reverse = not self.reverse
            case 112:
                self.show_percent = not self.show_percent
            case 63:
                self.help_popup(scr)
            case 114:
                self.rescan(scr)
            case 113 | 27:
                return False
            case _:
                pass
        if ch == ord("g") and self.sort:
            pass
        return True

    def popup(self, scr, title: str, lines: list[str]) -> None:
        h, w = scr.getmaxyx()
        ph = min(len(lines) + 4, h - 2)
        pw = min(max(len(title), *(len(l) for l in lines)) + 4, w - 2)
        win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
        win.box()
        win.addnstr(0, 2, f" {title} ", pw - 4, curses.A_BOLD)
        for i, line in enumerate(lines[: ph - 4]):
            win.addnstr(2 + i, 2, line, pw - 4)
        win.addnstr(ph - 1, 2, " press any key ", pw - 4)
        win.refresh()
        win.getch()
        del win
        scr.touchwin()
        scr.refresh()

    def help_popup(self, scr) -> None:
        self.popup(
            scr,
            "Help",
            [
                "up/down, j/k     move cursor",
                "right/enter      open directory",
                "left/backspace   go to parent",
                "n s C M          sort by name/size/items/mtime",
                "o                toggle sort order",
                "a                toggle apparent size / disk usage",
                "p                toggle percentage column",
                "i                item information",
                "d                delete selected item",
                "e                export JSON (ncdu v2)",
                "r                rescan current directory (8 workers)",
                "q                quit",
            ],
        )

    def info_popup(self, scr, n: Node) -> None:
        self.popup(
            scr,
            "Info",
            [
                f"name       : {n.name}",
                f"path       : {n.path}",
                f"type       : {'directory' if n.is_dir else 'file'}",
                f"apparent   : {fmt_size(n.size, self.si).strip()} ({n.size} B)",
                f"disk usage : {fmt_size(n.dsize, self.si).strip()} ({n.dsize} B)",
                f"items      : {fmt_count(n.items)}",
                f"inode/dev  : {n.ino} / {n.dev}   nlink={n.nlink}",
                f"mtime      : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(n.mtime))}",
                f"flags      : {'read-error ' if n.err else ''}{'hardlink-dup' if n.dup else ''}",
            ],
        )

    def confirm(self, scr, question: str) -> bool:
        h, w = scr.getmaxyx()
        scr.addnstr(
            h - 1,
            0,
            (question + "  [y/N] ").ljust(w)[:w],
            w,
            curses.color_pair(4) | curses.A_BOLD,
        )
        scr.refresh()
        return scr.getch() in (ord("y"), ord("Y"))

    def do_delete(self, scr, n: Node) -> None:
        target = n.path
        if not self.confirm(scr, f"Delete {target}?"):
            LOG.info("deletion of {} cancelled", target)
            self.message = " deletion cancelled "
            return
        LOG.warning("DELETING {}", target)
        try:
            if n.is_dir:
                shutil.rmtree(target)
            else:
                Path(target).unlink()
        except OSError as exc:
            LOG.error("delete failed: {}", exc)
            self.message = f" error: {exc.strerror or exc} "
            return
        s, d, it = n.size, n.dsize, n.items
        p = n.parent
        while p is not None:
            p.size -= s
            p.dsize -= d
            p.items -= it
            p = p.parent
        if n.parent:
            n.parent.children.remove(n)
        LOG.info("deleted {} (freed {})", target, fmt_size(d))
        self.message = f" deleted {target.name} ({fmt_size(d).strip()} freed) "

    def do_export(self, scr) -> None:
        out = Path.cwd() / f"ncdu2-export-{int(time.time())}.json"
        try:
            export_json(self.root, out)
            self.message = f" exported to {out} "
        except OSError as exc:
            LOG.error("export failed: {}", exc)
            self.message = f" export failed: {exc} "

    def rescan(self, scr) -> None:
        target = self.cur.path
        h, w = scr.getmaxyx()
        scr.addnstr(
            h - 1,
            0,
            f" rescanning {target} with {MAX_WORKERS} workers ... ".ljust(w)[:w],
            w,
            curses.color_pair(1),
        )
        scr.refresh()
        LOG.info("rescan requested for {}", target)
        try:
            new = parallel_scan(
                target,
                one_filesystem=False,
                follow_symlinks=False,
                exclude=(),
                verbosity=2,
            )
        except SystemExit as exc:
            self.message = f" rescan failed: {exc} "
            return
        new.name = self.cur.name
        new.parent = self.cur.parent
        if self.cur.parent:
            idx = self.cur.parent.children.index(self.cur)
            self.cur.parent.children[idx] = new
        else:
            self.root = new
        self.cur = new
        self.cursor = self.offset = 0
        aggregate(self.root)
        self.message = " rescan complete "


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog=PROGNAME,
        description="ncdu2 - NCurses Disk Usage v2 (Python 3.12, pathlib, 8 workers)",
    )
    ap.add_argument("path", nargs="?", default=".", type=Path, help="directory to scan")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="export to ncdu JSON v2 and exit (no UI)",
    )
    ap.add_argument(
        "-f",
        "--file",
        type=Path,
        metavar="FILE",
        help="load a previous export instead of scanning",
    )
    ap.add_argument(
        "-x",
        "--one-file-system",
        action="store_true",
        help="do not cross filesystem boundaries",
    )
    ap.add_argument(
        "-L",
        "--follow-symlinks",
        action="store_true",
        help="follow symlinks (dangerous: loops)",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="exclude entries with this exact name (repeatable)",
    )
    ap.add_argument(
        "--apparent-size",
        action="store_true",
        help="show apparent size instead of disk usage",
    )
    ap.add_argument("--si", action="store_true", help="use powers of 1000 instead of 1024")
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1,
        help="increase verbosity (-v, -vv, -vvv)",
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="only warnings/errors")
    ap.add_argument(
        "--log",
        type=Path,
        default=Path("ncdu2.log"),
        help="log file used while the TUI is active (default: ncdu2.log)",
    )
    ap.add_argument("--no-ui", action="store_true", help="scan, print a summary, exit")
    ap.add_argument(
        "--top",
        type=int,
        default=20,
        metavar="N",
        help="with --no-ui: show the N largest entries",
    )
    ap.add_argument("-V", "--version", action="version", version=f"{PROGNAME} {__version__}")
    return ap.parse_args(argv)


def print_summary(root: Node, top: int, apparent: bool, si: bool) -> None:
    val = (lambda n: n.size) if apparent else (lambda n: n.dsize)
    print(f"\n=== {root.path} ===")
    print(f"total {'apparent' if apparent else 'disk'} usage: {fmt_size(val(root), si).strip()}")
    print(f"items: {fmt_count(root.items)}\n")
    biggest = sorted((n for n in root.iter_all() if n is not root), key=val, reverse=True)[:top]
    for n in biggest:
        print(f"{fmt_size(val(n), si):>12}  {'d' if n.is_dir else '-'}  {n.path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verbosity = 0 if args.quiet else args.verbose
    setup_logging(verbosity, None)
    if args.file:
        root = import_json(args.file)
    else:
        root = parallel_scan(
            args.path,
            one_filesystem=args.one_file_system,
            follow_symlinks=args.follow_symlinks,
            exclude=tuple(args.exclude),
            verbosity=verbosity,
        )
    if args.output:
        export_json(root, args.output)
        return 0
    if args.no_ui or not sys.stdout.isatty():
        LOG.info("no TUI (no_ui={}, tty={})", args.no_ui, sys.stdout.isatty())
        print_summary(root, args.top, args.apparent_size, args.si)
        return 0
    redirect_logging_to_file(args.log)
    browser = Browser(root, apparent=args.apparent_size, si=args.si)
    try:
        curses.wrapper(browser.run)
    except KeyboardInterrupt:
        LOG.warning("interrupted by user")
    finally:
        LOG.info("{} exiting", PROGNAME)
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())

#!/data/data/com.termux/files/usr/bin/env python
"""Convert tabular data between CSV, JSON, JSONL, SQLite, SQL, Excel, Parquet and the other supported formats.
Detects the source format from the suffix or the SQLite magic bytes, loads the tables (optionally through an mmap for the big files), and writes them out in any registered format, keeping the multi-table structure where the target supports it.
Usage: dataconv INPUT -t TARGET_FORMAT [-o OUTPUT] Positional arguments: inputs ...
Input file(s) Options: -o, --output-dir Directory for outputs (default: next to input) Examples: $ dataconv data.csv -t sqlite -o data.db Command-line script: run ``python dataconv.py`` or the ``dataconv`` command.
The module is importable as well."""

import argparse
import csv
import importlib
import io
import json
import mmap
from multiprocessing import Pool
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Optional

from dh import MAX_WORKERS
from loguru import logger


Tables = dict[str, list[dict[str, Any]]]
MMAP_THRESHOLD = 5 * 1024 * 1024


def _require(modname: str):
    try:
        return importlib.import_module(modname)
    except ImportError as exc:
        msg = f"format requires '{modname}'; install with: pip install {modname}"
        raise ImportError(msg) from exc


def read_text(path: Path) -> str:
    size = path.stat().st_size
    if size == 0:
        return ""
    if size > MMAP_THRESHOLD:
        logger.debug(f"mmap read: {path} ({size / 1_048_576:.1f} MB)")
        with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mm[:].decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    cols: list[str] = []
    seen: set = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                cols.append(key)
    return cols


def _flat(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _infer_sql_type(values) -> str:
    kind: Optional[str] = None
    for v in values:
        if v is None:
            continue
        if isinstance(v, (bool, int)):
            if kind in (None, "INTEGER"):
                kind = "INTEGER"
            elif kind == "REAL":
                kind = "REAL"
            else:
                return "TEXT"
        elif isinstance(v, float):
            if kind in (None, "INTEGER", "REAL"):
                kind = "REAL"
            else:
                return "TEXT"
        else:
            return "TEXT"
    return kind or "TEXT"


def _sqlite_val(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str, bytes)):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return "'" + json.dumps(value, ensure_ascii=False, default=str).replace("'", "''") + "'"


def _is_table_dict(data: Any) -> bool:
    return isinstance(data, dict) and bool(data) and all(isinstance(v, list) for v in data.values())


def _as_tables(data: Any, fallback_name: str) -> Tables:
    if _is_table_dict(data):
        return {str(k): [r if isinstance(r, dict) else {"value": r} for r in v] for k, v in data.items()}
    if isinstance(data, list):
        return {fallback_name: [r if isinstance(r, dict) else {"value": r} for r in data]}
    return {fallback_name: [data if isinstance(data, dict) else {"value": data}]}


def load_csv(path: Path) -> Tables:
    text = read_text(path)
    dialect: Any = "excel-tab" if path.suffix.lower() == ".tsv" else "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return {path.stem: [dict(r) for r in reader]}


def write_csv(tables: Tables, out_path: Path) -> list[Path]:
    multi = len(tables) > 1
    written: list[Path] = []
    for name, rows in tables.items():
        target = out_path.with_name(f"{out_path.stem}.{name}{out_path.suffix}") if multi else out_path
        cols = _columns(rows)
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: _flat(row.get(c)) for c in cols})
        written.append(target)
    return written


def load_json(path: Path) -> Tables:
    return _as_tables(json.loads(read_text(path)), path.stem)


def write_json(tables: Tables, out_path: Path) -> list[Path]:
    payload: Any = next(iter(tables.values())) if len(tables) == 1 else tables
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return [out_path]


def load_jsonl(path: Path) -> Tables:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append(obj if isinstance(obj, dict) else {"value": obj})
    return {path.stem: rows}


def write_jsonl(tables: Tables, out_path: Path) -> list[Path]:
    _name, rows = next(iter(tables.items()))
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return [out_path]


def load_db(path: Path) -> Tables:
    size = path.stat().st_size
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if size > MMAP_THRESHOLD:
            logger.debug(f"sqlite mmap_size for {path} ({size / 1_048_576:.1f} MB)")
            con.execute(f"PRAGMA mmap_size={size}")
        names = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        out: Tables = {}
        for name in names:
            out[name] = [dict(r) for r in con.execute(f'SELECT * FROM "{name}"')]
        return out
    finally:
        con.close()


def write_db(tables: Tables, out_path: Path) -> list[Path]:
    if out_path.exists():
        out_path.unlink()
    con = sqlite3.connect(out_path)
    try:
        for name, rows in tables.items():
            cols = _columns(rows)
            if not cols:
                logger.warning(f"skipping empty table {name!r}")
                continue
            types = {c: _infer_sql_type([r.get(c) for r in rows]) for c in cols}
            col_defs = ", ".join(f'"{c}" {types[c]}' for c in cols)
            con.execute(f'CREATE TABLE "{name}" ({col_defs})')
            placeholders = ", ".join("?" * len(cols))
            con.executemany(
                f'INSERT INTO "{name}" VALUES ({placeholders})',
                [[_sqlite_val(r.get(c)) for c in cols] for r in rows],
            )
        con.commit()
    finally:
        con.close()
    return [out_path]


_CREATE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[\]]?(\w+)[`"\[\]]?\s*\(',
    re.IGNORECASE,
)
_INSERT_RE = re.compile(
    r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[`"\[\]]?(\w+)[`"\[\]]?\s*' r"(?:\(([^)]*)\))?\s*VALUES\s*",
    re.IGNORECASE,
)
_NON_COLUMN_KEYWORDS = {
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CHECK",
    "CONSTRAINT",
    "KEY",
    "INDEX",
}


def _split_top_level(s: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_str = False
    quote = ""
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == quote:
                in_str = False
        elif ch in "'\"":
            in_str, quote = True, ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _iter_create_tables(text: str):
    for m in _CREATE_RE.finditer(text):
        name = m.group(1)
        i, depth, start = m.end(), 1, m.end()
        in_str, quote = False, ""
        while i < len(text) and depth:
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    in_str = False
            elif c in "'\"":
                in_str, quote = True, c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        yield name, text[start : i - 1]


def _iter_inserts(text: str):
    for m in _INSERT_RE.finditer(text):
        name = m.group(1)
        cols = m.group(2)
        i, depth, start = m.end(), 0, m.end()
        in_str, quote = False, ""
        while i < len(text):
            c = text[i]
            if in_str:
                if c == "\\" and quote == "'":
                    i += 2
                    continue
                if c == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        i += 2
                        continue
                    in_str = False
                i += 1
            elif c in "'\"":
                in_str, quote = True, c
                i += 1
            elif c == "(":
                depth += 1
                i += 1
            elif c == ")":
                depth -= 1
                i += 1
            elif c == ";" and depth == 0:
                break
            else:
                i += 1
        yield name, cols, text[start:i]


def _convert_sql_value(tok: str) -> Any:
    if tok == "":
        return None
    upper = tok.upper()
    if upper == "NULL":
        return None
    if upper == "TRUE":
        return True
    if upper == "FALSE":
        return False
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\"":
        q = tok[0]
        return tok[1:-1].replace(q + q, q)
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def _parse_value_tuples(raw: str) -> list[list[Any]]:
    rows: list[list[Any]] = []
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] != "(":
            break
        i += 1
        row: list[str] = []
        cur: list[str] = []
        in_str, quote = False, ""
        while i < n:
            c = raw[i]
            if in_str:
                if c == "\\" and quote == "'" and i + 1 < n:
                    nxt = raw[i + 1]
                    cur.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == quote:
                    if i + 1 < n and raw[i + 1] == quote:
                        cur.append(c)
                        i += 2
                        continue
                    in_str = False
                    cur.append(c)
                    i += 1
                    continue
                cur.append(c)
                i += 1
            elif c in "'\"":
                in_str, quote = True, c
                cur.append(c)
                i += 1
            elif c == ",":
                row.append("".join(cur).strip())
                cur = []
                i += 1
            elif c == ")":
                row.append("".join(cur).strip())
                i += 1
                break
            else:
                cur.append(c)
                i += 1
        rows.append([_convert_sql_value(v) for v in row])
    return rows


def load_sql(path: Path) -> Tables:
    text = read_text(path)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*--.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    tables: Tables = {}
    declared_cols: dict[str, list[str]] = {}
    for name, body in _iter_create_tables(text):
        cols: list[str] = []
        for part in _split_top_level(body):
            part = part.strip()
            if not part:
                continue
            head = part.split()[0].strip('`"[]')
            if head.upper() in _NON_COLUMN_KEYWORDS:
                continue
            cols.append(head)
        declared_cols[name] = cols
        tables.setdefault(name, [])
    for name, col_list, raw_values in _iter_inserts(text):
        if col_list:
            cols = [c.strip().strip('`"[]') for c in col_list.split(",")]
        else:
            cols = list(declared_cols.get(name, []))
        for values in _parse_value_tuples(raw_values):
            if not cols:
                cols = [f"c{i}" for i in range(len(values))]
                declared_cols[name] = cols
            tables.setdefault(name, []).append(dict(zip(cols, values)))
    return tables


def write_sql(tables: Tables, out_path: Path) -> list[Path]:
    lines: list[str] = ["-- generated by dataconv.py", ""]
    for name, rows in tables.items():
        cols = _columns(rows)
        if not cols:
            continue
        types = {c: _infer_sql_type([r.get(c) for r in rows]) for c in cols}
        col_defs = ", ".join(f'"{c}" {types[c]}' for c in cols)
        lines.append(f'CREATE TABLE IF NOT EXISTS "{name}" ({col_defs});')
        col_list = ", ".join(f'"{c}"' for c in cols)
        for row in rows:
            values = ", ".join(_sql_literal(row.get(c)) for c in cols)
            lines.append(f'INSERT INTO "{name}" ({col_list}) VALUES ({values});')
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return [out_path]


def load_xlsx(path: Path) -> Tables:
    openpyxl = _require("openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out: Tables = {}
    try:
        for ws in wb.worksheets:
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header = [str(c) if c is not None else f"c{i}" for i, c in enumerate(next(rows_iter))]
            except StopIteration:
                out[ws.title] = []
                continue
            out[ws.title] = [dict(zip(header, r)) for r in rows_iter]
    finally:
        wb.close()
    return out


def load_xls(path: Path) -> Tables:
    xlrd = _require("xlrd")
    book = xlrd.open_workbook(path)
    out: Tables = {}
    for sheet in book.sheets():
        if sheet.nrows == 0:
            out[sheet.name] = []
            continue
        header = [str(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
        out[sheet.name] = [
            {header[c]: sheet.cell_value(r, c) for c in range(sheet.ncols)} for r in range(1, sheet.nrows)
        ]
    return out


def load_xlsb(path: Path) -> Tables:
    pyxlsb = _require("pyxlsb")
    out: Tables = {}
    with pyxlsb.open_workbook(path) as wb:
        for name in wb.sheets:
            with wb.get_sheet(name) as sheet:
                rows_iter = sheet.rows()
                try:
                    header = [str(c.v) if c.v is not None else f"c{i}" for i, c in enumerate(next(rows_iter))]
                except StopIteration:
                    out[name] = []
                    continue
                out[name] = [{header[i]: c.v for i, c in enumerate(row)} for row in rows_iter]
    return out


def load_ods(path: Path) -> Tables:
    odf = _require("odf.opendocument")
    table_mod = _require("odf.table")
    text_mod = _require("odf.text")
    doc = odf.load(path)
    out: Tables = {}
    for sheet in doc.spreadsheet.getElementsByType(table_mod.Table):
        name = sheet.getAttribute("name") or f"sheet{len(out)}"
        rows_out: list[dict[str, Any]] = []
        header: Optional[list[str]] = None
        for row in sheet.getElementsByType(table_mod.TableRow):
            cells = row.getElementsByType(table_mod.TableCell)
            values: list[Any] = []
            for cell in cells:
                repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                p = cell.getElementsByType(text_mod.P)
                text = "".join(node.data for node in p[0].childNodes) if p else ""
                values.extend([text] * repeat)
            if header is None:
                header = [str(v) if v != "" else f"c{i}" for i, v in enumerate(values)]
                continue
            rows_out.append({header[i] if i < len(header) else f"c{i}": v for i, v in enumerate(values)})
        out[name] = rows_out
    return out


def write_xlsx(tables: Tables, out_path: Path) -> list[Path]:
    openpyxl = _require("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in tables.items():
        ws = wb.create_sheet(title=name[:31])
        cols = _columns(rows)
        ws.append(cols)
        for row in rows:
            ws.append([_flat(row.get(c)) for c in cols])
    wb.save(out_path)
    return [out_path]


def write_ods(tables: Tables, out_path: Path) -> list[Path]:
    odf_opendoc = _require("odf.opendocument")
    table_mod = _require("odf.table")
    text_mod = _require("odf.text")
    doc = odf_opendoc.OpenDocumentSpreadsheet()
    for name, rows in tables.items():
        sheet = table_mod.Table(name=name[:31])
        cols = _columns(rows)
        header_row = table_mod.TableRow()
        for c in cols:
            cell = table_mod.TableCell(valuetype="string")
            cell.addElement(text_mod.P(text=str(c)))
            header_row.addElement(cell)
        sheet.addElement(header_row)
        for row in rows:
            tr = table_mod.TableRow()
            for c in cols:
                v = _flat(row.get(c))
                cell = table_mod.TableCell(valuetype="string")
                cell.addElement(text_mod.P(text="" if v is None else str(v)))
                tr.addElement(cell)
            sheet.addElement(tr)
        doc.spreadsheet.addElement(sheet)
    doc.save(str(out_path).rsplit(".", 1)[0])
    return [Path(str(out_path).rsplit(".", 1)[0] + ".ods")]


def load_parquet(path: Path) -> Tables:
    pq = _require("pyarrow.parquet")
    return {path.stem: pq.read_table(path).to_pylist()}


def write_parquet(tables: Tables, out_path: Path) -> list[Path]:
    pa = _require("pyarrow")
    pq = _require("pyarrow.parquet")
    multi = len(tables) > 1
    written: list[Path] = []
    for name, rows in tables.items():
        cols = _columns(rows)
        arrays = {c: [r.get(c) for r in rows] for c in cols}
        tbl = pa.table(arrays)
        target = out_path.with_name(f"{out_path.stem}.{name}{out_path.suffix}") if multi else out_path
        pq.write_table(tbl, target, compression="snappy")
        written.append(target)
    return written


def load_feather(path: Path) -> Tables:
    feather = _require("pyarrow.feather")
    return {path.stem: feather.read_table(path).to_pylist()}


def write_feather(tables: Tables, out_path: Path) -> list[Path]:
    pa = _require("pyarrow")
    feather = _require("pyarrow.feather")
    _name, rows = next(iter(tables.items()))
    cols = _columns(rows)
    arrays = {c: [r.get(c) for r in rows] for c in cols}
    feather.write_feather(pa.table(arrays), out_path, compression="lz4")
    return [out_path]


def load_orc(path: Path) -> Tables:
    orc = _require("pyarrow.orc")
    return {path.stem: orc.read_table(path).to_pylist()}


def write_orc(tables: Tables, out_path: Path) -> list[Path]:
    pa = _require("pyarrow")
    orc = _require("pyarrow.orc")
    _, rows = next(iter(tables.items()))
    cols = _columns(rows)
    arrays = {c: [r.get(c) for r in rows] for c in cols}
    orc.write_table(pa.table(arrays), out_path)
    return [out_path]


def load_arrow(path: Path) -> Tables:
    ipc = _require("pyarrow.ipc")
    ipc_feather = _require("pyarrow.feather")
    with path.open("rb") as fh:
        reader = ipc.open_file(fh)
        return {path.stem: reader.read_all().to_pylist()}


def write_arrow(tables: Tables, out_path: Path) -> list[Path]:
    pa = _require("pyarrow")
    ipc = _require("pyarrow.ipc")
    _, rows = next(iter(tables.items()))
    cols = _columns(rows)
    arrays = {c: [r.get(c) for r in rows] for c in cols}
    tbl = pa.table(arrays)
    with out_path.open("wb") as fh, ipc.new_file(fh, tbl.schema) as writer:
        writer.write_table(tbl)
    return [out_path]


def load_yaml(path: Path) -> Tables:
    yaml = _require("yaml")
    return _as_tables(yaml.safe_load(read_text(path)), path.stem)


def write_yaml(tables: Tables, out_path: Path) -> list[Path]:
    yaml = _require("yaml")
    payload = next(iter(tables.values())) if len(tables) == 1 else tables
    out_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return [out_path]


def load_toml(path: Path) -> Tables:
    try:
        import tomllib as toml_mod
    except ImportError:
        toml_mod = _require("tomli")
    return _as_tables(toml_mod.loads(read_text(path)), path.stem)


def write_toml(tables: Tables, out_path: Path) -> list[Path]:
    tomli_w = _require("tomli_w")
    payload = next(iter(tables.values())) if len(tables) == 1 else tables
    out_path.write_bytes(tomli_w.dumps(payload))
    return [out_path]


def load_xml(path: Path) -> Tables:
    ET = _require("xml.etree.ElementTree")
    tree = ET.parse(path)
    root = tree.getroot()
    if any(child.tag != "row" for child in root):
        out: Tables = {}
        for child in root:
            rows: list[dict[str, Any]] = []
            for r in child:
                rows.append({sub.tag: sub.text for sub in r})
            out[child.tag] = rows
        return out
    rows = []
    for child in root:
        rows.append({sub.tag: sub.text for sub in child})
    return {path.stem: rows}


def write_xml(tables: Tables, out_path: Path) -> list[Path]:
    ET = _require("xml.etree.ElementTree")
    root = ET.Element("data")
    for name, rows in tables.items():
        tbl = ET.SubElement(root, name)
        cols = _columns(rows)
        for row in rows:
            r = ET.SubElement(tbl, "row")
            for c in cols:
                v = row.get(c)
                el = ET.SubElement(r, c)
                el.text = "" if v is None else str(v)
    ET.ElementTree(root).write(out_path, encoding="utf-8", xml_declaration=True)
    return [out_path]


def load_pickle(path: Path) -> Tables:
    import pickle

    return _as_tables(pickle.loads(path.read_bytes()), path.stem)


def write_pickle(tables: Tables, out_path: Path) -> list[Path]:
    import pickle

    payload = next(iter(tables.values())) if len(tables) == 1 else tables
    out_path.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
    return [out_path]


def load_msgpack(path: Path) -> Tables:
    msgpack = _require("msgpack")
    data = msgpack.unpackb(path.read_bytes(), raw=False)
    return _as_tables(data, path.stem)


def write_msgpack(tables: Tables, out_path: Path) -> list[Path]:
    msgpack = _require("msgpack")
    payload = next(iter(tables.values())) if len(tables) == 1 else tables
    out_path.write_bytes(msgpack.packb(payload, use_bin_type=True))
    return [out_path]


def load_avro(path: Path) -> Tables:
    fastavro = _require("fastavro")
    with path.open("rb") as fh:
        return {path.stem: list(fastavro.reader(fh))}


def write_avro(tables: Tables, out_path: Path) -> list[Path]:
    fastavro = _require("fastavro")
    name, rows = next(iter(tables.items()))
    if not rows:
        msg = "cannot infer Avro schema from empty table"
        raise ValueError(msg)
    fields = []
    for c in _columns(rows):
        sample = next((r.get(c) for r in rows if r.get(c) is not None), None)
        if isinstance(sample, bool):
            ftype = "boolean"
        elif isinstance(sample, int):
            ftype = "long"
        elif isinstance(sample, float):
            ftype = "double"
        else:
            ftype = "string"
        fields.append({"name": c, "type": ["null", ftype], "default": None})
    schema = {"type": "record", "name": name, "fields": fields}
    with out_path.open("wb") as fh:
        fastavro.writer(fh, schema, rows)
    return [out_path]


def load_bson(path: Path) -> Tables:
    bson_mod = _require("bson")
    data = path.read_bytes()
    rows = list(bson_mod.decode_all(data))
    return _as_tables(rows, path.stem)


def write_bson(tables: Tables, out_path: Path) -> list[Path]:
    bson_mod = _require("bson")
    _, rows = next(iter(tables.items()))
    with out_path.open("wb") as fh:
        for row in rows:
            fh.write(bson_mod.encode(row))
    return [out_path]


def load_hdf5(path: Path) -> Tables:
    pd = _require("pandas")
    out: Tables = {}
    with pd.HDFStore(path, mode="r") as store:
        for key in store:
            clean = key.lstrip("/")
            df = store.get(key)
            out[clean] = df.to_dict(orient="records")
    return out


def write_hdf5(tables: Tables, out_path: Path) -> list[Path]:
    pd = _require("pandas")
    _require("tables")
    with pd.HDFStore(out_path, mode="w") as store:
        for name, rows in tables.items():
            store.put(name, pd.DataFrame(rows), format="table")
    return [out_path]


def load_netcdf(path: Path) -> Tables:
    xr = _require("xarray")
    ds = xr.open_dataset(path)
    try:
        return {var: ds[var].to_dataframe().reset_index().to_dict("records") for var in ds.data_vars}
    finally:
        ds.close()


def write_netcdf(tables: Tables, out_path: Path) -> list[Path]:
    xr = _require("xarray")
    pd = _require("pandas")
    ds = xr.Dataset()
    for name, rows in tables.items():
        df = pd.DataFrame(rows)
        ds[name] = xr.DataArray(df.values, dims=[f"{name}_i", f"{name}_j"])
    ds.to_netcdf(out_path)
    return [out_path]


def load_zarr(path: Path) -> Tables:
    xr = _require("xarray")
    ds = xr.open_zarr(path)
    try:
        return {var: ds[var].to_dataframe().reset_index().to_dict("records") for var in ds.data_vars}
    finally:
        ds.close()


def write_zarr(tables: Tables, out_path: Path) -> list[Path]:
    xr = _require("xarray")
    pd = _require("pandas")
    ds = xr.Dataset()
    for name, rows in tables.items():
        df = pd.DataFrame(rows)
        ds[name] = xr.DataArray(df.values, dims=[f"{name}_i", f"{name}_j"])
    ds.to_zarr(out_path, mode="w")
    return [out_path]


def load_rds(path: Path) -> Tables:
    pyreadr = _require("pyreadr")
    result = pyreadr.read_r(str(path))
    return {name: df.to_dict(orient="records") for name, df in result.items()}


def write_rds(tables: Tables, out_path: Path) -> list[Path]:
    pyreadr = _require("pyreadr")
    pd = _require("pandas")
    _, rows = next(iter(tables.items()))
    pyreadr.write_rds(str(out_path), pd.DataFrame(rows))
    return [out_path]


def load_dta(path: Path) -> Tables:
    pd = _require("pandas")
    df = pd.read_stata(path)
    return {path.stem: df.to_dict(orient="records")}


def write_dta(tables: Tables, out_path: Path) -> list[Path]:
    pd = _require("pandas")
    _, rows = next(iter(tables.items()))
    pd.DataFrame(rows).to_stata(out_path)
    return [out_path]


def load_sav(path: Path) -> Tables:
    pyreadstat = _require("pyreadstat")
    df, _meta = pyreadstat.read_sav(str(path))
    return {path.stem: df.to_dict(orient="records")}


def load_sas7bdat(path: Path) -> Tables:
    pyreadstat = _require("pyreadstat")
    df, _meta = pyreadstat.read_sas7bdat(str(path))
    return {path.stem: df.to_dict(orient="records")}


def load_dbf(path: Path) -> Tables:
    dbfread = _require("dbfread")
    return {path.stem: [dict(r) for r in dbfread.DBF(str(path))]}


def write_dbf(tables: Tables, out_path: Path) -> list[Path]:
    dbf = _require("dbf")
    _, rows = next(iter(tables.items()))
    if not rows:
        msg = "cannot write an empty dbf"
        raise ValueError(msg)
    cols = _columns(rows)
    table = dbf.Table(str(out_path), " ".join(f"{c} C(254)" for c in cols))
    table.open(dbf.READ_WRITE)
    try:
        for row in rows:
            table.append(tuple("" if row.get(c) is None else str(row.get(c)) for c in cols))
    finally:
        table.close()
    return [out_path]


def _geo_to_tables(gdf) -> Tables:
    df = gdf.copy()
    if "geometry" in df.columns:
        df["geometry"] = df["geometry"].apply(lambda g: g.wkt if g is not None else None)
    return {df.attrs.get("name", "features"): df.to_dict(orient="records")}


def load_geojson(path: Path) -> Tables:
    gpd = _require("geopandas")
    gdf = gpd.read_file(path)
    gdf.attrs["name"] = path.stem
    return _geo_to_tables(gdf)


def write_geojson(tables: Tables, out_path: Path) -> list[Path]:
    gpd = _require("geopandas")
    shapely_wkt = _require("shapely.wkt")
    _, rows = next(iter(tables.items()))
    df = _geo = None
    import pandas as pd

    df = pd.DataFrame(rows)
    if "geometry" in df.columns:
        df["geometry"] = df["geometry"].apply(lambda w: shapely_wkt.loads(w) if isinstance(w, str) and w else None)
        gdf = gpd.GeoDataFrame(df, geometry="geometry")
    else:
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy([0] * len(df), [0] * len(df)))
    gdf.to_file(out_path, driver="GeoJSON")
    return [out_path]


def load_shapefile(path: Path) -> Tables:
    gpd = _require("geopandas")
    gdf = gpd.read_file(path)
    gdf.attrs["name"] = path.stem
    return _geo_to_tables(gdf)


def write_shapefile(tables: Tables, out_path: Path) -> list[Path]:
    gpd = _require("geopandas")
    shapely_wkt = _require("shapely.wkt")
    import pandas as pd

    _, rows = next(iter(tables.items()))
    df = pd.DataFrame(rows)
    if "geometry" in df.columns:
        df["geometry"] = df["geometry"].apply(lambda w: shapely_wkt.loads(w) if isinstance(w, str) and w else None)
        gdf = gpd.GeoDataFrame(df, geometry="geometry")
    else:
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy([0] * len(df), [0] * len(df)))
    gdf.to_file(out_path)
    return [out_path]


def load_ini(path: Path) -> Tables:
    import configparser

    cp = configparser.ConfigParser()
    cp.read(path)
    out: Tables = {}
    for section in cp.sections():
        out[section] = [{"key": k, "value": v} for k, v in cp.items(section)]
    return out


def write_ini(tables: Tables, out_path: Path) -> list[Path]:
    import configparser

    cp = configparser.ConfigParser()
    for name, rows in tables.items():
        cp[name] = {str(r.get("key")): str(r.get("value", "")) for r in rows if r.get("key") is not None}
    with out_path.open("w", encoding="utf-8") as fh:
        cp.write(fh)
    return [out_path]


def load_fixed_width(path: Path) -> Tables:
    schema_path = path.with_suffix(path.suffix + ".schema.json")
    if not schema_path.exists():
        msg = f"fixed-width '{path}' requires a schema at {schema_path}"
        raise ValueError(msg)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    columns = schema["columns"]
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            pos = 0
            row: dict[str, Any] = {}
            for c in columns:
                w = int(c["width"])
                row[c["name"]] = line[pos : pos + w].strip()
                pos += w
            rows.append(row)
    return {path.stem: rows}


def write_fixed_width(tables: Tables, out_path: Path) -> list[Path]:
    _, rows = next(iter(tables.items()))
    cols = _columns(rows)
    widths = {}
    for c in cols:
        widths[c] = max([len(str(c))] + [len(str(row.get(c, ""))) for row in rows])
    schema = {"columns": [{"name": c, "width": widths[c]} for c in cols]}
    schema_path = out_path.with_suffix(out_path.suffix + ".schema.json")
    schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("".join(str(c).ljust(widths[c]) for c in cols) + "\n")
        for row in rows:
            fh.write("".join(str(row.get(c, "")).ljust(widths[c]) for c in cols) + "\n")
    return [out_path, schema_path]


EXT_TO_FMT: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".db": "db",
    ".sqlite": "db",
    ".sqlite3": "db",
    ".sql": "sql",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xls",
    ".xlsb": "xlsb",
    ".ods": "ods",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".feather": "feather",
    ".orc": "orc",
    ".arrow": "arrow",
    ".ipc": "arrow",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".pkl": "pickle",
    ".pickle": "pickle",
    ".msgpack": "msgpack",
    ".mp": "msgpack",
    ".avro": "avro",
    ".bson": "bson",
    ".h5": "hdf5",
    ".hdf5": "hdf5",
    ".nc": "netcdf",
    ".nc4": "netcdf",
    ".zarr": "zarr",
    ".rds": "rds",
    ".rdata": "rds",
    ".dta": "dta",
    ".sav": "sav",
    ".sas7bdat": "sas7bdat",
    ".dbf": "dbf",
    ".geojson": "geojson",
    ".shp": "shapefile",
    ".ini": "ini",
    ".fw": "fixedwidth",
    ".fixed": "fixedwidth",
}
FMT_TO_EXT: dict[str, str] = {
    "csv": ".csv",
    "json": ".json",
    "jsonl": ".jsonl",
    "db": ".db",
    "sql": ".sql",
    "xlsx": ".xlsx",
    "xls": ".xls",
    "xlsb": ".xlsb",
    "ods": ".ods",
    "parquet": ".parquet",
    "feather": ".feather",
    "orc": ".orc",
    "arrow": ".arrow",
    "yaml": ".yaml",
    "toml": ".toml",
    "xml": ".xml",
    "pickle": ".pkl",
    "msgpack": ".msgpack",
    "avro": ".avro",
    "bson": ".bson",
    "hdf5": ".h5",
    "netcdf": ".nc",
    "zarr": ".zarr",
    "rds": ".rds",
    "dta": ".dta",
    "sav": ".sav",
    "sas7bdat": ".sas7bdat",
    "dbf": ".dbf",
    "geojson": ".geojson",
    "shapefile": ".shp",
    "ini": ".ini",
    "fixedwidth": ".fw",
}
LOADERS: dict[str, Callable[[Path], Tables]] = {
    "csv": load_csv,
    "json": load_json,
    "jsonl": load_jsonl,
    "db": load_db,
    "sql": load_sql,
    "xlsx": load_xlsx,
    "xls": load_xls,
    "xlsb": load_xlsb,
    "ods": load_ods,
    "parquet": load_parquet,
    "feather": load_feather,
    "orc": load_orc,
    "arrow": load_arrow,
    "yaml": load_yaml,
    "toml": load_toml,
    "xml": load_xml,
    "pickle": load_pickle,
    "msgpack": load_msgpack,
    "avro": load_avro,
    "bson": load_bson,
    "hdf5": load_hdf5,
    "netcdf": load_netcdf,
    "zarr": load_zarr,
    "rds": load_rds,
    "dta": load_dta,
    "sav": load_sav,
    "sas7bdat": load_sas7bdat,
    "dbf": load_dbf,
    "geojson": load_geojson,
    "shapefile": load_shapefile,
    "ini": load_ini,
    "fixedwidth": load_fixed_width,
}
WRITERS: dict[str, Callable[[Tables, Path], list[Path]]] = {
    "csv": write_csv,
    "json": write_json,
    "jsonl": write_jsonl,
    "db": write_db,
    "sql": write_sql,
    "xlsx": write_xlsx,
    "ods": write_ods,
    "parquet": write_parquet,
    "feather": write_feather,
    "orc": write_orc,
    "arrow": write_arrow,
    "yaml": write_yaml,
    "toml": write_toml,
    "xml": write_xml,
    "pickle": write_pickle,
    "msgpack": write_msgpack,
    "avro": write_avro,
    "bson": write_bson,
    "hdf5": write_hdf5,
    "netcdf": write_netcdf,
    "zarr": write_zarr,
    "rds": write_rds,
    "dta": write_dta,
    "dbf": write_dbf,
    "geojson": write_geojson,
    "shapefile": write_shapefile,
    "ini": write_ini,
    "fixedwidth": write_fixed_width,
}


def _output_path(src: Path, target_fmt: str, out_dir: Optional[Path]) -> Path:
    name = src.stem + FMT_TO_EXT[target_fmt]
    return (out_dir / name) if out_dir else src.with_name(name)


def convert_job(
    job: tuple[str, str, Optional[str]],
) -> tuple[str, bool, list[str], str]:
    src_str, target_fmt, out_dir_str = job
    src = Path(src_str)
    try:
        src_fmt = detect_format(src)
        if src_fmt is None:
            msg = f"unsupported input extension {src.suffix!r}"
            raise ValueError(msg)
        if src_fmt not in LOADERS:
            msg = f"format {src_fmt!r} is write-only, cannot read"
            raise ValueError(msg)
        if target_fmt not in WRITERS:
            msg = f"format {target_fmt!r} is read-only, cannot write"
            raise ValueError(msg)
        if src_fmt == target_fmt:
            return src_str, True, [], f"skipped: already {target_fmt}"
        logger.info(f"{src} [{src_fmt}] -> {target_fmt}")
        tables = LOADERS[src_fmt](src)
        if not tables:
            msg = "no tables / rows found in input"
            raise ValueError(msg)
        out_path = _output_path(src, target_fmt, Path(out_dir_str) if out_dir_str else None)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        written = WRITERS[target_fmt](tables, out_path)
        return src_str, True, [str(p) for p in written], "ok"
    except Exception as exc:
        logger.exception(f"failed to convert {src}")
        return src_str, False, [], f"{type(exc).__name__}: {exc}"


def detect_format(path: Path) -> Optional[str]:
    return EXT_TO_FMT.get(path.suffix.lower())


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dataconv",
        description="Convert between CSV, JSON, SQLite, SQL, Excel, Parquet, "
        "YAML, XML, Avro, HDF5, NetCDF, GeoJSON, and many more.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    for fmt in sorted(WRITERS.keys()):
        group.add_argument(f"--{fmt}", action="store_true", help=f"write {fmt} output")
    parser.add_argument("inputs", nargs="+", type=Path, help="input file(s)")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directory for outputs (default: next to input)",
    )
    return parser.parse_args(argv)


def _resolve_target(args: argparse.Namespace) -> str:
    for fmt in WRITERS:
        if getattr(args, fmt, False):
            return fmt
    msg = "no output format specified"
    raise SystemExit(msg)


def main(argv: Optional[list[str]] = None) -> int:
    logger.remove()
    logger.add(sys.stderr, format="<level>{level: <8}</level> | {message}")
    args = parse_args(argv)
    target_fmt = _resolve_target(args)
    sources: list[Path] = []
    for p in args.inputs:
        if not p.exists():
            logger.error(f"input not found: {p}")
            continue
        if not p.is_file() and p.suffix.lower() != ".zarr":
            logger.error(f"not a file: {p}")
            continue
        sources.append(p)
    if not sources:
        logger.error("no usable inputs")
        return 2
    jobs = [(str(p), target_fmt, str(args.output_dir) if args.output_dir else None) for p in sources]
    if len(jobs) > 1 and MAX_WORKERS > 1:
        workers = min(MAX_WORKERS, len(jobs))
        logger.info(f"converting {len(jobs)} files with {workers} workers")
        with Pool(processes=workers) as pool:
            results = pool.map(convert_job, jobs, chunksize=1)
    else:
        results = [convert_job(j) for j in jobs]
    failures = 0
    for src, ok, outputs, msg in results:
        if ok:
            if outputs:
                logger.success(f"{src} -> {', '.join(outputs)}")
            elif msg:
                logger.warning(f"{src}: {msg}")
        else:
            failures += 1
            logger.error(f"{src}: {msg}")
    logger.info(f"done: {len(results) - failures} ok, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

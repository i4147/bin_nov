#!/data/data/com.termux/files/usr/bin/env python
"""Run SQL queries defined in YAML/JSON config files and export the results to JSON or CSV.
Reads the connection and query definitions (a config file or the ``queries`` folder), binds the parameters and the date placeholders (``CURRENT_DATE`` and the month/year boundaries), executes the statements in a read-only transaction with SQLAlchemy and writes the rows as JSON, NDJSON or CSV.
The CLI is driven by ``fire``.
Usage: sqltojson run_query CONFIG --params '{"key": "value"}' Examples: $ sqltojson run_query reports.yml --params '{"year": 2024}' Runs as a script (``python sqltojson.py``) in the directory it should act on; the module can also be imported."""

from __future__ import absolute_import, annotations
import calendar
import contextlib
import csv
import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
import fire
from sqlalchemy import create_engine
from sqlalchemy.sql import text


__version__: str = "0.0.0"
DATE_FIELDS: list[str] = [
    "CURRENT_DATE",
    "START_CURRENT_MONTH",
    "END_CURRENT_MONTH",
    "START_CURRENT_YEAR",
    "END_CURRENT_YEAR",
]


def _is_number(s: float) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _first_day_month(current_date: str) -> str:
    return current_date.replace(day=1)


def _first_day_year(current_date: str) -> str:
    return current_date.replace(day=1).replace(month=1)


def _last_day_year(current_date: str) -> str:
    return current_date.replace(day=31).replace(month=12)


def _last_day_month(current_date: str) -> str:
    month_last_day = calendar.monthrange(current_date.year, current_date.month)[1]
    return current_date.replace(day=month_last_day)


def _parse_field(field: str, to_add: Any, current_date: Any, date_format="%Y-%m-%d"):
    if field == "CURRENT_DATE":
        return (current_date + dt.timedelta(days=to_add)).strftime(date_format)
    elif field == "START_CURRENT_MONTH":
        return (_first_day_month(current_date) + relativedelta(months=to_add)).strftime(date_format)
    elif field == "END_CURRENT_MONTH":
        return (_last_day_month(current_date) + relativedelta(months=to_add)).strftime(date_format)
    elif field == "START_CURRENT_YEAR":
        return (_first_day_year(current_date) + relativedelta(years=to_add)).strftime(date_format)
    elif field == "END_CURRENT_YEAR":
        return (_last_day_year(current_date) + relativedelta(years=to_add)).strftime(date_format)
    else:
        return field


def _parse_formula(formula: str, current_date: Any, date_format="%Y-%m-%d"):
    if any(ext in formula for ext in DATE_FIELDS):
        if "+" in formula:
            parts = formula.split("+")
            return _parse_field(parts[0].strip(), int(parts[1]), current_date, date_format)
        elif "-" in formula:
            parts = formula.split("-")
            return _parse_field(parts[0].strip(), -int(parts[1]), current_date, date_format)
    return _parse_field(formula.strip(), 0, current_date, date_format)


def parse_parameter(param_value: str, current_date: Any, format_separator="|"):
    if not param_value:
        return param_value
    elif _is_number(param_value):
        return param_value
    elif format_separator in str(param_value):
        field, date_format = param_value.split(format_separator)
        return _parse_formula(field, current_date, date_format.strip())
    else:
        return _parse_formula(param_value, current_date)


def _current_date(timezone: Optional[str] = None) -> dt.date:
    if timezone:
        return dt.datetime.now(ZoneInfo(timezone)).date()
    return dt.date.today()


def map_result_proxy2list_dict(result_proxy: dict[str, Any]) -> list:
    keys = list(result_proxy.keys())
    return [dict(zip(keys, row)) for row in result_proxy]


_TRUTHY_STRINGS: set[str] = {"true", "t", "yes", "y", "1", "on"}


def _coerce_bool(value: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return bool(value)


def _begin_read_only(con: Any) -> None:
    dialect = con.engine.dialect.name
    try:
        if dialect == "sqlite":
            con.exec_driver_sql("PRAGMA query_only = ON")
        elif dialect in ("postgresql", "mysql", "mariadb"):
            con.exec_driver_sql("SET TRANSACTION READ ONLY")
    except Exception:
        pass


def _is_read_only_violation(exc: Exception) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "readonly" in message or "read only" in message or "read-only" in message


def handle_run_query2json(
    name="default",
    query="default",
    wrapper=False,
    first=False,
    key="",
    value="",
    jsonkeys="",
    format="json",
    output=None,
    list_connections=False,
    list_queries: bool | str = False,
    timezone=None,
    read_only=False,
    **kwargs: Any,
) -> None:
    try:
        if isinstance(name, str) and name.endswith(".sql") and Path(name).is_file():
            if query == "default":
                query = "@" + name
            if output is None:
                output = (str(Path(name).with_suffix("")), Path(name).suffix)[0] + ".json"
            name = "default"
        if list_connections:
            config_path = kwargs.get("config")
            print(json.dumps(_list_connections(config_path)))
            return
        if list_queries:
            config_path = kwargs.get("config")
            if isinstance(list_queries, str) and list_queries.lower() in {
                "legacy",
                "flat",
                "names",
            }:
                print(json.dumps(_list_queries(config_path)))
            else:
                print(json.dumps(_list_queries(config_path, scoped=True)))
            return
        result = run_query2json(
            name,
            query,
            wrapper,
            first,
            key,
            value,
            jsonkeys,
            timezone=timezone,
            read_only=read_only,
            **kwargs,
        )
        emit_result(result, format, output, key, value, first, timezone)
    except Exception as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), file=sys.stderr)
        sys.exit(1)


def run_query(
    engine: Any,
    raw_query: str,
    timezone: Optional[str] = None,
    read_only: bool = False,
    **kwargs: Any,
) -> list | dict:
    read_only = _coerce_bool(read_only)
    current_date = _current_date(timezone)
    parameters = {k: parse_parameter(v, current_date) for k, v in kwargs.items()}
    with engine.connect() as con:
        if read_only:
            _begin_read_only(con)
        try:
            result_proxy = con.execute(text(raw_query), parameters)
            if result_proxy.returns_rows:
                records: list | dict = map_result_proxy2list_dict(result_proxy)
            else:
                records = {"rowcount": max(result_proxy.rowcount, 0)}
        except Exception as exc:
            if read_only and _is_read_only_violation(exc):
                con.rollback()
                return {"rowcount": 0}
            raise
        finally:
            if read_only and con.engine.dialect.name == "sqlite":
                with contextlib.suppress(Exception):
                    con.exec_driver_sql("PRAGMA query_only = OFF")
        if read_only:
            con.rollback()
        else:
            con.commit()
    return records


def load_config_file(config_path: str) -> dict:
    try:
        with open(config_path) as json_file:
            return json.load(json_file)
    except Exception:
        pass
    return {
        "conections": {"default": "sqlite:///:memory:"},
        "queries": {"default": "SELECT 1 AS a, 2 AS b"},
    }


def _find_config() -> str:
    candidates = [
        Path(os.getcwd()).joinpath("sql2json.json"),
        Path(os.getcwd()).joinpath(".sql2json", "config.json"),
        Path(Path("~").expanduser()).joinpath(".sql2json", "config.json"),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return candidates[-1]


def load_query_from_file(sql_file_path: str) -> str:
    file_name = sql_file_path
    if sql_file_path.startswith("@"):
        file_name = sql_file_path[1:]
    with open(file_name, "r") as file:
        return file.read()


def get_for_key_or_first_map_value(my_dict: dict, key: Optional[str] = None):
    if key in my_dict:
        return my_dict.get(key)
    for v in my_dict.values():
        return v
    return ""


def _get_connections_dict(config: dict) -> dict:
    return config.get("connections") or config.get("conections", {})


def list_connections(config_path: Optional[str] = None) -> list:
    path = config_path or _find_config()
    config = load_config_file(path)
    return list(_get_connections_dict(config).keys())


def _get_connection_queries_dict(config: dict, connections: dict) -> dict:
    connection_queries = config.get("connection_queries", {})
    if connection_queries is None:
        return {}
    if not isinstance(connection_queries, dict):
        msg = "connection_queries must be an object"
        raise ValueError(msg)
    for connection_name, queries in connection_queries.items():
        if connection_name not in connections:
            msg = f"connection_queries references unknown connection '{connection_name}'"
            raise ValueError(msg)
        if not isinstance(queries, dict):
            msg = f"connection_queries.{connection_name} must be an object"
            raise ValueError(msg)
        for query_name, raw_query in queries.items():
            if not isinstance(raw_query, str):
                msg = f"connection_queries.{connection_name}.{query_name} must be a string"
                raise ValueError(msg)
    return connection_queries


def list_queries(
    config_path: Optional[str] = None,
    scoped: bool = False,
    connection: Optional[str] = None,
) -> list | dict:
    path = config_path or _find_config()
    config = load_config_file(path)
    global_query_names = list(config.get("queries", {}).keys())
    if not scoped and connection is None:
        return global_query_names
    connections = _get_connections_dict(config)
    connection_queries = _get_connection_queries_dict(config, connections)
    if scoped:
        return {
            "global": global_query_names,
            "connections": {
                connection_name: list(queries.keys()) for connection_name, queries in connection_queries.items()
            },
        }
    if connection is not None:
        effective_query_names = list(global_query_names)
        for query_name in connection_queries.get(connection, {}):
            if query_name not in effective_query_names:
                effective_query_names.append(query_name)
        return effective_query_names
    return global_query_names


_list_connections = list_connections
_list_queries = list_queries


def _resolve_query_string(connection_name: str, query_name: str, connections: dict, config: dict) -> str:
    connection_queries = _get_connection_queries_dict(config, connections)
    scoped_queries = connection_queries.get(connection_name, {})
    if connection_name in connections and query_name in scoped_queries:
        return scoped_queries[query_name]
    config_queries = config.get("queries", {})
    return config_queries.get(query_name, query_name)


def run_query_by_name(conection_name: str = "default", query_name: str = "default", **kwargs: Any) -> list | dict:
    config_path = kwargs.pop("config", None) or _find_config()
    timezone = kwargs.pop("timezone", None)
    read_only = kwargs.pop("read_only", False)
    config = load_config_file(config_path)
    config_dbs = _get_connections_dict(config)
    conection_string = config_dbs.get(conection_name, conection_name)
    raw_query_string = _resolve_query_string(conection_name, query_name, config_dbs, config)
    if raw_query_string.startswith("@"):
        raw_query_string = load_query_from_file(raw_query_string[1:])
    engine = create_engine(conection_string)
    return run_query(engine, raw_query_string, timezone=timezone, read_only=read_only, **kwargs)


def parse_json_columns(result: dict, jsonkeys: str = "") -> dict:
    jsonkeys_list = []
    if type(jsonkeys) is tuple:
        jsonkeys_list = [key.strip() for key in jsonkeys]
    elif type(jsonkeys) is str:
        jsonkeys_list = [key.strip() for key in jsonkeys.split(",")]
    if not jsonkeys_list:
        return result
    response = {}
    for key in result:
        if key in jsonkeys_list:
            response[key] = json.loads(result[key])
        else:
            response[key] = result[key]
    return response


def apply_wrapper(result: str | dict | list, wrapper: bool | str = False) -> str | dict | list:
    if isinstance(wrapper, str) and wrapper:
        return {wrapper: result}
    elif wrapper:
        return {"data": result}
    return result


def apply_output_transforms(
    unparsed_results: list,
    wrapper: bool | str = False,
    first: bool = False,
    key: str = "",
    value: str = "",
    jsonkeys: str = "",
) -> str | dict | list:
    results = [parse_json_columns(result, jsonkeys) for result in unparsed_results]
    result: str | dict | list | None = None
    if first:
        if results and len(results) > 0:
            item = results[0]
            if key and value:
                result = {item.get(key): item.get(value)}
            else:
                result = get_for_key_or_first_map_value(item, key) if key else item
        else:
            result = "" if key and not value else {}
    elif key and value:
        result = [
            ({item.get(key): item.get(value)} if key and key in item and value in item else item) for item in results
        ]
    else:
        result = [item.get(key) if key and key in item else item for item in results]
    return apply_wrapper(result, wrapper)


def _warn_read_only_write(result: dict) -> None:
    print(
        "read-only mode: write not persisted (re-run without --read-only to commit the change).",
        file=sys.stderr,
    )


def run_query2json(
    name: str = "default",
    query: str = "default",
    wrapper: bool | str = False,
    first: bool = False,
    key: str = "",
    value: str = "",
    jsonkeys: str = "",
    timezone: Optional[str] = None,
    read_only: bool = False,
    **kwargs: Any,
) -> str | dict | list:
    read_only = _coerce_bool(read_only)
    result = run_query_by_name(name, query, timezone=timezone, read_only=read_only, **kwargs)
    if isinstance(result, dict):
        if read_only:
            _warn_read_only_write(result)
        return apply_wrapper(result, wrapper)
    return apply_output_transforms(result, wrapper, first, key, value, jsonkeys)


def _parse_filename_after_brackets(part: str, current_date: Any):
    if "}" in part:
        new_parts = [parse_parameter(item, current_date) for item in part.split("}")]
        return "".join(new_parts)
    return parse_parameter(part, current_date)


def parse_filename(file_name: str, current_date: Any) -> str:
    parts = file_name.split("{")
    results = [_parse_filename_after_brackets(part, current_date) for part in parts]
    return "".join(results)


def json_default(value: float) -> float:
    if isinstance(value, Decimal):
        return float(value)
    msg = f"Object of type {type(value).__name__} is not JSON serializable"
    raise TypeError(msg)


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=json_default)


def save_json(rows: Iterable[Any], file_name: str, timezone=None) -> None:
    current_date = _current_date(timezone)
    parsed_filename = parse_filename(file_name, current_date)
    final_file_name = parsed_filename if ".json" in parsed_filename else parsed_filename + ".json"
    with open(final_file_name, "w") as outfile:
        outfile.write(json_dumps(rows))


def save_csv(rows: Iterable[Any], file_name: str, dialect: str, key: str, timezone=None) -> None:
    current_date = _current_date(timezone)
    parsed_filename = parse_filename(file_name, current_date)
    ext = ".xls" if dialect == "excel" else ".csv"
    final_file_name = parsed_filename if ext in parsed_filename else parsed_filename + ext
    first_row = None
    final_rows = None
    try:
        if isinstance(rows, str):
            first_row = {}
            final_key = key or "key"
            first_row[final_key] = rows
            final_rows = [first_row]
        elif isinstance(rows, dict):
            first_row = rows
            final_rows = [rows]
        else:
            first_row = rows[0]
            final_rows = rows
    except Exception:
        pass
    csv_columns = list(first_row.keys())
    with open(final_file_name, "w") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=csv_columns,
            dialect=dialect if dialect == "excel" else None,
        )
        writer.writeheader()
        for data in final_rows:
            writer.writerow(data)


def emit_result(result: str, format: str, output: Path, key: str, value: Any, first: Any, timezone: Any) -> None:
    if output:
        if format == "csv":
            save_csv(result, output, "csv", key, timezone=timezone)
        elif format == "excel":
            save_csv(result, output, "excel", key, timezone=timezone)
        else:
            save_json(result, output, timezone=timezone)
    elif key and value and first:
        print(json_dumps(result))
    elif key and first:
        print(result)
    else:
        print(json_dumps(result))


def handle_run_query2json(
    name="default",
    query="default",
    wrapper=False,
    first=False,
    key="",
    value="",
    jsonkeys="",
    format="json",
    output=None,
    list_connections=False,
    list_queries: bool | str = False,
    timezone=None,
    read_only=False,
    **kwargs: Any,
) -> None:
    try:
        if isinstance(name, str) and name.endswith(".sql") and Path(name).is_file():
            if query == "default":
                query = "@" + name
            if output is None:
                output = (str(Path(name).with_suffix("")), Path(name).suffix)[0] + ".json"
            name = "default"
        if list_connections:
            config_path = kwargs.get("config")
            print(json.dumps(_list_connections(config_path)))
            return
        if list_queries:
            config_path = kwargs.get("config")
            if isinstance(list_queries, str) and list_queries.lower() in {
                "legacy",
                "flat",
                "names",
            }:
                print(json.dumps(_list_queries(config_path)))
            else:
                print(json.dumps(_list_queries(config_path, scoped=True)))
            return
        result = run_query2json(
            name,
            query,
            wrapper,
            first,
            key,
            value,
            jsonkeys,
            timezone=timezone,
            read_only=read_only,
            **kwargs,
        )
        emit_result(result, format, output, key, value, first, timezone)
    except Exception as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if any(arg in ("--version", "-v") for arg in sys.argv[1:]):
        print(f"sql2json {__version__}")
        sys.exit(0)
    fire.Fire(handle_run_query2json)


if __name__ == "__main__":
    main()

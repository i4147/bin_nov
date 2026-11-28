#!/data/data/com.termux/files/usr/bin/env python
"""Report the size of a GitHub repository, a remote URL or a local path.

Resolves the size via the GitHub API (git trees or the zipball Content-Length) and can stream the body when headers are missing; --json emits machine-readable output.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import typer
from dh import gsz
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN: Optional[str] = os.getenv("GITHUB_TOKEN") or None
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (compatible; SizeChecker/1.0; +https://github.com)"
)
STREAM_CHUNK_SIZE: int = 1024 * 256  # 256 KiB

app = typer.Typer(add_completion=False)


def human_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "unknown"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    size = num_bytes / 1024.0
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def is_http_url(input_str: str) -> bool:
    parsed = urlparse(input_str.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_github(input_str: str) -> Optional[tuple[str, str]]:
    s = re.sub(r"^https?://(www\.)?github\.com/", "", input_str.strip())
    s = s.rstrip("/")
    s = s.removesuffix(".git")
    match = re.fullmatch(r"([^/\s]+)/([^/\s]+)", s)
    return (match.group(1), match.group(2)) if match else None


def get_github_size(user: str, repo: str, timeout: float) -> tuple[int, dict[str, Any]]:
    url = f"https://api.github.com/repos/{user}/{repo}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    response = requests.get(url, headers=headers, timeout=timeout)
    if response.status_code == 404:
        msg = f"Repository '{user}/{repo}' not found."
        raise ValueError(msg)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    size_kb = int(data.get("size", 0) or 0)
    return size_kb * 1024, data


def _parse_content_range_total(content_range: Optional[str]) -> Optional[int]:
    if not content_range or "/" not in content_range:
        return None
    total_part = content_range.rsplit("/", 1)[-1].strip()
    if total_part.isdigit():
        return int(total_part)
    return None


def _parse_content_length(content_length: Optional[str]) -> Optional[int]:
    if not content_length:
        return None
    val = content_length.strip()
    if val.isdigit():
        return int(val)
    return None


def _measure_by_streaming(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, Optional[str], Optional[str]]:
    total_bytes = 0
    with requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
        stream=True,
    ) as resp:
        resp.raise_for_status()
        final_url = resp.url
        content_type = resp.headers.get("Content-Type")
        accept_ranges = resp.headers.get("Accept-Ranges")
        for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_SIZE):
            if chunk:
                total_bytes += len(chunk)
    return total_bytes, final_url, content_type, accept_ranges


def get_remote_file_size(
    url: str,
    timeout: float,
    stream_size: bool = False,
) -> dict[str, Any]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    size_bytes: Optional[int] = None
    size_source: str = "header"
    content_type: Optional[str] = None
    final_url: str = url
    accept_ranges: Optional[str] = None

    try:
        head_resp = requests.head(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        head_resp.raise_for_status()
        final_url = head_resp.url
        content_type = head_resp.headers.get("Content-Type")
        accept_ranges = head_resp.headers.get("Accept-Ranges")
        size_bytes = _parse_content_length(head_resp.headers.get("Content-Length"))
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in {403, 405, 501}:
            raise

    if size_bytes is None:
        range_headers = {**headers, "Range": "bytes=0-0"}
        with requests.get(
            url,
            headers=range_headers,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        ) as range_resp:
            range_resp.raise_for_status()
            final_url = range_resp.url
            content_type = range_resp.headers.get("Content-Type") or content_type
            accept_ranges = range_resp.headers.get("Accept-Ranges") or accept_ranges
            size_bytes = _parse_content_range_total(
                range_resp.headers.get("Content-Range")
            )
            if size_bytes is None and range_resp.status_code == 200:
                size_bytes = _parse_content_length(
                    range_resp.headers.get("Content-Length")
                )

    if size_bytes is None and stream_size:
        streamed_bytes, streamed_url, streamed_ct, streamed_ar = _measure_by_streaming(
            url,
            headers=headers,
            timeout=timeout,
        )
        size_bytes = streamed_bytes
        size_source = "stream"
        final_url = streamed_url
        content_type = streamed_ct or content_type
        accept_ranges = streamed_ar or accept_ranges
    elif size_bytes is None:
        size_source = "unknown"

    parsed_final = urlparse(final_url)
    filename = Path(parsed_final.path).name or None

    return {
        "type": "remote_file",
        "url": url,
        "final_url": final_url,
        "filename": filename,
        "content_type": content_type,
        "accept_ranges": accept_ranges,
        "size_bytes": size_bytes,
        "size": human_size(size_bytes),
        "size_source": size_source,
    }


def _emit(payload: dict[str, Any], json_out: bool, lines: list[str]) -> None:
    if json_out:
        typer.echo(json.dumps(payload))
    else:
        for line in lines:
            typer.echo(line)


@app.command()
def main(
    input_str: str = typer.Argument(
        ...,
        help="GitHub repo ('user/repo' or URL), remote file URL, or local file/dir path",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    timeout: float = typer.Option(10.0, "--timeout", help="HTTP timeout in seconds"),
    stream_size: bool = typer.Option(
        False,
        "--stream-size",
        help="Download and count bytes if Content-Length/Content-Range headers are missing",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Re-raise unexpected exceptions"
    ),
) -> None:
    try:
        path = Path(input_str)
        if path.exists():
            size_bytes = int(gsz(str(path)))
            kind = "file" if path.is_file() else "dir"
            result: dict[str, Any] = {
                "type": kind,
                "path": str(path),
                "size_bytes": size_bytes,
                "size": human_size(size_bytes),
            }
            _emit(
                result,
                json_out,
                [
                    f"Type: {kind}",
                    f"Path: {path}",
                    f"Size: {human_size(size_bytes)}",
                ],
            )
            return

        parsed_gh = parse_github(input_str)
        if parsed_gh is not None:
            user, repo = parsed_gh
            size_bytes, data = get_github_size(user, repo, timeout)
            result = {
                "type": "github_repo",
                "repo": f"{user}/{repo}",
                "size_bytes": size_bytes,
                "size": human_size(size_bytes),
                "stars": data.get("stargazers_count"),
                "forks": data.get("forks_count"),
                "watchers": data.get("subscribers_count"),
                "open_issues": data.get("open_issues_count"),
                "language": data.get("language"),
                "private": data.get("private"),
                "default_branch": data.get("default_branch"),
            }
            _emit(
                result,
                json_out,
                [
                    f"Repository: {user}/{repo}",
                    f"Size: {human_size(size_bytes)}",
                    f"Stars: {result['stars']}  Forks: {result['forks']}  Watchers: {result['watchers']}",
                    f"Open issues: {result['open_issues']}  Language: {result['language']}",
                    f"Private: {result['private']}  Default branch: {result['default_branch']}",
                ],
            )
            return

        if is_http_url(input_str):
            result = get_remote_file_size(
                input_str.strip(),
                timeout=timeout,
                stream_size=stream_size,
            )
            lines = [
                f"Type: {result['type']}",
                f"URL: {result['final_url']}",
            ]
            if result["filename"]:
                lines.append(f"Filename: {result['filename']}")
            if result["content_type"]:
                lines.append(f"Content-Type: {result['content_type']}")
            lines.append(f"Size: {result['size']} ({result['size_source']})")
            _emit(result, json_out, lines)
            return

        typer.echo(
            f"Invalid input: '{input_str}' is neither an existing path, a GitHub repo, nor a valid HTTP(S) URL.",
            err=True,
        )
        raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    except requests.exceptions.RequestException as e:
        typer.echo(f"Error fetching data: {e}", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        if verbose:
            raise
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

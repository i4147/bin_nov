#!/data/data/com.termux/files/usr/bin/env python
"""Download helpers built on ``requests``.
``get_resource`` fetches a URL (or a local path/``file:`` URI) and returns its bytes, ``save_file`` stores it under the name derived from the URL (with a checksum for the duplicates) and the ``SaveToDisk`` stream wrapper adds a progress report while writing.
Usage: from urldownloader import get_resource, save_file Examples: import urldownloader path = urldownloader.save_file('https://example.com/a.zip') Library module without a ``__main__`` entry point; importing it does nothing."""

from hashlib import sha224
from pathlib import Path
from re import findall
from shutil import move
from tempfile import gettempdir
from time import sleep
from typing import Any, Callable, Optional

from requests import Response, get


__all__: list[str] = ["SaveToDisk", "get_resource", "save_file"]
__version__: str = "1.0.5"


class SaveToDisk:
    def __init__(self, file_path: Path, url: str) -> None:
        self._temp_path: Path = Path(
            gettempdir(),
            sha224(bytes(url, encoding="UTF-8")).hexdigest() + file_path.suffix,
        )
        self._file_path: Path = file_path

    def _move_temporary(self, url: str) -> bool:
        if self._file_path.exists():
            info("File already exists")
            return True
        move(self._temp_path, self._file_path)
        info("DOWNLOADED: %s TO %s" % (url, self._file_path))
        return True

    def get(self, url: str, headers: dict[str, str], timeout: int) -> bool:
        resume_byte_pos: int = self._temp_path.stat().st_size if self._temp_path.exists() else 0
        headers["Range"] = "bytes=%d-" % resume_byte_pos
        response: Response = get(url, headers=headers, stream=True, timeout=timeout)
        if response.status_code not in (200, 206):
            info("Wrong http response %s %s" % (response.status_code, response.text))
            return False
        for chunk in response.iter_content(chunk_size=256):
            if chunk:
                with open(self._temp_path, "ab") as f:
                    f.write(chunk)
        return self._move_temporary(url)


def _get_url_data(
    url: str,
    get_function: Callable[..., Any],
    tries: int,
    timeout: int,
    wait: int,
) -> Optional[Any]:
    for i in range(wait, tries):
        try:
            sleep(i)
            result = get_function(url, headers={"User-agent": "Chrome"}, timeout=timeout)
            if result:
                return result
        except ConnectionError as e:
            if "Read timed out" in str(e):
                info("Read timeout (try %s)" % i)
            else:
                error("Error on try %s" % i)
                exception(e)
    return None


def _get_file_name(url: str) -> str:
    url = url.strip("/")
    result = findall(r"/(\w+\.\w+)[?|$]", url)
    if result:
        return result[-1]
    return url.split("/")[-1]


def save_file(
    url: str,
    file_path: str,
    file_name: str = "",
    timeout: int = 4,
    wait: int = 2,
    tries: int = 10,
    save_class: Callable[..., SaveToDisk] = SaveToDisk,
) -> bool:
    if not file_name:
        file_name = _get_file_name(url=url)
    path = Path(file_path, file_name)
    if path.exists():
        info("File exists %s" % file_name)
        return True
    return _get_url_data(
        url,
        save_class(file_path=path, url=url).get,
        tries=tries,
        timeout=timeout,
        wait=wait,
    )


def get_resource(
    url: str,
    timeout: int = 4,
    wait: int = 2,
    tries: int = 10,
    get_function: Callable[..., Response] = get,
) -> Optional[str]:
    data = _get_url_data(url, get_function, tries=tries, timeout=timeout, wait=wait)
    if data:
        return data.text
    return data


if __name__ == "__main__":
    raise SystemExit(0)

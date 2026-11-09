#!/data/data/com.termux/files/usr/bin/env python
"""Search Google Images and download the results.
Builds the image-search URL from the query and the extra filters (exact/optional/excluded words, size, colour, type), scrapes the result links with Selenium and downloads them in parallel with requests, httpx or pycurl.
Usage: imgdownloader -q QUERY [-l LIMIT] [-o DIR] [--size large] [--type photo] Positional arguments: log-level=3 user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Options: -q, --query Query to search for -l, --limit Max images to download (default: 3) -o, --outputDir Full directory to store images in -c, --chromedriver Full path to the chromedriver binary -b, --backend HTTP backend library to use for downloading images (requests [default], pycurl, httpx) -t, --threads Number of parallel download threads (default: 4) -epq, --exactQuery Words that must be included -oq, --optionalQuery Words that are optional -eq, --exceptQuery Words that must not be included -cs, --customSize Find image of specific size (e.g., 1920*1080) ...
plus 12 more; run ``imgdownloader --help`` for the full list.
Examples: $ imgdownloader -q 'red fox' -l 20 -o foxes Command-line script: run ``python imgdownloader.py`` or the ``imgdownloader`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import os
from pathlib import Path
import re
import sys
import urllib.parse
from typing import Any, Iterable, Sequence


try:
    import requests
except ImportError:
    requests = None
try:
    import httpx
except ImportError:
    httpx = None
try:
    import pycurl
except ImportError:
    pycurl = None
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


def validDir(userDir: Path):
    if userDir and Path(userDir).exists():
        return userDir
    elif not userDir:
        return userDir
    return userDir


def validSize(size: int):
    valid = re.search(r"^[0-9]+\*[0-9]+$", size)
    if valid:
        dimensions = re.findall(r"[0-9]+", size)
        return dimensions
    msg = "Custom size must be in 'WIDTH*HEIGHT' format (e.g. 1920*1080)."
    raise argparse.ArgumentTypeError(msg)


def getParams(sysArgs: Any):
    parser = argparse.ArgumentParser(description="Get arguments for searching and downloading Google images")
    parser.add_argument("-q", "--query", help="Query to search for", type=str, required=True, nargs="+")
    parser.add_argument("-l", "--limit", help="Max images to download (default: 3)", type=int, default=3)
    parser.add_argument(
        "-o",
        "--outputDir",
        help="Full directory to store images in",
        type=validDir,
        required=False,
    )
    parser.add_argument(
        "-c",
        "--chromedriver",
        help="Full path to the chromedriver binary",
        type=str,
        required=False,
    )
    parser.add_argument(
        "-b",
        "--backend",
        help="HTTP backend library to use for downloading images (requests [default], pycurl, httpx)",
        type=str,
        choices=["requests", "pycurl", "httpx"],
        default="requests",
    )
    parser.add_argument(
        "-t",
        "--threads",
        help="Number of parallel download threads (default: 4)",
        type=int,
        default=4,
    )
    parser.add_argument("-epq", "--exactQuery", help="Words that must be included", type=str, default="")
    parser.add_argument("-oq", "--optionalQuery", help="Words that are optional", type=str, default="")
    parser.add_argument(
        "-eq",
        "--exceptQuery",
        help="Words that must not be included",
        type=str,
        default="",
    )
    sizeGroup = parser.add_mutually_exclusive_group(required=False)
    sizeGroup.add_argument(
        "-cs",
        "--customSize",
        help="Find image of specific size (e.g., 1920*1080)",
        type=validSize,
    )
    sizeGroup.add_argument(
        "-sz",
        "--size",
        help="Find images of pre-defined size category",
        type=str,
        choices=[
            "s",
            "m",
            "l",
            "i",
            ">2MP",
            ">4MP",
            ">6MP",
            ">8MP",
            ">10MP",
            ">12MP",
            ">15MP",
            ">20MP",
            ">40MP",
            ">70MP",
            ">400*300",
            ">640*480",
            ">800*600",
            ">1024*768",
        ],
    )
    parser.add_argument(
        "-ar",
        "--aspectRatio",
        help="Find images of specific aspect ratio",
        type=str,
        choices=["s", "t", "w", "p"],
    )
    colourGroup = parser.add_mutually_exclusive_group(required=False)
    colourGroup.add_argument(
        "-co",
        "--colour",
        help="Find images containing specific colour",
        type=str,
        choices=[
            "red",
            "orange",
            "yellow",
            "green",
            "teal",
            "blue",
            "purple",
            "pink",
            "white",
            "gray",
            "black",
            "brown",
        ],
    )
    colourGroup.add_argument(
        "-ct",
        "--colourType",
        help="Find images of specific color type",
        type=str,
        choices=["full-colour", "black-and-white", "transparent"],
    )
    parser.add_argument(
        "-it",
        "--imageType",
        help="Type of image",
        type=str,
        choices=["face", "photo", "clipart", "lineart", "animated"],
    )
    parser.add_argument(
        "-ft",
        "--fileType",
        help="Type of file extension",
        type=str,
        choices=["jpg", "gif", "png", "bmp", "svg", "webp", "ico", "raw"],
    )
    parser.add_argument("-s", "--site", help="Specific site or domain to search", type=str, default="")
    parser.add_argument(
        "-ss",
        "--SafeSearch",
        help="Enable SafeSearch filter",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "-ur",
        "--usageRights",
        help="Filter by usage rights",
        type=str,
        choices=["CC", "Commercial", "Other"],
    )
    args = parser.parse_args(sysArgs)
    params = vars(args)
    params["query"] = " ".join(params["query"]).strip()
    return params


def compileParams(params: dict[str, Any]):
    imgQuery = params["query"]
    compiled = {}
    for key, value in params.items():
        compiled[key] = getParamValue(key, value, imgQuery)
    return compiled


def getParamValue(k: str, v: Sequence[Any], imgQuery: str):
    if v:
        if k == "size":
            sizeSwitcher = {
                "s": "s",
                "m": "m",
                "l": "l",
                "i": "i",
                ">2MP": "2mp",
                ">4MP": "4mp",
                ">6MP": "6mp",
                ">8MP": "8mp",
                ">10MP": "10mp",
                ">12MP": "12mp",
                ">15MP": "15mp",
                ">20MP": "20mp",
                ">40MP": "40mp",
                ">70MP": "70mp",
                ">400*300": "qsvga",
                ">640*480": "vga",
                ">800*600": "svga",
                ">1024*768": "xvga",
            }
            return sizeSwitcher.get(v, "")
        elif k == "customSize":
            return f" imagesize:{v[0]}x{v[1]}"
        elif k == "aspectRatio":
            ratioSwitcher = {"s": "iar:s", "t": "iar:t", "w": "iar:w", "p": "iar:p"}
            return ratioSwitcher.get(v, "")
        elif k == "colourType":
            colourSwitcher = {
                "full-colour": "color",
                "black-and-white": "gray",
                "transparent": "trans",
            }
            return colourSwitcher.get(v, "")
        elif k == "SafeSearch":
            return "active" if v else "images"
        elif k == "usageRights":
            typeSwitcher = {
                "CC": "sur%3Acl",
                "Commercial": "sur%3Aol",
                "Other": "sur%3Aol",
            }
            return typeSwitcher.get(v, "")
        else:
            return v
    elif k == "limit":
        return 3
    elif k == "outputDir":
        safe_folder = re.sub(r"[^\w\s-]", "_", imgQuery).strip()
        return Path(os.getcwd()).joinpath(safe_folder)
    elif k == "SafeSearch":
        return "images"
    else:
        return ""


def getSearchUrl(params: dict[str, Any]) -> str:
    searchUrl = (
        f"https://www.google.com/search?as_st=y&tbm=isch"
        f"&as_q={urllib.parse.quote(params['query'] or '')}{params.get('customSize', '')}"
        f"&as_epq={urllib.parse.quote(params.get('exactQuery', ''))}"
        f"&as_oq={urllib.parse.quote(params.get('optionalQuery', ''))}"
        f"&as_eq={urllib.parse.quote(params.get('exceptQuery', ''))}"
        f"&imgsz={params.get('size', '')}"
        f"&imgc={params.get('colourType', '')}"
        f"&imgcolor={params.get('colour', '')}"
        f"&imgtype={params.get('imageType', '')}"
        f"&as_sitesearch={params.get('site', '')}"
        f"&safe={params.get('SafeSearch', 'images')}"
        f"&as_filetype={params.get('fileType', '')}"
        f"&tbs={params.get('aspectRatio', '')} {params.get('usageRights', '')}"
    )
    return searchUrl


def getImageUrls(params: dict[str, Any], url: str):
    resultsIndex = 0
    imageUrls = []
    maxImages = params["limit"]
    chromeOptions = webdriver.ChromeOptions()
    chromeOptions.add_argument("--no-sandbox")
    chromeOptions.add_argument("--headless=new")
    chromeOptions.add_argument("--disable-gpu")
    chromeOptions.add_argument("log-level=3")
    chromeOptions.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        if params.get("chromedriver"):
            service = Service(executable_path=params["chromedriver"])
            driver = webdriver.Chrome(service=service, options=chromeOptions)
        else:
            driver = webdriver.Chrome(options=chromeOptions)
    except Exception as e:
        print(f"Could not initialize Chrome driver: {e}")
        sys.exit(3)
    driver.set_window_size(1024, 768)
    driver.get(url)

    def scroll(drv: Any) -> None:
        drv.execute_script("window.scrollTo(0, document.body.scrollHeight);")

    try:
        while len(imageUrls) < maxImages:
            scroll(driver)
            thumbnails = driver.find_elements(By.CSS_SELECTOR, "img.Q4LuWd")
            resultsNo = len(thumbnails)
            if resultsIndex >= resultsNo:
                try:
                    loadButton = driver.find_element(By.CSS_SELECTOR, ".mye4qd")
                    if loadButton.is_displayed():
                        driver.execute_script("arguments[0].click();", loadButton)
                except Exception:
                    print("No more images found on Google.")
                    break
            for thumbnail in thumbnails[resultsIndex:resultsNo]:
                try:
                    thumbnail.click()
                    images = driver.find_elements(By.CSS_SELECTOR, "img.n3VNCb, img.iR330, img.r48fl")
                    for image in images:
                        imgSource = image.get_attribute("src")
                        if imgSource and imgSource.startswith("http") and imgSource not in imageUrls:
                            imageUrls.append(imgSource)
                            print(f"Collected URL ({len(imageUrls)}/{maxImages}): {imgSource[:60]}...")
                            if len(imageUrls) >= maxImages:
                                break
                    if len(imageUrls) >= maxImages:
                        break
                except Exception:
                    continue
            resultsIndex = resultsNo
    finally:
        driver.quit()
    print(f"Done getting URLs. Total retrieved: {len(imageUrls)}")
    return imageUrls


def download_with_requests(url: str, timeout=10, headers=None) -> tuple[str,]:
    if requests is None:
        msg = "The 'requests' library is not installed (`pip install requests`)."
        raise ImportError(msg)
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    return resp.content, content_type


def download_with_httpx(url: str, timeout=10, headers=None) -> tuple[str,]:
    if httpx is None:
        msg = "The 'httpx' library is not installed (`pip install httpx`)."
        raise ImportError(msg)
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        return resp.content, content_type


def download_with_pycurl(url: str, timeout=10, headers=None) -> tuple[str,]:
    if pycurl is None:
        msg = "The 'pycurl' library is not installed (`pip install pycurl`)."
        raise ImportError(msg)
    buffer = io.BytesIO()
    header_buffer = io.BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEDATA, buffer)
    c.setopt(c.HEADERFUNCTION, header_buffer.write)
    c.setopt(c.FOLLOWLOCATION, True)
    c.setopt(c.TIMEOUT, timeout)
    user_agent = headers.get("User-Agent", "Mozilla/5.0") if headers else "Mozilla/5.0"
    c.setopt(c.USERAGENT, user_agent)
    c.perform()
    status_code = c.getinfo(c.RESPONSE_CODE)
    c.close()
    if status_code != 200:
        msg = f"HTTP Status Code {status_code}"
        raise Exception(msg)
    header_text = header_buffer.getvalue().decode("iso-8859-1", errors="ignore")
    content_type = ""
    for line in header_text.splitlines():
        if line.lower().startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip()
    return buffer.getvalue(), content_type


def get_extension_from_content_type(content_type: str, default=".jpg"):
    if "image/png" in content_type:
        return ".png"
    elif "image/gif" in content_type:
        return ".gif"
    elif "image/webp" in content_type:
        return ".webp"
    elif "image/bmp" in content_type:
        return ".bmp"
    elif "image/svg+xml" in content_type:
        return ".svg"
    elif "image/x-icon" in content_type or "image/vnd.microsoft.icon" in content_type:
        return ".ico"
    elif "image/jpeg" in content_type:
        return ".jpg"
    return default


def single_download_worker(index: dict[str, Any], url: str, backend: Any, output_dir: Path, headers: Any) -> bool:
    backend = backend.lower()
    try:
        if backend == "requests":
            file_content, content_type = download_with_requests(url, headers=headers)
        elif backend == "httpx":
            file_content, content_type = download_with_httpx(url, headers=headers)
        elif backend == "pycurl":
            file_content, content_type = download_with_pycurl(url, headers=headers)
        else:
            msg = f"Unknown backend requested: {backend}"
            raise ValueError(msg)
        ext = get_extension_from_content_type(content_type)
        filename = f"image_{index:03d}{ext}"
        filepath = Path(output_dir).joinpath(filename)
        with open(filepath, "wb") as f:
            f.write(file_content)
        print(f"[+] [{backend.upper()}] Downloaded: {filename}")
        return True
    except Exception as err:
        print(f"[-] [{backend.upper()}] Failed image {index} ({url[:40]}...): {err}")
        return False


def downloadImages(params: dict[str, Any], imageUrls: Iterable[Any]) -> None:
    backend = params.get("backend", "requests")
    picDir = params["outputDir"]
    thread_count = params.get("threads", 4)
    os.makedirs(picDir, exist_ok=True)
    print(f"\nDownloading {len(imageUrls)} images using backend '{backend}' into '{picDir}'...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    success_count = 0
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [
            executor.submit(single_download_worker, idx + 1, url, backend, picDir, headers)
            for idx, url in enumerate(imageUrls)
        ]
        for future in as_completed(futures):
            if future.result():
                success_count += 1
    print(f"\nFinished: {success_count}/{len(imageUrls)} images successfully saved.")


def main(args: list[Any]) -> None:
    params = getParams(args)
    params = compileParams(params)
    url = getSearchUrl(params)
    imageUrls = getImageUrls(params, url)
    if imageUrls:
        downloadImages(params, imageUrls)
    else:
        print("No image URLs found to download.")


if __name__ == "__main__":
    main(sys.argv[1:])

#!/data/data/com.termux/files/usr/bin/env python
"""Download English subtitles for a video, or search for them by title.
The ``file`` subcommand scans a local video with subliminal and writes the subtitle next to it, ``search`` queries the services by title; the language, providers, extension and the output name are configurable.
Usage: getsub {file,search} [options] Positional arguments: video Path to the video file output_dir Output directory (defaults to the video directory) query ...
Movie or series title Subcommands: file Scan a local video with subliminal search Search subtitle services by title Options: --language Babelfish language code (default: eng) --providers Subliminal providers (default: %(default)s) --filename-suffix Suffix inserted before the extension (default: empty) --extension --scan-output Scan messages: full details, one processing line, or none --warn-non-mkv Warn when the input does not have an .mkv extension (default: enabled) --output Output filename (defaults to a sanitized title) --sources Search order (default: OpenSubtitles, then YTS) --user-agent --timeout HTTP timeout in seconds (default: none) ...
plus 10 more; run ``getsub --help`` for the full list.
Examples: $ getsub file movie.mkv $ getsub search 'Some Film' Command-line script: run ``python getsub.py`` or the ``getsub`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
from typing import Any, Sequence

from loguru import logger


LOGGER = logger
DEFAULT_PROVIDERS = ["opensubtitles", "podnapisi", "addic7ed", "tvsubtitles"]
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def normalize_extension(value: str) -> str:
    if not value:
        msg = "extension must not be empty"
        raise argparse.ArgumentTypeError(msg)
    return value if value.startswith(".") else f".{value}"


def normalize_title(title: str) -> str:
    return title.replace("_", " ").replace(".", " ")


def search_output_name(title: str, extension: str) -> str:
    name = title.replace(" ", "_").replace(".", "_")
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "_", name).strip("_")
    if not name:
        name = "subtitle"
    if not name.lower().endswith(extension.lower()):
        name += extension
    return name


def run_file_mode(args: argparse.Namespace) -> int:
    try:
        import babelfish
        from subliminal import download_best_subtitles, save_subtitles
        from subliminal.providers import ProviderError
        from subliminal.video import scan_video
    except ImportError as exc:
        LOGGER.error(
            "File mode requires babelfish and subliminal. Install them with "
            "'python -m pip install babelfish subliminal': {}",
            exc,
        )
        return 1
    video_path: Path = args.video
    if not video_path.exists():
        LOGGER.error("File not found: {}", video_path)
        return 1
    if args.warn_non_mkv and video_path.suffix.lower() != ".mkv":
        LOGGER.warning("File is not an MKV: {}", video_path)
    output_dir: Path = args.output_dir if args.output_dir is not None else video_path.parent
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error("Could not create output directory {}: {}", output_dir, exc)
        return 1
    if args.scan_output == "details":
        print(f"Scanning video: {video_path}")
    try:
        video = scan_video(video_path)
        if args.scan_output == "details":
            print(f"Found video: {video.name}")
            print(f"-Size: {video.size} bytes")
            print(f"-Duration: {video.duration:.2f} seconds")
            print(f"-Hashes: {video.hashes}")
        elif args.scan_output == "processing":
            print(f"Processing: {video.name}")
    except Exception as exc:
        LOGGER.error("Error scanning video: {}", exc)
        return 1
    try:
        language = babelfish.Language(args.language)
        print(f"Searching for {args.language} subtitles...")
        subtitles_by_video = download_best_subtitles([video], {language}, providers=args.providers)
        subtitles = subtitles_by_video.get(video, set())
        if not subtitles:
            LOGGER.warning("No subtitles found for {}", video_path.name)
            return 1
        output_path = output_dir / f"{video_path.stem}{args.filename_suffix}{args.extension}"
        save_subtitles(video, subtitles, single=True, path=output_path)
        print(f"Subtitles saved to: {output_path}")
        print("✓ Subtitles downloaded successfully!")
        return 0
    except ProviderError as exc:
        LOGGER.error("Provider error: {}", exc)
        return 1
    except Exception as exc:
        LOGGER.error("Error downloading subtitles: {}", exc)
        return 1


class TitleSubtitleClient:
    def __init__(self, args: argparse.Namespace) -> None:
        try:
            import requests
        except ImportError as exc:
            msg = "Search mode requires requests. Install it with 'python -m pip install requests'."
            raise RuntimeError(msg) from exc
        self.session: Any = requests.Session()
        self.session.headers.update({"User-Agent": args.user_agent})
        self.timeout: float | None = args.timeout
        self.opensubtitles_url: str = args.opensubtitles_url
        self.opensubtitles_download_url: str = args.opensubtitles_download_url
        self.opensubtitles_api_key: str | None = args.opensubtitles_api_key
        self.opensubtitles_language: str = args.opensubtitles_language
        self.opensubtitles_type: str = args.opensubtitles_type
        self.yts_movies_url: str = args.yts_movies_url
        self.yts_details_url: str = args.yts_details_url
        self.yts_limit: int = args.yts_limit
        self.yts_with_images: bool = args.yts_with_images
        self.yts_language: str = args.yts_language
        self.sources: list[str] = args.sources

    def timeout_kwargs(self) -> dict[str, Any]:
        return {} if self.timeout is None else {"timeout": self.timeout}

    def api_headers(self) -> dict[str, str]:
        return {"Api-Key": self.opensubtitles_api_key} if self.opensubtitles_api_key else {}

    def search_opensubtitles(self, title: str) -> dict[str, Any] | None:
        try:
            params = {
                "query": normalize_title(title),
                "languages": self.opensubtitles_language,
                "type": self.opensubtitles_type,
            }
            response = self.session.get(
                self.opensubtitles_url,
                params=params,
                headers=self.api_headers(),
                **self.timeout_kwargs(),
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            entries = payload.get("data", []) if isinstance(payload, dict) else []
            for entry in entries:
                attributes = entry.get("attributes", {})
                if attributes.get("language") == self.opensubtitles_language:
                    return {
                        "id": entry.get("id"),
                        "title": attributes.get("title", title),
                        "url": attributes.get("url"),
                        "files": attributes.get("files", []),
                    }
            return None
        except Exception as exc:
            LOGGER.error("Error searching OpenSubtitles: {}", exc)
            return None

    def download_srt_from_url(self, url: str) -> str | None:
        try:
            response = self.session.get(url, **self.timeout_kwargs())
            if response.status_code != 200:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            content = response.content
            if "application/zip" in content_type or content.startswith(b"PK\x03\x04"):
                LOGGER.info("ZIP file detected; trying another subtitle source")
                return None
            return response.text
        except Exception as exc:
            LOGGER.error("Error downloading subtitle: {}", exc)
            return None

    def download_from_opensubtitles(self, subtitle: dict[str, Any] | None) -> str | None:
        if not subtitle:
            return None
        for file_info in subtitle.get("files", []):
            if not isinstance(file_info, dict):
                continue
            file_name = str(file_info.get("file_name", ""))
            if not file_name.lower().endswith(".srt"):
                continue
            direct_url = file_info.get("url")
            if direct_url:
                content = self.download_srt_from_url(str(direct_url))
                if content:
                    return content
            file_id = file_info.get("file_id")
            if file_id is not None and self.opensubtitles_api_key:
                try:
                    response = self.session.post(
                        self.opensubtitles_download_url,
                        json={"file_id": file_id},
                        headers=self.api_headers(),
                        **self.timeout_kwargs(),
                    )
                    if response.status_code == 200:
                        download_info = response.json()
                        download_url = download_info.get("link")
                        if download_url:
                            content = self.download_srt_from_url(str(download_url))
                            if content:
                                return content
                except Exception as exc:
                    LOGGER.error("Error requesting OpenSubtitles download link: {}", exc)
        return None

    def search_yts(self, title: str) -> str | None:
        try:
            response = self.session.get(
                self.yts_movies_url,
                params={"query_term": normalize_title(title), "limit": self.yts_limit},
                **self.timeout_kwargs(),
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            movie_data = payload.get("data", {}) if isinstance(payload, dict) else {}
            movies = movie_data.get("movies", []) if isinstance(movie_data, dict) else []
            for movie in movies:
                movie_id = movie.get("id")
                if movie_id is None:
                    continue
                details = self.session.get(
                    self.yts_details_url,
                    params={
                        "movie_id": movie_id,
                        "with_images": str(self.yts_with_images).lower(),
                    },
                    **self.timeout_kwargs(),
                )
                if details.status_code != 200:
                    continue
                details_payload = details.json()
                movie_info = details_payload.get("data", {}).get("movie", {})
                for subtitle in movie_info.get("subtitles", []):
                    if str(subtitle.get("lang", "")).lower() == self.yts_language.lower():
                        subtitle_url = subtitle.get("url")
                        if subtitle_url:
                            return str(subtitle_url)
            return None
        except Exception as exc:
            LOGGER.error("Error searching YTS subtitles: {}", exc)
            return None

    def download_subtitles(self, title: str) -> tuple[str | None, str | None]:
        print(f"Searching for subtitles: {title}")
        for source in self.sources:
            if source == "opensubtitles":
                print("Trying OpenSubtitles.org...")
                result = self.search_opensubtitles(title)
                content = self.download_from_opensubtitles(result)
                if content:
                    return content, "OpenSubtitles.org"
            elif source == "yts":
                print("Trying YIFY subtitles...")
                url = self.search_yts(title)
                content = self.download_srt_from_url(url) if url else None
                if content:
                    return content, "YIFY Subtitles"
        return None, None


def run_search_mode(args: argparse.Namespace) -> int:
    title = " ".join(args.query)
    try:
        client = TitleSubtitleClient(args)
    except RuntimeError as exc:
        LOGGER.error("{}", exc)
        return 1
    content, source = client.download_subtitles(title)
    if not content:
        print("❌ No subtitles found for the given query")
        print("Tips:")
        print("- Try using the exact movie or TV show name")
        print("- For TV series, use a format such as show_name_season_episode")
        print("- Example: breaking_bad_s01e01")
        print("- Try removing special characters")
        return 1
    output_path: Path = args.output or Path(search_output_name(title, args.extension))
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        LOGGER.error("Error saving subtitle to {}: {}", output_path, exc)
        return 1
    print(f"✅ Subtitles found from {source}")
    print(f"✅ Subtitle saved to: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download English subtitles for a local video or search by movie/series title."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    file_parser = subparsers.add_parser("file", help="scan a local video with subliminal")
    file_parser.add_argument("video", type=Path, help="path to the video file")
    file_parser.add_argument(
        "output_dir", nargs="?", type=Path, help="output directory (defaults to the video directory)"
    )
    file_parser.add_argument("--language", default="eng", help="Babelfish language code (default: eng)")
    file_parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        help="subliminal providers (default: %(default)s)",
    )
    file_parser.add_argument(
        "--filename-suffix", default="", help="suffix inserted before the extension (default: empty)"
    )
    file_parser.add_argument("--extension", type=normalize_extension, default=".srt")
    file_parser.add_argument(
        "--scan-output",
        choices=("details", "processing", "none"),
        default="details",
        help="scan messages: full details, one processing line, or none",
    )
    file_parser.add_argument(
        "--warn-non-mkv",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="warn when the input does not have an .mkv extension (default: enabled)",
    )
    search_parser = subparsers.add_parser("search", help="search subtitle services by title")
    search_parser.add_argument("query", nargs="+", help="movie or series title")
    search_parser.add_argument("--output", type=Path, help="output filename (defaults to a sanitized title)")
    search_parser.add_argument("--extension", type=normalize_extension, default=".srt")
    search_parser.add_argument(
        "--sources",
        nargs="+",
        choices=("opensubtitles", "yts"),
        default=["opensubtitles", "yts"],
        help="search order (default: OpenSubtitles, then YTS)",
    )
    search_parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    search_parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds (default: none)")
    search_parser.add_argument("--opensubtitles-url", default="https://rest.opensubtitles.com/api/v1/subtitles")
    search_parser.add_argument("--opensubtitles-download-url", default="https://api.opensubtitles.com/api/v1/download")
    search_parser.add_argument("--opensubtitles-api-key", default=None)
    search_parser.add_argument("--opensubtitles-language", default="en")
    search_parser.add_argument("--opensubtitles-type", default="all")
    search_parser.add_argument("--yts-movies-url", default="https://yts.mx/api/v2/list_movies.json")
    search_parser.add_argument("--yts-details-url", default="https://yts.mx/api/v2/movie_details.json")
    search_parser.add_argument("--yts-limit", type=int, default=1)
    search_parser.add_argument(
        "--yts-with-images",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="include images in the YTS details response (default: false)",
    )
    search_parser.add_argument("--yts-language", default="english")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "file":
        return run_file_mode(args)
    return run_search_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())

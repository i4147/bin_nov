#!/data/data/com.termux/files/usr/bin/env python
"""Download or mirror the content of a Google Drive folder.
The ``download`` and ``sync`` subcommands authenticate read-only with the OAuth client (``credentials.json``/``.env``, or the out-of-band ``--auth-mode manual``), walk the folder tree, sanitize the names and fetch the files into the destination, skipping the up-to-date ones unless ``--force``.
Usage: gdrive_sync {download,sync} [--folder NAME] [--dest DIR] Subcommands: download Download a single named folder from Drive root (gdrive_downloader.py).
sync Sync the entire Drive or one named folder (gdrive_syncer*.py).
Options: --auth-mode Installed = local browser server flow (credentials.json or env vars).
manual = out-of-band code entry (needs env vars).
Default: installed.
--credentials-file --token-file --env-file --client-id Override GOOGLE_CLIENT_ID.
--client-secret Override GOOGLE_CLIENT_SECRET.
--folder Folder name directly under Drive root.
--dest Destination directory (default: ./<folder>).
--sanitize Replace filesystem-unsafe characters in names.
--force Re-download files even if they look up to date.
Examples: $ gdrive_sync sync --folder Documents --dest ./backup Command-line script: run ``python gdrive_sync.py`` or the ``gdrive_sync`` command.
The module is importable as well."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import pickle
import sys
from typing import Any, Optional
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*_a: Any, **_kw: Any) -> bool:
        return False


from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import requests

SCOPES: list[str] = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME: str = "application/vnd.google-apps.folder"
DEFAULT_CREDENTIALS_FILE: str = "credentials.json"
DEFAULT_TOKEN_FILE: str = "token.pickle"
DEFAULT_ENV_FILE = Path.home() / ".env"
DEFAULT_BACKUP_DIR: str = "./google_drive_backup"


def _iso_to_timestamp(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _sanitize(name: str) -> str:
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


class DriveClient:
    def __init__(
        self,
        auth_mode: str = "installed",
        credentials_file: str = DEFAULT_CREDENTIALS_FILE,
        token_file: str = DEFAULT_TOKEN_FILE,
        env_file: Path = DEFAULT_ENV_FILE,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self.auth_mode = auth_mode
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.env_file = Path(env_file) if env_file else DEFAULT_ENV_FILE
        if self.env_file.exists():
            load_dotenv(dotenv_path=str(self.env_file))
        self.client_id = client_id or os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET")
        self.service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None
        if Path(self.token_file).exists():
            with open(self.token_file, "rb") as fh:
                creds = pickle.load(fh)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif self.auth_mode == "manual":
                creds = self._manual_oauth_flow()
            else:
                creds = self._installed_oauth_flow()
            with open(self.token_file, "wb") as fh:
                pickle.dump(creds, fh)
        return build("drive", "v3", credentials=creds)

    def _installed_oauth_flow(self):
        if Path(self.credentials_file).exists():
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
        else:
            if not self.client_id or not self.client_secret:
                msg = (
                    f"Neither '{self.credentials_file}' nor "
                    "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are available "
                    "for 'installed' auth mode."
                )
                raise ValueError(msg)
            flow = InstalledAppFlow.from_client_config(
                {
                    "installed": {
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"],
                    }
                },
                SCOPES,
            )
        return flow.run_local_server(port=0)

    def _manual_oauth_flow(self):
        if not self.client_id or not self.client_secret:
            msg = "Manual auth mode requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (env file or CLI flags)."
            raise ValueError(msg)
        params = {
            "client_id": self.client_id,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
        }
        auth_url = f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"
        print("\n" + "=" * 40)
        print("MANUAL AUTHENTICATION REQUIRED")
        print("-" * 40)
        print(f"1. Open this URL in your browser:\n\n{auth_url}\n")
        print("2. Sign in to your Google account")
        print("3. Grant Google Drive (read-only) access")
        print("4. Copy the authorization code")
        print("-" * 40)
        code = input("\nEnter authorization code: ").strip()
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            msg = f"Token exchange failed: {resp.text}"
            raise RuntimeError(msg)
        tok: dict[str, Any] = resp.json()
        return Credentials(
            token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=SCOPES,
        )

    def list_children(self, folder_id: str = "root") -> list[dict]:
        results: list[dict] = []
        page_token: Optional[str] = None
        while True:
            try:
                resp = (
                    self.service
                    .files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed=false",
                        pageSize=1000,
                        fields=("nextPageToken, files(id, name, mimeType, size, modifiedTime)"),
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as err:
                print(f"An error occurred: {err}")
                break
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    def find_folder(self, name: str, parent: str = "root") -> Optional[dict]:
        query = f"name='{name}' and mimeType='{FOLDER_MIME}' and '{parent}' in parents and trashed=false"
        resp = self.service.files().list(q=query, fields="files(id, name)").execute()
        files = resp.get("files", [])
        return files[0] if files else None

    def download_file(self, file_id: str, name: str, dest: str) -> bool:
        try:
            request = self.service.files().get_media(fileId=file_id)
            os.makedirs(Path(dest).parent or ".", exist_ok=True)
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    print(
                        f"\rDownloading {name}: {int(status.progress() * 100)}%",
                        end="",
                        flush=True,
                    )
            print(f"\n✓ Downloaded: {name}")
            return True
        except HttpError as err:
            print(f"\n✗ Failed to download {name}: {err}")
            return False

    def sync_folder(
        self,
        folder_id: str,
        dest: str,
        label: str = "root",
        skip_existing: bool = True,
        sanitize: bool = False,
        indent: int = 0,
    ) -> None:
        prefix = "  " * indent
        print(f"{prefix}📁 Syncing: {label}")
        os.makedirs(dest, exist_ok=True)
        for item in self.list_children(folder_id):
            raw_name = item["name"]
            name = _sanitize(raw_name) if sanitize else raw_name
            path = Path(dest).joinpath(name)
            if item["mimeType"] == FOLDER_MIME:
                self.sync_folder(
                    item["id"],
                    path,
                    raw_name,
                    skip_existing=skip_existing,
                    sanitize=sanitize,
                    indent=indent + 1,
                )
                continue
            modified = item.get("modifiedTime")
            if skip_existing and Path(path).exists() and modified:
                remote_ts = _iso_to_timestamp(modified)
                if remote_ts is not None and Path(path).stat().st_mtime >= remote_ts:
                    print(f"{prefix}  ⏭ Up to date: {raw_name}")
                    continue
            if self.download_file(item["id"], raw_name, path) and modified:
                ts = _iso_to_timestamp(modified)
                if ts is not None:
                    os.utime(path, (ts, ts))


def _make_client(args: argparse.Namespace) -> DriveClient:
    return DriveClient(
        auth_mode=args.auth_mode,
        credentials_file=args.credentials_file,
        token_file=args.token_file,
        env_file=Path(args.env_file),
        client_id=args.client_id,
        client_secret=args.client_secret,
    )


def cmd_download(args: argparse.Namespace) -> int:
    try:
        client = _make_client(args)
        folder = client.find_folder(args.folder)
        if not folder:
            msg = f"Folder '{args.folder}' not found in Google Drive"
            raise SystemExit(msg)
        print(f"Found folder '{args.folder}' with ID: {folder['id']}")
        dest = args.dest or Path(os.getcwd()).joinpath(args.folder)
        os.makedirs(dest, exist_ok=True)
        client.sync_folder(
            folder["id"],
            dest,
            label=args.folder,
            skip_existing=False,
            sanitize=args.sanitize,
        )
        print(f"\nSuccessfully downloaded '{args.folder}' to {dest}")
        return 0
    except SystemExit:
        raise
    except Exception as err:
        print(f"Error: {err}")
        return 1


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        client = _make_client(args)
        dest = args.dest or DEFAULT_BACKUP_DIR
        if args.folder:
            print(f"Searching for folder: {args.folder}")
            folder = client.find_folder(args.folder)
            if not folder:
                msg = f'Folder "{args.folder}" not found in root directory'
                raise SystemExit(msg)
            client.sync_folder(
                folder["id"],
                dest,
                label=args.folder,
                skip_existing=not args.force,
                sanitize=args.sanitize,
            )
        else:
            print("Starting full Google Drive sync...")
            client.sync_folder(
                "root",
                dest,
                label="My Drive",
                skip_existing=not args.force,
                sanitize=args.sanitize,
            )
        print("\n✅ Sync completed!")
        return 0
    except KeyboardInterrupt:
        print("\n\n⚠️ Sync interrupted by user")
        return 130
    except SystemExit:
        raise
    except ValueError as err:
        print(f"Configuration error: {err}")
        print("\nPlease create ~/.env with:")
        print("  GOOGLE_CLIENT_ID=your_id.apps.googleusercontent.com")
        print("  GOOGLE_CLIENT_SECRET=your_secret")
        return 1
    except Exception as err:
        print(f"\n❌ Error: {err}")
        print("\nTroubleshooting:")
        print("  1. Check your internet connection")
        print("  2. Verify credentials in --env-file / --credentials-file")
        print("  3. On Android run: termux-setup-storage")
        return 1


def _add_auth_arguments(parser: argparse.ArgumentParser) -> None:
    grp = parser.add_argument_group("authentication")
    grp.add_argument(
        "--auth-mode",
        choices=("installed", "manual"),
        default="installed",
        help=(
            "installed = local browser server flow (credentials.json or env "
            "vars). manual = out-of-band code entry (needs env vars). "
            "Default: installed."
        ),
    )
    grp.add_argument(
        "--credentials-file",
        default=DEFAULT_CREDENTIALS_FILE,
        help=f"OAuth client secrets for installed mode (default: {DEFAULT_CREDENTIALS_FILE}).",
    )
    grp.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help=f"Pickle cache for OAuth tokens (default: {DEFAULT_TOKEN_FILE}).",
    )
    grp.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=f"dotenv file with GOOGLE_CLIENT_ID/SECRET (default: {DEFAULT_ENV_FILE}).",
    )
    grp.add_argument("--client-id", default=None, help="Override GOOGLE_CLIENT_ID.")
    grp.add_argument("--client-secret", default=None, help="Override GOOGLE_CLIENT_SECRET.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdrive_sync.py",
        description="Download or sync Google Drive content (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python gdrive_sync.py download --folder notebooks\n"
            "  python gdrive_sync.py sync\n"
            "  python gdrive_sync.py sync --folder photos --dest ./photos\n"
            "  python gdrive_sync.py sync --auth-mode manual --sanitize "
            "--dest /sdcard/GoogleDriveBackup\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_down = subparsers.add_parser(
        "download",
        help="Download a single named folder from Drive root (gdrive_downloader.py).",
    )
    p_down.add_argument("--folder", required=True, help="Folder name directly under Drive root.")
    p_down.add_argument("--dest", default=None, help="Destination directory (default: ./<folder>).")
    p_down.add_argument(
        "--sanitize",
        action="store_true",
        help="Replace filesystem-unsafe characters in names.",
    )
    _add_auth_arguments(p_down)
    p_down.set_defaults(func=cmd_download)
    p_sync = subparsers.add_parser(
        "sync",
        help="Sync the entire Drive or one named folder (gdrive_syncer*.py).",
    )
    p_sync.add_argument(
        "--folder",
        default=None,
        help="Folder name under root to sync; omit for a full Drive sync.",
    )
    p_sync.add_argument(
        "--dest",
        default=DEFAULT_BACKUP_DIR,
        help=f"Destination directory (default: {DEFAULT_BACKUP_DIR}).",
    )
    p_sync.add_argument(
        "--force",
        action="store_true",
        help="Re-download files even if they look up to date.",
    )
    p_sync.add_argument(
        "--sanitize",
        action="store_true",
        help="Replace filesystem-unsafe characters in names.",
    )
    _add_auth_arguments(p_sync)
    p_sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/data/data/com.termux/files/usr/bin/env python
"""Upload image from clipboard and return Markdown link."""

import argparse
from collections import namedtuple
from datetime import datetime
from io import BytesIO
from pathlib import Path
import pickle
import platform
import subprocess
import sys
from traceback import print_exc
from urllib.parse import urljoin
from typing import Any, Iterator

from PIL import Image, ImageGrab
from pyperclip import copy as copy2clipboard
import upyun

SYSTEM = platform.system()
CONFIG_PATH = Path(Path("~").expanduser()).joinpath(".upimg_config.pkl")
CONFIG_FIELDS: list[str] = ["service", "username", "password", "upload_path", "url_base"]
CONFIG_DEFAULTS: list[str] = ["", "", "", "/", "https://test.upimg.com"]
ConfigTuple = namedtuple("ConfigTuple", CONFIG_FIELDS)


def send_notify(title="UpImg", message="") -> bool:
    try:
        if SYSTEM == "Darwin":
            cmd = f'display notification "{message}" with title "{title}"'
            subprocess.run(["osascript", "-e", cmd], check=True)
        elif SYSTEM == "Windows":
            ps_script = f'[void][System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms");$objNotifyIcon=New-Object System.Windows.Forms.NotifyIcon;$objNotifyIcon.Icon=[System.Drawing.SystemIcons]::Information;$objNotifyIcon.BalloonTipTitle="{title}";$objNotifyIcon.BalloonTipText="{message}";$objNotifyIcon.Visible=$True;$objNotifyIcon.ShowBalloonTip(5000);'
            subprocess.run(["powershell", "-Command", ps_script], capture_output=True)
        return True
    except Exception:
        return False


def get_clipboard_file_paths():
    paths = []
    if SYSTEM == "Darwin":
        try:
            script = 'tell application "Finder" to set theFiles to selection\nreturn POSIX path of (theFiles as text)'
            proc = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, text=True)
            if "file" in proc.stdout:
                file_path_proc = subprocess.run(
                    ["osascript", "-e", "POSIX path of (the clipboard as alias)"],
                    capture_output=True,
                    text=True,
                )
                path = file_path_proc.stdout.strip()
                if path and Path(path).exists():
                    paths.append(path)
        except Exception:
            pass
    elif SYSTEM == "Windows":
        from ctypes import create_unicode_buffer, windll

        user32 = windll.user32
        kernel32 = windll.kernel32
        cf_hdrop = 15
        if user32.OpenClipboard(None):
            h_global = user32.GetClipboardData(cf_hdrop)
            if h_global:
                h_drop = kernel32.GlobalLock(h_global)
                if h_drop:
                    count = windll.shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
                    for i in range(count):
                        length = windll.shell32.DragQueryFileW(h_drop, i, None, 0)
                        buffer = create_unicode_buffer(length)
                        windll.shell32.DragQueryFileW(h_drop, i, buffer, length + 1)
                        paths.append(buffer.value)
                    kernel32.GlobalUnlock(h_global)
            user32.CloseClipboard()
    return paths


class ClipboardFile:
    @property
    def file_objects(self):
        file_paths = get_clipboard_file_paths()
        if file_paths:
            for path in file_paths:
                if Path(path).is_file():
                    fp = open(path, "rb")
                    ext = (str(Path(path).with_suffix("")), Path(path).suffix)[1].lower()
                    yield fp, ext
            return
        image = ImageGrab.grabclipboard()
        if isinstance(image, Image.Image):
            image_bytes_io = BytesIO()
            image.save(image_bytes_io, format="PNG")
            image_bytes_io.seek(0)
            yield image_bytes_io, ".png"


def set_config() -> None:
    args = {}
    print("--- UpImg Configuration Setup ---")
    for field, default in zip(CONFIG_FIELDS, CONFIG_DEFAULTS):
        display_default = f" [{default}]" if default else ""
        value = input(f"{field.replace('_', ' ')}{display_default}: ").strip()
        args[field] = value or default
    with open(CONFIG_PATH, "wb") as fp:
        pickle.dump(ConfigTuple(**args), fp)
    if SYSTEM == "Windows":
        try:
            import winreg

            import pythoncom
            from win32com.shell import shell

            reg_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            desktop_path = winreg.QueryValueEx(reg_key, "Desktop")[0]
            exe_path = Path(sys.argv[0]).absolute()
            lnk_path = Path(desktop_path).joinpath("UpImg.lnk")
            shortcut = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink,
                None,
                pythoncom.CLSCTX_INPROC_SERVER,
                shell.IID_IShellLink,
            )
            shortcut.SetPath(sys.executable)
            shortcut.SetArguments(f'"{exe_path}"')
            shortcut.SetHotkey((0x02 << 8) | 0x31)
            shortcut.SetWorkingDirectory(Path(exe_path).parent)
            shortcut.SetShowCmd(7)
            shortcut.QueryInterface(pythoncom.IID_IPersistFile).Save(lnk_path, 0)
        except Exception:
            print("Notice: Desktop hotkey link shortcut could not be configured.")


def get_config():
    with open(CONFIG_PATH, "rb") as fp:
        return pickle.load(fp)


class UpImage:
    def __init__(self, config: dict[str, Any]) -> None:
        self.upyun = upyun.UpYun(config.service, config.username, config.password)
        self.upload_path = config.upload_path

    def upload(self):
        path_list = []
        clipboard_file = ClipboardFile()
        for file, ext in clipboard_file.file_objects:
            full_path = self.upload_path.rstrip("/") + "/" + datetime.now().strftime("%Y%m%d%H%M%S%f") + ext
            self.upyun.put(full_path, file)
            file.close()
            path_list.append(full_path)
        return path_list


def parse_args():
    parser = argparse.ArgumentParser(description="Upload image from clipboard and return Markdown link.")
    parser.add_argument("-c", "--config", action="store_true", help="file upload config")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.config:
        try:
            set_config()
            print('\nConfig success! Execute script or use global hotkey "Ctrl + 1" (Windows).')
        except Exception:
            print_exc()
    else:
        if not Path(CONFIG_PATH).exists():
            message = "Config file is not found. Please setup profile parameters first.\nUsage: upimg --config"
            send_notify(
                "Config Missing",
                "Execute configuration step via terminal instruction profile.",
            )
            print(message)
            return
        config = get_config()
        try:
            send_notify(title="UpImg", message="Uploading file payload...")
            up = UpImage(config)
            markdown_links = [f"![]({urljoin(config.url_base, path)})" for path in up.upload()]
            files_count = len(markdown_links)
            if files_count > 0:
                joined_markdown = "\n".join(markdown_links)
                copy2clipboard(joined_markdown)
                send_notify(
                    title=f"{files_count} Upload Success",
                    message="Link ready to paste via Ctrl+V / ⌘+V",
                )
                print(joined_markdown)
            else:
                send_notify(
                    title="Upload Cancelled",
                    message="No valid file object image data parsed from clipboard.",
                )
        except Exception:
            error_log = Path("./upimg-error.log").absolute()
            with open(error_log, "w") as fp:
                print_exc(file=fp)
            send_notify("Execution Error!!!", message=f"Trace log written to: {error_log}")
            print(f"Error detected. Visual diagnostic footprint isolated inside {error_log}")


if __name__ == "__main__":
    main()

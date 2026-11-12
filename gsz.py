#!/data/data/com.termux/files/usr/bin/env python
"""Print the size of a GitHub repository.
Accepts ``owner/repo`` or a full ``https://github.com/...`` URL, asks the GitHub API for the repository metadata and prints the size in megabytes.
Usage: gsz owner/repo Examples: $ gsz i4147/bin_oct Runs as a script (``python gsz.py``) in the directory it should act on; the module can also be imported.
"""

import re
import sys

import requests


def get_repo_size(input_str: str) -> None:
    if input_str.startswith("https://github.com/"):
        match = re.search(r"github\.com/([^/]+)/([^/]+)", input_str)
        if not match:
            print("Invalid GitHub URL format.")
            return
        user, repo = match.groups()
    else:
        parts = input_str.split("/")
        if len(parts) != 2:
            print("Invalid format. Use 'user/repo' or full URL.")
            return
        user, repo = parts
    url = f"https://api.github.com/repos/{user}/{repo}"
    try:
        response = requests.get(url)
        if response.status_code == 404:
            print(f"Repository '{user}/{repo}' not found.")
            return
        response.raise_for_status()
        data = response.json()
        size_kb = data.get("size", 0)
        size_mb = size_kb / 1024
        print(f"Repository: {user}/{repo}")
        print(f"Size: {size_mb:.2f} MB")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python get-repo-size <user/repo> or <https://github.com/user/repo>")
        sys.exit(1)
    get_repo_size(sys.argv[1])

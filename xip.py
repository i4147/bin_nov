#!/data/data/com.termux/files/usr/bin/env python
"""Print the public IP address and its country and city via ipinfo.
Loads the ``IPINFO_TOKEN`` from ``~/.env`` with python-dotenv, queries the ipinfo service and prints the address, the country and the city in colour.
Usage: xip Examples: $ xip Runs at import time and needs ``IPINFO_TOKEN`` in ``~/.env``."""

import os
from pathlib import Path

from dh import cyan, reset
from dotenv import load_dotenv
import ipinfo

if __name__ == "__main__":
    env_path = Path.home() / ".env"
    load_dotenv(env_path)
    token = os.environ.get("IPINFO_TOKEN")
    handler = ipinfo.getHandler(token)
    details = handler.getDetails()
    print(f"ip: {cyan}{details.ip}{reset}")
    print(f"country: {cyan}{details.country}{reset}")
    print(f"city: {cyan}{details.city}{reset}")

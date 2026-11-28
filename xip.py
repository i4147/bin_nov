#!/data/data/com.termux/files/usr/bin/env python
"""
Print the public IP address and its country and city via ipinfo. Loads the IPINFO_TOKEN from ~/.env with python-dotenv, queries the ipinfo service and prints the address, the country and the city in colour.

Usage:
    xip

Examples:
    $ xip Runs at import time and needs IPINFO_TOKEN in ~/.env.

"""

from __future__ import annotations
import os
from pathlib import Path

from dh import cprint
from dotenv import load_dotenv
import ipinfo


if __name__ == "__main__":
    env_path = Path.home() / ".env"
    load_dotenv(env_path)
    token = os.environ.get("IPINFO_TOKEN")
    handler = ipinfo.getHandler(token)
    details = handler.getDetails()
    cprint(f"ip: {details.ip}", color="cyan")
    cprint(f"country: {details.country}", color=(189, 239, 67))
    cprint(f"city: {details.city}", color="#c7a8fa")

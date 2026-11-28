#!/data/data/com.termux/files/usr/bin/env python
"""Generate a setup.py from an unpacked wheel's .dist-info directory.

Reads METADATA (RFC 822 headers) and the license/readme files from *.dist-info in the current folder and emits a setup.py with the extracted fields.
"""

from __future__ import annotations
import configparser
from email.parser import HeaderParser
import glob
import os
import pprint


def generate_setup(base_dir="."):
    # 1. Locate the .dist-info directory
    dist_info_dirs = glob.glob(os.path.join(base_dir, "*.dist-info"))
    if not dist_info_dirs:
        msg = "No .dist-info directory found in the current folder."
        raise FileNotFoundError(msg)
    dist_info = dist_info_dirs[0]

    # 2. Parse the METADATA file (uses RFC 822 email header format)
    metadata_path = os.path.join(dist_info, "METADATA")
    with open(metadata_path, "r", encoding="utf-8") as f:
        raw_meta = f.read()

    # Split headers from the long description (separated by a blank line)
    parts = raw_meta.split("\n\n", 1)
    headers = HeaderParser().parsestr(parts[0])
    long_description = parts[1].strip() if len(parts) > 1 else ""

    # 3. Extract core fields
    name = headers.get("Name", "unknown-package")
    version = headers.get("Version", "0.0.0")
    summary = headers.get("Summary", "")
    author = headers.get("Author", "")
    author_email = headers.get("Author-email", "")
    url = headers.get("Home-page", "")
    license_name = headers.get("License", "")
    python_requires = headers.get("Requires-Python", "")
    classifiers = headers.get_all("Classifier") or []

    # 4. Separate standard dependencies from optional extras
    install_requires = []
    extras_require = {}
    for req in headers.get_all("Requires-Dist") or []:
        if "; extra ==" in req:
            pkg_part, extra_part = req.split("; extra ==", 1)
            # Clean up quotes and potential additional environment markers
            extra_name = extra_part.strip().strip("'\"").split(";")[0].strip("'\" ")
            extras_require.setdefault(extra_name, []).append(pkg_part.strip())
        else:
            install_requires.append(req.strip())

    # 5. Parse entry_points.txt (for CLI commands / plugins)
    entry_points = {}
    ep_path = os.path.join(dist_info, "entry_points.txt")
    if os.path.exists(ep_path):
        config = configparser.ConfigParser()
        config.read(ep_path, encoding="utf-8")
        for section in config.sections():
            entry_points[section] = [f"{k} = {v}" for k, v in config.items(section)]

    # 6. Format and write setup.py
    setup_code = f"""from setuptools import setup, find_packages

setup(
    name={name!r},
    version={version!r},
    description={summary!r},
    long_description={long_description!r},
    long_description_content_type={headers.get("Description-Content-Type", "text/markdown")!r},
    author={author!r},
    author_email={author_email!r},
    url={url!r},
    license={license_name!r},
    packages=find_packages(exclude=["*.dist-info", "*.data"]),
    include_package_data=True,
    python_requires={python_requires!r},
    install_requires={pprint.pformat(install_requires, indent=8)},
    extras_require={pprint.pformat(extras_require, indent=8)},
    entry_points={pprint.pformat(entry_points, indent=8)},
    classifiers={pprint.pformat(classifiers, indent=8)},
)
"""

    output_path = os.path.join(base_dir, "setup.py")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(setup_code)

    print(f"Successfully generated {output_path} for {name} ({version})")


if __name__ == "__main__":
    generate_setup(".")

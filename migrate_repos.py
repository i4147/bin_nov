#!/data/data/com.termux/files/usr/bin/env python
"""Migrate the local GitHub identity across the repositories and SSH keys.
Same workflow as :doc:`migrate_git` with the extra ``--skip-ssh``/``--skip-files`` switches, home and SSH directory overrides and the new key path; the default is a dry run and ``--apply`` writes the changes.
Usage: migrate_repos [--old-username U] [--new-username U] [--apply] Options: --old-username --old-email --new-username --new-email --home Home directory to scan (default: current user's home) --ssh-dir SSH directory (default: ~/.ssh) --new-key-path Path for the new private key (default: ~/.ssh/id_ed25519_github_new) --apply Actually write changes / delete files.
Without this flag the script is dry-run only.
--skip-ssh Skip SSH key generation and old-key removal steps.
--skip-files Skip the home-directory file scan/replace step.
Examples: $ migrate_repos --skip-ssh --apply Command-line script: run ``python migrate_repos.py`` or the ``migrate_repos`` command.
The module is importable as well."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from dh import SKIP_DIRS
from loguru import logger

DEFAULT_OLD_USERNAME: str = "i4147"
DEFAULT_OLD_EMAIL: str = "yesnacoc@gmail.com"
DEFAULT_NEW_USERNAME: str = "i4147"
DEFAULT_NEW_EMAIL: str = "yesnacoc@gmail.com"
TEXT_EXT: set[str] = {
    ".py",
    ".toml",
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".yml",
    ".yaml",
    ".txt",
    ".rst",
    ".env",
    ".sh",
    ".bash",
    ".zshrc",
    ".gitconfig",
    ".gitmodules",
    ".npmrc",
    ".yarnrc",
}
TEXT_FILENAMES: set[str] = {
    "setup.py",
    "pyproject.toml",
    "package.json",
    "config",
    ".gitconfig",
    "README.md",
    "README",
    ".npmrc",
    "Makefile",
}
MAX_FILE_SIZE: int = 5 * 1024 * 1024
LOG_FILE: str = "github_identity_migrate.log"
_console = logger.add(sys.stdout)
_file_handler = logger.add(
    LOG_FILE,
)


class C:
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"


def color(text: str, c: str):
    if not sys.stdout.isatty():
        return text
    return f"{c}{text}{C.RESET}"


def is_probably_text_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        return b"\x00" not in chunk
    except OSError:
        return False


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS or dirname.startswith(".git")


def matches_target_file(path: Path) -> bool:
    if path.name in TEXT_FILENAMES:
        return True
    return path.suffix in TEXT_EXT


def backup_file(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.{ts}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def run_cmd(cmd: str, input_text=None, check=False) -> subprocess.CompletedProcess[str]:
    logger.debug(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def update_global_git_config(new_username: str, new_email: str, apply_changes: Any) -> None:
    logger.info(color("\n=== Step 1: Global git config ===", C.BOLD))
    result = run_cmd(["git", "config", "--global", "--get", "user.name"])
    current_name = result.stdout.strip()
    result = run_cmd(["git", "config", "--global", "--get", "user.email"])
    current_email = result.stdout.strip()
    logger.info(f"Current global user.name : {current_name or '(not set)'}")
    logger.info(f"Current global user.email: {current_email or '(not set)'}")
    logger.info(f"New global user.name      -> {new_username}")
    logger.info(f"New global user.email     -> {new_email}")
    if not apply_changes:
        logger.info(color("[DRY-RUN] Would update global git config.", C.YELLOW))
        return
    run_cmd(["git", "config", "--global", "user.name", new_username], check=True)
    run_cmd(["git", "config", "--global", "user.email", new_email], check=True)
    logger.info(color("[APPLIED] Global git config updated.", C.GREEN))


def find_local_git_configs(home_dir: Path):
    configs = []
    for root, dirs, files in os.walk(home_dir):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        if Path(root).name == ".git" and "config" in files:
            configs.append(Path(root) / "config")
    return configs


def update_local_git_configs(
    home_dir: Path, old_username: str, old_email: str, new_username: str, new_email: str, apply_changes: Any
) -> None:
    logger.info(color("\n=== Step 1b: Local repo .git/config files ===", C.BOLD))
    configs = find_local_git_configs(home_dir)
    logger.info(f"Found {len(configs)} local repo git config file(s).")
    changed = []
    for cfg in configs:
        try:
            content = cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            logger.warning(f"Could not read {cfg}: {e}")
            continue
        if old_username not in content and old_email not in content:
            continue
        new_content = content.replace(old_username, new_username).replace(old_email, new_email)
        changed.append(cfg)
        logger.info(f"  Would update: {cfg}")
        if apply_changes:
            backup_path = backup_file(cfg)
            cfg.write_text(new_content, encoding="utf-8")
            logger.info(color(f"    [APPLIED] Updated (backup: {backup_path})", C.GREEN))
    if not changed:
        logger.info("No local repo configs reference the old identity.")
    elif not apply_changes:
        logger.info(
            color(
                f"[DRY-RUN] {len(changed)} local config file(s) would be updated.",
                C.YELLOW,
            )
        )


def scan_files(home_dir: Path):
    matches = []
    for root, dirs, files in os.walk(home_dir):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for fname in files:
            fpath = Path(root) / fname
            if matches_target_file(fpath):
                matches.append(fpath)
    return matches


def preview_and_replace(
    home_dir: Path, old_username: str, old_email: str, new_username: str, new_email: str, apply_changes: Any
) -> None:
    logger.info(color("\n=== Step 2: Scanning files under home folder ===", C.BOLD))
    logger.info(f"Home directory: {home_dir}")
    candidates = scan_files(home_dir)
    logger.info(f"Scanning {len(candidates)} candidate file(s)...")
    to_change = []
    for fpath in candidates:
        try:
            if fpath.stat().st_size > MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        if not is_probably_text_file(fpath):
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if old_username not in content and old_email not in content:
            continue
        new_content = content.replace(old_username, new_username).replace(old_email, new_email)
        old_lines = content.splitlines()
        new_lines = new_content.splitlines()
        diff_preview = []
        for i, (ol, nl) in enumerate(zip(old_lines, new_lines)):
            if ol != nl:
                diff_preview.append((i + 1, ol, nl))
        to_change.append((fpath, content, new_content, diff_preview))
    if not to_change:
        logger.info(color("No files contain traces of the old identity.", C.GREEN))
        return
    logger.info(color(f"\nFound {len(to_change)} file(s) that would change:", C.YELLOW))
    for fpath, _, _, diff_preview in to_change:
        logger.info(color(f"\n  {fpath}", C.BLUE))
        for lineno, old_l, new_l in diff_preview[:5]:
            logger.info(f"    line {lineno}:")
            logger.info(color(f"      - {old_l.strip()}", C.RED))
            logger.info(color(f"      + {new_l.strip()}", C.GREEN))
        if len(diff_preview) > 5:
            logger.info(f"    ... and {len(diff_preview) - 5} more changed line(s)")
    if not apply_changes:
        logger.info(
            color(
                f"\n[DRY-RUN] {len(to_change)} file(s) would be modified. Re-run with --apply to write changes.",
                C.YELLOW,
            )
        )
        return
    confirm = (
        input(
            color(
                f"\nProceed to modify {len(to_change)} file(s) in-place? Backups (.bak) will be created. [y/N]: ",
                C.BOLD,
            )
        )
        .strip()
        .lower()
    )
    if confirm != "y":
        logger.info("Aborted by user. No files were modified.")
        return
    for fpath, _, new_content, _ in to_change:
        backup_path = backup_file(fpath)
        fpath.write_text(new_content, encoding="utf-8")
        logger.info(color(f"[APPLIED] Updated {fpath} (backup: {backup_path})", C.GREEN))


def create_new_ssh_key(new_email: str, key_path: Path, apply_changes: Any) -> Path | None:
    logger.info(color("\n=== Step 3: Create new SSH key ===", C.BOLD))
    logger.info(f"Target key path: {key_path}")
    logger.info(f"Key type: ed25519, comment: {new_email}")
    if key_path.exists():
        logger.warning(f"Key already exists at {key_path}. Skipping generation.")
        return key_path.with_suffix(".pub")
    if not apply_changes:
        logger.info(color("[DRY-RUN] Would run ssh-keygen to create a new key.", C.YELLOW))
        return None
    key_path.parent.mkdir(parents=True, exist_ok=True)
    use_passphrase = input("Set a passphrase for the new SSH key? [y/N] (recommended): ").strip().lower()
    passphrase = ""
    if use_passphrase == "y":
        import getpass

        passphrase = getpass.getpass("Enter passphrase: ")
        confirm_pass = getpass.getpass("Confirm passphrase: ")
        if passphrase != confirm_pass:
            logger.error("Passphrases do not match. Aborting SSH key creation.")
            return None
    cmd = [
        "ssh-keygen",
        "-t",
        "ed25519",
        "-C",
        new_email,
        "-f",
        str(key_path),
        "-N",
        passphrase,
    ]
    result = run_cmd(cmd, check=True)
    if result.returncode != 0:
        logger.error("ssh-keygen failed. See log for details.")
        return None
    logger.info(color(f"[APPLIED] New SSH key created at {key_path}", C.GREEN))
    pub_path = key_path.with_suffix(".pub")
    if pub_path.exists():
        pub_content = pub_path.read_text(encoding="utf-8").strip()
        logger.info(color("\nYour new PUBLIC key (add this to GitHub):", C.BOLD))
        logger.info(color(pub_content, C.BLUE))
        logger.info("Add it at: https://github.com/settings/keys\n")
    add_agent = input("Add new key to ssh-agent now? [y/N]: ").strip().lower()
    if add_agent == "y":
        run_cmd(["ssh-add", str(key_path)])
        logger.info(color("[APPLIED] Key added to ssh-agent.", C.GREEN))
    return pub_path


def find_old_ssh_keys(ssh_dir: Path, old_username: str, old_email: str):
    matches = []
    if not ssh_dir.exists():
        return matches
    for pub_file in ssh_dir.glob("*.pub"):
        try:
            content = pub_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if old_username in content or old_email in content:
            private_key = pub_file.with_suffix("")
            matches.append((pub_file, private_key if private_key.exists() else None))
    return matches


def remove_old_ssh_keys(ssh_dir: Path, old_username: str, old_email: str, apply_changes: Any) -> None:
    logger.info(color("\n=== Step 4: Find & remove old SSH keys ===", C.BOLD))
    logger.info(f"SSH directory: {ssh_dir}")
    matches = find_old_ssh_keys(ssh_dir, old_username, old_email)
    if not matches:
        logger.info(
            color(
                "No old SSH public keys matching the previous identity were found.",
                C.GREEN,
            )
        )
        return
    logger.info(color(f"Found {len(matches)} old key pair(s):", C.YELLOW))
    for pub, priv in matches:
        logger.info(f"  Public : {pub}")
        if priv:
            logger.info(f"  Private: {priv}")
        else:
            logger.info("  Private: (not found)")
    if not apply_changes:
        logger.info(color("[DRY-RUN] Would remove the above key files.", C.YELLOW))
        return
    confirm = (
        input(
            color(
                f"\nPermanently delete these {len(matches)} key pair(s)? [y/N]: ",
                C.BOLD,
            )
        )
        .strip()
        .lower()
    )
    if confirm != "y":
        logger.info("Aborted by user. No SSH keys were deleted.")
        return
    for pub, priv in matches:
        try:
            if priv and priv.exists():
                bak = backup_file(priv)
                priv.unlink()
                logger.info(color(f"[APPLIED] Removed private key {priv} (backup: {bak})", C.GREEN))
            if pub.exists():
                bak = backup_file(pub)
                pub.unlink()
                logger.info(color(f"[APPLIED] Removed public key  {pub} (backup: {bak})", C.GREEN))
        except OSError as e:
            logger.error(f"Failed to remove key files: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate local GitHub identity (git config, files, SSH keys). "
        "Defaults to dry-run; pass --apply to make changes."
    )
    parser.add_argument(
        "--old-username",
        default=DEFAULT_OLD_USERNAME,
        help=f"Old GitHub username (default: {DEFAULT_OLD_USERNAME})",
    )
    parser.add_argument(
        "--old-email",
        default=DEFAULT_OLD_EMAIL,
        help=f"Old email (default: {DEFAULT_OLD_EMAIL})",
    )
    parser.add_argument(
        "--new-username",
        default=DEFAULT_NEW_USERNAME,
        help=f"New GitHub username (default: {DEFAULT_NEW_USERNAME})",
    )
    parser.add_argument(
        "--new-email",
        default=DEFAULT_NEW_EMAIL,
        help=f"New email (default: {DEFAULT_NEW_EMAIL})",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home directory to scan (default: current user's home)",
    )
    parser.add_argument(
        "--ssh-dir",
        type=Path,
        default=Path.home() / ".ssh",
        help="SSH directory (default: ~/.ssh)",
    )
    parser.add_argument(
        "--new-key-path",
        type=Path,
        default=Path.home() / ".ssh" / "id_ed25519_github_new",
        help="Path for the new private key (default: ~/.ssh/id_ed25519_github_new)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes / delete files. Without this flag the script is dry-run only.",
    )
    parser.add_argument(
        "--skip-ssh",
        action="store_true",
        help="Skip SSH key generation and old-key removal steps.",
    )
    parser.add_argument(
        "--skip-files",
        action="store_true",
        help="Skip the home-directory file scan/replace step.",
    )
    args = parser.parse_args()
    logger.info(color("GitHub Identity Migration Tool", C.BOLD))
    logger.info(f"Mode: {'APPLY (changes will be written)' if args.apply else 'DRY-RUN (no changes)'}")
    logger.info(f"Old identity: {args.old_username} ")
    logger.info(f"New identity: {args.new_username} ")
    logger.info(f"Log file: {LOG_FILE}")
    update_global_git_config(args.new_username, args.new_email, args.apply)
    update_local_git_configs(
        args.home,
        args.old_username,
        args.old_email,
        args.new_username,
        args.new_email,
        args.apply,
    )
    if not args.skip_files:
        preview_and_replace(
            args.home,
            args.old_username,
            args.old_email,
            args.new_username,
            args.new_email,
            args.apply,
        )
    else:
        logger.info(color("\n=== Step 2: Skipped (--skip-files) ===", C.YELLOW))
    if not args.skip_ssh:
        create_new_ssh_key(args.new_email, args.new_key_path, args.apply)
        remove_old_ssh_keys(args.ssh_dir, args.old_username, args.old_email, args.apply)
    else:
        logger.info(color("\n=== Steps 3/4: Skipped (--skip-ssh) ===", C.YELLOW))
    logger.info(color("\n=== Done ===", C.BOLD))
    if not args.apply:
        logger.info(
            color(
                "This was a dry-run. Re-run with --apply to perform the changes.",
                C.YELLOW,
            )
        )
    else:
        logger.info(
            color(
                "Remember the MANUAL steps listed at the top of the script "
                "(add new public key to GitHub, revoke old key, update credentials).",
                C.YELLOW,
            )
        )


if __name__ == "__main__":
    main()

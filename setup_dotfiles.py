#!/data/data/com.termux/files/usr/bin/env python
"""Turn ``$HOME`` into an age-encrypted bare dotfiles repository.
Installs nothing but verifies the required tools (git, age, git-age-crypt), creates an age key pair, writes the ``git-age-filter`` clean/smudge filter, the ``.gitattributes`` and ``.gitignore`` rules and initialises a bare ``~/dotfiles.git`` with the ``$HOME`` work tree.
Usage: setup_dotfiles Examples: $ setup_dotfiles Runs as a script (``python setup_dotfiles.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap


HOME = Path("~").expanduser()
AGE_DIR = Path(HOME).joinpath(".config", "age")
AGE_KEY = Path(AGE_DIR).joinpath("keys.txt")
AGE_PUB = Path(AGE_DIR).joinpath("public.key")
FILTER_PATH = Path(HOME).joinpath(".local", "bin", "git-age-filter")
REPO_DIR = Path(HOME).joinpath("dotfiles.git")
GITATTRIBUTES = Path(HOME).joinpath(".gitattributes")
GITIGNORE = Path(HOME).joinpath(".gitignore")
SECRET_PATTERNS = [
    "secrets/**",
    "*.secret",
    ".env",
    "*.token",
]
IGNORE_PATTERNS = [
    ".config/age/",
    "dotfiles.git/",
    "storage/",
    "*.log",
    ".cache/",
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def which(binary: str) -> str | None:
    return shutil.which(binary) or (
        Path(f"/data/data/com.termux/files/usr/bin/{binary}").exists()
        and f"/data/data/com.termux/files/usr/bin/{binary}"
    )


def ensure_tools() -> None:
    missing = [b for b in ("age", "age-keygen", "git") if not which(b)]
    if missing:
        print(f"Missing tools: {', '.join(missing)}")
        print("Install with:  pkg install age git")
        sys.exit(1)


def ensure_age_key() -> str:
    os.makedirs(AGE_DIR, mode=0o700, exist_ok=True)
    if not Path(AGE_KEY).exists():
        print("Generating age keypair...")
        out = subprocess.run(
            [which("age-keygen") or "age-keygen", "-o", AGE_KEY],
            capture_output=True,
        )
        if out.returncode != 0:
            print(out.stderr.decode(), file=sys.stderr)
            sys.exit(1)
        os.chmod(AGE_KEY, 0o600)
    pubkey = None
    with open(AGE_KEY) as f:
        for line in f:
            if line.startswith("# public key:"):
                pubkey = line.split(":", 1)[1].strip()
                break
    if not pubkey:
        print("Could not find public key in keys.txt", file=sys.stderr)
        sys.exit(1)
    with open(AGE_PUB, "w") as f:
        f.write(pubkey + "\n")
    os.chmod(AGE_PUB, 0o644)
    print(f"age public key: {pubkey}")
    return pubkey


FILTER_SOURCE = r'''#!/data/data/com.termux/files/usr/bin/python
"""Git clean/smudge filter using age. Managed by setup_dotfiles.py."""
import os, subprocess, sys
from pathlib import Path
AGE_BIN = "age"
AGE_KEY_FILE = Path("~/.config/age/keys.txt").expanduser()
AGE_PUBKEY_FILE = Path("~/.config/age/public.key").expanduser()
def die(msg, code=1):
    print(f"git-age-filter: {msg}", file=sys.stderr)
    sys.exit(code)
def clean():
    if not AGE_PUBKEY_FILE.exists():
        die(f"missing public key: {AGE_PUBKEY_FILE}")
    pubkey = open(AGE_PUBKEY_FILE).read().strip()
    p = subprocess.run([AGE_BIN, "-r", pubkey, "-a"],
                       input=sys.stdin.buffer.read(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        die(f"encrypt failed: {p.stderr.decode(errors='replace')}")
    sys.stdout.buffer.write(p.stdout)
def smudge():
    data = sys.stdin.buffer.read()
    if not data.lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
        sys.stdout.buffer.write(data); return
    if not AGE_KEY_FILE.exists():
        print("git-age-filter: no private key; leaving ciphertext", file=sys.stderr)
        sys.stdout.buffer.write(data); return
    p = subprocess.run([AGE_BIN, "-d", "-i", AGE_KEY_FILE],
                       input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        die(f"decrypt failed: {p.stderr.decode(errors='replace')}")
    sys.stdout.buffer.write(p.stdout)
if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("clean", "smudge"):
        die("usage: git-age-filter {clean|smudge}")
    (clean if sys.argv[1] == "clean" else smudge)()
'''


def write_filter() -> None:
    os.makedirs(Path(FILTER_PATH).parent, exist_ok=True)
    with open(FILTER_PATH, "w") as f:
        f.write(FILTER_SOURCE)
    os.chmod(FILTER_PATH, 0o755)
    print(f"wrote filter: {FILTER_PATH}")


def write_gitattributes() -> None:
    lines = ["# Managed by setup_dotfiles.py — encrypted via age filter"]
    lines += [f"{p} filter=age diff=age" for p in SECRET_PATTERNS]
    with open(GITATTRIBUTES, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote: {GITATTRIBUTES}")


def write_gitignore() -> None:
    existing = ""
    if Path(GITIGNORE).exists():
        existing = open(GITIGNORE).read()
    lines = ["# Managed by setup_dotfiles.py"]
    lines += [p for p in IGNORE_PATTERNS if p not in existing]
    mode = "a" if existing else "w"
    with open(GITIGNORE, mode) as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n".join(lines) + "\n")
    print(f"updated: {GITIGNORE}")


def git(*args: str, cwd: str | None = None) -> None:
    cmd = ["git", f"--git-dir={REPO_DIR}", f"--work-tree={HOME}", *args]
    subprocess.run(cmd, check=True, cwd=cwd)


def init_repo() -> None:
    if not Path(REPO_DIR).is_dir():
        run(["git", "init", "--bare", REPO_DIR])
        print(f"initialized bare repo: {REPO_DIR}")
    git("config", "filter.age.clean", FILTER_PATH + " clean")
    git("config", "filter.age.smudge", FILTER_PATH + " smudge")
    git("config", "filter.age.required", "true")
    git("config", "diff.age.textconv", FILTER_PATH + " smudge")
    git("config", "status.showUntrackedFiles", "no")
    git("config", "core.excludesfile", GITIGNORE)
    print("registered age clean/smudge filters")


def main() -> None:
    ensure_tools()
    ensure_age_key()
    write_filter()
    write_gitattributes()
    write_gitignore()
    init_repo()
    print(
        textwrap.dedent(f"""
    ✅ Setup complete.
    Your repo is a bare repo at:
        {REPO_DIR}
    with work-tree = {HOME}
    Add this alias to ~/.bashrc (or ~/.zshrc):
        alias dot='git --git-dir=$HOME/dotfiles.git --work-tree=$HOME'
    Then:
        dot add .gitattributes .gitignore
        dot add .bashrc .zshrc .config/nvim secrets/api-keys.env
        dot commit -m "initial dotfiles (encrypted secrets)"
        dot remote add origin <your-repo-url>
        dot push -u origin main
    Verify a secret is encrypted before pushing:
        dot show HEAD:secrets/api-keys.env
        # should print -----BEGIN AGE ENCRYPTED FILE-----
    🔑 BACK UP {AGE_KEY} somewhere safe (password manager).
    Lose it and the encrypted files are unrecoverable.
    """)
    )


if __name__ == "__main__":
    main()

#!/data/data/com.termux/files/usr/bin/env python
"""Clone the GitHub repositories named in a file, skipping large or unwanted ones.
Reads one ``owner/repo`` per line from the file given on the command line, looks up each repository size through the GitHub API (cached for a week in ``repo_sizes.json``) and clones only the repositories no larger than 5 MB.
Names containing django, flask, torch, tensor, llm or mcp are skipped.
A ``GITHUB_TOKEN`` in ``~/.env`` raises the API rate limit.
Usage: autoclone REPOS_FILE Examples: $ autoclone repos.txt Runs as a script (``python autoclone.py``) in the directory it should act on; the module can also be imported."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import load_dotenv
import requests


env_path = Path.home() / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✅ Loaded environment from {env_path}")
else:
    print("⚠️  ~/.env not found, using system environment variables")
SIZE_CACHE_FILE = "repo_sizes.json"
CACHE_EXPIRY_DAYS = 7


def get_github_token():
    token = os.getenv("GITHUB_TOKEN")
    if token:
        print("✅ GitHub token found")
        return token
    else:
        print("⚠️  No GITHUB_TOKEN found in environment")
        print("   (Using unauthenticated requests - rate limit: 60/hr)")
        return None


def load_size_cache():
    cache_file = Path(SIZE_CACHE_FILE)
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cache_data = json.load(f)
                cache_date = datetime.fromisoformat(cache_data.get("_cache_date", "2000-01-01"))
                if datetime.now() - cache_date < timedelta(days=CACHE_EXPIRY_DAYS):
                    print(f"📂 Loaded cache from {SIZE_CACHE_FILE} ({len(cache_data) - 1} repos)")
                    return cache_data
                else:
                    print(f"⏰ Cache expired (older than {CACHE_EXPIRY_DAYS} days), refreshing...")
                    return {}
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"⚠️  Error reading cache file: {e}")
            return {}
    return {}


def save_size_cache(cache_data):
    cache_data["_cache_date"] = datetime.now().isoformat()
    cache_data["_cache_version"] = "1.0"
    try:
        with open(SIZE_CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
        print(f"💾 Saved {len(cache_data) - 1} repository sizes to {SIZE_CACHE_FILE}")
    except Exception as e:
        print(f"⚠️  Error saving cache: {e}")


def get_repo_size(repo, token=None, cache_data=None):
    if cache_data and repo in cache_data:
        cached_size = cache_data[repo].get("size_mb")
        cached_date = cache_data[repo].get("fetched_at", "")
        if cached_size is not None:
            print(f"  📦 Using cached size: {cached_size:.2f} MB (fetched: {cached_date})")
            return cached_size
    api_url = f"https://api.github.com/repos/{repo}"
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        response = requests.get(api_url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            size_kb = data.get("size", 0)
            size_mb = size_kb / 1024
            if cache_data is not None:
                cache_data[repo] = {
                    "size_mb": size_mb,
                    "size_kb": size_kb,
                    "fetched_at": datetime.now().isoformat(),
                    "repo_name": repo,
                    "full_name": data.get("full_name", repo),
                    "description": data.get("description", ""),
                    "private": data.get("private", False),
                }
            return size_mb
        elif response.status_code == 403 and "rate limit" in response.text.lower():
            print("  ⚠️  Rate limit exceeded! Please check your GITHUB_TOKEN")
            return None
        else:
            print(f"  ⚠️  Error fetching {repo}: HTTP {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Error fetching {repo}: {e}")
        return None


def clone_repo(repo):
    clone_url = f"https://github.com/{repo}.git"
    repo_name = repo.split("/")[-1]
    if Path(repo_name).exists():
        print(f"  ⏭️  {repo_name} already exists, skipping...")
        return True
    try:
        print(f"  🔄 Cloning {repo}...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print(f"  ✅ Successfully cloned {repo_name}")
            return True
        else:
            print(f"  ❌ Failed to clone {repo}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ❌ Timeout cloning {repo}")
        return False
    except Exception as e:
        print(f"  ❌ Error cloning {repo}: {e}")
        return False


def display_cached_stats(cache_data):
    if not cache_data or len(cache_data) <= 1:
        return
    repos = {k: v for k, v in cache_data.items() if k not in ["_cache_date", "_cache_version"]}
    if not repos:
        return
    sizes = [v["size_mb"] for v in repos.values() if "size_mb" in v]
    total_size = sum(sizes)
    avg_size = total_size / len(sizes) if sizes else 0
    largest = max(sizes) if sizes else 0
    smallest = min(sizes) if sizes else 0
    print("\n📊 Cache Statistics:")
    print(f"  📦 Total repos in cache: {len(repos)}")
    print(f"  📊 Total size: {total_size:.2f} MB")
    print(f"  📊 Average size: {avg_size:.2f} MB")
    print(f"  📈 Largest: {largest:.2f} MB")
    print(f"  📉 Smallest: {smallest:.2f} MB")
    print(f"  📅 Cached on: {cache_data.get('_cache_date', 'Unknown')}")


def main():
    fn = sys.argv[1]
    repos_file = Path(fn)
    if not repos_file.exists():
        print("❌ repos.txt not found in current directory")
        sys.exit(1)
    token = get_github_token()
    cache_data = load_size_cache()
    with open(repos_file) as f:
        repos = [line.strip() for line in f if line.strip()]
    if not repos:
        print("❌ No repositories found in repos.txt")
        sys.exit(1)
    print(f"\n📚 Found {len(repos)} repositories")
    print("🔍 Checking and cloning in one pass...")
    print("-" * 40)
    size_limit_mb = 5
    cloned_count = 0
    skipped_count = 0
    failed_count = 0
    total_size = 0
    no_size_count = 0
    for idx, repo in enumerate(repos, 1):
        repo_lower = repo.lower()
        if "django" in repo_lower or "flask" in repo_lower:
            skipped_count += 1
            continue
        if "torch" in repo_lower or "tensor" in repo_lower:
            skipped_count += 1
            continue
        if "llm" in repo_lower or "mcp" in repo_lower:
            skipped_count += 1
            continue
        print(f"\n[{idx}/{len(repos)}] 📦 Processing {repo}")
        print("-" * 40)
        size_mb = get_repo_size(repo, token, cache_data)
        if size_mb is None:
            no_size_count += 1
            continue
        print(f"  📊 {size_mb:.2f} MB")
        if size_mb > size_limit_mb:
            skipped_count += 1
            continue
        print(f"  ✅ Within limit ({size_limit_mb} MB)")
        total_size += size_mb
        if clone_repo(repo):
            cloned_count += 1
        else:
            failed_count += 1
    if cache_data:
        save_size_cache(cache_data)
        display_cached_stats(cache_data)
    print("\n" + "=" * 40)
    print("📊 FINAL SUMMARY")
    print("-" * 40)
    print(f"✅ Successfully cloned: {cloned_count}")
    print(f"❌ Failed to clone: {failed_count}")
    print(f"⏭️  Skipped (too large): {skipped_count}")
    print(f"⚠️  Skipped (no size info): {no_size_count}")
    print(f"📊 Total size of cloned repos: {total_size:.2f} MB")
    print(f"📚 Total repos processed: {len(repos)}")
    if token:
        print("\n💡 Using authenticated requests (rate limit: 5000/hr)")
    else:
        print("\n💡 Using unauthenticated requests (rate limit: 60/hr)")
        print("   Consider adding GITHUB_TOKEN to ~/.env for higher limits")
    print(f"\n💾 Repository size data saved to: {SIZE_CACHE_FILE}")


if __name__ == "__main__":
    raise SystemExit(main())

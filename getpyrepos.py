#!/data/data/com.termux/files/usr/bin/env python
"""List the Python repositories of a GitHub user and save them as JSON.
Queries the GitHub API for the user's repositories, keeps those written in Python and writes ``<user>_python_repos.json``.
Without an argument it walks the currently trending users and writes ``trending_github_python_users.json`` instead.
Usage: getpyrepos [USERNAME] Examples: $ getpyrepos psf Runs as a script (``python getpyrepos.py``) in the directory it should act on; the module can also be imported."""

from datetime import datetime, timedelta
import json
import sys

import requests


def get_user_repos(username: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        url = f"https://api.github.com/users/{username}/repos"
        params = {"page": page, "per_page": 100, "sort": "updated"}
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            page_repos = response.json()
            if not page_repos:
                break
            for repo in page_repos:
                if repo.get("language") == "Python":
                    repos.append({
                        "name": repo["name"],
                        "full_name": repo["full_name"],
                        "description": repo.get("description", ""),
                        "url": repo["html_url"],
                        "stars": repo["stargazers_count"],
                        "forks": repo["forks_count"],
                        "language": repo["language"],
                        "created_at": repo["created_at"],
                        "updated_at": repo["updated_at"],
                        "private": repo["private"],
                    })
            if len(page_repos) < 100:
                break
            page += 1
        except requests.exceptions.RequestException as e:
            print(f"Error fetching repos for {username}: {e}", file=sys.stderr)
            break
    return repos


def get_top_trending_users() -> list[dict]:
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    url = "https://api.github.com/search/users"
    params = {
        "q": f"language:python created:>{week_ago}",
        "sort": "followers",
        "order": "desc",
        "per_page": 10,
    }
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        trending_users = []
        for user in data.get("items", []):
            user_url = user["url"]
            user_response = requests.get(user_url)
            user_response.raise_for_status()
            user_data = user_response.json()
            trending_users.append({
                "username": user_data["login"],
                "name": user_data.get("name", ""),
                "followers": user_data["followers"],
                "repos_url": user_data["repos_url"],
                "html_url": user_data["html_url"],
                "repositories": get_user_repos(user_data["login"]),
            })
        return trending_users
    except requests.exceptions.RequestException as e:
        print(f"Error fetching trending users: {e}", file=sys.stderr)
        return []


def save_to_json(data: any, filename: str = "github_repos.json") -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Data saved to {filename}")


def main():
    if len(sys.argv) > 1:
        username = sys.argv[1]
        print(f"Fetching Python repositories for user: {username}")
        repos = get_user_repos(username)
        if repos:
            output = {
                "username": username,
                "total_python_repos": len(repos),
                "repositories": repos,
            }
            filename = f"{username}_python_repos.json"
            save_to_json(output, filename)
            print(f"Found {len(repos)} Python repositories")
        else:
            print(f"No Python repositories found for user {username}")
    else:
        print("No username provided. Fetching top trending GitHub users with Python repos...")
        trending_data = get_top_trending_users()
        if trending_data:
            output = {
                "timestamp": datetime.now().isoformat(),
                "trending_users": trending_data,
            }
            save_to_json(output, "trending_github_python_users.json")
            print(f"Found {len(trending_data)} trending users")
            for user in trending_data:
                print(f"\n👤 {user['username']} ({user['name']})")
                print(f"   Followers: {user['followers']}")
                print(f"   Python repos: {len(user['repositories'])}")
        else:
            print("No trending users found")


if __name__ == "__main__":
    raise SystemExit(main())

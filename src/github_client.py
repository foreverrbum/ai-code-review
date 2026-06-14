"""
github_client.py

Fetches PR diffs and metadata from GitHub using the GitHub API.

Enables: python main.py --github owner/repo/pull/123 --repo /path/to/local/checkout

Why we still need --repo (local checkout):
  The GitHub API gives us the diff, but context retrieval (finding callers,
  tests, type definitions) requires reading arbitrary files from the repo.
  We use the diff from GitHub + the local repo filesystem for context.

  Alternative: use GitHub's Contents API to fetch files on demand.
  We implement that as a fallback when no local repo is available.

Token setup:
  1. github.com/settings/tokens → New token (classic) → scope: repo
  2. Add to .env: GITHUB_TOKEN=ghp_...

Rate limits:
  - Authenticated: 5,000 requests/hour
  - A single PR fetch uses 2-3 requests (PR metadata + diff)
  - At 1,000 PRs/day you'd use ~3,000 requests/hour at peak — plan accordingly
"""

import os
import re
import requests
from dotenv import load_dotenv

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    override=True
)


def _get_token() -> str:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN not set in .env\n"
            "Create one at: github.com/settings/tokens\n"
            "Required scope: repo"
        )
    return token


def _parse_pr_ref(pr_ref: str) -> tuple:
    """
    Parse 'owner/repo/pull/123' or a full GitHub URL into (owner, repo, pr_number).

    Accepts:
      - owner/repo/pull/123
      - https://github.com/owner/repo/pull/123
    """
    # Full URL
    url_match = re.match(
        r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_ref
    )
    if url_match:
        return url_match.group(1), url_match.group(2), int(url_match.group(3))

    # Short form: owner/repo/pull/123
    short_match = re.match(r'([^/]+)/([^/]+)/pull/(\d+)', pr_ref)
    if short_match:
        return short_match.group(1), short_match.group(2), int(short_match.group(3))

    # Even shorter: owner/repo/123
    short2 = re.match(r'([^/]+)/([^/]+)/(\d+)$', pr_ref)
    if short2:
        return short2.group(1), short2.group(2), int(short2.group(3))

    raise ValueError(
        f"Cannot parse PR reference: {pr_ref!r}\n"
        f"Expected format: owner/repo/pull/123 or https://github.com/owner/repo/pull/123"
    )


def fetch_pr_diff(pr_ref: str) -> str:
    """
    Download the unified diff for a GitHub PR.

    Args:
        pr_ref: 'owner/repo/pull/123' or a full GitHub PR URL

    Returns:
        The raw diff text (same format as `git diff`)
    """
    owner, repo, pr_number = _parse_pr_ref(pr_ref)
    token = _get_token()

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",  # request raw diff format
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code == 401:
        raise RuntimeError("GitHub token is invalid or expired. Regenerate at github.com/settings/tokens")
    elif response.status_code == 403:
        raise RuntimeError("GitHub token lacks 'repo' scope. Regenerate with repo access.")
    elif response.status_code == 404:
        raise RuntimeError(f"PR not found: {owner}/{repo}/pull/{pr_number}. Is the repo private?")
    elif response.status_code != 200:
        raise RuntimeError(f"GitHub API error {response.status_code}: {response.text[:200]}")

    return response.text


def fetch_pr_metadata(pr_ref: str) -> dict:
    """
    Fetch PR metadata: title, description, author, base/head branches.
    Useful for enriching the review prompt.
    """
    owner, repo, pr_number = _parse_pr_ref(pr_ref)
    token = _get_token()

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()

    return {
        "title":        data.get("title", ""),
        "description":  data.get("body", "") or "",
        "author":       data.get("user", {}).get("login", ""),
        "base_branch":  data.get("base", {}).get("ref", ""),
        "head_branch":  data.get("head", {}).get("ref", ""),
        "additions":    data.get("additions", 0),
        "deletions":    data.get("deletions", 0),
        "changed_files": data.get("changed_files", 0),
        "url":          data.get("html_url", ""),
    }


def fetch_file_from_github(owner: str, repo: str, path: str, ref: str = "main") -> str:
    """
    Fetch a single file's content from GitHub (fallback when no local repo).
    Useful for context retrieval without a local checkout.

    Args:
        owner, repo: GitHub repo coordinates
        path:        File path within the repo (e.g. 'src/auth/service.py')
        ref:         Branch or commit SHA (default: main)
    """
    import base64
    token = _get_token()

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    params = {"ref": ref}

    response = requests.get(url, headers=headers, params=params, timeout=30)
    if response.status_code == 404:
        return ''
    response.raise_for_status()

    data = response.json()
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
    return data.get("content", "")


def post_review_comment(pr_ref: str, review_text: str, event: str = "COMMENT") -> dict:
    """
    Post the AI review back to the GitHub PR.

    Args:
        pr_ref:      'owner/repo/pull/123'
        review_text: The review markdown text
        event:       'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT'

    Returns:
        The GitHub API response dict
    """
    owner, repo, pr_number = _parse_pr_ref(pr_ref)
    token = _get_token()

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "body":  review_text,
        "event": event,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

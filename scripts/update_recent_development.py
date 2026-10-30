#!/usr/bin/env python3
"""Update a Profile README from public commits, using only the standard library."""

import argparse
import html
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

START = "<!-- recent-development:start -->"
END = "<!-- recent-development:end -->"
ROOT = Path(__file__).resolve().parents[1]
FALLBACKS = {
    "feat": "Feature development",
    "fix": "Fixes and maintenance",
    "perf": "Performance improvements",
    "refactor": "Code refactoring",
    "docs": "Documentation update",
    "ci": "CI workflow update",
    "build": "Build configuration update",
    "test": "Test updates",
    "chore": "Repository maintenance",
    "style": "Code style update",
    "revert": "Revert a change",
}


class UpdateError(Exception):
    pass


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", config["account"]):
        raise UpdateError("Invalid account in configuration.")
    if not config["repositories"] or len(config["repositories"]) > 10:
        raise UpdateError("Configure between 1 and 10 repositories.")
    for repo in config["repositories"]:
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("Invalid repository in configuration.")
    if not 1 <= config["limit"] <= 10 or not 1 <= config["max_pages"] <= 10:
        raise UpdateError("Invalid limit or pagination bound.")
    return config


def request_commits(repository, account, page, token):
    query = urlencode({"author": account, "per_page": 100, "page": page})
    url = "https://api.github.com/repos/" + repository + "/commits?" + query
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": account + "-profile-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            rows = json.load(response)
    except HTTPError as error:
        raise UpdateError(
            "GitHub returned HTTP " + str(error.code) + " for " + repository
            + "; keeping the existing README."
        ) from None
    except (URLError, TimeoutError, ValueError) as error:
        raise UpdateError(
            "Could not read " + repository + "; keeping the existing README."
        ) from error
    if not isinstance(rows, list):
        raise UpdateError("Unexpected API response; keeping the existing README.")
    return rows


def is_bot(user):
    return bool(user) and (
        user.get("type", "").lower() == "bot"
        or user.get("login", "").lower().endswith("[bot]")
    )


def commit_title(commit):
    lines = commit.get("message", "").splitlines()
    title = lines[0].strip() if lines else ""
    title = re.sub(
        r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]",
        "", title, flags=re.IGNORECASE,
    )
    return " ".join(title.split())


def english_title(commit, sha, account):
    title = commit_title(commit)
    names = [
        (commit.get(role) or {}).get("name", "").strip()
        for role in ("author", "committer")
    ]
    exposes_name = any(
        len(name) >= 4 and name.casefold() != account.casefold()
        and name.casefold() in title.casefold()
        for name in names
    )
    if title and title.isascii() and not exposes_name:
        return title if len(title) <= 88 else title[:85].rstrip() + "..."
    prefix = re.match(r"^([a-z]+)(?:\([^)]*\))?!?:", title, re.IGNORECASE)
    category = FALLBACKS.get(prefix.group(1).lower(), "Code update") if prefix else "Code update"
    return category + " (" + sha[:7] + ")"


def collect_entries(config, token):
    entries = {}
    topics = {}
    for repository in config["repositories"]:
        for page in range(1, config["max_pages"] + 1):
            rows = request_commits(repository, config["account"], page, token)
            for row in rows:
                author = row.get("author") or {}
                if author.get("login", "").casefold() != config["account"].casefold():
                    continue
                if is_bot(author) or is_bot(row.get("committer")):
                    continue
                if len(row.get("parents", [])) > 1:
                    continue
                sha = row.get("sha", "")
                if not re.fullmatch(r"[0-9a-f]{40}", sha):
                    raise UpdateError("Invalid commit SHA; keeping the existing README.")
                commit = row["commit"]
                committed_at = commit["committer"]["date"]
                timestamp = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise UpdateError("Commit timestamp has no timezone.")
                key = (repository, sha)
                entries[key] = {
                    "repository": repository,
                    "sha": sha,
                    "url": "https://github.com/" + repository + "/commit/" + sha,
                    "title": english_title(commit, sha, config["account"]),
                    "committed_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                # Compare full source titles before display truncation or translation.
                topics[key] = commit_title(commit) or sha
            if len(rows) < 100:
                break
        # Every configured repository must succeed before any README is written.
    ordered = sorted(
        entries.values(),
        key=lambda entry: (entry["committed_at"], entry["repository"], entry["sha"]),
        reverse=True,
    )
    selected = []
    seen = set()
    for entry in ordered:
        topic = (
            entry["repository"].casefold(),
            topics[(entry["repository"], entry["sha"])],
        )
        if topic in seen:
            continue
        seen.add(topic)
        selected.append(entry)
        if len(selected) == config["limit"]:
            break
    return selected


def replace_section(readme, entries):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise UpdateError("README must contain exactly one pair of update markers.")
    start = readme.index(START) + len(START)
    end = readme.index(END)
    if end < start:
        raise UpdateError("README update markers are out of order.")
    blocks = []
    for entry in entries:
        title = html.escape(entry["title"], quote=True)
        repo = html.escape(entry["repository"].split("/", 1)[1], quote=True)
        blocks.append(
            '<a href="' + entry["url"] + '">' + title + "</a><br>\n"
            "<sub>" + repo + " · " + entry["committed_at"][:10] + "</sub>"
        )
    return readme[:start] + "\n" + "\n\n".join(blocks) + "\n" + readme[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "profile-config.json")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--snapshot", type=Path, help="Optional public display data for a local preview.")
    parser.add_argument("--preview-data", type=Path, help="Optional JavaScript data file for an offline preview.")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        original = args.readme.read_text(encoding="utf-8")
        # Validate markers before making network requests.
        replace_section(original, [])
        entries = collect_entries(config, os.environ.get("GITHUB_TOKEN", ""))
        if not entries:
            print("No matching public commits; keeping the existing README.")
            return 0
        updated = replace_section(original, entries)
        if updated != original:
            args.readme.write_text(updated, encoding="utf-8")
        if args.snapshot:
            snapshot = {"account": config["account"], "entries": entries}
            args.snapshot.write_text(
                json.dumps(snapshot, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.preview_data:
            args.preview_data.write_text(
                "window.RECENT_DEVELOPMENT_DATA = "
                + json.dumps({"account": config["account"], "entries": entries}, ensure_ascii=True, indent=2)
                + ";\n",
                encoding="utf-8",
            )
        print("Recent development: " + str(len(entries)) + " public commits; "
              + ("README updated." if updated != original else "already up to date."))
        return 0
    except (UpdateError, KeyError, OSError, ValueError, TypeError) as error:
        print("Profile update failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

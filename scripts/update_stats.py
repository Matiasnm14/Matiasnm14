#!/usr/bin/env python3
"""Update only the statistics block, using GitHub's public GraphQL data."""

import datetime as dt
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- GITHUB-STATS:START -->"
END = "<!-- GITHUB-STATS:END -->"


class GitHub:
    def __init__(self, token):
        self.token = token

    def query(self, query, **variables):
        body = json.dumps({"query": query, "variables": variables}).encode()
        for attempt in range(4):
            request = urllib.request.Request(
                "https://api.github.com/graphql", data=body,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json",
                         "User-Agent": "profile-stats"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    result = json.load(response)
                if result.get("errors"):
                    messages = "; ".join(e["message"] for e in result["errors"])
                    raise RuntimeError(f"GitHub GraphQL: {messages}")
                return result["data"]
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"GitHub HTTP {exc.code}; README preserved") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise RuntimeError("GitHub unavailable; README preserved") from None
            time.sleep(2 ** attempt)


def repositories(api, username, contributed=False):
    field = "repositoriesContributedTo" if contributed else "repositories"
    filters = (
        "includeUserRepositories: false, contributionTypes: [COMMIT]"
        if contributed else "ownerAffiliations: [OWNER]"
    )
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        id
        COLLECTION(first: 100, after: $cursor, privacy: PUBLIC, FILTERS) {
          nodes { nameWithOwner isPrivate }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """.replace("COLLECTION", field).replace("FILTERS", filters)
    cursor = None
    found = set()
    while True:
        user = api.query(query, login=username, cursor=cursor)["user"]
        if user is None:
            raise RuntimeError(f"GitHub user not found: {username}")
        connection = user[field]
        found.update(node["nameWithOwner"] for node in connection["nodes"]
                     if node and not node["isPrivate"])
        if not connection["pageInfo"]["hasNextPage"]:
            return user["id"], found
        cursor = connection["pageInfo"]["endCursor"]


def authored_commits(api, repository, author_id):
    # Resolve the branch once; subsequent pages follow the same immutable head.
    owner, name = repository.split("/", 1)
    repo = api.query("""
      query($owner: String!, $name: String!) {
        repository(owner: $owner, name: $name) {
          isPrivate defaultBranchRef { target { oid } }
        }
      }
    """, owner=owner, name=name)["repository"]
    if repo is None:
        raise RuntimeError(f"Repository unavailable: {repository}; README preserved")
    if repo["isPrivate"] or repo["defaultBranchRef"] is None:
        return
    head = repo["defaultBranchRef"]["target"]["oid"]
    cursor = None
    query = """
      query($owner: String!, $name: String!, $head: String!, $id: ID!, $cursor: String) {
        repository(owner: $owner, name: $name) {
          isPrivate
          object(expression: $head) {
            ... on Commit {
              history(first: 100, after: $cursor, author: {id: $id}) {
                nodes { oid additions deletions }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
      }
    """
    while True:
        repo = api.query(query, owner=owner, name=name, head=head,
                         id=author_id, cursor=cursor)["repository"]
        if not repo or repo["isPrivate"] or not repo["object"]:
            raise RuntimeError(f"Repository changed while reading: {repository}")
        history = repo["object"]["history"]
        yield from history["nodes"]
        if not history["pageInfo"]["hasNextPage"]:
            return
        cursor = history["pageInfo"]["endCursor"]


def collect_stats(api, username):
    author_id, owned = repositories(api, username)
    _, contributed = repositories(api, username, contributed=True)
    contributed -= owned
    seen = set()
    additions = deletions = 0
    for repo in sorted(owned | contributed):
        print(f"Reading {repo}", flush=True)
        for commit in authored_commits(api, repo, author_id):
            if commit["oid"] in seen:
                continue  # The same commit can be present in a fork and its upstream.
            seen.add(commit["oid"])
            additions += commit["additions"]
            deletions += commit["deletions"]
    return {"repositories": len(owned), "contributed_recent": len(contributed),
            "commits": len(seen), "lines_added": additions,
            "lines_removed": deletions, "lines_net": additions - deletions}


def render_block(stats, date):
    labels = [("Repos.Owned", "repositories"),
              ("Repos.Contributed.Recent", "contributed_recent"),
              ("Commits", "commits"), ("Lines.Added", "lines_added"),
              ("Lines.Removed", "lines_removed"), ("Lines.Net", "lines_net")]
    lines = [START, "```yaml",
             "# GitHub Stats ────────────────────────────────────────────────"]
    for label, key in labels:
        lines.append(f"{label}: {'·' * (26 - len(label))} {stats[key]:,}")
    lines.extend([f"Updated: {'·' * 19} {date} (UTC)", "```", END])
    return "\n".join(lines)


def replace_block(readme, block):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one statistics block")
    pattern = re.escape(START) + r".*?" + re.escape(END)
    updated, count = re.subn(pattern, lambda _: block, readme, flags=re.DOTALL)
    if count != 1:
        raise ValueError("Invalid statistics marker order")
    return updated


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    username = os.environ.get("PROFILE_USERNAME", "Matiasnm14")
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    replace_block(readme, "validation")  # Validate before making API calls.
    stats = collect_stats(GitHub(token), username)
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    updated = replace_block(readme, render_block(stats, date))
    temporary = readme_path.with_suffix(".md.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(readme_path)
    print("Statistics updated successfully")


if __name__ == "__main__":
    main()

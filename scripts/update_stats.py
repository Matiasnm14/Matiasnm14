#!/usr/bin/env python3
"""Update only the statistics block, using GitHub's public GraphQL data."""

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

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


def render_block(stats):
    labels = [("Repos.Owned", "repositories"),
              ("Repos.Contributed.Recent", "contributed_recent"),
              ("Commits", "commits"), ("Lines.Added", "lines_added"),
              ("Lines.Removed", "lines_removed"), ("Lines.Net", "lines_net")]
    lines = [START, "```diff",
             "# GitHub Stats ────────────────────────────────────────────────"]
    for label, key in labels:
        prefix = "++ " if key == "lines_added" else "-- " if key == "lines_removed" else "   "
        lines.append(f"{prefix}{label}: {'·' * (26 - len(label))} {stats[key]:,}")
    lines.extend(["```", END])
    return "\n".join(lines)


def replace_block(readme, block):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one statistics block")
    pattern = re.escape(START) + r".*?" + re.escape(END)
    updated, count = re.subn(pattern, lambda _: block, readme, flags=re.DOTALL)
    if count != 1:
        raise ValueError("Invalid statistics marker order")
    return updated


def render_terminal(stats, readme):
    """Render a GitHub-safe SVG using the README as the personal-data source."""
    profile = dict(re.findall(r"^([A-Za-z][\w.]*):[ .]*(.+)$", readme, re.MULTILINE))
    identity = re.search(r"^([\w-]+) ─", readme, re.MULTILINE)
    if not identity:
        raise ValueError("README must include the terminal identity")
    username = identity.group(1)
    muted, label, value = "#7d8590", "#d2a173", "#a8c7df"
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="580" viewBox="0 0 1120 580" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(username)} — terminal profile</title>',
        '<desc id="desc">System, languages, tools, hobbies, contact and public GitHub statistics. A plain-text version is available in the README.</desc>',
        '<rect x="0.5" y="0.5" width="1119" height="579" rx="8" fill="#161b22" stroke="#30363d"/>',
        '<g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, Liberation Mono, monospace" font-size="15.5">',
    ]

    def text(x, y, content, color=value, size=None, weight=None):
        extra = (f' font-size="{size}"' if size else "") + (f' font-weight="{weight}"' if weight else "")
        elements.append(f'<text x="{x}" y="{y}" fill="{color}"{extra}>{escape(str(content))}</text>')

    def section(x, y, title, end):
        text(x, y, title, "#c9d1d9")
        line_start = x + len(title) * 9.3 + 16
        elements.append(f'<path d="M{line_start} {y-5}H{end}" stroke="#484f58"/>')

    def row(x, y, title, content, color=value):
        text(x, y, title + ":", label)
        leaders = max(0, 23 - len(title))
        text(x + (len(title) + 1) * 9.3, y, "." * leaders, muted)
        text(x + 238, y, content, color)

    text(28, 38, username + "@github", "#c9d1d9", 18, "600")
    text(927, 38, "$ ./profile", muted)
    elements.append('<path d="M28 60H1092" stroke="#30363d"/>')

    section(28, 94, "System", 528)
    for y, title, key in [(126, "OS", "OS"), (152, "Distribution", "Distribution"),
                          (178, "Kernel", "Kernel"), (204, "IDE", "IDE")]:
        row(28, y, title, profile[key])
    section(28, 250, "Languages", 528)
    for y, title, key in [(282, "Programming", "Languages.Programming"),
                          (308, "Computer", "Languages.Computer"), (334, "Spoken", "Languages.Real")]:
        row(28, y, title, profile[key])
    section(28, 380, "Frameworks & Tools", 528)
    row(28, 412, "Frameworks", profile["Frameworks"])
    row(28, 438, "Tools", profile["Tools"])
    section(28, 484, "Hobbies", 528)
    row(28, 516, "Gaming", profile["Hobbies.Gaming"])
    row(28, 542, "Other", profile["Hobbies.Other"])

    section(594, 94, "Contact", 1092)
    for y, title in [(126, "Email.Personal"), (152, "Email.Work"), (178, "Discord")]:
        row(594, y, title, profile[title])
    row(594, 204, "GitHub", "github.com/" + username)
    section(594, 250, "GitHub Stats", 1092)
    for y, title, key in [(282, "Repos.Owned", "repositories"),
                          (308, "Contributed.Recent", "contributed_recent"), (334, "Commits", "commits")]:
        row(594, y, title, f"{stats[key]:,}")
    section(594, 380, "Lines of Code", 1092)
    for y, title, key, color in [(412, "Added", "lines_added", "#7ee787"),
                                 (438, "Removed", "lines_removed", "#ffa198"),
                                 (464, "Net", "lines_net", value)]:
        row(594, y, title, f"{stats[key]:,}", color)
    text(594, 542, "Updated daily · public activity", muted, 14)
    elements.extend(["</g>", "</svg>"])
    return "\n".join(elements) + "\n"


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN or GITHUB_TOKEN is required")
    username = os.environ.get("PROFILE_USERNAME", "Matiasnm14")
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    replace_block(readme, "validation")  # Validate before making API calls.
    stats = collect_stats(GitHub(token), username)
    updated = replace_block(readme, render_block(stats))
    graphic = render_terminal(stats, updated)
    graphic_path = ROOT / "assets/profile-terminal.svg"
    graphic_path.parent.mkdir(parents=True, exist_ok=True)
    graphic_temporary = graphic_path.with_suffix(".svg.tmp")
    temporary = readme_path.with_suffix(".md.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        graphic_temporary.write_text(graphic, encoding="utf-8")
        graphic_temporary.replace(graphic_path)
        temporary.replace(readme_path)
    finally:
        temporary.unlink(missing_ok=True)
        graphic_temporary.unlink(missing_ok=True)
    print("Statistics and terminal profile updated successfully")


if __name__ == "__main__":
    main()

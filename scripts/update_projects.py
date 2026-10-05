#!/usr/bin/env python3
"""Synchronize the profile's project cards with its owner's public repositories."""

import argparse
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
START = "<!-- PROJECTS:START -->"
END = "<!-- PROJECTS:END -->"
PAGE_SIZE = 100


def fetch_repositories(owner, token=None):
    repositories = []
    page = 1
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-profile-project-sync",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        query = urlencode({
            "type": "owner", "sort": "created", "direction": "desc",
            "per_page": PAGE_SIZE, "page": page,
        })
        request = Request(
            f"https://api.github.com/users/{quote(owner, safe='')}/repos?{query}",
            headers=headers,
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    batch = json.load(response)
                break
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f"GitHub API returned HTTP {error.code}.") from None
                time.sleep(2 ** attempt)
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError("Could not read repositories from GitHub.") from None
                time.sleep(2 ** attempt)
        if not isinstance(batch, list) or any(
            not isinstance(repo, dict) or not isinstance(repo.get("name"), str)
            or not isinstance(repo.get("owner"), dict)
            for repo in batch
        ):
            raise ValueError("Unexpected GitHub repository response; README was not updated.")
        repositories.extend(batch)
        if len(batch) < PAGE_SIZE:
            return repositories
        page += 1


def select_repositories(repositories, settings):
    owner = settings["owner"].casefold()
    excluded = {name.casefold() for name in settings.get("excluded_repositories", [])}
    excluded.add(owner)  # The profile repository itself is not a project card.
    eligible = {}
    for repo in repositories:
        name = repo["name"].casefold()
        if (
            repo.get("owner", {}).get("login", "").casefold() != owner
            or repo.get("private", False)
            or repo.get("visibility", "public") != "public"
            or repo.get("fork", False)
            or repo.get("archived", False)
            or repo.get("disabled", False)
            or name in excluded
        ):
            continue
        eligible[name] = repo

    selected = []
    for featured in settings.get("featured", []):
        repo = eligible.pop(featured["repository"].casefold(), None)
        if repo:
            selected.append((repo, featured))
    # Creation date, rather than update date, keeps normal pushes from moving cards.
    remaining = sorted(eligible.values(), key=lambda repo: repo["name"].casefold())
    remaining.sort(key=lambda repo: repo.get("created_at") or "", reverse=True)
    selected.extend((repo, {}) for repo in remaining)
    return selected


def escaped_text(value):
    return html.escape(" ".join(str(value or "").split()), quote=True)


def render_card(repo, featured, owner):
    title = escaped_text(featured.get("title") or repo["name"])
    subtitle = escaped_text(featured.get("subtitle"))
    heading = f"{title}</a>" + (f" · {subtitle}" if subtitle else "")
    description = escaped_text(
        featured.get("description") or repo.get("description")
        or "项目简介待补充，点击仓库了解详情。"
    )
    tags = featured.get("tags") or " · ".join(
        [repo["language"]] if repo.get("language") else ["公开项目"]
    )
    icon = featured.get("icon", "assets/project.svg")
    if not re.fullmatch(r"assets/[A-Za-z0-9_-]+\.svg", icon):
        raise ValueError("Project icons must be SVG files in assets/.")
    url = f"https://github.com/{quote(owner, safe='')}/{quote(repo['name'], safe='')}"
    return "\n".join([
        '    <td width="50%" valign="top">',
        f'      <img src="{icon}" width="36" height="36" alt=""><br>',
        f'      <h3><a href="{url}">{heading}</h3>',
        f"      <p>{description}</p>",
        f"      <p><sub>{escaped_text(tags)}</sub></p>",
        "    </td>",
    ])


def render_projects(selected, owner):
    if not selected:
        return "<p>新的项目正在准备中。</p>"
    rows = ["<table>"]
    for index in range(0, len(selected), 2):
        rows.append("  <tr>")
        for repo, featured in selected[index:index + 2]:
            rows.append(render_card(repo, featured, owner))
        if index + 1 == len(selected):
            rows.append('    <td width="50%" valign="top"></td>')
        rows.append("  </tr>")
    rows.append("</table>")
    return "\n".join(rows)


def replace_project_section(readme, content):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one pair of project markers.")
    start = readme.index(START) + len(START)
    end = readme.index(END)
    if end < start:
        raise ValueError("README project markers are in the wrong order.")
    return readme[:start] + "\n" + content + "\n" + readme[end:]


def render_sync_state(project_count):
    month = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")
    return json.dumps({"verified_month": month, "public_projects": project_count},
                      ensure_ascii=False, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without modifying README.")
    args = parser.parse_args(argv)
    settings = json.loads((ROOT / "profile-projects.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", settings["owner"]):
        raise ValueError("Invalid GitHub account name.")
    repositories = fetch_repositories(settings["owner"], os.environ.get("GITHUB_TOKEN"))
    selected = select_repositories(repositories, settings)
    for _, featured in selected:
        if featured.get("icon") and not (ROOT / featured["icon"]).is_file():
            raise ValueError("A configured project icon does not exist.")
    readme_path = ROOT / "README.md"
    old = readme_path.read_bytes().decode("utf-8")
    new = replace_project_section(old, render_projects(selected, settings["owner"]))
    state_path = ROOT / ".github" / "project-sync-state.json"
    old_state = state_path.read_text(encoding="utf-8") if state_path.exists() else ""
    new_state = render_sync_state(len(selected))
    if old == new and old_state == new_state:
        print(f"Project cards are up to date ({len(selected)} public projects).")
        return 0
    if args.check:
        print(f"Project cards need updating ({len(selected)} public projects).")
        return 1
    if old != new:
        readme_path.write_text(new, encoding="utf-8", newline="")
    if old_state != new_state:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(new_state, encoding="utf-8", newline="")
    print(f"Updated project cards ({len(selected)} public projects).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as error:
        print(f"Project sync failed: {error}", file=sys.stderr)
        sys.exit(1)

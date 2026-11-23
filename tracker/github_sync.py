"""Pull recent commits for projects with a linked GitHub repo.

Run by `flask sync-github` (a systemd timer on the server calls it every few
hours). Needs GITHUB_TOKEN: a fine-grained, read-only token (Contents: read).
Commits are stored once per project (unique on SHA), so re-running is safe.
A failing repo is logged and skipped; the next run retries it.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from .models import Commit, Project, db, utcnow
from .services import REPO_URL

log = logging.getLogger(__name__)

API = "https://api.github.com"
FIRST_SYNC_DAYS = 90  # how far back to look for a newly linked repo
MAX_PAGES = 5  # 100 commits per page


def fetch_json(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "idea-tracker",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)


def sync_project(project, token, fetch=None):
    """Fetch and store new commits for one project. Returns how many were added."""
    fetch = fetch or fetch_json
    owner, repo = REPO_URL.match(project.repo_url).groups()
    newest = db.session.query(db.func.max(Commit.committed_at)).filter_by(project_id=project.id).scalar()
    # Overlap by a day: commits can arrive with older author dates (rebases, late pushes).
    since = (newest - timedelta(days=1)) if newest else utcnow() - timedelta(days=FIRST_SYNC_DAYS)
    known = {sha for (sha,) in db.session.query(Commit.sha).filter_by(project_id=project.id)}
    added = 0
    for page in range(1, MAX_PAGES + 1):
        url = (f"{API}/repos/{owner}/{repo}/commits?per_page=100&page={page}"
               f"&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        batch = fetch(url, token)
        for item in batch:
            if item["sha"] in known:
                continue
            info = item.get("commit", {})
            db.session.add(Commit(
                project_id=project.id,
                sha=item["sha"],
                message=info.get("message", ""),
                author=(info.get("author") or {}).get("name", ""),
                url=item.get("html_url", ""),
                committed_at=_parse_time((info.get("committer") or info.get("author") or {})["date"]),
            ))
            known.add(item["sha"])
            added += 1
        if len(batch) < 100:
            break
    return added


def sync_all(token=None, fetch=None):
    """Sync every live project with a repo. Returns {project title: added count or error}."""
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.warning("GITHUB_TOKEN not set; skipping GitHub sync.")
        return {}
    results = {}
    projects = Project.query.filter(Project.archived_at.is_(None), Project.repo_url != "",
                                    Project.repo_url.isnot(None)).all()
    for project in projects:
        try:
            results[project.title] = sync_project(project, token, fetch)
            db.session.commit()
        except urllib.error.HTTPError as e:
            db.session.rollback()
            hint = " (check the token can see this repo)" if e.code in (403, 404) else ""
            results[project.title] = f"error: HTTP {e.code}{hint}"
            log.error("GitHub sync failed for %s: HTTP %s", project.repo_url, e.code)
        except Exception as e:  # network errors, bad JSON: log and retry next run
            db.session.rollback()
            results[project.title] = f"error: {e.__class__.__name__}"
            log.exception("GitHub sync failed for %s", project.repo_url)
    return results

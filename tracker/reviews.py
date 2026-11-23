"""Weekly / monthly / yearly reviews: stats from the activity history plus
saved reflections. The web Reviews tab and the get_review MCP tool both call
compute(), so there is one source of truth for the numbers.

Periods are in the configured timezone (America/Los_Angeles): a week runs
Monday-Sunday, a month from the 1st, a year from Jan 1.
"""

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required

from . import activity
from .models import PROJECT_TYPES, Project, Review, db, utcnow

KINDS = ("weekly", "monthly", "yearly")
DEFAULT_PROMPTS = {
    "weekly": [
        "What moved forward?",
        "What stalled, and why?",
        "What's the one thing to focus on next week?",
    ],
    "monthly": [
        "What am I proudest of this month?",
        "Anything to archive or drop?",
        "Am I spending time on what I actually care about?",
    ],
    "yearly": [
        "What were the biggest wins?",
        "What patterns do I notice in what I finish vs. abandon?",
        "What do I want to build next year?",
    ],
}
# Counted totals, in display order: key -> label.
TOTALS = {
    "projects_touched": "Projects touched",
    "notes_added": "Notes added",
    "milestones_completed": "Milestones completed",
    "status_changes": "Status changes",
    "commits": "Commits",
    "projects_started": "Projects started",
    "ideas_activated": "Ideas → active",
    "projects_completed": "Projects completed",
    "projects_archived": "Projects archived",
}


# ---------- Periods ----------

def _tz():
    return ZoneInfo(current_app.config["TIMEZONE"])


def local_today():
    return datetime.now(_tz()).date()


def period_bounds(kind, offset=0, today=None):
    """(start, end) local dates of the period `offset` steps from today's; end is exclusive."""
    today = today or local_today()
    if kind == "weekly":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return start, start + timedelta(days=7)
    if kind == "monthly":
        months = today.year * 12 + (today.month - 1) + offset
        start = date(months // 12, months % 12 + 1, 1)
        nxt = months + 1
        return start, date(nxt // 12, nxt % 12 + 1, 1)
    if kind == "yearly":
        return date(today.year + offset, 1, 1), date(today.year + offset + 1, 1, 1)
    raise ValueError(f"period must be one of {', '.join(KINDS)}")


def period_label(kind, start, end):
    last = end - timedelta(days=1)
    if kind == "weekly":
        first = f"{start:%b} {start.day}"
        tail = f"{last.day}" if last.month == start.month else f"{last:%b} {last.day}"
        return f"{first} – {tail}, {last.year}"
    if kind == "monthly":
        return f"{start:%B %Y}"
    return str(start.year)


def _utc(d):
    """Local midnight at the start of date d, as naive UTC (how timestamps are stored)."""
    return datetime.combine(d, time.min, tzinfo=_tz()).astimezone(UTC).replace(tzinfo=None)


def _local_date(ts):
    return ts.replace(tzinfo=UTC).astimezone(_tz()).date()


def prompts(kind):
    return current_app.config.get("REVIEW_PROMPTS", DEFAULT_PROMPTS)[kind]


# ---------- Stats ----------

def _summarise(evs):
    """Counts and lists for one window of events."""
    touched = Counter(e.project_id for e in evs)
    # A milestone counts as completed if its last done/reopened event in the window is "done".
    milestone_state = {}
    for e in evs:
        if e.type in ("milestone_done", "milestone_reopened"):
            milestone_state[e.entity_id] = e
    milestones = [e for e in milestone_state.values() if e.type == "milestone_done"]
    status_changes = [e for e in evs if e.type == "status_changed"]
    activated = {e.project_id for e in status_changes if (e.from_value, e.to_value) == ("idea", "active")}
    completed = {e.project_id for e in status_changes if e.to_value == "done"}
    return {
        "touched": touched,
        "milestones": milestones,
        "status_changes": status_changes,
        "activated": activated,
        "completed": completed,
        "totals": {
            "projects_touched": len(touched),
            "notes_added": sum(e.type == "note_added" for e in evs),
            "milestones_completed": len(milestones),
            "status_changes": len(status_changes),
            "commits": sum(e.type == "commit" for e in evs),
            "projects_started": sum(e.type == "created" for e in evs),
            "ideas_activated": len(activated),
            "projects_completed": len(completed),
            "projects_archived": sum(e.type == "archived" for e in evs),
        },
    }


def _durations(all_events, start, end):
    """Average days idea→active and active→done, for transitions inside [start, end)."""
    entered = {}  # project_id -> (status, since)
    idea_to_active, active_to_done = [], []
    for e in all_events:
        if e.type == "created":
            entered[e.project_id] = (e.to_value, e.ts)
        elif e.type == "status_changed":
            prev_status, since = entered.get(e.project_id, (e.from_value, e.ts))
            if start <= e.ts < end:
                days = (e.ts - since).total_seconds() / 86400
                if (prev_status, e.to_value) == ("idea", "active"):
                    idea_to_active.append(days)
                elif (prev_status, e.to_value) == ("active", "done"):
                    active_to_done.append(days)
            entered[e.project_id] = (e.to_value, e.ts)

    def avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    return avg(idea_to_active), avg(active_to_done), entered


def compute(kind, offset=0, today=None):
    """All stats for one review period, as plain JSON-friendly data."""
    today = today or local_today()
    start, end = period_bounds(kind, offset, today)
    prev_start, prev_end = period_bounds(kind, offset - 1, today)
    projects = {p.id: p for p in Project.query.all()}

    def title(pid):
        return projects[pid].title if pid in projects else f"#{pid}"

    now = _summarise(activity.events(_utc(start), _utc(end)))
    prev = _summarise(activity.events(_utc(prev_start), _utc(prev_end)))

    touched = [{"id": pid, "title": title(pid), "type": projects[pid].type if pid in projects else "",
                "events": n} for pid, n in now["touched"].most_common()]
    by_type = Counter()
    for t in touched:
        by_type[t["type"]] += t["events"]

    live = [p for p in projects.values() if p.archived_at is None]
    stale_info = activity.staleness(live)
    stale = sorted(({"id": p.id, "title": p.title, "days": stale_info[p.id]["days"]}
                    for p in live if stale_info[p.id]["stale"]), key=lambda s: -s["days"])

    commits = Counter(e.project_id for e in activity.events(_utc(start), _utc(end)) if e.type == "commit")
    result = {
        "period": {
            "type": kind, "offset": offset, "label": period_label(kind, start, end),
            "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
            "is_current": start <= today < end,
        },
        "totals": now["totals"],
        "previous_totals": prev["totals"],
        "touched": touched,
        "most_active": touched[0] if touched else None,
        "milestones_completed": [
            {"project": title(e.project_id), "title": e.to_value, "date": _local_date(e.ts).isoformat()}
            for e in sorted(now["milestones"], key=lambda e: e.ts)],
        "status_changes": [
            {"project": title(e.project_id), "from": e.from_value, "to": e.to_value,
             "date": _local_date(e.ts).isoformat()} for e in now["status_changes"]],
        "ideas_activated": sorted(title(pid) for pid in now["activated"]),
        "projects_completed": sorted(title(pid) for pid in now["completed"]),
        "by_type": [{"type": t, "events": by_type[t]} for t in PROJECT_TYPES if by_type[t]],
        "stale": stale,
        "commits_by_project": [{"id": pid, "title": title(pid), "commits": n} for pid, n in commits.most_common()],
    }

    if kind == "yearly":
        result.update(_yearly_extras(start, end, projects, live))
    return result


def _yearly_extras(start, end, projects, live):
    year_events = activity.events(_utc(start), _utc(end))
    per_day = Counter(_local_date(e.ts) for e in year_events)
    per_month = Counter(d.month for d in per_day.elements())
    best_month = max(per_month, key=per_month.get) if per_month else None

    # GitHub-style grid: columns are weeks (Mon-Sun), from the Monday on/before Jan 1.
    first = start - timedelta(days=start.weekday())
    days = [first + timedelta(days=i) for i in range((end - first).days)]
    heatmap = [{"date": d.isoformat(), "events": per_day.get(d, 0), "in_year": start <= d < end}
               for d in days]

    idea_to_active, active_to_done, entered = _durations(activity.events(end=_utc(end)), _utc(start), _utc(end))
    running = [(p, entered.get(p.id, ("active", p.created_at))[1]) for p in live if p.status == "active"]
    longest = max(running, key=lambda r: utcnow() - r[1], default=None)

    return {
        "most_active_month": ({"month": date(start.year, best_month, 1).strftime("%B"),
                               "events": per_month[best_month]} if best_month else None),
        "heatmap": heatmap,
        "heatmap_max": max(per_day.values(), default=0),
        "longest_running_active": ({"title": longest[0].title, "days": (utcnow() - longest[1]).days}
                                   if longest else None),
        "avg_days_idea_to_active": idea_to_active,
        "avg_days_active_to_done": active_to_done,
    }


# ---------- Reflections ----------

def get_reflection(kind, start):
    review = Review.query.filter_by(period_type=kind, period_start=start).first()
    return dict(review.answers) if review else {}


def save_reflection(kind, offset, answers, today=None):
    """Store answers ({prompt: text}) for a period; unknown prompts are rejected.
    Answers for prompts not passed are kept as they were."""
    start, _ = period_bounds(kind, offset, today)
    allowed = prompts(kind)
    unknown = [k for k in answers if k not in allowed]
    if unknown:
        raise ValueError(f"Unknown prompt(s): {unknown}. Use the exact prompt text: {allowed}")
    review = Review.query.filter_by(period_type=kind, period_start=start).first()
    if review is None:
        review = Review(period_type=kind, period_start=start, answers={})
        db.session.add(review)
    merged = {**review.answers, **{k: (v or "").strip() for k, v in answers.items()}}
    review.answers = {k: v for k, v in merged.items() if v}
    db.session.commit()
    return review.answers


def review(kind, offset=0, today=None):
    """Stats plus prompts and saved answers: what the page and get_review show."""
    data = compute(kind, offset, today)
    start = date.fromisoformat(data["period"]["start"])
    data["reflection"] = {"prompts": prompts(kind), "answers": get_reflection(kind, start)}
    return data


# ---------- Web ----------

bp = Blueprint("reviews", __name__, url_prefix="/reviews")


@bp.before_request
@login_required
def require_login():
    pass


def _kind_and_offset(kind):
    if kind not in KINDS:
        abort(404)
    return kind, min(request.args.get("offset", 0, type=int), 0)


@bp.route("/")
def index():
    return redirect(url_for("reviews.show", kind="weekly"))


@bp.route("/<kind>")
def show(kind):
    kind, offset = _kind_and_offset(kind)
    return render_template("reviews.html", data=review(kind, offset), kinds=KINDS, totals=TOTALS)


@bp.route("/<kind>/reflection", methods=["POST"])
def save(kind):
    kind, offset = _kind_and_offset(kind)
    answers = {p: request.form.get(f"answer-{i}", "") for i, p in enumerate(prompts(kind))}
    save_reflection(kind, offset, answers)
    flash("Reflection saved.")
    return redirect(url_for("reviews.show", kind=kind, offset=offset or None))

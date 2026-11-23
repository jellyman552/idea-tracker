"""Demo data for the public demo (DEMO_MODE=1).

`flask seed-demo` fills an empty database with fictional projects and about ten
months of backdated history. Everything goes through services.py, the same path
the web UI and MCP use, so the audit log, stale badges, reviews and the yearly
heatmap all look like real use. Afterwards each audit row is moved back to the
moment it "happened".
"""
from datetime import UTC, datetime, time, timedelta

from . import reviews, services
from .models import AuditLog, Milestone, Project, User, db

DEMO_USER = "demo"

# days: days ago the project was created. ms: (milestone, days_ago_ticked or None).
# notes: (days_ago, text, by_claude). then: extra status changes (days_ago, status).
PROJECTS = [
    dict(title="Portfolio website", type="software", days=46, status="active",
         summary="Personal site in React + Vite with live demos of my side projects.",
         ms=[("Pick a stack and theme", 44), ("Projects section", 30), ("Deploy to GitHub Pages", 21),
             ("Seeded demos for each project", None), ("Custom domain", None)],
         notes=[(45, "Sketched the layout: hero, projects grid, experience, contact.", False),
                (29, "Projects are one JSON file each, so adding one is a single file.", True),
                (21, "Live on GitHub Pages; the Actions workflow rebuilds on every push.", True),
                (3, "Started the demo containers. Each one resets nightly.", True)]),
    dict(title="Home server with Docker", type="software", days=210, status="active",
         summary="A small ARM VM running my apps in Docker behind Caddy with automatic HTTPS.",
         ms=[("Provision the VM", 205), ("Docker + Caddy", 198), ("Nightly backups", 186), ("Uptime alerts", 160)],
         notes=[(206, "VM is up. Opened 80/443 in both the cloud firewall and iptables.", False),
                (197, "Caddy handles certificates; each app joins its Docker network.", False),
                (161, "Backups and alerts done. Calling this finished.", True)]),
    dict(title="Half-marathon training", type="personal", days=150, status="active",
         summary="16-week plan, three runs a week, building to a spring half marathon.",
         ms=[("Run 5K without stopping", 132), ("10K under an hour", 88), ("15K long run", 41),
             ("Race day", None)],
         notes=[(149, "Week 1. Easy pace, mostly walking the hills.", False),
                (88, "10K in 58:40.", False),
                (41, "15K felt fine until the last two. More fuel next time.", False),
                (6, "Taper starts next week.", True)]),
    dict(title="Sourdough baking", type="DIY", days=120, status="active",
         summary="Keep a starter alive and get a consistent open crumb.",
         ms=[("Starter doubles reliably", 109), ("First edible loaf", 96), ("Open crumb", None),
             ("Bake for a party", None)],
         notes=[(118, "Day 1 of the starter: 50 g flour, 50 g water.", False),
                (96, "First loaf! Dense but tasty.", False),
                (12, "Trying a longer cold proof: 16 hours in the fridge.", False)]),
    dict(title="AWS certification", type="career", days=75, status="active",
         summary="Cloud Practitioner first, then Solutions Architect Associate.",
         ms=[("Finish the video course", 52), ("Two practice exams over 80%", None), ("Book the exam", None)],
         notes=[(74, "Signed up for the course. Aiming for 30 minutes a day.", False),
                (52, "Course done. Practice exam #1: 72%.", True),
                (18, "Weak spots: billing and the shared responsibility model.", True)]),
    dict(title="Team onboarding docs", type="work", days=58, status="active",
         summary="Turn the scattered setup notes into one onboarding guide for new hires.",
         ms=[("Collect existing notes", 55), ("Draft the guide", 33), ("Review with two new hires", None)],
         notes=[(55, "Found notes in four places. Merging them.", False),
                (33, "Draft done: setup, access requests, first-week checklist.", True)]),
    dict(title="Raised garden beds", type="DIY", days=300, status="active",
         summary="Two 4x8 cedar beds for tomatoes, peppers and herbs.",
         ms=[("Build the frames", 290), ("Fill with soil", 284), ("First planting", 270)],
         notes=[(291, "Frames built. Cedar was pricier than planned.", False),
                (270, "Tomatoes, peppers, basil and parsley in.", False)]),
    dict(title="Learn conversational Spanish", type="personal", days=260, status="active",
         summary="Twenty minutes a day, plus one conversation class a week.",
         ms=[("30-day streak", 228), ("First conversation class", 201), ("Hold a 10-minute chat", None)],
         notes=[(228, "30-day streak!", False),
                (201, "First class. Understood about half.", False),
                (100, "Pausing until marathon training is over.", True)],
         then=[(100, "paused")]),
    dict(title="Board game night", type="personal", days=185, status="active",
         summary="A monthly game night with friends, rotating hosts.",
         ms=[("Pick a date and host", 182), ("Build a shared game list", None)],
         notes=[(182, "First one is at my place. Six people.", False)],
         then=[(120, "paused")]),
    dict(title="Recipe scaler app", type="software", days=95, status="idea",
         summary="Paste a recipe, pick servings, get scaled amounts with sensible units.",
         ms=[], notes=[(95, "Idea came from doubling a sourdough recipe by hand.", True)]),
    dict(title="Freelance sites for local shops", type="business", days=22, status="idea",
         summary="Simple, fast websites for neighborhood businesses with no web presence.",
         ms=[], notes=[(22, "Three shops on my street have no website at all.", True),
                       (9, "Drafted a one-page offer: site, hosting and a monthly update.", True)]),
    dict(title="Candle shop", type="business", days=240, status="active",
         summary="Hand-poured soy candles sold online.",
         ms=[("Test five scents", 230)],
         notes=[(230, "Margins are thinner than expected after shipping.", False)],
         archive=150),
]

REFLECTION = {
    "What moved forward?": "Portfolio demos are running, and the taper plan is set.",
    "What stalled, and why?": "AWS practice exams. Training took the evenings.",
    "What's the one thing to focus on next week?": "Finish the remaining demos.",
}


def _at(days_ago, hour):
    """A naive-UTC timestamp `days_ago` days back, at `hour` (UTC)."""
    day = datetime.now(UTC).date() - timedelta(days=days_ago)
    return datetime.combine(day, time(hour % 24, (days_ago * 7) % 60))


def seed():
    """Create the demo user and fictional history. Expects an empty database."""
    if Project.query.first():
        raise RuntimeError("Database isn't empty; seed-demo only runs on a fresh one.")

    user = User(username=DEMO_USER)
    user.set_password(DEMO_USER)  # unused: DEMO_MODE logs visitors in automatically
    db.session.add(user)

    steps = []  # (timestamp, action)
    for i, spec in enumerate(PROJECTS):
        steps += _project_steps(spec, hour=15 + i % 6)
    steps.sort(key=lambda s: s[0])

    project_last = {}
    for when, action in steps:
        before = db.session.query(db.func.max(AuditLog.id)).scalar() or 0
        project = action()
        db.session.flush()
        AuditLog.query.filter(AuditLog.id > before).update({"ts": when})
        Milestone.query.filter(Milestone.created_at > when).update({"created_at": when})
        project_last[project.id] = (project, when)

    for project, when in project_last.values():
        project.updated_at = when
    db.session.commit()

    reviews.save_reflection("weekly", -1, REFLECTION)
    return len(PROJECTS)


def _project_steps(spec, hour):
    web, mcp = services.Actor(DEMO_USER, "web"), services.Actor("claude", "mcp")
    ref = {}

    def create():
        p = services.create_project(mcp, {k: spec[k] for k in ("title", "type", "summary")} | {"status": "idea"})
        p.created_at = _at(spec["days"], hour)
        ref["p"] = p
        return p

    steps = [(_at(spec["days"], hour), create)]

    if spec["status"] != "idea":
        def activate():
            services.update_project(web, ref["p"], {"status": "active"})
            for title, _ in spec["ms"]:
                ref.setdefault("ms", []).append(services.add_milestone(mcp, ref["p"], title))
            return ref["p"]
        steps.append((_at(spec["days"] - 1, hour), activate))

    for idx, (title, ticked) in enumerate(spec["ms"]):
        if ticked is not None:
            def tick(idx=idx):
                services.set_milestone_done(web, ref["ms"][idx], True)
                return ref["p"]
            steps.append((_at(ticked, hour + 1), tick))

    for days_ago, text, by_claude in spec["notes"]:
        when = _at(days_ago, hour + 2)

        def note(text=text, by_claude=by_claude, on=when.date()):
            services.add_note(mcp if by_claude else web, ref["p"], text, on=on)
            return ref["p"]
        steps.append((when, note))

    for days_ago, status in spec.get("then", []):
        def change(status=status):
            services.update_project(web, ref["p"], {"status": status})
            return ref["p"]
        steps.append((_at(days_ago, hour + 3), change))

    if "archive" in spec:
        def archive():
            services.archive_project(web, ref["p"])
            ref["p"].archived_at = _at(spec["archive"], hour + 3)
            return ref["p"]
        steps.append((_at(spec["archive"], hour + 3), archive))

    return steps

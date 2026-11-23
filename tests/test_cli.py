from tracker.models import AuditLog, Project


def test_seed_is_idempotent_and_audited(app):
    runner = app.test_cli_runner()
    assert "Added 3 project(s)" in runner.invoke(args=["seed"]).output
    assert "Added 0 project(s)" in runner.invoke(args=["seed"]).output
    assert Project.query.count() == 3
    assert AuditLog.query.filter_by(source="cli", entity="project").count() == 3


def test_seed_demo_builds_backdated_history(app):
    from tracker import demo
    runner = app.test_cli_runner()
    assert f"Seeded {len(demo.PROJECTS)} demo project(s)" in runner.invoke(args=["seed-demo"]).output
    # Refuses to run twice: it only seeds an empty database.
    assert runner.invoke(args=["seed-demo"]).exit_code != 0
    oldest = AuditLog.query.order_by(AuditLog.ts).first().ts
    newest = AuditLog.query.order_by(AuditLog.ts.desc()).first().ts
    assert (newest - oldest).days > 250
    assert Project.query.filter_by(status="done").count() >= 2  # milestones auto-complete projects
    assert Project.query.filter(Project.archived_at.isnot(None)).count() == 1

import os
from getpass import getpass

import click

from . import services
from .models import db, Project, User

STARTER_PROJECTS = [
    ("Idea tracker app", "software", "active",
     "This app. Flask + SQLite, with an MCP server so Claude can post updates."),
    ("Learn a new language", "personal", "idea",
     "Twenty minutes a day, plus one conversation class a week."),
    ("Raised garden beds", "DIY", "idea",
     "Two cedar beds for tomatoes, peppers and herbs."),
]


def register(app):
    @app.cli.command("create-user")
    @click.argument("username")
    def create_user(username):
        """Create (or reset the password for) the login user."""
        password = getpass("Password: ")
        if len(password) < 8:
            raise click.ClickException("Use at least 8 characters.")
        if password != getpass("Confirm password: "):
            raise click.ClickException("Passwords didn't match.")
        user = User.query.filter_by(username=username).first() or User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Saved user '{username}'.")

    @app.cli.command("ensure-user")
    def ensure_user():
        """Create the login user from ADMIN_USERNAME / ADMIN_PASSWORD (used at
        container start). Resets the password if it changed; no-op if unset."""
        username = os.environ.get("ADMIN_USERNAME", "").strip()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not username or not password:
            click.echo("ADMIN_USERNAME/ADMIN_PASSWORD not set; skipping.")
            return
        if len(password) < 8:
            raise click.ClickException("ADMIN_PASSWORD must be at least 8 characters.")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            click.echo(f"User '{username}' is up to date.")
            return
        user = user or User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Saved user '{username}'.")

    @app.cli.command("seed")
    def seed():
        """Load the starter projects (skips any that already exist)."""
        actor = services.Actor(user="cli", source="cli")
        added = 0
        for title, ptype, status, summary in STARTER_PROJECTS:
            if Project.query.filter_by(title=title).first():
                continue
            p = services.create_project(
                actor, {"title": title, "type": ptype, "status": status, "summary": summary})
            services.add_note(actor, p, "Added to the tracker.")
            added += 1
        db.session.commit()
        click.echo(f"Added {added} project(s).")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Fill an EMPTY database with fictional demo projects and history."""
        from . import demo
        try:
            count = demo.seed()
        except RuntimeError as e:
            raise click.ClickException(str(e))
        click.echo(f"Seeded {count} demo project(s).")

    @app.cli.command("sync-github")
    def sync_github():
        """Fetch recent commits for projects linked to a GitHub repo."""
        from .github_sync import sync_all
        results = sync_all()
        if not results:
            click.echo("Nothing to sync (no linked repos, or GITHUB_TOKEN not set).")
        for title, outcome in results.items():
            click.echo(f"{title}: {outcome if isinstance(outcome, str) else f'{outcome} new commit(s)'}")

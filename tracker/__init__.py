import os
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, abort, request, session
from flask_migrate import Migrate

from .models import PROJECT_TYPES, STATUSES, db, User, login_manager


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL",
            "sqlite:///" + os.path.join(app.instance_path, "tracker.db"),
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TIMEZONE=os.environ.get("TIMEZONE", "America/Los_Angeles"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # A project is stale when its last activity is older than this many days.
        # Statuses not listed (done) never go stale.
        STALE_AFTER_DAYS={"active": 14, "paused": 14, "idea": 60},
        # Public demo: visitors are logged in as the demo user automatically.
        DEMO_MODE=os.environ.get("DEMO_MODE") == "1",
    )
    # Behind Caddy in production: trust its X-Forwarded-* headers and send cookies over HTTPS only.
    if os.environ.get("BEHIND_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config.update(SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True)
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    # Schema changes are versioned migrations (migrations/): run `flask db upgrade`.
    # render_as_batch lets Alembic alter SQLite tables (copy-and-swap).
    Migrate(app, db, render_as_batch=True)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # Simple CSRF protection: every POST form must send back the session token.
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, PROJECT_TYPES=PROJECT_TYPES, STATUSES=STATUSES,
                                 DEMO_MODE=app.config["DEMO_MODE"])

    if app.config["DEMO_MODE"]:
        from flask_login import current_user, login_user
        from flask import redirect, url_for

        @app.before_request
        def demo_auto_login():
            if request.endpoint == "static":
                return None
            if not current_user.is_authenticated:
                user = User.query.filter_by(username="demo").first()
                if user:
                    login_user(user)
            if request.endpoint == "auth.login":
                return redirect(url_for("projects.dashboard"))
            return None
    register_filters(app)

    @app.before_request
    def check_csrf():
        if request.method == "POST" and not app.config.get("TESTING_SKIP_CSRF"):
            sent = request.form.get("_csrf")
            if not sent or sent != session.get("_csrf"):
                abort(400, "Missing or invalid form token. Reload the page and try again.")

    @app.route("/sw.js")
    def service_worker():
        from flask import send_from_directory

        resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    from . import auth, projects, reviews, cli
    app.register_blueprint(auth.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(reviews.bp)
    # Reflection prompts per review period; override in config to change them.
    app.config.setdefault("REVIEW_PROMPTS", reviews.DEFAULT_PROMPTS)
    cli.register(app)

    # Tests use a throwaway in-memory database built straight from the models.
    if app.config.get("TESTING"):
        with app.app_context():
            db.create_all()

    return app


def register_filters(app):
    def local(value):
        """Stored timestamps are naive UTC; show them in the configured timezone."""
        return value.replace(tzinfo=UTC).astimezone(ZoneInfo(app.config["TIMEZONE"]))

    def today():
        return datetime.now(ZoneInfo(app.config["TIMEZONE"])).date()

    @app.template_filter("date")
    def date_filter(value):
        if not value:
            return ""
        d = local(value).date() if isinstance(value, datetime) else value
        return f"{d:%b} {d.day}, {d.year}"

    @app.template_filter("datetime")
    def datetime_filter(value):
        if not value:
            return ""
        t = local(value)
        return f"{t:%b} {t.day}, {t.year}, {t.hour % 12 or 12}:{t:%M %p}"

    @app.template_filter("ago")
    def ago_filter(value):
        d = local(value).date() if isinstance(value, datetime) else value
        days = (today() - d).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 30:
            return f"{days}d ago"
        return date_filter(value)

    app.jinja_env.globals["today"] = lambda: today().isoformat()

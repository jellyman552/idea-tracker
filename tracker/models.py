from datetime import UTC, date, datetime

from flask_login import LoginManager, UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
login_manager = LoginManager()

PROJECT_TYPES = ["business", "software", "work", "career", "personal", "DIY"]
STATUSES = ["idea", "active", "paused", "done"]
SOURCES = ["web", "cli", "mcp"]


def utcnow():
    """Naive UTC timestamp (SQLite has no timezone support)."""
    return datetime.now(UTC).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(30), nullable=False, default="personal")
    status = db.Column(db.String(20), nullable=False, default="idea")
    summary = db.Column(db.Text, default="")
    chat_link = db.Column(db.String(500), default="")
    repo_url = db.Column(db.String(300), default="")  # https://github.com/owner/repo
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    # Archiving is final: archived rows are read-only and never deleted.
    archived_at = db.Column(db.DateTime)

    logs = db.relationship(
        "ProgressLog",
        backref="project",
        order_by="ProgressLog.date.desc(), ProgressLog.id.desc()",
    )
    milestones = db.relationship(
        "Milestone", backref="project", order_by="Milestone.position, Milestone.id"
    )
    commits = db.relationship(
        "Commit", backref="project", order_by="Commit.committed_at.desc()", lazy="dynamic"
    )

    @property
    def active_logs(self):
        return [n for n in self.logs if not n.archived_at]

    @property
    def archived_logs(self):
        return [n for n in self.logs if n.archived_at]

    @property
    def active_milestones(self):
        return [m for m in self.milestones if not m.archived_at]

    @property
    def latest_note(self):
        logs = self.active_logs
        return logs[0] if logs else None

    @property
    def progress(self):
        """(done, total, pct) for the progress bar, or None when it should be hidden."""
        if self.status == "idea":
            return None
        ms = self.active_milestones
        total = len(ms)
        if self.status == "done":
            return (total, total, 100)
        if not total:
            return None
        done = sum(1 for m in ms if m.done)
        return (done, total, round(done * 100 / total))


class ProgressLog(db.Model):
    __tablename__ = "progress_log"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    date = db.Column(db.Date, default=date.today, nullable=False)
    note = db.Column(db.Text, nullable=False)
    archived_at = db.Column(db.DateTime)


class Milestone(db.Model):
    __tablename__ = "milestones"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    done = db.Column(db.Boolean, default=False, nullable=False)
    position = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    archived_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    """Append-only record of every create, update and archive."""

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=utcnow, nullable=False)
    user = db.Column(db.String(80), nullable=False)
    source = db.Column(db.String(10), nullable=False)
    action = db.Column(db.String(20), nullable=False)  # create | update | archive
    entity = db.Column(db.String(20), nullable=False)  # project | note | milestone
    entity_id = db.Column(db.Integer, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    # {field: [old, new]}; old is null on create.
    changes = db.Column(db.JSON, default=dict, nullable=False)

    project = db.relationship("Project")


class Review(db.Model):
    """Reflection answers for one review period (week, month or year)."""

    __tablename__ = "reviews"
    __table_args__ = (db.UniqueConstraint("period_type", "period_start"),)

    id = db.Column(db.Integer, primary_key=True)
    period_type = db.Column(db.String(10), nullable=False)  # weekly | monthly | yearly
    period_start = db.Column(db.Date, nullable=False)  # Pacific-time start of the period
    answers = db.Column(db.JSON, default=dict, nullable=False)  # {prompt: answer}
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Commit(db.Model):
    """A GitHub commit on a project's linked repo, pulled in by `flask sync-github`.
    Commits count as activity (stale detection, reviews)."""

    __tablename__ = "commits"
    __table_args__ = (db.UniqueConstraint("project_id", "sha"),)

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    sha = db.Column(db.String(40), nullable=False)
    message = db.Column(db.Text, default="")
    author = db.Column(db.String(200), default="")
    url = db.Column(db.String(500), default="")
    committed_at = db.Column(db.DateTime, nullable=False)  # naive UTC
    fetched_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @property
    def headline(self):
        return (self.message or "").split("\n", 1)[0]

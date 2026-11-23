from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade

from tracker import create_app
from tracker.models import db


def test_migrations_build_exactly_the_models(tmp_path):
    """Running every migration on an empty DB must give the schema models.py describes,
    i.e. nobody changed a model without adding a migration."""
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'm.db'}"})
    with app.app_context():
        upgrade()
        with db.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []

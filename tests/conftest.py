import re

import pytest

from tracker import create_app
from tracker.models import User, db
from tracker.services import Actor

PASSWORD = "correct-horse"


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "SECRET_KEY": "test",
    })
    with app.app_context():
        user = User(username="me")
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


class Client:
    """Test client wrapper that sends the CSRF token with every POST."""

    def __init__(self, client):
        self.raw = client

    def token(self):
        html = self.raw.get("/login").get_data(as_text=True)
        return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)

    def get(self, *args, **kwargs):
        return self.raw.get(*args, **kwargs)

    def post(self, url, data=None, csrf=True, **kwargs):
        data = dict(data or {})
        if csrf:
            data["_csrf"] = self.token()
        return self.raw.post(url, data=data, **kwargs)

    def login(self, username="me", password=PASSWORD):
        return self.post("/login", {"username": username, "password": password})


@pytest.fixture
def client(app):
    return Client(app.test_client())


@pytest.fixture
def auth(client):
    client.login()
    return client


@pytest.fixture
def actor():
    return Actor(user="me", source="web")


@pytest.fixture
def make_project(app, actor):
    from tracker import services

    def make(**fields):
        data = {"title": "Test project", "type": "software", "status": "active", **fields}
        project = services.create_project(actor, data)
        db.session.commit()
        return project

    return make

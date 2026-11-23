from tracker.models import User, db


def test_pages_require_login(client):
    for url in ["/", "/archive", "/audit", "/projects/1"]:
        r = client.get(url)
        assert r.status_code == 302
        assert "/login" in r.location


def test_login_wrong_password(client):
    r = client.login(password="nope")
    assert r.status_code == 200
    assert b"Wrong username or password" in r.data


def test_login_and_logout(client):
    r = client.login()
    assert r.status_code == 302 and r.location == "/"
    assert client.get("/").status_code == 200
    r = client.post("/logout")
    assert r.status_code == 302
    assert client.get("/").status_code == 302


def test_logout_requires_post(auth):
    assert auth.get("/logout").status_code == 405


def test_login_redirects_to_next_only_on_same_site(client):
    r = client.post("/login?next=/archive", {"username": "me", "password": "correct-horse"})
    assert r.location == "/archive"
    client.post("/logout")
    for bad in ["//evil.example", "https://evil.example"]:
        r = client.post(f"/login?next={bad}", {"username": "me", "password": "correct-horse"})
        assert r.location == "/"
        client.post("/logout")


def test_post_without_csrf_token_is_rejected(auth):
    assert auth.post("/logout", csrf=False).status_code == 400
    r = auth.raw.post("/logout", data={"_csrf": "wrong"})
    assert r.status_code == 400


def test_create_user_command(app, monkeypatch):
    answers = iter(["longenough", "longenough"])
    monkeypatch.setattr("tracker.cli.getpass", lambda prompt: next(answers))
    r = app.test_cli_runner().invoke(args=["create-user", "newbie"])
    assert "Saved user 'newbie'" in r.output
    assert db.session.query(User).filter_by(username="newbie").one().check_password("longenough")


def test_create_user_rejects_short_password(app, monkeypatch):
    monkeypatch.setattr("tracker.cli.getpass", lambda prompt: "short")
    r = app.test_cli_runner().invoke(args=["create-user", "x"])
    assert r.exit_code != 0
    assert "at least 8" in r.output

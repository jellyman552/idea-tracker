from tracker import mcp_server, services
from tracker.models import db


def test_claude_label_on_mcp_notes_only(app, auth, make_project, actor, monkeypatch):
    monkeypatch.setattr(mcp_server, "app", app)
    p = make_project()
    services.add_note(actor, p, "Typed on the web")
    db.session.commit()
    mcp_server.add_note(p.id, "Decided to launch after the worksheet.")
    html = auth.get(f"/projects/{p.id}").get_data(as_text=True)
    assert html.count('class="badge b-claude"') == 1
    claude_note = html.index("Decided to launch")
    assert html.rfind("b-claude", 0, claude_note) > html.rfind("Typed on the web", 0, claude_note)


def test_add_note_tool_describes_auto_notes():
    # The docstring is the tool description Claude sees.
    assert "own initiative" in mcp_server.add_note.__doc__
    assert "small talk" in mcp_server.add_note.__doc__
    assert "Auto-notes" in mcp_server.INSTRUCTIONS

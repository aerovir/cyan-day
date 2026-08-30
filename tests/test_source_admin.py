import json

from app.source_admin import run

URL = "https://calend.ru/day/{date}/"


def test_cli_lifecycle(tmp_path, capsys):
    db = str(tmp_path / "sources.sqlite3")
    assert run(["--db", db, "add", "--name", "Calendru", "--type", "calendru_day", "--url", URL]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["enabled"] is False
    assert run(["--db", db, "enable", "Calendru"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    assert run(["--db", db, "list", "--enabled"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert run(["--db", db, "remove", "Calendru"]) == 2
    capsys.readouterr()
    assert run(["--db", db, "remove", "Calendru", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] is True


def test_cli_reports_invalid_command_data(tmp_path, capsys):
    result = run(["--db", str(tmp_path / "db.sqlite"), "add", "--name", "bad", "--type", "calendru_day", "--url", "http://example.com/{date}/"])
    captured = capsys.readouterr()
    assert result == 2
    assert "source:" in captured.err

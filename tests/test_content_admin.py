"""Тесты CLI управления контентом."""

import json

from app.content_admin import run


def make_card(card_id="card-1"):
    return {
        "card_id": card_id,
        "calendar_day": "00-00",
        "title": f"Карточка {card_id}",
        "summary": "Описание.",
        "status": "unverified",
        "tags": ["topic.history"],
        "claims": [{"claim_id": f"{card_id}-c1", "text": "Утверждение.", "provenance_id": f"{card_id}-src-1"}],
        "provenance": [{"provenance_id": f"{card_id}-src-1", "source_type": "popular", "title": "Сайт"}],
    }


def _write_cards(tmp_path, cards):
    for card in cards:
        (tmp_path / f"{card['card_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path


def test_import_directory_imports_all_json_files(tmp_path, capsys):
    _write_cards(tmp_path, [make_card("a-1"), make_card("b-2")])

    exit_code = run(["--db", ":memory:", "import", "--file", str(tmp_path), "--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "a-1" in out and "b-2" in out


def test_import_directory_without_json_fails(tmp_path, capsys):
    exit_code = run(["--db", ":memory:", "import", "--file", str(tmp_path), "--dry-run"])

    assert exit_code == 2
    assert "no JSON files" in capsys.readouterr().out

from pathlib import Path

import pytest

from app import image_admin


def test_run_generates_without_vk_or_mistral(monkeypatch, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "download.jpg"
    source.write_bytes(b"jpeg")
    monkeypatch.setattr(image_admin, "download_image", lambda *args, **kwargs: str(source))
    assert image_admin.run(["generate", "--prompt", "тест", "--output-root", str(output), "--output", "image.jpg"]) == 0
    assert (output / "image.jpg").read_bytes() == b"jpeg"


def test_run_rejects_empty_prompt(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    assert image_admin.run(["generate", "--prompt", " ", "--output-root", str(output), "--output", "image.jpg"]) == 1
    assert "prompt" in capsys.readouterr().err

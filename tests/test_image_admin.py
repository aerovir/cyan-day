from pathlib import Path

import pytest

from app import image_admin


def test_run_generates_without_vk_or_Gemini(monkeypatch, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "download.jpg"
    source.write_bytes(b"jpeg")
    monkeypatch.setattr(image_admin, "download_image", lambda *args, **kwargs: str(source))
    assert image_admin.run(["generate", "--prompt", "тест", "--output-root", str(output), "--output", "image.jpg"]) == 0
    assert (output / "image.jpg").read_bytes() == b"jpeg"


def test_run_generates_with_cloudflare_from_environment(monkeypatch, mocker, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "download.png"
    source.write_bytes(b"\\x89PNG\\r\\n\\x1a\\nimage")
    expected = source.read_bytes()
    provider = mocker.Mock()
    provider.generate.return_value = str(source)
    cloudflare = mocker.patch("app.image_admin.CloudflareImageProvider", return_value=provider)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")

    assert image_admin.run([
        "generate", "--provider", "cloudflare", "--prompt", "test",
        "--output-root", str(output), "--output", "cloudflare.png",
    ]) == 0

    cloudflare.assert_called_once()
    assert cloudflare.call_args.kwargs["account_id"] == "0123456789abcdef0123456789abcdef"
    assert cloudflare.call_args.kwargs["api_token"] == "secret"
    assert (output / "cloudflare.png").read_bytes() == expected
    assert not source.exists()


def test_run_rejects_empty_prompt(tmp_path, capsys):
    output = tmp_path / "output"
    output.mkdir()
    assert image_admin.run(["generate", "--prompt", " ", "--output-root", str(output), "--output", "image.jpg"]) == 1
    assert "prompt" in capsys.readouterr().err

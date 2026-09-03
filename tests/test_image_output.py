from pathlib import Path

import pytest

from app.image_output import ImageOutputError, save_image


def test_save_image_atomically(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")
    output = tmp_path / "output"
    output.mkdir()
    result = save_image(source, output, "result.jpg")
    assert result.read_bytes() == b"image"


def test_save_image_rejects_escape_and_overwrite(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(ImageOutputError):
        save_image(source, output, "../escape.jpg")
    save_image(source, output, "result.jpg")
    with pytest.raises(ImageOutputError):
        save_image(source, output, "result.jpg")
    assert save_image(source, output, "result.jpg", overwrite=True).exists()


def test_save_image_requires_real_output_root(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")
    with pytest.raises(ImageOutputError):
        save_image(source, tmp_path / "missing", "result.jpg")

"""Safe materialization of downloaded images into an output directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class ImageOutputError(ValueError):
    """Output path or atomic write operation is unsafe or invalid."""


def save_image(source_path: str | Path, output_root: str | Path, output_name: str, *, overwrite: bool = False) -> Path:
    """Atomically copy an image into an existing, trusted output root."""
    root = Path(output_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ImageOutputError("output root must be an existing real directory")
    target = root / output_name
    if Path(output_name).is_absolute() or "\x00" in output_name:
        raise ImageOutputError("output name must be relative")
    try:
        target = target.resolve(strict=False)
        target.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ImageOutputError("output path escapes output root") from exc
    if target.exists() and (target.is_symlink() or not overwrite):
        raise ImageOutputError("output file exists; use --force to overwrite")
    if target.parent != root.resolve() or target.parent.is_symlink():
        raise ImageOutputError("nested or symlink output paths are not allowed")
    source = Path(source_path)
    if not source.is_file() or source.is_symlink():
        raise ImageOutputError("downloaded image file is missing")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(root))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            while True:
                chunk = input_file.read(64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary_path, target)
        else:
            os.link(temporary_path, target)
            temporary_path.unlink()
        return target
    except FileExistsError as exc:
        raise ImageOutputError("output file appeared during save; refusing overwrite") from exc
    except OSError as exc:
        raise ImageOutputError(f"cannot save image: {exc}") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass

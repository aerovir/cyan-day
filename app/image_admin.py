"""Manual Pollinations image generation command for Docker/operations."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .image_output import ImageOutputError, save_image
from .image_provider import DEFAULT_TIMEOUT, MAX_PROMPT_LENGTH, POLLINATIONS_BASE_URL, PollinationsImageProvider
from .main import download_image

DEFAULT_OUTPUT_ROOT = "/app/output"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an image with Pollinations without publishing to VK")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="generate one image")
    prompt = generate.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    generate.add_argument("--output", required=True, help="output filename inside output root")
    generate.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    generate.add_argument("--model", default="flux")
    generate.add_argument("--base-url", default=POLLINATIONS_BASE_URL)
    generate.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    generate.add_argument("--force", action="store_true")
    return parser


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    path = Path(args.prompt_file)
    if not path.is_file():
        raise ValueError("prompt file must be an existing file")
    return path.read_text(encoding="utf-8")


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "generate":
        return 2
    temporary_path: str | None = None
    try:
        prompt = _read_prompt(args).strip()
        if not prompt or len(prompt) > MAX_PROMPT_LENGTH:
            raise ValueError(f"prompt must contain 1..{MAX_PROMPT_LENGTH} characters")
        provider = PollinationsImageProvider(model=args.model, timeout=args.timeout, base_url=args.base_url)
        temporary_path = provider.generate(prompt, downloader=download_image)
        if temporary_path is None:
            raise ValueError("Pollinations did not return a valid image")
        saved = save_image(temporary_path, args.output_root, args.output, overwrite=args.force)
        print(saved)
        return 0
    except (OSError, ValueError, ImageOutputError) as exc:
        print(f"image generation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(run())

"""Manual Pollinations image generation command for Docker/operations."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .image_output import ImageOutputError, save_image
from .image_provider import (
    CLOUDFLARE_DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    MAX_PROMPT_LENGTH,
    POLLINATIONS_BASE_URL,
    CloudflareImageProvider,
    PollinationsImageProvider,
)
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
    generate.add_argument("--provider", choices=("pollinations", "cloudflare"), default="pollinations")
    generate.add_argument("--model")
    generate.add_argument("--base-url")
    generate.add_argument("--timeout", type=int)
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
        if args.provider == "cloudflare":
            provider = CloudflareImageProvider(
                account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
                api_token=os.getenv("CLOUDFLARE_API_TOKEN", ""),
                model=args.model or os.getenv("CLOUDFLARE_IMAGE_MODEL", CLOUDFLARE_DEFAULT_MODEL),
                timeout=args.timeout if args.timeout is not None else int(os.getenv("CLOUDFLARE_IMAGE_TIMEOUT_SECONDS") or "60"),
            )
            temporary_path = provider.generate(prompt)
        else:
            provider = PollinationsImageProvider(
                model=args.model or os.getenv("IMAGE_MODEL", "flux"),
                timeout=args.timeout if args.timeout is not None else int(os.getenv("IMAGE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT))),
                base_url=args.base_url or os.getenv("IMAGE_BASE_URL", POLLINATIONS_BASE_URL),
            )
            temporary_path = provider.generate(prompt, downloader=download_image)
        if temporary_path is None:
            raise ValueError("Pollinations did not return a valid image")
        output_name = args.output
        if args.provider == "cloudflare":
            actual_suffix = Path(temporary_path).suffix.lower()
            requested_suffix = Path(output_name).suffix.lower()
            if requested_suffix and requested_suffix != actual_suffix:
                raise ImageOutputError(f"output extension must match Cloudflare image format ({actual_suffix})")
            if not requested_suffix:
                output_name += actual_suffix
        saved = save_image(temporary_path, args.output_root, output_name, overwrite=args.force)
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

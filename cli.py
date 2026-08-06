"""
cli.py — Command-line interface for the Multilingual AI Media Summarizer.

Usage examples:
    # Summarise a video in English (default)
    python cli.py video.mp4

    # Summarise an audio file
    python cli.py podcast.mp3 --language French

    # Specify language and save to file
    python cli.py lecture.mp4 --language French --output summary.md

    # With custom instructions
    python cli.py interview.mp4 --language Spanish --instructions "Focus on the Q&A"

    # Use a specific model
    python cli.py tutorial.mp4 --model gemini-2.5-flash --language German
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.syntax import Syntax
from rich.text import Text

from config import SUPPORTED_LANGUAGES, config
from file_handler import (
    FileTooLargeError,
    InvalidFileTypeError,
    delete_local_file,
    human_readable_size,
    validate_media_file,
)
from gemini_client import (
    APIKeyError,
    GeminiVideoClient,
    SummaryGenerationError,
    VideoProcessingError,
)

console = Console()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-summarizer",
        description="🎨 Multilingual AI Media Summarizer — powered by Google Gemini",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported languages:
  """ + ", ".join(SUPPORTED_LANGUAGES),
    )
    parser.add_argument(
        "media",
        type=Path,
        help="Path to the input media file (video or audio).",
    )
    parser.add_argument(
        "-l", "--language",
        default=config.default_language,
        choices=SUPPORTED_LANGUAGES,
        help=f"Target language for the summary (default: {config.default_language}).",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Save the summary to this file (Markdown). "
             "If omitted, prints to stdout.",
    )
    parser.add_argument(
        "-m", "--model",
        default=config.gemini_model,
        help=f"Gemini model name (default: {config.gemini_model}).",
    )
    parser.add_argument(
        "--instructions",
        default="",
        help='Additional instructions for the AI (e.g., "Focus on the Q&A section").',
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw Markdown instead of the rendered version.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Override the GEMINI_API_KEY from .env for this run.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the CLI. Returns 0 on success, 1 on failure."""
    parser = build_parser()
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Apply CLI overrides to config
    if args.api_key:
        config.gemini_api_key = args.api_key
    if args.model:
        config.gemini_model = args.model

    # ── Header banner ────────────────────────────────────────────────────
    console.print(
        Panel.fit(
            Text.from_markup(
                "[bold #818cf8]🎨 Multilingual AI Media Summarizer[/bold #818cf8]\n"
                "[dim]Powered by Google Gemini[/dim]"
            ),
            border_style="#4f46e5",
        )
    )

    # ── Input validation ─────────────────────────────────────────────────
    video_path: Path = args.media.resolve()

    if not video_path.exists():
        console.print(f"[red]❌ File not found:[/red] {video_path}")
        return 1

    if not video_path.is_file():
        console.print(f"[red]❌ Path is not a file:[/red] {video_path}")
        return 1

    file_size = video_path.stat().st_size
    try:
        validate_media_file(video_path.name, file_size)
    except (InvalidFileTypeError, FileTooLargeError) as exc:
        console.print(f"[red]❌ Validation failed:[/red] {exc}")
        return 1

    console.print(
        f"\n[bold]📁 File:[/bold] [cyan]{video_path.name}[/cyan] "
        f"([dim]{human_readable_size(file_size)}[/dim])"
    )
    console.print(f"[bold]🌐 Language:[/bold] [cyan]{args.language}[/cyan]")
    console.print(f"[bold]🤖 Model:[/bold] [cyan]{config.gemini_model}[/cyan]")
    if args.instructions:
        console.print(f"[bold]✏️ Instructions:[/bold] [dim]{args.instructions}[/dim]")
    console.print()

    # ── Progress display ─────────────────────────────────────────────────
    log_lines: list[str] = []

    def on_progress(message: str) -> None:
        log_lines.append(message)
        # Colour based on prefix
        if message.startswith("✅") or message.startswith("🗑") or message.startswith("🎉"):
            console.print(f"  [green]{message}[/green]")
        elif message.startswith("⚠️"):
            console.print(f"  [yellow]{message}[/yellow]")
        elif message.startswith("❌"):
            console.print(f"  [red]{message}[/red]")
        else:
            console.print(f"  [blue]{message}[/blue]")

    # ── Run pipeline ─────────────────────────────────────────────────────
    start = time.time()
    result = None

    try:
        gemini = GeminiVideoClient()

        with open(video_path, "rb") as fh:
            stream_result = gemini.summarise_stream(
                file_obj=fh,
                file_name=video_path.name,
                target_language=args.language,
                extra_instructions=args.instructions,
            )
            
            console.print("\n[bold]🤖 Generating Summary...[/bold]\n")
            
            full_markdown = ""
            # Stream the chunks live if not outputting to a file and not raw
            if not args.output and not args.raw:
                with console.status("[bold green]Streaming output...", spinner="dots"):
                    for chunk in stream_result.stream:
                        console.print(chunk, end="")
                        full_markdown += chunk
                console.print() # newline after stream
            else:
                with console.status("[bold green]Generating summary...", spinner="dots"):
                    for chunk in stream_result.stream:
                        full_markdown += chunk
                        
            # Store it in a dummy result object to keep the output logic intact
            class DummyResult:
                summary_markdown = full_markdown
            result = DummyResult()

    except APIKeyError as exc:
        console.print(f"\n[red]❌ API Key Error:[/red] {exc}")
        console.print(
            "[dim]Hint: Set GEMINI_API_KEY in your .env file or pass --api-key.[/dim]"
        )
        return 1
    except VideoProcessingError as exc:
        console.print(f"\n[red]❌ Video Processing Error:[/red] {exc}")
        return 1
    except SummaryGenerationError as exc:
        console.print(f"\n[red]❌ Summary Generation Error:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️  Interrupted by user.[/yellow]")
        return 1
    except Exception as exc:
        console.print(f"\n[red]❌ Unexpected error:[/red] {exc}")
        if args.verbose:
            console.print_exception()
        return 1

    elapsed = time.time() - start
    console.print(f"\n[bold green]✅ Done in {elapsed:.1f}s[/bold green]\n")

    # ── Output ───────────────────────────────────────────────────────────
    if args.output:
        output_path: Path = args.output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.summary_markdown, encoding="utf-8")
        console.print(
            Panel.fit(
                f"[bold green]Summary saved to:[/bold green] [cyan]{output_path}[/cyan]",
                border_style="green",
            )
        )
    else:
        console.print(Panel("[bold #818cf8]📄 Summary Output[/bold #818cf8]", expand=False))
        if args.raw:
            console.print(
                Syntax(result.summary_markdown, "markdown", theme="monokai")
            )
        else:
            console.print(Markdown(result.summary_markdown))

    return 0


if __name__ == "__main__":
    sys.exit(main())

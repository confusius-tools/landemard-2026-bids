"""CLI entry point for uploading the BIDS dataset to OSF."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from . import upload

console = Console()

DEFAULT_BIDS_DIR = Path.home() / "work" / "DATA" / "landemard_2026_dataset"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="landemard-upload",
        description="Upload the Landemard 2026 BIDS dataset to OSF.",
    )
    p.add_argument(
        "--bids-dir",
        type=Path,
        default=DEFAULT_BIDS_DIR,
        help=f"Local BIDS root (default: {DEFAULT_BIDS_DIR})",
    )
    p.add_argument(
        "--project",
        default=None,
        help="OSF project ID (or set OSF_PROJECT env var)",
    )
    p.add_argument(
        "--token",
        default=None,
        help="OSF personal access token (or set OSF_TOKEN env var)",
    )
    p.add_argument(
        "--index-only",
        action="store_true",
        help="Skip upload; regenerate the index from the remote file listing",
    )
    p.add_argument(
        "--rebuild-index",
        action="store_true",
        help="After upload, rebuild the index from a full remote scan "
        "instead of using the incremental index",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    token = args.token or os.environ.get("OSF_TOKEN")
    if not token:
        console.print("[red]Error:[/] Provide --token or set OSF_TOKEN.", style="bold")
        sys.exit(1)

    project_id = args.project or os.environ.get("OSF_PROJECT")
    if not project_id:
        console.print(
            "[red]Error:[/] Provide --project or set OSF_PROJECT.", style="bold"
        )
        sys.exit(1)

    console.rule("[bold blue]OSF Upload Workflow")
    console.print(f"[bold]Project[/]: [cyan]{project_id}[/]")

    index = None

    if args.index_only:
        console.print("[bold]Mode[/]: [yellow]Index only[/]")
    else:
        bids_dir = args.bids_dir.expanduser().resolve()
        if not bids_dir.is_dir():
            console.print(
                f"[red]Error:[/] {bids_dir} is not a directory.", style="bold"
            )
            sys.exit(1)
        console.print(f"[bold]BIDS directory[/]: [cyan]{bids_dir}[/]")
        console.rule("[bold blue]Step 1/3: Upload Dataset Files")
        index = upload.upload_dataset(
            bids_dir,
            token,
            project_id,
        )

    console.rule("[bold blue]Step 2/3: Build Dataset Index")
    if args.rebuild_index or args.index_only or index is None:
        console.print("[cyan]Generating index from OSF storage...[/]")
        index = upload.generate_index_with_retry(token, project_id)
    else:
        console.print("[cyan]Using incrementally updated index from upload run...[/]")
    console.print(f"[green]Index contains {len(index)} files.[/]")

    console.rule("[bold blue]Step 3/3: Upload Dataset Index")
    console.print("[cyan]Uploading dataset_index.json to OSF...[/]")
    upload.upload_index(index, token, project_id)
    console.rule("[bold green]Upload Finished")

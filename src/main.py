"""CLI entry point: raw footage + creative brief → rendered ad.

This module exposes the single user-facing entry point for the agentic
video editor. It wires together the preprocess step
(:func:`src.pipeline.preprocess.preprocess_footage`) and the pipeline
runner (:func:`src.pipeline.runner.run_pipeline`) behind a Click CLI so
the happy path is one command::

    python -m src.main edit \\
        --footage-dir ~/Downloads/footage/A-Roll \\
        --brief '{"product": "My Product", "audience": "Women 25-45", \\
                  "tone": "authentic, confident", "duration_seconds": 30}' \\
        --style styles/dtc-testimonial.yaml

The CLI itself owns no FFmpeg, Gemini, or agent logic — it only:

* Validates and normalizes inputs (footage directory, brief JSON or
  JSON file, style override, output directory).
* Calls :func:`preprocess_footage` to build (or reuse) a
  :class:`~src.models.schemas.FootageIndex` on disk.
* Calls :func:`run_pipeline` to drive the Director → Editor → Reviewer
  loop defined by the selected YAML manifest.
* Prints a human-readable summary of the run (output path, review
  dimensions, retries, warnings, total duration).

All hard errors (missing files, bad JSON, validation failures, FFmpeg
crashes, API errors) are raised as :class:`click.ClickException`, which
Click maps to exit code 1. Unexpected exceptions propagate so Click can
render a traceback — those are real bugs the caller should see.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click
import yaml
from pydantic import ValidationError

from src.models.schemas import CreativeBrief
from src.pipeline.preprocess import preprocess_footage
from src.pipeline.runner import PipelineResult, run_pipeline

DEFAULT_PIPELINE = "pipelines/ugc-ad.yaml"
DEFAULT_OUTPUT_DIR = "output"
FOOTAGE_INDEX_FILENAME = "footage_index.json"


def _parse_brief(raw: str) -> CreativeBrief:
    """Parse a ``--brief`` value as either inline JSON or a JSON file path.

    The heuristic is: try ``json.loads`` on the raw string first. If it
    parses to a mapping, treat that as the inline brief. Otherwise, if
    the raw string resolves to an existing file on disk, read the file
    and parse its contents. Anything else is a user error.

    Args:
        raw: The verbatim ``--brief`` option value from the command line.

    Returns:
        A validated :class:`CreativeBrief`.

    Raises:
        click.ClickException: If the string is neither valid JSON nor an
            existing file, or if the resulting payload fails
            :class:`CreativeBrief` validation.
    """
    payload: object | None = None
    inline_error: Exception | None = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        inline_error = exc

    if not isinstance(payload, dict):
        candidate = Path(raw).expanduser()
        if candidate.exists() and candidate.is_file():
            try:
                file_text = candidate.read_text(encoding="utf-8")
            except OSError as exc:
                raise click.ClickException(
                    f"--brief file {candidate} could not be read: {exc}"
                ) from exc
            try:
                payload = json.loads(file_text)
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"--brief file {candidate} is not valid JSON: {exc}"
                ) from exc
        else:
            detail = (
                f" (JSON parse error: {inline_error})"
                if inline_error is not None
                else ""
            )
            raise click.ClickException(
                f"--brief {raw!r} is neither inline JSON nor an existing "
                f"file path{detail}"
            )

    if not isinstance(payload, dict):
        raise click.ClickException(
            f"--brief must resolve to a JSON object, got "
            f"{type(payload).__name__}"
        )

    try:
        return CreativeBrief.model_validate(payload)
    except ValidationError as exc:
        raise click.ClickException(
            f"--brief failed CreativeBrief validation: {exc}"
        ) from exc


def _apply_style_override(brief: CreativeBrief, style_path: Path) -> CreativeBrief:
    """Return a copy of ``brief`` with ``style_ref`` set to an absolute path.

    Args:
        brief: A validated brief — never mutated in place.
        style_path: Path to the style YAML from the ``--style`` option.

    Returns:
        A new :class:`CreativeBrief` with ``style_ref`` pointing at the
        resolved absolute path of the style file.

    Raises:
        click.ClickException: If ``style_path`` does not exist on disk.
    """
    resolved = style_path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise click.ClickException(
            f"--style file does not exist: {style_path}"
        )
    return brief.model_copy(update={"style_ref": str(resolved)})


def _print_summary(result: PipelineResult, duration_seconds: float) -> None:
    """Render a human-readable summary of a completed pipeline run."""
    click.echo("")
    click.echo("=" * 60)
    click.echo("Pipeline run summary")
    click.echo("=" * 60)
    click.echo(
        f"output video       : {result.final_video_path or 'none'}"
    )
    review = result.review
    if review is None:
        click.echo("review             : none (pipeline aborted before reviewer)")
    else:
        click.echo("review dimensions  :")
        click.echo(f"  adherence        : {review.adherence:.2f}")
        click.echo(f"  pacing           : {review.pacing:.2f}")
        click.echo(f"  visual_quality   : {review.visual_quality:.2f}")
        click.echo(f"  watchability     : {review.watchability:.2f}")
        click.echo(f"  overall          : {review.overall:.2f}")
    click.echo(f"retries_used       : {result.retries_used}")
    click.echo(f"total duration     : {duration_seconds:.2f}s")
    if result.warnings:
        click.echo("")
        for warning in result.warnings:
            click.echo(f"[pipeline] WARNING: {warning}")
    click.echo("=" * 60)


@click.group()
def cli() -> None:
    """Agentic video editor — raw footage + creative brief → rendered ad."""


@cli.command("edit")
@click.option(
    "--footage-dir",
    "footage_dir",
    required=True,
    type=click.Path(
        exists=False,
        file_okay=False,
        dir_okay=True,
        path_type=Path,
    ),
    help="Directory containing raw footage clips to preprocess.",
)
@click.option(
    "--brief",
    "brief_arg",
    required=True,
    type=str,
    help=(
        "Creative brief as inline JSON OR a path to a .json file. "
        "Must validate against CreativeBrief."
    ),
)
@click.option(
    "--pipeline",
    "pipeline_path",
    type=click.Path(
        exists=False,
        file_okay=True,
        dir_okay=False,
        path_type=Path,
    ),
    default=DEFAULT_PIPELINE,
    show_default=True,
    help="YAML pipeline manifest to run.",
)
@click.option(
    "--style",
    "style_path",
    type=click.Path(
        exists=False,
        file_okay=True,
        dir_okay=False,
        path_type=Path,
    ),
    default=None,
    help=(
        "Optional style YAML. When set, resolved to an absolute path and "
        "assigned to brief.style_ref before the Director runs."
    ),
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(
        exists=False,
        file_okay=False,
        dir_okay=True,
        path_type=Path,
    ),
    default=DEFAULT_OUTPUT_DIR,
    show_default=True,
    help=(
        "Directory for the cached footage_index.json. Note: rendered "
        "artifacts currently go to the editor agent's default output/ "
        "directory; threading this override through run_pipeline is "
        "deferred to a follow-up story."
    ),
)
@click.option(
    "--skip-preprocess",
    is_flag=True,
    default=False,
    help=(
        "Skip preprocessing when {output-dir}/footage_index.json already "
        "exists. Falls through to preprocess if the index is missing."
    ),
)
@click.option(
    "--no-approval",
    is_flag=True,
    default=False,
    help="Auto-approve human_approval gates (headless/non-interactive runs).",
)
def edit(
    footage_dir: Path,
    brief_arg: str,
    pipeline_path: Path,
    style_path: Path | None,
    output_dir: Path,
    skip_preprocess: bool,
    no_approval: bool,
) -> None:
    """Run the full pipeline: preprocess → director → editor → reviewer."""
    started_at = time.monotonic()

    # Resolve and validate core paths. Expand tildes so shell-style home
    # references work even when Click didn't see them.
    footage_dir = footage_dir.expanduser()
    pipeline_path = pipeline_path.expanduser()
    output_dir = output_dir.expanduser()

    if not pipeline_path.exists() or not pipeline_path.is_file():
        raise click.ClickException(
            f"--pipeline file does not exist: {pipeline_path}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    footage_index_path = output_dir / FOOTAGE_INDEX_FILENAME

    brief = _parse_brief(brief_arg)
    if style_path is not None:
        brief = _apply_style_override(brief, style_path)
        click.echo(f"[cli] using style override: {brief.style_ref}")

    # Decide whether to run preprocess. --skip-preprocess is a best-effort
    # hint: if the cached index is missing we fall through to preprocess
    # rather than silently running the pipeline against stale or empty
    # input.
    should_preprocess = True
    if skip_preprocess and footage_index_path.exists():
        should_preprocess = False
        click.echo(
            f"[cli] reusing existing footage index at {footage_index_path}"
        )
    elif skip_preprocess and not footage_index_path.exists():
        click.echo(
            f"[cli] --skip-preprocess set but {footage_index_path} is "
            "missing; running preprocess anyway"
        )

    if should_preprocess:
        if not footage_dir.exists():
            raise click.ClickException(
                f"--footage-dir does not exist: {footage_dir}"
            )
        if not footage_dir.is_dir():
            raise click.ClickException(
                f"--footage-dir is not a directory: {footage_dir}"
            )
        click.echo(
            f"[cli] preprocessing {footage_dir} -> {footage_index_path}"
        )
        try:
            preprocess_footage(
                str(footage_dir),
                str(footage_index_path),
            )
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            raise click.ClickException(
                f"preprocess failed: {exc}"
            ) from exc
        except RuntimeError as exc:
            raise click.ClickException(
                f"preprocess runtime error: {exc}"
            ) from exc

    click.echo(
        f"[cli] running pipeline {pipeline_path} against "
        f"{footage_index_path}"
    )
    try:
        result = run_pipeline(
            str(pipeline_path),
            brief,
            str(footage_index_path),
            human_approval=not no_approval,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(f"pipeline input missing: {exc}") from exc
    except ValidationError as exc:
        raise click.ClickException(
            f"pipeline schema validation failed: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise click.ClickException(
            f"pipeline YAML is malformed: {exc}"
        ) from exc
    except ValueError as exc:
        raise click.ClickException(f"pipeline error: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(
            f"pipeline runtime error: {exc}"
        ) from exc

    duration_seconds = time.monotonic() - started_at
    _print_summary(result, duration_seconds)

    # Successful exit even if the reviewer score was low — AC 6 says a
    # complete-with-warnings run should still exit 0. Only hard errors
    # should exit non-zero, and those all route through ClickException
    # above.
    sys.exit(0)


if __name__ == "__main__":
    cli()

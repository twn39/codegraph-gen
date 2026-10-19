from pathlib import Path
import click

from codegraph_gen.cli_ui import (
    console,
    pipeline_progress_context,
    print_build_summary,
    print_components_table,
    print_config_summary,
    print_empty_graph_warning,
    print_quality_gate_error,
    print_vault_tips,
    update_pipeline_progress,
)
from codegraph_gen.config import (
    CodegraphConfig,
    DEFAULT_EXCLUSIONS,
    LANGUAGE_EXTENSIONS,
    load_project_config,
)

try:
    from importlib.metadata import version

    __version__ = version("codegraph-gen")
except Exception:
    __version__ = "1.7.0"


@click.group()
@click.version_option(version=__version__, prog_name="codegraph")
def cli():
    """codegraph - Build a Markdown knowledge graph of your codebase for AI analysis."""
    pass


@cli.command()
@click.argument(
    "src_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=Path(".codegraph"),
    help="Directory where the Markdown vault will be written.",
)
@click.option(
    "--exclude",
    "-e",
    multiple=True,
    type=str,
    help="Additional folder names/patterns to exclude from scanning.",
)
@click.option(
    "--parallel/--no-parallel",
    default=True,
    help="Enable/disable parallel parsing (using multiprocessing).",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=None,
    help="Number of worker processes to use for parallel parsing.",
)
@click.option(
    "--cache/--no-cache",
    default=True,
    help="Enable/disable incremental parsing cache.",
)
@click.option(
    "--export-json/--no-export-json",
    default=True,
    help="Write graph.json machine-readable export in the vault output directory.",
)
@click.option(
    "--exclude-tests-from-clustering/--include-tests-in-clustering",
    default=None,
    help="Exclude (default) or include test-path nodes in Louvain clustering.",
)
@click.option(
    "--component-naming",
    type=click.Choice(["hybrid", "package", "symbol"], case_sensitive=False),
    default=None,
    help="Component naming strategy: hybrid (default), package, or symbol.",
)
@click.option(
    "--min-resolve-rate",
    type=float,
    default=None,
    help="CI gate: fail if overall edge resolve rate is below this value (0.0–1.0).",
)
@click.option(
    "--max-unresolved-edges",
    type=int,
    default=None,
    help="CI gate: fail if overall unresolved edge count exceeds this value.",
)
@click.option(
    "--min-internal-resolve-rate",
    type=float,
    default=None,
    help="CI gate: fail if *internal* edge resolve rate is below this (0.0–1.0).",
)
@click.option(
    "--max-internal-unresolved-edges",
    type=int,
    default=None,
    help="CI gate: fail if unresolved *internal* edge count exceeds this value.",
)
@click.option(
    "--strict/--no-strict",
    default=None,
    help="Fail the build if any source file fails to parse.",
)
@click.option(
    "--report-language",
    type=click.Choice(["en", "zh"], case_sensitive=False),
    default=None,
    help="Language for AGENT_PROMPT instructions (en or zh).",
)
def build(
    src_dir: Path,
    output: Path,
    exclude: list[str],
    parallel: bool,
    workers: int | None,
    cache: bool,
    export_json: bool,
    exclude_tests_from_clustering: bool | None,
    component_naming: str | None,
    min_resolve_rate: float | None,
    max_unresolved_edges: int | None,
    min_internal_resolve_rate: float | None,
    max_internal_unresolved_edges: int | None,
    strict: bool | None,
    report_language: str | None,
):
    """Parses the codebase in SRC_DIR and exports the Markdown graph vault."""
    console.print("[bold blue]Starting codegraph analysis...[/bold blue]")

    workspace = src_dir.resolve()

    # ── Load project config file (.codegraphrc) ────────────────────────────
    project_cfg = load_project_config(workspace)


    # ── Merge: CLI args take priority over .codegraphrc ────────────────────
    # Exclusions: default set + .codegraphrc extras + CLI --exclude (cumulative)
    exclusions = set(DEFAULT_EXCLUSIONS)
    if project_cfg and project_cfg.exclude:
        exclusions.update(project_cfg.exclude)
    if exclude:
        exclusions.update(exclude)

    # Output directory: CLI --output > .codegraphrc output > default
    if output != Path(".codegraph"):
        # User explicitly passed --output on CLI
        resolved_output = output.resolve()
    elif project_cfg and project_cfg.output != ".codegraph":
        resolved_output = (workspace / project_cfg.output).resolve()
    else:
        resolved_output = (workspace / ".codegraph").resolve()

    # Languages: CLI has no language filter option yet; use .codegraphrc if present
    all_languages = set(LANGUAGE_EXTENSIONS.keys())
    if project_cfg and project_cfg.languages:
        valid = {lang for lang in project_cfg.languages if lang in all_languages}
        invalid = set(project_cfg.languages) - valid
        if invalid:
            console.print(
                f"[yellow]⚠ .codegraphrc: unknown languages ignored: {', '.join(sorted(invalid))}[/yellow]"
            )
        languages = valid if valid else all_languages
    else:
        languages = all_languages

    # Workers: CLI --workers > .codegraphrc workers > CPU count
    import os

    if not parallel:
        max_workers = 1
    elif workers is not None:
        max_workers = workers
    elif project_cfg and project_cfg.workers is not None:
        max_workers = project_cfg.workers
    else:
        max_workers = os.cpu_count() or 4

    # Cache: CLI --cache/--no-cache flag always applies; .codegraphrc only when CLI default
    # (click default for cache is True, so we trust project_cfg when CLI wasn't explicitly set)
    effective_cache = (
        cache if cache is not None else (project_cfg.cache if project_cfg else True)
    )

    # Include dirs: from .codegraphrc include whitelist (no CLI equivalent)
    include_dirs = None
    if project_cfg and project_cfg.include:
        include_dirs = []
        for subdir in project_cfg.include:
            resolved = (workspace / subdir).resolve()
            if not resolved.exists():
                console.print(
                    f"[yellow]⚠ .codegraphrc include '{subdir}' does not exist, skipping.[/yellow]"
                )
            else:
                include_dirs.append(resolved)
        if not include_dirs:
            include_dirs = None  # All entries invalid → fall back to full scan

    print_config_summary(workspace, project_cfg, include_dirs, languages)

    # Clustering / quality options: CLI overrides .codegraphrc, else defaults
    effective_exclude_tests = (
        exclude_tests_from_clustering
        if exclude_tests_from_clustering is not None
        else (
            project_cfg.exclude_tests_from_clustering
            if project_cfg is not None
            else True
        )
    )
    effective_naming = (
        component_naming
        if component_naming is not None
        else (project_cfg.component_naming if project_cfg is not None else "hybrid")
    ).lower()
    effective_min_rate = (
        min_resolve_rate
        if min_resolve_rate is not None
        else (project_cfg.min_resolve_rate if project_cfg is not None else None)
    )
    effective_max_unresolved = (
        max_unresolved_edges
        if max_unresolved_edges is not None
        else (project_cfg.max_unresolved_edges if project_cfg is not None else None)
    )
    effective_min_internal_rate = (
        min_internal_resolve_rate
        if min_internal_resolve_rate is not None
        else (
            project_cfg.min_internal_resolve_rate if project_cfg is not None else None
        )
    )
    effective_max_internal_unresolved = (
        max_internal_unresolved_edges
        if max_internal_unresolved_edges is not None
        else (
            project_cfg.max_internal_unresolved_edges
            if project_cfg is not None
            else None
        )
    )
    effective_strict = (
        strict
        if strict is not None
        else (project_cfg.strict if project_cfg is not None else False)
    )
    effective_report_language = (
        report_language
        if report_language is not None
        else (project_cfg.report_language if project_cfg is not None else "en")
    ).lower()

    for label, rate in (
        ("--min-resolve-rate", effective_min_rate),
        ("--min-internal-resolve-rate", effective_min_internal_rate),
    ):
        if rate is not None and not (0.0 <= rate <= 1.0):
            console.print(
                f"[bold red]Error:[/bold red] {label} must be between 0.0 and 1.0"
            )
            raise SystemExit(2)

    config = CodegraphConfig(
        workspace_dir=workspace,
        output_dir=resolved_output,
        exclusions=exclusions,
        languages=languages,
        max_workers=max_workers,
        use_cache=effective_cache,
        include_dirs=include_dirs,
        export_json=export_json,
        exclude_tests_from_clustering=effective_exclude_tests,
        component_naming=effective_naming,
        min_resolve_rate=effective_min_rate,
        max_unresolved_edges=effective_max_unresolved,
        min_internal_resolve_rate=effective_min_internal_rate,
        max_internal_unresolved_edges=effective_max_internal_unresolved,
        strict=effective_strict,
        report_language=effective_report_language,
    )

    from codegraph_gen.engine import CodegraphEngine

    engine = CodegraphEngine()

    with pipeline_progress_context() as (progress, task):
        def progress_callback(stage, current_item, idx, total):
            update_pipeline_progress(progress, task, stage, current_item, idx, total)

        try:
            result = engine.run_pipeline(config, progress_callback=progress_callback)
        except RuntimeError as e:
            progress.update(task, description="Failed quality gate")
            print_quality_gate_error(e)
            raise SystemExit(1) from e

    if result.graph.number_of_nodes() == 0:
        print_empty_graph_warning()
        return

    print_build_summary(result, config)
    print_components_table(result)
    print_vault_tips(config.absolute_output_dir)


@cli.command()
@click.option(
    "--platform",
    "-p",
    default="codex",
    type=click.Choice(
        [
            "codex",
            "antigravity",
            "crush",
            "claude",
            "cursor",
            "gemini",
            "windsurf",
        ],
        case_sensitive=False,
    ),
    help="The AI agent platform to integrate with.",
)
def install(platform: str):
    """Installs the codegraph slash command into your AI Agent's global config."""
    from codegraph_gen.install_skill import skills_dir_for_platform, write_skill

    platform = platform.lower()
    console.print(
        f"[bold blue]Installing codegraph integration for {platform}...[/bold blue]"
    )
    skills_dir = skills_dir_for_platform(platform)
    try:
        skill_file = write_skill(skills_dir)
        console.print(
            f"[bold green]Successfully installed /codegraph slash command to: "
            f"[underline]{skill_file}[/underline][/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to write skill configuration: {e}[/bold red]")


@cli.command()
@click.argument(
    "src_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=Path(".codegraph"),
    help="Directory where the Markdown vault was written.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Automatically open the interactive HTML page in the browser.",
)
def visualize(src_dir: Path, output: Path, open_browser: bool):
    """Generates an interactive Plotly-based HTML visualization of the graph."""
    import sys

    console.print(
        "[bold blue]Generating interactive graph visualization...[/bold blue]"
    )

    workspace = src_dir.resolve()

    # Resolve output directory using same logic as build
    project_cfg = load_project_config(workspace)
    if output != Path(".codegraph"):
        resolved_output = output.resolve()
    elif project_cfg and project_cfg.output != ".codegraph":
        resolved_output = (workspace / project_cfg.output).resolve()
    else:
        resolved_output = (workspace / ".codegraph").resolve()

    try:
        from codegraph_gen.visualizer import generate_visualization

        html_path = generate_visualization(
            workspace, resolved_output, open_browser=open_browser
        )
        console.print(
            f"[bold green]Success![/bold green] Interactive graph exported to: [bold underline]{html_path}[/bold underline]"
        )
    except ImportError as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error generating visualization: {e}[/bold red]")
        sys.exit(1)


@cli.command()
def info():
    """Prints tool info and supported languages."""
    console.print(f"[bold]codegraph v{__version__}[/bold]")
    console.print(
        "Supported languages: Python, JavaScript, TypeScript, Kotlin, Go, Rust, Swift, C, C++, OCaml, Dart"
    )


def main():
    cli()


if __name__ == "__main__":
    main()

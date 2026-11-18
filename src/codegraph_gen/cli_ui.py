"""Terminal presentation layer for codegraph CLI using Rich."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from codegraph_gen.pipeline_stages import PipelineStage

if TYPE_CHECKING:
    from codegraph_gen.config import CodegraphConfig, ProjectConfig
    from codegraph_gen.engine import PipelineResult

console = Console()


@contextmanager
def pipeline_progress_context() -> Generator[tuple[Progress, Any], None, None]:
    """Provides a managed Rich progress bar for pipeline execution."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Initializing...", total=None)
        yield progress, task


def update_pipeline_progress(
    progress: Progress,
    task: Any,
    stage: PipelineStage,
    current_item: Any,
    idx: int,
    total: int,
) -> None:
    """Updates progress bar state for a given pipeline stage."""
    if stage == PipelineStage.DISCOVERING:
        progress.update(task, description="Discovering source files...")
    elif stage == PipelineStage.PARSING:
        if total > 0:
            progress.update(task, total=total)
        progress.update(
            task,
            description=f"Parsing {current_item.name if current_item else ''}",
            completed=idx,
        )
    elif stage == PipelineStage.BUILDING:
        progress.update(task, description="Building reference graph...")
    elif stage == PipelineStage.CLUSTERING:
        progress.update(task, description="Clustering components...")
    elif stage == PipelineStage.ANALYZING:
        progress.update(task, description="Analyzing graph metrics...")
    elif stage == PipelineStage.RENDERING:
        progress.update(task, description="Rendering Markdown vault...")
    elif stage == PipelineStage.WRITING:
        progress.update(task, description="Writing files to disk...")
    elif stage == PipelineStage.COMPLETED:
        progress.update(task, description="Done!")


def print_config_summary(
    workspace: Path,
    project_cfg: ProjectConfig | None,
    include_dirs: list[Path] | None,
    languages: set[str],
) -> None:
    """Prints loaded configuration details and warnings."""
    if project_cfg is not None:
        from codegraph_gen.config import PROJECT_CONFIG_FILE

        console.print(
            f"[dim]📄 Found project config: [underline]{workspace / PROJECT_CONFIG_FILE}[/underline][/dim]"
        )
        parts: list[str] = []
        if include_dirs:
            parts.append(f"include: {', '.join(p.name for p in include_dirs)}")
        if project_cfg.exclude:
            parts.append(f"extra exclude: {', '.join(project_cfg.exclude)}")
        if project_cfg.languages:
            parts.append(f"languages: {', '.join(sorted(languages))}")
        if parts:
            console.print(f"[dim]   {' | '.join(parts)}[/dim]")


def print_quality_gate_error(error: Exception | str) -> None:
    """Prints a styled quality gate failure message."""
    console.print(f"[bold red]Quality gate failed:[/bold red] {error}")


def print_empty_graph_warning() -> None:
    """Prints warning when the assembled graph contains zero nodes."""
    console.print("[bold yellow]Completed build, but graph is empty.[/bold yellow]")


def print_build_summary(result: PipelineResult, config: CodegraphConfig) -> None:
    """Prints summary statistics of the completed build."""
    g = result.graph
    files_count = len(result.files)
    symbols_count = g.number_of_nodes() - files_count

    console.print(f"Found [green]{files_count}[/green] supported files to analyze.")
    console.print(
        f"Assembled graph with [green]{g.number_of_nodes()}[/green] nodes and [green]{g.number_of_edges()}[/green] edges."
    )
    console.print(f"  - Files: {files_count}")
    console.print(f"  - Symbols (Classes/Functions/Methods): {symbols_count}")

    if result.analysis.resolution is not None:
        res = result.analysis.resolution
        console.print(
            f"  - Edge resolution: [green]{res.resolved}[/green]/{res.attempted} "
            f"([cyan]{100.0 * res.resolve_rate:.1f}%[/cyan]), unresolved={res.unresolved}"
        )
        console.print(
            f"  - Internal resolution: [green]{res.internal_resolved}[/green]/"
            f"{res.internal_attempted} "
            f"([cyan]{100.0 * res.internal_resolve_rate:.1f}%[/cyan]), "
            f"internal_unresolved={res.internal_unresolved}"
        )
        console.print(
            f"  - Unresolved by category: external={res.category_unresolved('external')}, "
            f"builtin={res.category_unresolved('builtin')}, "
            f"attribute={res.category_unresolved('attribute')}, "
            f"internal={res.category_unresolved('internal')}"
        )

    if result.write_stats is not None:
        ws = result.write_stats
        console.print(
            f"  - Vault write: written={ws.written}, "
            f"skipped={ws.skipped}, removed={ws.removed}"
        )

    if result.render_stats is not None:
        rs = result.render_stats
        console.print(
            f"  - Incremental render: nodes "
            f"{rs.nodes_rendered} rendered / "
            f"{rs.nodes_reused} reused; components "
            f"{rs.components_rendered} / "
            f"{rs.components_reused} "
            f"(dirty_files={rs.dirty_files})"
        )

    if config.export_json:
        console.print(
            f"  - JSON export: [underline]{config.absolute_output_dir / 'graph.json'}[/underline]"
        )

    if result.parse_errors:
        console.print(
            f"  - [yellow]Parse errors: {len(result.parse_errors)}[/yellow] "
            f"(use --strict to fail the build)"
        )
        for err in result.parse_errors[:5]:
            short = err if len(err) <= 160 else err[:157] + "..."
            console.print(f"      [dim]- {short}[/dim]")
        if len(result.parse_errors) > 5:
            console.print(f"      [dim]… and {len(result.parse_errors) - 5} more[/dim]")

    if result.reused_snapshot:
        console.print(
            "  - [cyan]Pipeline snapshot reused[/cyan] "
            "(skipped build/resolve/cluster/analyze)"
        )

    console.print(
        "[bold green]Success! Codebase knowledge graph built successfully.[/bold green]"
    )


def print_components_table(result: PipelineResult) -> None:
    """Prints a styled Rich table of logical components."""
    table = Table(title="Logical Components Summary")
    table.add_column("Component Name", style="cyan", no_wrap=True)
    table.add_column("Cohesion (Density)", style="magenta")
    table.add_column("Size (Nodes)", style="green")

    for cid, members in result.components.items():
        table.add_row(
            result.component_names[cid],
            str(result.cohesion_scores[cid]),
            str(len(members)),
        )

    console.print(table)


def print_vault_tips(output_dir: Path) -> None:
    """Prints pointers to the generated vault README and AI prompt instructions."""
    console.print(
        f"\nView the main graph entrypoint at: [bold underline]{output_dir}/README.md[/bold underline]"
    )
    console.print(
        f"💡 [bold yellow]AI Insight Tip:[/bold yellow] Ask your AI Agent (e.g. Antigravity, Claude Code, Codex) "
        f"to read [bold]{output_dir}/AGENT_PROMPT.md[/bold] and write the architectural report "
        f"directly to [bold]{output_dir}/README.md[/bold].\n"
    )

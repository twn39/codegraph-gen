from pathlib import Path
import click
from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    MofNCompleteColumn,
)

from codegraph.config import CodegraphConfig, DEFAULT_EXCLUSIONS

console = Console()


@click.group()
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
def build(
    src_dir: Path,
    output: Path,
    exclude: list[str],
    parallel: bool,
    workers: int | None,
    cache: bool,
):
    """Parses the codebase in SRC_DIR and exports the Markdown graph vault."""
    console.print("[bold blue]Starting codegraph analysis...[/bold blue]")

    # 1. Prepare configuration
    exclusions = set(DEFAULT_EXCLUSIONS)
    if exclude:
        exclusions.update(exclude)

    import os

    if not parallel:
        max_workers = 1
    elif workers is not None:
        max_workers = workers
    else:
        max_workers = os.cpu_count() or 4

    config = CodegraphConfig(
        workspace_dir=src_dir.resolve(),
        output_dir=output.resolve(),
        exclusions=exclusions,
        max_workers=max_workers,
        use_cache=cache,
    )

    from codegraph.engine import CodegraphEngine, PipelineStage

    engine = CodegraphEngine(config)

    # Run pipeline with click progress bar
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Initializing...", total=None)

        def progress_callback(stage: PipelineStage, current_item, idx, total):
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

        result = engine.run_pipeline(progress_callback=progress_callback)

    G = result.graph
    if G.number_of_nodes() == 0:
        console.print("[bold yellow]Completed build, but graph is empty.[/bold yellow]")
        return

    files_count = len(result.files)
    symbols_count = G.number_of_nodes() - files_count

    console.print(f"Found [green]{files_count}[/green] supported files to analyze.")
    console.print(
        f"Assembled graph with [green]{G.number_of_nodes()}[/green] nodes and [green]{G.number_of_edges()}[/green] edges."
    )
    console.print(f"  - Files: {files_count}")
    console.print(f"  - Symbols (Classes/Functions/Methods): {symbols_count}")

    console.print(
        "[bold green]Success! Codebase knowledge graph built successfully.[/bold green]"
    )

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
    console.print(
        f"\nView the main graph entrypoint at: [bold underline]{config.absolute_output_dir}/README.md[/bold underline]"
    )
    console.print(
        f"💡 [bold yellow]AI Insight Tip:[/bold yellow] Ask your AI Agent (e.g. Antigravity, Claude Code, Codex) to read [bold]{config.absolute_output_dir}/AGENT_PROMPT.md[/bold] and write the architectural report directly to [bold]{config.absolute_output_dir}/README.md[/bold].\n"
    )


@cli.command()
@click.option(
    "--platform",
    "-p",
    default="codex",
    type=click.Choice(["codex", "antigravity"]),
    help="The AI agent platform to integrate with.",
)
def install(platform: str):
    """Installs the codegraph slash command into your AI Agent's global config."""
    console.print(
        f"[bold blue]Installing codegraph integration for {platform}...[/bold blue]"
    )

    # 1. Resolve skills directory based on target platform
    if platform == "codex":
        skills_dir = Path.home() / ".codex" / "skills" / "codegraph"
    elif platform == "antigravity":
        skills_dir = Path.home() / ".gemini" / "config" / "skills" / "codegraph"
    else:
        skills_dir = Path.home() / ".codex" / "skills" / "codegraph"

    # 2. Skill file content
    skill_content = """---
name: codegraph
description: "Build a Markdown codebase knowledge graph using codegraph, perform logical component clustering, analyze god nodes/circular dependencies, and write deep architectural insights to .codegraph/README.md."
trigger: /codegraph
---

# /codegraph

Build a codebase knowledge graph using `codegraph` for any folder, cluster symbols into logical components, detect god nodes and cycles, and perform a deep architectural analysis to write insights directly to the `.codegraph/README.md` vault.

## Usage

```
/codegraph                                            # Run the full build & AI analysis pipeline on the current directory
/codegraph <path>                                     # Run the pipeline on a specific subfolder/path
/codegraph --exclude <pattern>                        # Build and exclude specific folders/patterns
```

## What You Must Do When Invoked

If the user invoked `/codegraph` with no path, do not ask the user for a path. Instead of scanning the entire project root directory `.` (which may include non-essential scripts, docs, or huge subfolders), you MUST prioritize targeting the primary source directory (e.g. `src/`, `lib/`, `app/`) and test directory (e.g. `tests/`, `test/`).
- If specific source or test folders are found, run the build targeting those folders, or build the root `.` but exclude other non-code/non-test directories (e.g., `docs/`, `scripts/`, `examples/`) using the `--exclude` flag to keep the graph focused on code and tests.
- Otherwise, default to `.` (current directory).

Follow these steps in order. Do not skip any steps.

### Step 1 - Ensure codegraph is installed

Check and locate the `codegraph` executable. To support virtual environments, resolve the binary in the following priority order:
1. Local virtual environment: `.venv/bin/codegraph` or `venv/bin/codegraph`
2. Global command: `codegraph` (installed globally or via uv tool)

You can use this shell logic to resolve the executable:
```bash
if [ -f ".venv/bin/codegraph" ]; then
    CODEGRAPH_BIN=".venv/bin/codegraph"
elif [ -f "venv/bin/codegraph" ]; then
    CODEGRAPH_BIN="venv/bin/codegraph"
else
    if ! command -v codegraph >/dev/null 2>&1; then
        uv tool install codegraph
    fi
    CODEGRAPH_BIN="codegraph"
fi
echo "Using codegraph binary: $CODEGRAPH_BIN"
```

### Step 2 - Build the Knowledge Graph

Run the resolved `$CODEGRAPH_BIN` on the specified directory:
```bash
$CODEGRAPH_BIN build INPUT_PATH
# Or with additional exclude arguments if provided by the user
```
*(Replace `INPUT_PATH` with the resolved target path, e.g. `.`)*

If the command fails or errors out, capture the terminal stderr/logs, display them to the user with a helpful explanation, and ask them if they want to exclude specific directories or fix the errors. Do not fail silently.

### Step 3 - Perform Deep Architectural Analysis

Once the graph is built successfully:
1. Read the newly generated `<path>/.codegraph/AGENT_PROMPT.md` file using your file reading tools.
2. Read the project statistics, communities, god nodes, and cycle warnings from it.
3. Perform a deep, professional architectural review of the codebase (using **English** as the report language), combined with deep insight analysis of the code implementation of existing features.
4. Focus your review on:
   - **System Architecture Evaluation**: Explain the design patterns, modularity level, and alignment between physical directories and logical components in the codebase.
   - **Core Abstractions & Boundary Evaluation**: Deeply analyze God Nodes to determine which ones are core support and which ones have excessive responsibilities (God Object / Fat Class) that may lead to high risk.
   - **Potential Bottlenecks & Architectural Refactoring Recommendations**: Point out high-coupling risk points and negative impacts of circular dependencies, and provide specific, actionable refactoring optimization plans (e.g., decoupling, extracting interfaces, dependency inversion).
5. Read the existing `<path>/.codegraph/README.md` first. If there's an existing `## AI Architectural Insights` section, merge your new findings with it rather than silently overwriting and discarding previous edits.
6. Write the completed report into `<path>/.codegraph/README.md` under the `## AI Architectural Insights` section, replacing any placeholder instructions.

### Step 4 - Present Summary to the User

Finally, reply to the user in English, summarizing:
- The graph statistics (number of files, symbols, edges).
- The logical component summary (with sizes and cohesion scores).
- A brief bulleted summary of your key architectural findings and recommendations.
- Clickable markdown links pointing to:
  - The main entrypoint: `[README.md](file:///<absolute_path_to_vault>/README.md)`
  - The agent guidelines: `[AGENTS.md](file:///<absolute_path_to_vault>/AGENTS.md)`
  - The detailed components folder: `[components/](file:///<absolute_path_to_vault>/components/)`
"""

    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skills_dir / "SKILL.md"
        skill_file.write_text(skill_content, encoding="utf-8")
        console.print(
            f"[bold green]Successfully installed /codegraph slash command to: [underline]{skill_file}[/underline][/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]Failed to write skill configuration: {e}[/bold red]")


@cli.command()
def info():
    """Prints tool info and supported languages."""
    try:
        from importlib.metadata import version

        ver = version("codegraph")
    except Exception:
        ver = "0.2.0"
    console.print(f"[bold]codegraph v{ver}[/bold]")
    console.print(
        "Supported languages: Python, JavaScript, TypeScript, Go, Rust, Swift"
    )


def main():
    cli()


if __name__ == "__main__":
    main()

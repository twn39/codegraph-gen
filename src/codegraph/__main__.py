import logging
from pathlib import Path
import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

from codegraph.config import CodegraphConfig, DEFAULT_EXCLUSIONS
from codegraph.detect import discover_files
from codegraph.parser import get_parser
from codegraph.builder import build_graph
from codegraph.cluster import detect_components
from codegraph.exporter import to_markdown_vault

console = Console()

@click.group()
def cli():
    """codegraph - Build a Markdown knowledge graph of your codebase for AI analysis."""
    pass

@cli.command()
@click.argument("src_dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=Path(".codegraph"),
              help="Directory where the Markdown vault will be written.")
@click.option("--exclude", "-e", multiple=True, type=str,
              help="Additional folder names/patterns to exclude from scanning.")
def build(src_dir: Path, output: Path, exclude: list[str]):
    """Parses the codebase in SRC_DIR and exports the Markdown graph vault."""
    console.print("[bold blue]Starting codegraph analysis...[/bold blue]")
    
    # 1. Prepare configuration
    exclusions = set(DEFAULT_EXCLUSIONS)
    if exclude:
        exclusions.update(exclude)
        
    config = CodegraphConfig(
        workspace_dir=src_dir.resolve(),
        output_dir=output,
        exclusions=exclusions
    )
    
    # 2. Discover files
    console.print(f"Scanning [cyan]{config.workspace_dir}[/cyan] for source files...")
    files = discover_files(config)
    
    if not files:
        console.print("[bold yellow]No supported source files found in the workspace.[/bold yellow]")
        return
        
    console.print(f"Found [green]{len(files)}[/green] supported files to analyze.")
    
    # 3. Parse files with progress bar
    extractions = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Parsing AST files", total=len(files))
        
        for file_path, lang in files:
            progress.update(task, description=f"Parsing {file_path.name}")
            try:
                parser = get_parser(lang)
                result = parser.parse_file(file_path, config.workspace_dir)
                extractions.append(result)
            except Exception as e:
                console.print(f"[bold red]Error parsing {file_path}: {e}[/bold red]")
            progress.advance(task)

    # 4. Build graph
    console.print("Assembling and resolving semantic call-graph...")
    G = build_graph(extractions, config.workspace_dir)
    
    files_count = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "file")
    symbols_count = G.number_of_nodes() - files_count
    
    console.print(f"Assembled graph with [green]{G.number_of_nodes()}[/green] nodes and [green]{G.number_of_edges()}[/green] edges.")
    console.print(f"  - Files: {files_count}")
    console.print(f"  - Symbols (Classes/Functions/Methods): {symbols_count}")

    # 5. Component clustering
    console.print("Clustering code structure into logical components...")
    components, cohesion_scores, component_names = detect_components(G)
    
    # 6. Export to Markdown
    abs_out = config.absolute_output_dir
    console.print(f"Writing Markdown storage to [cyan]{abs_out}[/cyan]...")
    to_markdown_vault(G, components, cohesion_scores, component_names, abs_out)
    
    console.print("[bold green]Success! Codebase knowledge graph built successfully.[/bold green]")
    
    # Display components summary table
    table = Table(title="Logical Components Summary")
    table.add_column("Component Name", style="cyan", no_wrap=True)
    table.add_column("Cohesion (Density)", style="magenta")
    table.add_column("Size (Nodes)", style="green")
    
    for cid, members in components.items():
        table.add_row(
            component_names[cid],
            str(cohesion_scores[cid]),
            str(len(members))
        )
        
    console.print(table)
    console.print(f"\nView the main graph entrypoint at: [bold underline]{abs_out}/README.md[/bold underline]")
    console.print(f"💡 [bold yellow]AI Insight Tip:[/bold yellow] Ask your AI Agent (e.g. Antigravity, Claude Code, Codex) to read [bold]{abs_out}/AGENT_PROMPT.md[/bold] and write the architectural report directly to [bold]{abs_out}/README.md[/bold].\n")

@cli.command()
@click.option("--platform", "-p", default="codex", type=click.Choice(["codex", "antigravity"]),
              help="The AI agent platform to integrate with.")
def install(platform: str):
    """Installs the codegraph slash command into your AI Agent's global config."""
    console.print(f"[bold blue]Installing codegraph integration for {platform}...[/bold blue]")
    
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
description: "Build a Markdown codebase knowledge graph using codegraph, perform logical component clustering, analyze god nodes/circular dependencies, and write deep Chinese architectural insights to .codegraph/README.md."
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

If the user invoked `/codegraph` with no path, default to `.` (current directory). Do not ask the user for a path.

Follow these steps in order. Do not skip any steps.

### Step 1 - Ensure codegraph is installed

Check if the `codegraph` CLI is available in the current environment:
```bash
if ! command -v codegraph >/dev/null 2>&1; then
    # Try installing it globally using uv
    if command -v uv >/dev/null 2>&1; then
        uv tool install codegraph
    else
        python3 -m pip install --break-system-packages .
    fi
fi
```

### Step 2 - Build the Knowledge Graph

Run the `codegraph build` command on the specified directory:
```bash
codegraph build INPUT_PATH
# Or with additional exclude arguments if provided by the user
```
Replace `INPUT_PATH` with the resolved target path (e.g. `.`). This will create the `.codegraph/` folder containing the main `README.md`, `AGENT_PROMPT.md`, `AGENTS.md`, `nodes/`, and `components/`.

### Step 3 - Perform Deep Architectural Analysis

Once the graph is built successfully:
1. Read the newly generated `<path>/.codegraph/AGENT_PROMPT.md` file using your file reading tools.
2. Read the project statistics, communities, god nodes, and cycle warnings from it.
3. Perform a deep, professional architectural review of the codebase (using **Chinese** as the report language).
4. Focus your review on:
   - **系统架构评估**：说明代码库的设计模式、模块化水准、物理目录与逻辑组件契合度。
   - **核心抽象与边界评估**：对 God Nodes 进行深入把脉，分析哪些 is 核心支撑，哪些职责过重（God Object / Fat Class）可能导致高风险。
   - **潜在瓶颈与架构重构建议**：指出高耦合风险点、循环依赖负面影响，并给出具体、可操作的重构优化方案（如解耦、提取接口、依赖倒置等）。
5. Write the completed report into `<path>/.codegraph/README.md` under the `## AI 架构深度洞察 (AI Architectural Insights)` section, replacing any placeholder instructions.

### Step 4 - Present Summary to the User

Finally, reply to the user in Chinese, summarizing:
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
        console.print(f"[bold green]Successfully installed /codegraph slash command to: [underline]{skill_file}[/underline][/bold green]")
    except Exception as e:
        console.print(f"[bold red]Failed to write skill configuration: {e}[/bold red]")

@cli.command()
def info():
    """Prints tool info and supported languages."""
    console.print("[bold]codegraph v0.1.0[/bold]")
    console.print("Supported languages: Python, JavaScript, TypeScript, Go, Rust, Swift")

def main():
    cli()

if __name__ == "__main__":
    main()

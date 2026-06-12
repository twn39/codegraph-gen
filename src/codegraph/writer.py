import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class VaultWriter:
    def clear_directory(self, path: Path):
        """Clears the output directory before exporting."""
        if path.exists():
            try:
                shutil.rmtree(path)
                logger.info(f"Cleared directory: {path}")
            except Exception as e:
                logger.warning(f"Could not fully clear output directory {path}: {e}")

    def write_file(self, path: Path, content: str):
        """Helper to write content to a file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write file at {path}: {e}")
            raise

    def write_vault(
        self,
        output_dir: Path,
        rendered_nodes: dict[str, str],
        rendered_components: dict[str, str],
        readme_content: str,
        prompt_content: str,
    ):
        """Writes all rendered markdown pages to their respective directories."""
        self.clear_directory(output_dir)

        nodes_dir = output_dir / "nodes"
        comps_dir = output_dir / "components"

        nodes_dir.mkdir(parents=True, exist_ok=True)
        comps_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write Node Pages
        for fname, content in rendered_nodes.items():
            self.write_file(nodes_dir / fname, content)

        # 2. Write Component Pages
        for fname, content in rendered_components.items():
            self.write_file(comps_dir / fname, content)

        # 3. Write README.md and AGENT_PROMPT.md
        self.write_file(output_dir / "README.md", readme_content)
        self.write_file(output_dir / "AGENT_PROMPT.md", prompt_content)

        # 4. Write AGENTS.md to root
        project_root = output_dir.parent
        agents_file = project_root / "AGENTS.md"

        agents_rule_body = """## codegraph

This project maintains a codebase knowledge graph at `.codegraph/`.

### Guidelines for AI Agents (Antigravity, Claude Code, Cursor, Roo Code, etc.)

You MUST follow these rules when working in this codebase:

1. **Prioritize the Knowledge Graph**:
   - Before answering architecture, design, or codebase structure questions, you **MUST** read [.codegraph/README.md](.codegraph/README.md) to understand the system overview, god nodes, and logical community structure.
   - Use [.codegraph/components/](.codegraph/components/) and [.codegraph/nodes/](.codegraph/nodes/) to navigate component boundaries, file relationships, and symbol definitions. This is much faster and more token-efficient than reading raw source files directly.

2. **AI Architectural Insights**:
   - Check [.codegraph/README.md](.codegraph/README.md) for a section titled `AI Architectural Insights`.
   - If this section is missing, incomplete, or contains placeholders, read [.codegraph/AGENT_PROMPT.md](.codegraph/AGENT_PROMPT.md), perform a deep architectural analysis of the project, and write your report into that section. Do not overwrite other sections.

3. **Keep Graph Sync'd**:
   - Whenever you create, delete, or modify code files, you **SHOULD** remind the user to run `codegraph build .` to rebuild the knowledge graph and keep it current.
   - When running the build command, exclude irrelevant or generated directories (e.g., third-party dependencies, build folders, or documentation) using the `-e`/`--exclude` flag to keep the graph focused and clean (e.g., `codegraph build . -e third_party/`).
"""

        if agents_file.exists():
            try:
                content = agents_file.read_text(encoding="utf-8")
                if "## codegraph" not in content:
                    new_content = content.rstrip() + "\n\n" + agents_rule_body
                    self.write_file(agents_file, new_content)
                    logger.info(
                        "Appended codegraph rules to existing AGENTS.md at root"
                    )
            except Exception as e:
                logger.warning(f"Could not read or append to existing AGENTS.md: {e}")
        else:
            try:
                self.write_file(agents_file, agents_rule_body)
                logger.info("Created new AGENTS.md with codegraph rules at root")
            except Exception as e:
                logger.warning(f"Could not create AGENTS.md at root: {e}")

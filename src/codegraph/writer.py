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

This project has a codegraph knowledge graph at .codegraph/.

Rules for AI Agents (Antigravity, Claude Code, Codex, etc.):
- Before answering architecture or codebase questions, read .codegraph/README.md for god nodes and community structure.
- Refer to .codegraph/components/ and .codegraph/nodes/ to navigate codebase relationships and definitions instead of reading raw files directly.
- If .codegraph/README.md does not contain AI Architectural Insights, read .codegraph/AGENT_PROMPT.md, perform the deep architectural analysis, and write your report into the `AI 架构深度洞察` section of .codegraph/README.md.
- After modifying code files in this workspace, remind the user to run `codegraph build .` to keep the knowledge graph current.
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

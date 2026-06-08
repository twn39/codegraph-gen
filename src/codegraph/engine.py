import logging
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import networkx as nx
from pydantic import BaseModel, ConfigDict

from codegraph.config import CodegraphConfig
from codegraph.detect import discover_files
from codegraph.parser import get_parser
from codegraph.builder import build_graph
from codegraph.cluster import detect_components
from codegraph.analyzer import analyze_graph, AnalysisResult
from codegraph.renderer import (
    MarkdownRenderer,
    get_node_filename,
    get_component_filename,
)
from codegraph.writer import VaultWriter

logger = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    DISCOVERING = "discovering"
    PARSING = "parsing"
    BUILDING = "building"
    CLUSTERING = "clustering"
    ANALYZING = "analyzing"
    RENDERING = "rendering"
    WRITING = "writing"
    COMPLETED = "completed"


class PipelineResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    graph: nx.DiGraph
    files: List[Tuple[Path, str]]
    components: Dict[int, List[str]]
    cohesion_scores: Dict[int, float]
    component_names: Dict[int, str]
    analysis: AnalysisResult


class CodegraphEngine:
    def __init__(self, config: CodegraphConfig):
        self.config = config
        self.renderer = MarkdownRenderer(config.workspace_dir)
        self.writer = VaultWriter()

    def run_pipeline(
        self,
        progress_callback: Optional[
            Callable[[PipelineStage, Any, int, int], None]
        ] = None,
    ) -> PipelineResult:
        """
        Runs the full codegraph generation pipeline.
        Args:
            progress_callback: A function taking (stage, current_item, index, total)
        """
        logger.info("Starting codegraph engine pipeline...")

        # 1. Discover files
        if progress_callback:
            progress_callback(PipelineStage.DISCOVERING, None, 0, 0)
        files = discover_files(self.config)
        if not files:
            logger.warning("No supported files found.")
            if progress_callback:
                progress_callback(PipelineStage.COMPLETED, None, 0, 0)
            return PipelineResult(
                graph=nx.DiGraph(),
                files=[],
                components={},
                cohesion_scores={},
                component_names={},
                analysis=AnalysisResult(god_nodes=[], cycles=[], inter_comp_deps={}),
            )

        # 2. Parse files
        extractions = []
        total_files = len(files)
        for idx, (file_path, lang) in enumerate(files, start=1):
            if progress_callback:
                progress_callback(PipelineStage.PARSING, file_path, idx, total_files)
            try:
                parser = get_parser(lang)
                result = parser.parse_file(file_path, self.config.workspace_dir)
                extractions.append(result)
            except Exception as e:
                logger.error(f"Error parsing file {file_path}: {e}")

        # 3. Build graph
        if progress_callback:
            progress_callback(PipelineStage.BUILDING, None, 0, 0)
        G = build_graph(extractions, self.config.workspace_dir)

        # 4. Component clustering
        if progress_callback:
            progress_callback(PipelineStage.CLUSTERING, None, 0, 0)
        components, cohesion_scores, component_names = detect_components(G)

        # 5. Graph analysis
        if progress_callback:
            progress_callback(PipelineStage.ANALYZING, None, 0, 0)
        analysis = analyze_graph(G, components)

        # 6. Render pages in memory
        if progress_callback:
            progress_callback(PipelineStage.RENDERING, None, 0, 0)
        node_component_map = {}
        for cid, members in components.items():
            comp_name = component_names.get(cid, f"Component {cid}")
            for member in members:
                node_component_map[member] = comp_name

        rendered_nodes = {}
        for nid, ndata in G.nodes(data=True):
            fname = get_node_filename(nid)
            content = self.renderer.render_node_page(nid, ndata, G, node_component_map)
            rendered_nodes[fname] = content

        rendered_components = {}
        for cid, members in components.items():
            comp_name = component_names[cid]
            cohesion = cohesion_scores[cid]
            fname = get_component_filename(comp_name)
            content = self.renderer.render_component_page(
                cid,
                members,
                G,
                cohesion,
                comp_name,
                analysis.inter_comp_deps,
                component_names,
            )
            rendered_components[fname] = content

        # Check if README already has AI Insights and preserve it
        ai_insights = None
        readme_path = self.config.absolute_output_dir / "README.md"
        if readme_path.exists():
            try:
                old_readme = readme_path.read_text(encoding="utf-8")
                marker = "## AI 架构深度洞察 (AI Architectural Insights)"
                if marker in old_readme:
                    parts = old_readme.split(marker, 1)
                    insights_text = parts[1].strip()
                    if insights_text:
                        ai_insights = insights_text
            except Exception as e:
                logger.warning(
                    f"Could not read existing README.md to preserve AI insights: {e}"
                )

        readme_content = self.renderer.render_readme(
            G,
            components,
            cohesion_scores,
            component_names,
            analysis,
            ai_insights=ai_insights,
        )

        prompt_content = self.renderer.render_agent_prompt(
            G, components, cohesion_scores, component_names, analysis
        )

        # 7. Write vault to disk
        if progress_callback:
            progress_callback(PipelineStage.WRITING, None, 0, 0)
        self.writer.write_vault(
            self.config.absolute_output_dir,
            rendered_nodes,
            rendered_components,
            readme_content,
            prompt_content,
        )

        if progress_callback:
            progress_callback(PipelineStage.COMPLETED, None, 0, 0)

        logger.info("Pipeline executed successfully.")
        return PipelineResult(
            graph=G,
            files=files,
            components=components,
            cohesion_scores=cohesion_scores,
            component_names=component_names,
            analysis=analysis,
        )

import logging
import json
import hashlib
import concurrent.futures
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import networkx as nx
from pydantic import BaseModel, ConfigDict

from codegraph.config import CodegraphConfig, CacheEntry
from codegraph.parser.base import ExtractionResult
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


def get_file_hash(path: Path) -> str:
    """Computes MD5 hash of a file."""
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
    except Exception:
        return ""
    return hasher.hexdigest()


def _parse_file_worker(
    file_path: Path, lang: str, workspace_dir: Path
) -> tuple[Path, Optional[ExtractionResult], Optional[str]]:
    """Worker function for parallel file parsing."""
    try:
        from codegraph.parser import get_parser

        parser = get_parser(lang)
        result = parser.parse_file(file_path, workspace_dir)
        return file_path, result, None
    except Exception as e:
        import traceback

        err_msg = f"{e}\n{traceback.format_exc()}"
        return file_path, None, err_msg


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

        # 2. Parse files (with caching and optional parallel processing)
        extractions = []
        total_files = len(files)

        cache_path = self.config.absolute_output_dir / "cache.json"
        cache_entries = {}
        if self.config.use_cache and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                    for k, v in cache_data.items():
                        cache_entries[k] = CacheEntry(**v)
                logger.info(f"Loaded {len(cache_entries)} cache entries.")
            except Exception as e:
                logger.warning(f"Could not load cache: {e}")

        files_to_parse = []
        new_cache_entries = {}

        for file_path, lang in files:
            rel_path = str(file_path.relative_to(self.config.workspace_dir))
            try:
                stat = file_path.stat()
                mtime = stat.st_mtime
                size = stat.st_size
                file_hash = get_file_hash(file_path)

                # Check cache hit
                if rel_path in cache_entries:
                    entry = cache_entries[rel_path]
                    if (
                        entry.mtime == mtime
                        and entry.size == size
                        and entry.hash == file_hash
                    ):
                        extractions.append(entry.result)
                        new_cache_entries[rel_path] = entry
                        continue

                # Cache miss
                files_to_parse.append(
                    (file_path, lang, rel_path, mtime, size, file_hash)
                )
            except Exception as e:
                logger.error(f"Error accessing file metadata for {file_path}: {e}")
                # Fallback to parsing without cache metadata
                files_to_parse.append((file_path, lang, rel_path, 0.0, 0, ""))

        num_hits = total_files - len(files_to_parse)
        if num_hits > 0:
            logger.info(
                f"Cache hit: {num_hits} / {total_files} files loaded from cache."
            )

        if not files_to_parse:
            if progress_callback:
                progress_callback(PipelineStage.PARSING, None, total_files, total_files)
        else:
            max_workers = self.config.max_workers
            if max_workers > 1 and len(files_to_parse) > 1:
                logger.info(
                    f"Parsing {len(files_to_parse)} files in parallel with {max_workers} workers..."
                )
                with concurrent.futures.ProcessPoolExecutor(
                    max_workers=max_workers
                ) as executor:
                    futures = {
                        executor.submit(
                            _parse_file_worker,
                            file_path,
                            lang,
                            self.config.workspace_dir,
                        ): (file_path, rel_path, mtime, size, file_hash)
                        for file_path, lang, rel_path, mtime, size, file_hash in files_to_parse
                    }

                    for idx, future in enumerate(
                        concurrent.futures.as_completed(futures), start=1
                    ):
                        file_path, rel_path, mtime, size, file_hash = futures[future]
                        progress_idx = num_hits + idx
                        if progress_callback:
                            progress_callback(
                                PipelineStage.PARSING,
                                file_path,
                                progress_idx,
                                total_files,
                            )

                        try:
                            _, result, err_msg = future.result()
                            if err_msg:
                                logger.error(
                                    f"Error parsing file {file_path} in worker: {err_msg}"
                                )
                            elif result:
                                extractions.append(result)
                                if file_hash:
                                    new_cache_entries[rel_path] = CacheEntry(
                                        mtime=mtime,
                                        size=size,
                                        hash=file_hash,
                                        result=result,
                                    )
                        except Exception as e:
                            logger.error(f"Failed to parse file {file_path}: {e}")
            else:
                logger.info(f"Parsing {len(files_to_parse)} files sequentially...")
                for idx, (
                    file_path,
                    lang,
                    rel_path,
                    mtime,
                    size,
                    file_hash,
                ) in enumerate(files_to_parse, start=1):
                    progress_idx = num_hits + idx
                    if progress_callback:
                        progress_callback(
                            PipelineStage.PARSING, file_path, progress_idx, total_files
                        )
                    try:
                        parser = get_parser(lang)
                        result = parser.parse_file(file_path, self.config.workspace_dir)
                        extractions.append(result)
                        if file_hash:
                            new_cache_entries[rel_path] = CacheEntry(
                                mtime=mtime, size=size, hash=file_hash, result=result
                            )
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
                marker = "## AI Architectural Insights"
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

        # Write updated cache back to disk
        if self.config.use_cache:
            try:
                self.config.absolute_output_dir.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {k: v.model_dump() for k, v in new_cache_entries.items()},
                        f,
                        indent=2,
                    )
                logger.info(f"Saved {len(new_cache_entries)} cache entries.")
            except Exception as e:
                logger.warning(f"Could not save cache: {e}")

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

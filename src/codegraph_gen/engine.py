"""Pipeline orchestration engine for codegraph-gen."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from codegraph_gen.config import CacheEntry, CodegraphConfig
from codegraph_gen.detect import discover_files
from codegraph_gen.incremental import get_file_hash
from codegraph_gen.pipeline_stages import (
    PipelineContext,
    PipelineResult,
    PipelineStage,
    ProgressCallback,
    RenderStats,
    VaultWriteStats,
    empty_pipeline_result,
    parse_files_with_cache,
    run_stage_analyze_and_check,
    run_stage_build_and_cluster,
    run_stage_discover,
    run_stage_parse,
    run_stage_render,
    run_stage_write,
    to_pipeline_result,
)
from codegraph_gen.renderer import MarkdownRenderer
from codegraph_gen.schema import ExtractionResult
from codegraph_gen.writer import VaultWriter

logger = logging.getLogger(__name__)

__all__ = [
    "CodegraphEngine",
    "PipelineContext",
    "PipelineResult",
    "PipelineStage",
    "ProgressCallback",
    "RenderStats",
    "VaultWriteStats",
    "discover_files",
    "get_file_hash",
]


class CodegraphEngine:
    """Coordinates execution of the end-to-end knowledge graph pipeline."""

    def __init__(self, writer: Optional[VaultWriter] = None):
        self.writer = writer if writer is not None else VaultWriter()

    def _parse_with_cache(
        self,
        config: CodegraphConfig,
        files: list[tuple[Path, str]],
        cache_path: Path,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> tuple[list[ExtractionResult], set[str], dict[str, CacheEntry], list[str]]:
        return parse_files_with_cache(config, files, cache_path, progress_callback)

    def run_pipeline(
        self,
        config: CodegraphConfig,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> PipelineResult:
        """Run the full codegraph generation pipeline as ordered stages."""
        logger.info("Starting codegraph engine pipeline...")
        ctx = PipelineContext(config=config, progress_callback=progress_callback)
        renderer = MarkdownRenderer(config.workspace_dir)

        # Stage 1: File discovery (uses discover_files imported in this module)
        if not run_stage_discover(ctx, discover_fn=discover_files):
            return empty_pipeline_result()

        # Stage 2: Parsing (with cache / multi-core workers)
        run_stage_parse(ctx, parse_fn=self._parse_with_cache)

        # Stages 3-5: Reference graph, modularity clustering, and analysis
        reused_snapshot = run_stage_build_and_cluster(ctx)

        # Strict checks & CI quality gates
        run_stage_analyze_and_check(ctx)

        # Stage 6: Incremental documentation rendering
        skipped_nodes, skipped_components = run_stage_render(ctx, renderer)

        # Stage 7: Vault writing, JSON export, and snapshot persistence
        run_stage_write(
            ctx,
            self.writer,
            skipped_nodes=skipped_nodes,
            skipped_components=skipped_components,
            reused_snapshot=reused_snapshot,
        )

        return to_pipeline_result(ctx)

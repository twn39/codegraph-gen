"""Pipeline execution stages and context for codegraph generation.

Follows modern architectural design principles:
- Clear separation between state container (PipelineContext) and pure functional stage runners.
- Functional composition over class inheritance.
- Preserves Locality of Behavior (LoB) by co-locating parsing workers and incremental stages.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import networkx as nx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from codegraph_gen.analyzer import AnalysisResult, ResolutionStats, analyze_graph
from codegraph_gen.builder import build_graph
from codegraph_gen.cluster import detect_components
from codegraph_gen.config import CacheEntry, CodegraphConfig
from codegraph_gen.detect import discover_files
from codegraph_gen.incremental import (
    PIPELINE_SNAPSHOT_NAME,
    SIGNATURE_STORE_NAME,
    component_signature,
    import_dependents,
    load_pipeline_snapshot,
    load_signature_store,
    node_neighborhood_signature,
    save_pipeline_snapshot,
    save_signature_store,
    should_force_render_node,
    workspace_fingerprint,
)
from codegraph_gen.parser import get_parser
from codegraph_gen.renderer import (
    MarkdownRenderer,
    get_component_filename,
    get_node_filename,
)
from codegraph_gen.schema import ExtractionResult
from codegraph_gen.writer import VaultWriter

logger = logging.getLogger(__name__)

_CACHE_ADAPTER: TypeAdapter[dict[str, CacheEntry]] = TypeAdapter(dict[str, CacheEntry])
_WORKER_PARSER_CACHE: dict[str, Any] = {}


class PipelineStage(str, Enum):
    """Enumeration of stages in the codegraph generation pipeline."""

    DISCOVERING = "discovering"
    PARSING = "parsing"
    BUILDING = "building"
    CLUSTERING = "clustering"
    ANALYZING = "analyzing"
    RENDERING = "rendering"
    WRITING = "writing"
    COMPLETED = "completed"


ProgressCallback = Callable[[PipelineStage, Any, int, int], None]


class PipelineResult(BaseModel):
    """Final result bundle returned by CodegraphEngine."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    graph: nx.DiGraph
    files: List[Tuple[Path, str]]
    components: Dict[int, List[str]]
    cohesion_scores: Dict[int, float]
    component_names: Dict[int, str]
    analysis: AnalysisResult
    parse_errors: List[str] = []


@dataclass
class PipelineContext:
    """Mutable context state passed between pipeline execution stages."""

    config: CodegraphConfig
    progress_callback: Optional[ProgressCallback] = None

    files: list[tuple[Path, str]] = field(default_factory=list)
    extractions: list[ExtractionResult] = field(default_factory=list)
    dirty_files: set[str] = field(default_factory=set)
    parse_errors: list[str] = field(default_factory=list)
    cache_entries: dict[str, CacheEntry] = field(default_factory=dict)
    cache_path: Path | None = None

    graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    components: dict[int, list[str]] = field(default_factory=dict)
    cohesion_scores: dict[int, float] = field(default_factory=dict)
    component_names: dict[int, str] = field(default_factory=dict)
    analysis: AnalysisResult | None = None

    rendered_nodes: dict[str, str] = field(default_factory=dict)
    rendered_components: dict[str, str] = field(default_factory=dict)
    new_signatures: dict[str, str] = field(default_factory=dict)
    readme_content: str = ""
    prompt_content: str = ""


def notify(
    ctx: PipelineContext,
    stage: PipelineStage,
    item: Any = None,
    idx: int = 0,
    total: int = 0,
) -> None:
    """Emits progress notification if callback is registered."""
    if ctx.progress_callback:
        ctx.progress_callback(stage, item, idx, total)


# ── Multiprocessing & Hash Workers ──────────────────────────────────────────


def _get_worker_parser(lang: str) -> Any:
    parser = _WORKER_PARSER_CACHE.get(lang)
    if parser is None:
        parser = get_parser(lang)
        _WORKER_PARSER_CACHE[lang] = parser
    return parser


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
    """Worker function for single-file parallel parsing."""
    try:
        parser = _get_worker_parser(lang)
        result = parser.parse_file(file_path, workspace_dir)
        return file_path, result, None
    except Exception as e:
        import traceback

        err_msg = f"{e}\n{traceback.format_exc()}"
        return file_path, None, err_msg


def _parse_chunk_worker(
    chunk: list[tuple[Path, str, str, float, int, str, int]],
    workspace_dir: Path,
) -> list[
    tuple[Path, str, float, int, str, int, Optional[ExtractionResult], Optional[str]]
]:
    """Worker function for chunked parallel file parsing."""
    results = []
    for file_path, lang, rel_path, mtime, size, file_hash, mtime_ns in chunk:
        try:
            parser = _get_worker_parser(lang)
            res = parser.parse_file(file_path, workspace_dir)
            results.append(
                (file_path, rel_path, mtime, size, file_hash, mtime_ns, res, None)
            )
        except Exception as e:
            import traceback

            err_msg = f"{e}\n{traceback.format_exc()}"
            results.append(
                (file_path, rel_path, mtime, size, file_hash, mtime_ns, None, err_msg)
            )
    return results


def _load_cache_entries(cache_path: Path) -> dict[str, CacheEntry]:
    """Loads cache entries using fast adapter with standard JSON fallback."""
    cache_entries: dict[str, CacheEntry] = {}
    if not cache_path.exists():
        return cache_entries
    try:
        raw_bytes = cache_path.read_bytes()
        if raw_bytes:
            cache_entries = _CACHE_ADAPTER.validate_json(raw_bytes)
            logger.info(f"Loaded {len(cache_entries)} cache entries.")
    except Exception as e:
        logger.warning(f"Could not load cache with fast adapter, trying fallback: {e}")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                for k, v in cache_data.items():
                    cache_entries[k] = CacheEntry(**v)
            logger.info(f"Loaded {len(cache_entries)} cache entries via fallback.")
        except Exception as e2:
            logger.warning(f"Could not load cache: {e2}")
    return cache_entries


# ── Pipeline Stage Runners ──────────────────────────────────────────────────


def run_stage_discover(ctx: PipelineContext, discover_fn: Any = None) -> bool:
    """Stage 1: Discovers all matching source files in workspace."""
    notify(ctx, PipelineStage.DISCOVERING)
    fn = discover_fn if discover_fn is not None else discover_files
    ctx.files = fn(
        ctx.config.workspace_dir,
        ctx.config.languages,
        ctx.config.exclusions,
        ctx.config.include_dirs,
    )
    if not ctx.files:
        logger.warning("No supported files found.")
        notify(ctx, PipelineStage.COMPLETED)
        return False
    return True


def parse_files_with_cache(
    config: CodegraphConfig,
    files: list[tuple[Path, str]],
    cache_path: Path,
    progress_callback: Optional[ProgressCallback] = None,
) -> tuple[list[ExtractionResult], set[str], dict[str, CacheEntry], list[str]]:
    """Parses source files with caching and parallel worker pools."""
    total_files = len(files)
    cache_entries = _load_cache_entries(cache_path) if config.use_cache else {}

    extractions: list[ExtractionResult] = []
    files_to_parse: list[tuple[Path, str, str, float, int, str, int]] = []
    new_cache_entries: dict[str, CacheEntry] = {}
    dirty_files: set[str] = set()
    parse_errors: list[str] = []

    for file_path, lang in files:
        rel_path = str(file_path.relative_to(config.workspace_dir))
        try:
            stat = file_path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
            mtime_ns = getattr(stat, "st_mtime_ns", int(mtime * 1e9))

            if rel_path in cache_entries:
                entry = cache_entries[rel_path]
                mtime_matches = (
                    (entry.mtime_ns == mtime_ns)
                    if (entry.mtime_ns is not None)
                    else (entry.mtime == mtime)
                )
                if mtime_matches and entry.size == size:
                    extractions.append(entry.result)
                    if entry.mtime_ns is None:
                        entry.mtime_ns = mtime_ns
                    new_cache_entries[rel_path] = entry
                    continue

            file_hash = get_file_hash(file_path)
            dirty_files.add(rel_path)
            files_to_parse.append(
                (file_path, lang, rel_path, mtime, size, file_hash, mtime_ns)
            )
        except Exception as e:
            logger.error(f"Error accessing file metadata for {file_path}: {e}")
            dirty_files.add(rel_path)
            files_to_parse.append((file_path, lang, rel_path, 0.0, 0, "", 0))

    removed_files = set(cache_entries.keys()) - {
        str(fp.relative_to(config.workspace_dir)) for fp, _ in files
    }
    dirty_files |= removed_files

    num_hits = total_files - len(files_to_parse)
    if num_hits > 0:
        logger.info(f"Cache hit: {num_hits} / {total_files} files loaded from cache.")
    if dirty_files:
        logger.info(
            "Dirty / re-parsed files: %s (removed=%s)",
            len(dirty_files),
            len(removed_files),
        )

    if not files_to_parse:
        if progress_callback:
            progress_callback(PipelineStage.PARSING, None, total_files, total_files)
        return extractions, dirty_files, new_cache_entries, parse_errors

    max_workers = config.max_workers
    if max_workers > 1 and len(files_to_parse) > 1:
        logger.info(
            f"Parsing {len(files_to_parse)} files in parallel with {max_workers} workers..."
        )
        chunksize = max(1, min(64, len(files_to_parse) // (max_workers * 4)))
        chunks = [
            files_to_parse[i : i + chunksize]
            for i in range(0, len(files_to_parse), chunksize)
        ]

        processed_count = 0
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers
        ) as executor:
            futures = {
                executor.submit(
                    _parse_chunk_worker,
                    chunk,
                    config.workspace_dir,
                ): chunk
                for chunk in chunks
            }

            for future in concurrent.futures.as_completed(futures):
                chunk_results = future.result()
                for (
                    file_path,
                    rel_path,
                    mtime,
                    size,
                    file_hash,
                    mtime_ns,
                    result,
                    err_msg,
                ) in chunk_results:
                    processed_count += 1
                    progress_idx = num_hits + processed_count
                    if progress_callback:
                        progress_callback(
                            PipelineStage.PARSING,
                            file_path,
                            progress_idx,
                            total_files,
                        )
                    if err_msg:
                        msg = f"{rel_path}: {err_msg}"
                        parse_errors.append(msg)
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
                                mtime_ns=mtime_ns,
                            )
    else:
        logger.info(f"Parsing {len(files_to_parse)} files sequentially...")
        for idx, (
            file_path,
            lang,
            rel_path,
            mtime,
            size,
            file_hash,
            mtime_ns,
        ) in enumerate(files_to_parse, start=1):
            progress_idx = num_hits + idx
            if progress_callback:
                progress_callback(
                    PipelineStage.PARSING, file_path, progress_idx, total_files
                )
            try:
                parser = get_parser(lang)
                result = parser.parse_file(file_path, config.workspace_dir)
                extractions.append(result)
                if file_hash:
                    new_cache_entries[rel_path] = CacheEntry(
                        mtime=mtime,
                        size=size,
                        hash=file_hash,
                        result=result,
                        mtime_ns=mtime_ns,
                    )
            except Exception as e:
                msg = f"{rel_path}: {e}"
                parse_errors.append(msg)
                logger.error(f"Error parsing file {file_path}: {e}")

    if parse_errors:
        logger.warning(
            "Parse completed with %s error(s). First: %s",
            len(parse_errors),
            parse_errors[0][:200],
        )

    return extractions, dirty_files, new_cache_entries, parse_errors


_parse_with_cache = parse_files_with_cache


def run_stage_parse(ctx: PipelineContext, parse_fn: Any = None) -> None:
    """Stage 2: Parses files with incremental cache and parallel worker pools."""
    config = ctx.config
    ctx.cache_path = config.absolute_output_dir / "cache.json"
    fn = parse_fn if parse_fn is not None else parse_files_with_cache

    (
        ctx.extractions,
        ctx.dirty_files,
        ctx.cache_entries,
        ctx.parse_errors,
    ) = fn(config, ctx.files, ctx.cache_path, ctx.progress_callback)


def run_stage_build_and_cluster(ctx: PipelineContext) -> bool:
    """Stage 3-5: Assembles reference graph, clusters components, and computes metrics.

    Returns True if cached pipeline snapshot was reused, False if graph was rebuilt.
    """
    config = ctx.config

    file_hashes = {
        rel: entry.hash for rel, entry in ctx.cache_entries.items() if entry.hash
    }
    include_names = (
        [str(p.relative_to(config.workspace_dir)) for p in config.include_dirs]
        if config.include_dirs
        else None
    )
    fingerprint = workspace_fingerprint(
        file_hashes,
        languages=config.languages,
        exclusions=config.exclusions,
        include_dirs=include_names,
        naming_mode=config.component_naming,
        exclude_tests_from_clustering=config.exclude_tests_from_clustering,
    )
    snapshot_path = config.absolute_output_dir / PIPELINE_SNAPSHOT_NAME
    reused_snapshot = False

    if config.use_cache and not ctx.dirty_files:
        snap = load_pipeline_snapshot(snapshot_path, fingerprint)
        if snap is not None:
            ctx.graph = snap.graph
            ctx.components = snap.components
            ctx.cohesion_scores = snap.cohesion_scores
            ctx.component_names = snap.component_names
            ctx.analysis = snap.analysis
            reused_snapshot = True
            ctx.graph.graph["pipeline_reused_snapshot"] = True
            logger.info("Skipped build/cluster/analyze (pipeline snapshot hit).")

    if not reused_snapshot:
        notify(ctx, PipelineStage.BUILDING)
        ctx.graph = build_graph(ctx.extractions, config.workspace_dir)

        notify(ctx, PipelineStage.CLUSTERING)
        (
            ctx.components,
            ctx.cohesion_scores,
            ctx.component_names,
        ) = detect_components(
            ctx.graph,
            exclude_tests_from_clustering=config.exclude_tests_from_clustering,
            naming_mode=config.component_naming,
        )

        notify(ctx, PipelineStage.ANALYZING)
        ctx.analysis = analyze_graph(ctx.graph, ctx.components)
        ctx.graph.graph["pipeline_reused_snapshot"] = False
    else:
        notify(ctx, PipelineStage.ANALYZING)

    return reused_snapshot


def _enforce_parse_errors(config: CodegraphConfig, parse_errors: list[str]) -> None:
    if not parse_errors:
        return
    summary = "; ".join(parse_errors[:5])
    if len(parse_errors) > 5:
        summary += f" … (+{len(parse_errors) - 5} more)"
    if config.strict:
        raise RuntimeError(
            f"Strict mode: {len(parse_errors)} file(s) failed to parse. {summary}"
        )


def _enforce_quality_gates(
    config: CodegraphConfig,
    resolution: ResolutionStats | None,
) -> None:
    if resolution is None:
        return
    res = resolution

    if config.min_resolve_rate is not None and res.resolve_rate < config.min_resolve_rate:
        raise RuntimeError(
            f"Edge resolve rate {res.resolve_rate:.1%} is below "
            f"min_resolve_rate={config.min_resolve_rate:.1%} "
            f"({res.resolved}/{res.attempted} resolved, {res.unresolved} unresolved)"
        )

    if config.max_unresolved_edges is not None and res.unresolved > config.max_unresolved_edges:
        raise RuntimeError(
            f"Unresolved edges {res.unresolved} exceed "
            f"max_unresolved_edges={config.max_unresolved_edges} "
            f"(resolve rate {res.resolve_rate:.1%})"
        )

    if (
        config.min_internal_resolve_rate is not None
        and res.internal_resolve_rate < config.min_internal_resolve_rate
    ):
        raise RuntimeError(
            f"Internal edge resolve rate {res.internal_resolve_rate:.1%} "
            f"is below min_internal_resolve_rate={config.min_internal_resolve_rate:.1%} "
            f"({res.internal_resolved}/{res.internal_attempted} internal, "
            f"{res.internal_unresolved} internal unresolved)"
        )

    if (
        config.max_internal_unresolved_edges is not None
        and res.internal_unresolved > config.max_internal_unresolved_edges
    ):
        raise RuntimeError(
            f"Internal unresolved edges {res.internal_unresolved} exceed "
            f"max_internal_unresolved_edges={config.max_internal_unresolved_edges} "
            f"(internal resolve rate {res.internal_resolve_rate:.1%})"
        )


def run_stage_analyze_and_check(ctx: PipelineContext) -> None:
    """Enforces strict parsing rules and CI resolution quality gates."""
    _enforce_parse_errors(ctx.config, ctx.parse_errors)
    if ctx.analysis is None:
        raise RuntimeError("Pipeline analysis stage produced no result")
    _enforce_quality_gates(ctx.config, ctx.analysis.resolution)


def _render_incremental(
    config: CodegraphConfig,
    renderer: MarkdownRenderer,
    G: nx.DiGraph,
    components: dict[int, list[str]],
    cohesion_scores: dict[int, float],
    component_names: dict[int, str],
    analysis: AnalysisResult,
    dirty_files: set[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], set[str], set[str]]:
    """Render node/component pages with signature-based skip when cache on."""
    node_component_map: dict[str, str] = {}
    for cid, members in components.items():
        comp_name = component_names.get(cid, f"Component {cid}")
        for member in members:
            node_component_map[member] = comp_name

    force_files = (
        import_dependents(G, dirty_files)
        if (config.use_cache and dirty_files)
        else set()
    )
    sig_path = config.absolute_output_dir / SIGNATURE_STORE_NAME
    prev_sigs = load_signature_store(sig_path) if config.use_cache else {}
    new_sigs: dict[str, str] = {}
    nodes_dir = config.absolute_output_dir / "nodes"
    comps_dir = config.absolute_output_dir / "components"

    rendered_nodes: dict[str, str] = {}
    skipped_nodes: set[str] = set()
    nodes_reused = 0
    nodes_rendered = 0
    for nid, ndata in G.nodes(data=True):
        fname = get_node_filename(nid)
        comp_name = node_component_map.get(nid, "None")
        sig = node_neighborhood_signature(G, nid, comp_name)
        new_sigs[f"node:{nid}"] = sig
        disk_path = nodes_dir / fname
        can_reuse = (
            config.use_cache
            and prev_sigs.get(f"node:{nid}") == sig
            and disk_path.is_file()
            and not should_force_render_node(G, nid, force_files)
        )
        if can_reuse:
            skipped_nodes.add(fname)
            nodes_reused += 1
            continue
        content = renderer.render_node_page(nid, ndata, G, node_component_map)
        rendered_nodes[fname] = content
        nodes_rendered += 1

    rendered_components: dict[str, str] = {}
    skipped_components: set[str] = set()
    comps_reused = 0
    comps_rendered = 0
    for cid, members in components.items():
        comp_name = component_names[cid]
        cohesion = cohesion_scores[cid]
        fname = get_component_filename(comp_name)
        inter = analysis.inter_comp_deps.get(cid, {})
        sig = component_signature(members, cohesion, comp_name, inter)
        new_sigs[f"comp:{comp_name}"] = sig
        disk_path = comps_dir / fname
        member_forced = any(
            should_force_render_node(G, m, force_files) for m in members
        )
        can_reuse = (
            config.use_cache
            and prev_sigs.get(f"comp:{comp_name}") == sig
            and disk_path.is_file()
            and not member_forced
        )
        if can_reuse:
            skipped_components.add(fname)
            comps_reused += 1
            continue
        content = renderer.render_component_page(
            cid,
            members,
            G,
            cohesion,
            comp_name,
            analysis.inter_comp_deps,
            component_names,
        )
        rendered_components[fname] = content
        comps_rendered += 1

    logger.info(
        "Incremental render: nodes %s rendered / %s reused; components %s / %s",
        nodes_rendered,
        nodes_reused,
        comps_rendered,
        comps_reused,
    )
    G.graph["render_stats"] = {
        "nodes_rendered": nodes_rendered,
        "nodes_reused": nodes_reused,
        "components_rendered": comps_rendered,
        "components_reused": comps_reused,
        "dirty_files": len(dirty_files),
        "force_files": len(force_files),
    }
    G.graph["_signature_store_path"] = str(sig_path)
    return (
        rendered_nodes,
        rendered_components,
        new_sigs,
        skipped_nodes,
        skipped_components,
    )


def _load_preserved_ai_insights(output_dir: Path) -> str | None:
    readme_path = output_dir / "README.md"
    if not readme_path.exists():
        return None
    try:
        old_readme = readme_path.read_text(encoding="utf-8")
        marker = None
        for m in (
            "## AI Architectural Insights",
            "## AI 架构深度洞察 (AI Architectural Insights)",
            "## AI 架构深度洞察",
        ):
            if m in old_readme:
                marker = m
                break
        if marker:
            parts = old_readme.split(marker, 1)
            insights_text = parts[1].strip()
            if insights_text:
                return insights_text
    except Exception as e:
        logger.warning(f"Could not read existing README.md to preserve AI insights: {e}")
    return None


def run_stage_render(
    ctx: PipelineContext, renderer: MarkdownRenderer
) -> tuple[set[str], set[str]]:
    """Stage 6: Renders Markdown documentation pages with incremental diffing."""
    notify(ctx, PipelineStage.RENDERING)
    if ctx.analysis is None:
        raise RuntimeError("Pipeline analysis stage missing")

    (
        ctx.rendered_nodes,
        ctx.rendered_components,
        ctx.new_signatures,
        skipped_nodes,
        skipped_components,
    ) = _render_incremental(
        ctx.config,
        renderer,
        ctx.graph,
        ctx.components,
        ctx.cohesion_scores,
        ctx.component_names,
        ctx.analysis,
        ctx.dirty_files,
    )

    ai_insights = _load_preserved_ai_insights(ctx.config.absolute_output_dir)
    ctx.readme_content = renderer.render_readme(
        ctx.graph,
        ctx.components,
        ctx.cohesion_scores,
        ctx.component_names,
        ctx.analysis,
        ai_insights=ai_insights,
    )
    ctx.prompt_content = renderer.render_agent_prompt(
        ctx.graph,
        ctx.components,
        ctx.cohesion_scores,
        ctx.component_names,
        ctx.analysis,
        language=ctx.config.report_language,
    )
    return skipped_nodes, skipped_components


def _export_json_if_enabled(
    config: CodegraphConfig,
    G: nx.DiGraph,
    components: dict[int, list[str]],
    cohesion_scores: dict[int, float],
    component_names: dict[int, str],
    analysis: AnalysisResult,
) -> None:
    if not getattr(config, "export_json", True):
        return
    try:
        from codegraph_gen.export_json import graph_to_export_dict, write_graph_json

        payload = graph_to_export_dict(
            G,
            components,
            cohesion_scores,
            component_names,
            analysis,
        )
        write_graph_json(config.absolute_output_dir / "graph.json", payload)
        logger.info("Wrote graph.json export.")
    except Exception as e:
        logger.warning(f"Could not write graph.json: {e}")


def _save_cache_and_signatures(
    config: CodegraphConfig,
    cache_path: Path | None,
    new_cache_entries: dict[str, CacheEntry],
    new_sigs: dict[str, str],
) -> None:
    if not config.use_cache or cache_path is None:
        return
    try:
        config.absolute_output_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(_CACHE_ADAPTER.dump_json(new_cache_entries))
        logger.info(f"Saved {len(new_cache_entries)} cache entries.")
        sig_path = config.absolute_output_dir / SIGNATURE_STORE_NAME
        save_signature_store(sig_path, new_sigs)
    except Exception as e:
        logger.warning(f"Could not save cache: {e}")


def run_stage_write(
    ctx: PipelineContext,
    writer: VaultWriter,
    skipped_nodes: set[str],
    skipped_components: set[str],
    reused_snapshot: bool,
) -> None:
    """Stage 7: Writes vault pages, export JSON, cache, and pipeline snapshots to disk."""
    config = ctx.config
    notify(ctx, PipelineStage.WRITING)

    write_stats = writer.write_vault(
        config.absolute_output_dir,
        ctx.rendered_nodes,
        ctx.rendered_components,
        ctx.readme_content,
        ctx.prompt_content,
        skipped_nodes=skipped_nodes,
        skipped_components=skipped_components,
    )
    ctx.graph.graph["vault_write_stats"] = {
        "written": write_stats.written,
        "skipped": write_stats.skipped,
        "removed": write_stats.removed,
    }
    if ctx.parse_errors:
        ctx.graph.graph["parse_errors"] = list(ctx.parse_errors)

    if ctx.analysis is not None:
        _export_json_if_enabled(
            config,
            ctx.graph,
            ctx.components,
            ctx.cohesion_scores,
            ctx.component_names,
            ctx.analysis,
        )

    _save_cache_and_signatures(
        config, ctx.cache_path, ctx.cache_entries, ctx.new_signatures
    )

    snapshot_path = config.absolute_output_dir / PIPELINE_SNAPSHOT_NAME
    if config.use_cache and not reused_snapshot and ctx.analysis is not None:
        file_hashes = {
            rel: entry.hash for rel, entry in ctx.cache_entries.items() if entry.hash
        }
        include_names = (
            [str(p.relative_to(config.workspace_dir)) for p in config.include_dirs]
            if config.include_dirs
            else None
        )
        fingerprint = workspace_fingerprint(
            file_hashes,
            languages=config.languages,
            exclusions=config.exclusions,
            include_dirs=include_names,
            naming_mode=config.component_naming,
            exclude_tests_from_clustering=config.exclude_tests_from_clustering,
        )
        try:
            save_pipeline_snapshot(
                snapshot_path,
                fingerprint=fingerprint,
                G=ctx.graph,
                components=ctx.components,
                cohesion_scores=ctx.cohesion_scores,
                component_names=ctx.component_names,
                analysis=ctx.analysis,
            )
        except Exception as e:
            logger.warning("Could not write pipeline snapshot: %s", e)

    notify(ctx, PipelineStage.COMPLETED)
    logger.info(
        "Pipeline executed successfully (parse_errors=%s, snapshot_reused=%s).",
        len(ctx.parse_errors),
        reused_snapshot,
    )


def to_pipeline_result(ctx: PipelineContext) -> PipelineResult:
    """Transforms completed pipeline context into a PipelineResult model."""
    if ctx.analysis is None:
        raise RuntimeError("Pipeline analysis stage missing")
    return PipelineResult(
        graph=ctx.graph,
        files=ctx.files,
        components=ctx.components,
        cohesion_scores=ctx.cohesion_scores,
        component_names=ctx.component_names,
        analysis=ctx.analysis,
        parse_errors=ctx.parse_errors,
    )


def empty_pipeline_result() -> PipelineResult:
    """Creates an empty PipelineResult when no source files are discovered."""
    return PipelineResult(
        graph=nx.DiGraph(),
        files=[],
        components={},
        cohesion_scores={},
        component_names={},
        analysis=AnalysisResult(god_nodes=[], cycles=[], inter_comp_deps={}),
        parse_errors=[],
    )

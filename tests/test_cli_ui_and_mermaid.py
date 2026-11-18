from pathlib import Path
import networkx as nx

from codegraph_gen.analyzer import AnalysisResult, ResolutionStats
from codegraph_gen.cli_ui import (
    pipeline_progress_context,
    print_build_summary,
    print_components_table,
    print_config_summary,
    print_empty_graph_warning,
    print_quality_gate_error,
    print_vault_tips,
    update_pipeline_progress,
)
from codegraph_gen.config import CodegraphConfig, ProjectConfig
from codegraph_gen.engine import (
    PipelineResult,
    PipelineStage,
    RenderStats,
    VaultWriteStats,
)
from codegraph_gen.mermaid_util import (
    build_mermaid_call_edge,
    render_component_flowchart,
    render_node_call_graph_block,
    sanitize_mermaid_id,
    sanitize_mermaid_label,
)


def test_mermaid_util_sanitization_and_edges():
    assert sanitize_mermaid_id("foo/bar:baz.py") == "foo_bar_baz_py"
    assert (
        sanitize_mermaid_label('Hello "World" \\ Test') == 'Hello \\"World\\" \\\\ Test'
    )

    edge = build_mermaid_call_edge("src.a", "ClassA", "src.b", 'Method"B"')
    assert 'src_a["ClassA"] --> src_b["Method\\"B\\""]' in edge


def test_mermaid_util_node_calls_and_clipping():
    empty_block = render_node_call_graph_block([])
    assert empty_block == []

    calls = [f"  node_{i} --> node_{i + 1}" for i in range(10)]
    rendered = render_node_call_graph_block(calls, max_edges=5)
    assert rendered[0] == "```mermaid"
    assert rendered[1] == "flowchart TD"
    assert any("omitted for readability" in line for line in rendered)
    assert rendered[-2] == "```"


def test_mermaid_util_component_flowchart():
    deps = {0: {1: 5, 2: 2}}
    names = {0: "Core", 1: "Parser", 2: "Writer"}

    flowchart = render_component_flowchart(deps, names)
    assert "flowchart TD" in flowchart
    assert 'comp_0["Core"] -->|5| comp_1["Parser"]' in flowchart
    assert 'comp_0["Core"] -->|2| comp_2["Writer"]' in flowchart

    # Empty relations
    empty_fc = render_component_flowchart({}, names)
    assert 'comp_0["Core"]' in empty_fc
    assert "-->" not in empty_fc

    # Clipped relations
    many_deps = {i: {i + 1: 1} for i in range(10)}
    many_names = {i: f"Comp_{i}" for i in range(11)}
    clipped_fc = render_component_flowchart(many_deps, many_names, max_edges=3)
    assert "displayed" in clipped_fc


def test_cli_ui_progress_and_prints(tmp_path: Path):
    with pipeline_progress_context() as (progress, task):
        for stage in (
            PipelineStage.DISCOVERING,
            PipelineStage.PARSING,
            PipelineStage.BUILDING,
            PipelineStage.CLUSTERING,
            PipelineStage.ANALYZING,
            PipelineStage.RENDERING,
            PipelineStage.WRITING,
            PipelineStage.COMPLETED,
        ):
            update_pipeline_progress(progress, task, stage, Path("test.py"), 1, 10)

    print_empty_graph_warning()
    print_quality_gate_error(RuntimeError("Sample failure"))
    print_vault_tips(tmp_path)

    pcfg = ProjectConfig(exclude=["vendor"], languages=["python"])
    print_config_summary(tmp_path, pcfg, [tmp_path / "src"], {"python"})

    G = nx.DiGraph()
    G.add_node("file.py", type="file")
    G.add_node("file.py:func", type="function")
    G.add_edge("file.py:func", "file.py")

    stats = ResolutionStats(
        attempted=10,
        resolved=10,
        unresolved=0,
        by_category={"internal": {"attempted": 10, "resolved": 10, "unresolved": 0}},
    )
    result = PipelineResult(
        graph=G,
        files=[(tmp_path / "file.py", "python")],
        components={0: ["file.py", "file.py:func"]},
        cohesion_scores={0: 1.0},
        component_names={0: "Core"},
        analysis=AnalysisResult(
            god_nodes=[], cycles=[], inter_comp_deps={}, resolution=stats
        ),
        parse_errors=["sample_error.py: error"],
        write_stats=VaultWriteStats(written=2, skipped=0, removed=0),
        render_stats=RenderStats(
            nodes_rendered=2,
            nodes_reused=0,
            components_rendered=1,
            components_reused=0,
            dirty_files=1,
        ),
        reused_snapshot=True,
    )
    cfg = CodegraphConfig(
        workspace_dir=tmp_path,
        output_dir=tmp_path / ".codegraph",
        export_json=True,
    )
    print_build_summary(result, cfg)
    print_components_table(result)

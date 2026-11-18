"""Tests for signature-based incremental rendering and visitor mixin wiring."""

from pathlib import Path

import networkx as nx

from codegraph_gen.config import CodegraphConfig
from codegraph_gen.engine import CodegraphEngine
from codegraph_gen.incremental import (
    component_signature,
    import_dependents,
    node_neighborhood_signature,
)
from codegraph_gen.parser.common import VisitorMixin
from codegraph_gen.parser.go import GoVisitor
from codegraph_gen.parser.javascript import JavaScriptVisitor
from codegraph_gen.parser.rust import RustVisitor
from codegraph_gen.parser.swift import SwiftVisitor


def test_visitors_use_mixin():
    for cls in (GoVisitor, RustVisitor, SwiftVisitor, JavaScriptVisitor):
        assert issubclass(cls, VisitorMixin)


def test_node_signature_changes_with_edges():
    G = nx.DiGraph()
    G.add_node(
        "a",
        label="a",
        type="function",
        source_file="a.py",
        line_start=1,
        line_end=1,
        signature="",
        docstring="",
    )
    G.add_node(
        "b",
        label="b",
        type="function",
        source_file="a.py",
        line_start=2,
        line_end=2,
        signature="",
        docstring="",
    )
    s1 = node_neighborhood_signature(G, "a", "comp")
    G.add_edge("a", "b", relation="calls")
    s2 = node_neighborhood_signature(G, "a", "comp")
    assert s1 != s2


def test_import_dependents():
    G = nx.DiGraph()
    G.add_edge("a.py", "b.py", relation="imports")
    G.add_edge("c.py", "a.py", relation="imports")
    G.add_edge("x", "y", relation="calls")
    affected = import_dependents(G, {"a.py"})
    assert "a.py" in affected
    assert "b.py" in affected
    assert "c.py" in affected
    assert "x" not in affected


def test_component_signature_stable():
    s1 = component_signature(["m1", "m2"], 0.5, "pkg", {2: 3})
    s2 = component_signature(["m2", "m1"], 0.5, "pkg", {2: 3})
    assert s1 == s2
    s3 = component_signature(["m1", "m2"], 0.6, "pkg", {2: 3})
    assert s1 != s3


def test_engine_reuses_node_pages_on_second_build(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.py").write_text(
        "def greet():\n    return 'hi'\n",
        encoding="utf-8",
    )
    out = tmp_path / ".codegraph"
    config = CodegraphConfig(
        workspace_dir=tmp_path,
        output_dir=out,
        languages={"python"},
        max_workers=1,
        use_cache=True,
        include_dirs=[src],
        export_json=False,
    )
    engine = CodegraphEngine()
    r1 = engine.run_pipeline(config)
    assert r1.graph.number_of_nodes() > 0
    assert r1.render_stats is not None
    assert r1.render_stats.nodes_rendered > 0

    r2 = engine.run_pipeline(config)
    assert r2.render_stats is not None
    # Second build should reuse most node pages
    assert r2.render_stats.nodes_reused >= 1
    assert r2.render_stats.dirty_files == 0

    # Touch file → dirty + re-render some pages
    (src / "hello.py").write_text(
        "def greet():\n    return 'hello'\n\ndef other():\n    greet()\n",
        encoding="utf-8",
    )
    r3 = engine.run_pipeline(config)
    assert r3.render_stats is not None
    assert r3.render_stats.dirty_files >= 1
    assert r3.render_stats.nodes_rendered >= 1


def test_ai_insights_baseline_drift_notice_not_duplicated(tmp_path: Path):
    from codegraph_gen.pipeline_stages import _load_preserved_ai_insights

    src = tmp_path / "app.py"
    src.write_text("def a():\n    pass\n", encoding="utf-8")
    out = tmp_path / ".codegraph"
    out.mkdir(parents=True)
    (out / "README.md").write_text(
        "# Codebase Knowledge Graph\n\n## AI Architectural Insights\n"
        "> ⚠️ *Baseline drift notice: old*\n\n"
        "*Generated from graph stats: 99 files · 999 symbols · 100 edges.*\n",
        encoding="utf-8",
    )

    preserved = _load_preserved_ai_insights(out)
    assert preserved is not None
    assert "Baseline drift notice" not in preserved

    config = CodegraphConfig(
        workspace_dir=tmp_path,
        output_dir=out,
        languages={"python"},
        max_workers=1,
        use_cache=False,
        export_json=False,
    )
    CodegraphEngine().run_pipeline(config)
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert readme.count("Baseline drift notice") == 1

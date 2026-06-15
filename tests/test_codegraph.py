import tempfile
from pathlib import Path
import networkx as nx

from codegraph_gen.config import CodegraphConfig
from codegraph_gen.detect import discover_files
from codegraph_gen.parser.python import PythonParser
from codegraph_gen.builder import build_graph
from codegraph_gen.cluster import detect_components


def test_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        config = CodegraphConfig(workspace_dir=workspace)
        assert config.output_dir == Path(".codegraph")
        assert config.absolute_output_dir == workspace / ".codegraph"
        assert "venv" in config.exclusions


def test_detect():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        config = CodegraphConfig(workspace_dir=workspace)

        # Create dummy structure
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('hello')")
        (workspace / "src" / "test.ts").write_text("const a = 1;")

        # Excluded directory (.venv)
        (workspace / ".venv").mkdir()
        (workspace / ".venv" / "lib.py").write_text("print('ignore')")

        # Excluded case-insensitive directory (PODS -> Pods)
        (workspace / "PODS").mkdir()
        (workspace / "PODS" / "dep.py").write_text("print('ignore')")

        # Newly added exclusions: DerivedData, target, build_ios, .build
        (workspace / "DerivedData").mkdir()
        (workspace / "DerivedData" / "x.py").write_text("print('ignore')")

        (workspace / "target").mkdir()
        (workspace / "target" / "y.py").write_text("print('ignore')")

        (workspace / "build_ios").mkdir()
        (workspace / "build_ios" / "z.py").write_text("print('ignore')")

        (workspace / ".build").mkdir()
        (workspace / ".build" / "w.swift").write_text("print('ignore')")

        # Unsupported file extension
        (workspace / "src" / "doc.txt").write_text("some text")

        files = discover_files(
            config.workspace_dir, config.languages, config.exclusions
        )
        paths = [p for p, _ in files]

        assert len(files) == 2
        assert workspace / "src" / "main.py" in paths
        assert workspace / "src" / "test.ts" in paths
        assert workspace / ".venv" / "lib.py" not in paths
        assert workspace / "PODS" / "dep.py" not in paths
        assert workspace / "DerivedData" / "x.py" not in paths
        assert workspace / "target" / "y.py" not in paths
        assert workspace / "build_ios" / "z.py" not in paths
        assert workspace / ".build" / "w.swift" not in paths
        assert workspace / "src" / "doc.txt" not in paths


def test_python_parser():
    parser = PythonParser()
    code = b"""
def my_func(a, b):
    \"\"\"This is a test docstring.\"\"\"
    return a + b

class MyClass:
    def method(self, x):
        pass
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        file_path = workspace / "test.py"
        file_path.write_bytes(code)

        res = parser.parse_file(file_path, workspace)

        # Verify node counts
        # Expecting: 1 file, 1 function, 1 class, 1 method = 4 nodes
        assert len(res.nodes) == 4

        # Check node types
        types = [n.type for n in res.nodes]
        assert "file" in types
        assert "function" in types
        assert "class" in types
        assert "method" in types

        # Check signatures and docstrings
        func_node = next(n for n in res.nodes if n.type == "function")
        assert func_node.label == "my_func"
        assert "my_func" in func_node.signature
        assert "test docstring" in func_node.docstring

        class_node = next(n for n in res.nodes if n.type == "class")
        assert class_node.label == "MyClass"

        # Check contains edges
        # Expecting contains: file -> class, file -> function, class -> method
        contains_relations = [e for e in res.edges if e.relation == "contains"]
        assert len(contains_relations) == 3


def test_builder_and_resolution():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # File A: defines a function and calls another function
        file_a = workspace / "module_a.py"
        file_a.write_text("""
from module_b import target_func
def caller_func():
    target_func()
""")

        # File B: defines target_func
        file_b = workspace / "module_b.py"
        file_b.write_text("""
def target_func():
    pass
""")

        # Run parsing
        py_parser = PythonParser()
        res_a = py_parser.parse_file(file_a, workspace)
        res_b = py_parser.parse_file(file_b, workspace)

        # Build graph
        G = build_graph([res_a, res_b], workspace)

        # Verify call edge resolution
        # 'module_a.py::caller_func' should call 'module_b.py::target_func'
        caller_id = "module_a.py::caller_func"
        target_id = "module_b.py::target_func"

        assert G.has_edge(caller_id, target_id)
        assert G.edges[caller_id, target_id]["relation"] == "calls"

        # Verify import edge resolution
        assert G.has_edge("module_a.py", "module_b.py")
        assert G.edges["module_a.py", "module_b.py"]["relation"] == "imports"


def test_clustering_and_exporter():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        output = workspace / ".codegraph"

        # Build a small dummy graph
        G = nx.DiGraph()
        G.add_node(
            "file_a.py",
            label="file_a.py",
            type="file",
            source_file="file_a.py",
            line_start=1,
            line_end=10,
            signature="",
            docstring="",
        )
        G.add_node(
            "file_b.py",
            label="file_b.py",
            type="file",
            source_file="file_b.py",
            line_start=1,
            line_end=10,
            signature="",
            docstring="",
        )
        G.add_node(
            "file_a.py::func_a",
            label="func_a",
            type="function",
            source_file="file_a.py",
            line_start=2,
            line_end=5,
            signature="def func_a()",
            docstring="Hello A",
        )
        G.add_node(
            "file_b.py::func_b",
            label="func_b",
            type="function",
            source_file="file_b.py",
            line_start=2,
            line_end=5,
            signature="def func_b()",
            docstring="Hello B",
        )

        G.add_edge("file_a.py", "file_a.py::func_a", relation="contains")
        G.add_edge("file_b.py", "file_b.py::func_b", relation="contains")
        G.add_edge("file_a.py::func_a", "file_b.py::func_b", relation="calls")

        # Test detect components
        components, cohesion, names = detect_components(G)
        assert len(components) > 0

        # Export using new decoupled classes
        from codegraph_gen.analyzer import analyze_graph
        from codegraph_gen.renderer import (
            MarkdownRenderer,
            get_node_filename,
            get_component_filename,
        )
        from codegraph_gen.writer import VaultWriter

        analysis = analyze_graph(G, components)
        renderer = MarkdownRenderer(workspace)
        writer = VaultWriter()

        node_component_map = {}
        for cid, members in components.items():
            comp_name = names.get(cid, f"Component {cid}")
            for member in members:
                node_component_map[member] = comp_name

        rendered_nodes = {}
        for nid, ndata in G.nodes(data=True):
            fname = get_node_filename(nid)
            content = renderer.render_node_page(nid, ndata, G, node_component_map)
            rendered_nodes[fname] = content

        rendered_components = {}
        for cid, members in components.items():
            comp_name = names[cid]
            cohesion_val = cohesion[cid]
            fname = get_component_filename(comp_name)
            content = renderer.render_component_page(
                cid,
                members,
                G,
                cohesion_val,
                comp_name,
                analysis.inter_comp_deps,
                names,
            )
            rendered_components[fname] = content

        readme_content = renderer.render_readme(
            G, components, cohesion, names, analysis
        )
        prompt_content = renderer.render_agent_prompt(
            G, components, cohesion, names, analysis
        )

        writer.write_vault(
            output, rendered_nodes, rendered_components, readme_content, prompt_content
        )

        # Verify files exist
        assert (output / "README.md").exists()
        assert (output / "nodes" / get_node_filename("file_a.py::func_a")).exists()
        assert (output / "nodes" / get_node_filename("file_a.py")).exists()
        assert len(list((output / "components").glob("*.md"))) > 0
        assert (output / "AGENT_PROMPT.md").exists()
        assert (output.parent / "AGENTS.md").exists()

        # Verify agent files contents
        agents_txt = (output.parent / "AGENTS.md").read_text()
        assert "Guidelines for AI Agents" in agents_txt

        prompt_txt = (output / "AGENT_PROMPT.md").read_text()
        assert "Codebase Architecture Analysis Prompt" in prompt_txt

        # Verify standard relative link in func_a
        func_a_md = (
            output / "nodes" / get_node_filename("file_a.py::func_a")
        ).read_text()
        # Just check if link format matches [label](target.md)
        assert ".md" in func_a_md
        assert "func_b" in func_a_md


def test_advanced_symbol_resolution():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # Build 3 files
        # file_a.py: imports file_b and calls:
        # 1. file_b's duplicate_func (directly)
        # 2. file_b's duplicate_func (module-qualified via file_b.duplicate_func)
        # 3. file_b's Class.method (class-qualified)
        file_a = workspace / "file_a.py"
        file_a.write_text("""
from file_b import duplicate_func
import file_b

def caller():
    duplicate_func()
    file_b.duplicate_func()
    file_b.SomeClass.some_method()
""")

        # file_b.py: defines duplicate_func and SomeClass
        file_b = workspace / "file_b.py"
        file_b.write_text("""
def duplicate_func():
    pass

class SomeClass:
    def some_method(self):
        pass
""")

        # file_c.py: defines a duplicate duplicate_func but not imported by file_a
        file_c = workspace / "file_c.py"
        file_c.write_text("""
def duplicate_func():
    pass
""")

        # Parse files using PythonParser
        from codegraph_gen.parser.python import PythonParser

        parser = PythonParser()
        res_a = parser.parse_file(file_a, workspace)
        res_b = parser.parse_file(file_b, workspace)
        res_c = parser.parse_file(file_c, workspace)

        # Build graph
        G = build_graph([res_a, res_b, res_c], workspace)

        # Let's verify resolution edges from caller node 'file_a.py::caller'
        caller_nid = "file_a.py::caller"
        target_b_func = "file_b.py::duplicate_func"
        target_c_func = "file_c.py::duplicate_func"
        target_b_method = "file_b.py::SomeClass.some_method"

        # Assert calls from caller:
        # It must call duplicate_func in file_b (NOT file_c)
        assert G.has_edge(caller_nid, target_b_func)

        # It must call some_method in file_b
        assert G.has_edge(caller_nid, target_b_method)

        # Confirm it has no call edge to file_c's duplicate_func (which is not imported)
        assert not G.has_edge(caller_nid, target_c_func)


def test_engine_pipeline_and_callbacks():
    from codegraph_gen.engine import CodegraphEngine, PipelineStage
    from codegraph_gen.config import CodegraphConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        # Create a dummy python file so there is at least one file to scan
        (workspace / "foo.py").write_text("def test():\n    pass\n")

        config = CodegraphConfig(workspace_dir=workspace)
        engine = CodegraphEngine()

        stages_seen = []

        def callback(stage, current_item, idx, total):
            stages_seen.append(stage)

        result = engine.run_pipeline(config, progress_callback=callback)

        # Verify that all stages were traversed in order
        assert PipelineStage.DISCOVERING in stages_seen
        assert PipelineStage.PARSING in stages_seen
        assert PipelineStage.BUILDING in stages_seen
        assert PipelineStage.CLUSTERING in stages_seen
        assert PipelineStage.ANALYZING in stages_seen
        assert PipelineStage.RENDERING in stages_seen
        assert PipelineStage.WRITING in stages_seen
        assert PipelineStage.COMPLETED in stages_seen

        # Verify result structure
        assert result.graph is not None
        assert len(result.files) == 1
        assert len(result.components) > 0
        assert len(result.cohesion_scores) > 0
        assert len(result.component_names) > 0
        assert result.analysis is not None


def test_weighted_louvain_containment():
    from codegraph_gen.cluster import detect_components

    # Construct a graph that would normally pull a symbol away under unweighted clustering:
    # file_a contains func_a (1 contains edge)
    # func_a has 4 call relationships to file_b's symbols (4 calls edges)
    # Under weighted clustering, contains=10.0 and calls=1.0, so func_a will remain with file_a.

    G = nx.DiGraph()
    G.add_node("file_a.py", type="file", label="file_a.py")
    G.add_node("file_a.py::func_a", type="function", label="func_a")
    G.add_node("file_b.py", type="file", label="file_b.py")
    G.add_node("file_b.py::func_b", type="function", label="func_b")
    G.add_node("file_b.py::func_c", type="function", label="func_c")
    G.add_node("file_b.py::func_d", type="function", label="func_d")
    G.add_node("file_b.py::func_e", type="function", label="func_e")

    # Physical containment
    G.add_edge("file_a.py", "file_a.py::func_a", relation="contains")
    G.add_edge("file_b.py", "file_b.py::func_b", relation="contains")
    G.add_edge("file_b.py", "file_b.py::func_c", relation="contains")
    G.add_edge("file_b.py", "file_b.py::func_d", relation="contains")
    G.add_edge("file_b.py", "file_b.py::func_e", relation="contains")

    # 4 calling dependencies from func_a to file_b symbols
    G.add_edge("file_a.py::func_a", "file_b.py::func_b", relation="calls")
    G.add_edge("file_a.py::func_a", "file_b.py::func_c", relation="calls")
    G.add_edge("file_a.py::func_a", "file_b.py::func_d", relation="calls")
    G.add_edge("file_a.py::func_a", "file_b.py::func_e", relation="calls")

    components, cohesion, names = detect_components(G)

    comp_a = None
    for cid, members in components.items():
        if "file_a.py" in members:
            comp_a = members
            break

    assert comp_a is not None
    assert "file_a.py::func_a" in comp_a
    assert "file_b.py" not in comp_a


def test_c_parser():
    from codegraph_gen.parser.cpp import CParser

    parser = CParser()
    code = b"""
#include "my_header.h"
#include <stdio.h>

struct Point {
    int x;
    int y;
};

void print_point(struct Point p) {
    printf("%d, %d", p.x, p.y);
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()
        file_path = workspace / "main.c"
        file_path.write_bytes(code)

        res = parser.parse_file(file_path, workspace)

        # Verify Node types
        # 1 file, 1 struct, 1 function
        assert len(res.nodes) == 3
        types = [n.type for n in res.nodes]
        assert "file" in types
        assert "struct" in types
        assert "function" in types

        # Verify edge counts
        # contains: file -> struct, file -> function
        # imports: file -> my_header.h, file -> stdio.h
        # calls: function -> printf
        contains_edges = [e for e in res.edges if e.relation == "contains"]
        imports_edges = [e for e in res.edges if e.relation == "imports"]
        calls_edges = [e for e in res.edges if e.relation == "calls"]

        assert len(contains_edges) == 2
        assert len(imports_edges) == 2
        assert len(calls_edges) == 1
        assert imports_edges[0].target == "my_header.h"
        assert calls_edges[0].target == "printf"


def test_cpp_parser_and_resolution():
    from codegraph_gen.parser.cpp import CppParser
    from codegraph_gen.builder import build_graph

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # my_class.h: defines class MyClass and method declaration
        header_path = workspace / "my_class.h"
        header_path.write_text("""
namespace MyNamespace {
    class MyClass {
    public:
        void hello();
    };
}
""")

        # my_class.cpp: includes header, defines out-of-line method
        source_path = workspace / "my_class.cpp"
        source_path.write_text("""
#include "my_class.h"

void MyNamespace::MyClass::hello() {
    // some implementation
}
""")

        # main.cpp: includes header, creates instance and calls method
        main_path = workspace / "main.cpp"
        main_path.write_text("""
#include "my_class.h"

int main() {
    MyNamespace::MyClass obj;
    obj.hello();
    return 0;
}
""")

        cpp_parser = CppParser()
        res_h = cpp_parser.parse_file(header_path, workspace)
        res_cpp = cpp_parser.parse_file(source_path, workspace)
        res_main = cpp_parser.parse_file(main_path, workspace)

        # Check parser output nodes
        # my_class.h should have class MyClass and namespace MyNamespace
        h_types = [n.type for n in res_h.nodes]
        assert "namespace" in h_types
        assert "class" in h_types

        # my_class.cpp should have out-of-line hello method
        cpp_funcs = [n for n in res_cpp.nodes if n.type == "method"]
        assert len(cpp_funcs) == 1
        assert cpp_funcs[0].label == "hello"
        assert cpp_funcs[0].id == "my_class.cpp::MyNamespace.MyClass.hello"

        # Build dependency graph
        G = build_graph([res_h, res_cpp, res_main], workspace)

        # Verify call resolution:
        # main.cpp's main should call hello method in my_class.cpp
        caller_nid = "main.cpp::main"
        target_nid = "my_class.cpp::MyNamespace.MyClass.hello"

        assert G.has_edge(caller_nid, target_nid)


def test_incremental_caching_and_parallel_pipeline():
    import json
    from codegraph_gen.engine import CodegraphEngine
    from codegraph_gen.config import CodegraphConfig
    import time

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # 1. Create two test python files
        file_a = workspace / "file_a.py"
        file_a.write_text("def func_a():\n    print('A')\n")

        file_b = workspace / "file_b.py"
        file_b.write_text("def func_b():\n    print('B')\n")

        # Configure to use parallel parsing with 2 workers and caching enabled
        config = CodegraphConfig(workspace_dir=workspace, max_workers=2, use_cache=True)
        engine = CodegraphEngine()

        # --- First Run (Cold Run: Cache Miss) ---
        res1 = engine.run_pipeline(config)
        cache_file = config.absolute_output_dir / "cache.json"

        # Assert cache was written and has 2 entries
        assert cache_file.exists()
        with open(cache_file, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
        assert len(cache_data) == 2
        assert "file_a.py" in cache_data
        assert "file_b.py" in cache_data

        # Check extraction results
        assert len(res1.files) == 2
        assert any(
            n.get("label") == "func_a"
            for nid, n in res1.graph.nodes(data=True)
            if n.get("type") == "function"
        )
        assert any(
            n.get("label") == "func_b"
            for nid, n in res1.graph.nodes(data=True)
            if n.get("type") == "function"
        )

        # --- Second Run (Hot Run: Cache Hit, No Changes) ---
        # Run again. It should read from cache and skip parsing.
        res2 = engine.run_pipeline(config)
        # Verify result is the same
        assert len(res2.files) == 2
        assert any(
            n.get("label") == "func_a"
            for nid, n in res2.graph.nodes(data=True)
            if n.get("type") == "function"
        )
        assert any(
            n.get("label") == "func_b"
            for nid, n in res2.graph.nodes(data=True)
            if n.get("type") == "function"
        )

        # --- Third Run (Incremental Run: One file modified) ---
        # Modify file_a.py. Since mtime resolution might be 1s on some systems, sleep a bit or modify content.
        # But wait, changing the content changes the size and MD5 hash, which is checked in our cache filter!
        # So it will trigger a cache miss for file_a.py, but file_b.py will remain a cache hit.
        time.sleep(0.01)  # small sleep
        file_a.write_text("def func_a_modified():\n    print('A modified')\n")

        res3 = engine.run_pipeline(config)
        assert len(res3.files) == 2
        # Verify func_a_modified is present
        assert any(
            n.get("label") == "func_a_modified"
            for nid, n in res3.graph.nodes(data=True)
            if n.get("type") == "function"
        )
        # Verify func_a is NOT present
        assert not any(
            n.get("label") == "func_a"
            for nid, n in res3.graph.nodes(data=True)
            if n.get("type") == "function"
        )
        # Verify func_b is still present (from cache)
        assert any(
            n.get("label") == "func_b"
            for nid, n in res3.graph.nodes(data=True)
            if n.get("type") == "function"
        )

        # Verify updated cache file
        with open(cache_file, "r", encoding="utf-8") as f:
            updated_cache = json.load(f)
        assert len(updated_cache) == 2
        assert "func_a_modified" in str(updated_cache["file_a.py"])


def test_ast_visitor_caching_and_pruning():
    import tree_sitter
    import tree_sitter_python
    from codegraph_gen.parser.base import ASTVisitor
    from codegraph_gen.schema import ExtractionResult

    code = b"""
# A comment here
class TargetClass:
    def method(self):
        # another comment
        pass
"""
    language = tree_sitter.Language(tree_sitter_python.language())
    parser = tree_sitter.Parser(language)
    tree = parser.parse(code)
    root = tree.root_node

    result = ExtractionResult()
    visited_types = []

    class DummyVisitor(ASTVisitor):
        def visit(self, node):
            visited_types.append(node.type)
            super().visit(node)

    visitor = DummyVisitor(code, "test_file.py", result)
    visitor.visit(root)

    # 1. Caching verification
    # _visitor_cache should map node types to resolved visitor functions
    assert len(visitor._visitor_cache) > 0
    assert "class_definition" in visitor._visitor_cache
    # The cache should route repeated types using the cached visitor
    cached_fn = visitor._visitor_cache["class_definition"]
    assert (
        cached_fn.__name__ == "visit_class_definition"
        or cached_fn.__name__ == "generic_visit"
    )

    # 2. Pruning & Safety Routing verification
    class MockNode:
        def __init__(self, node_type):
            self.type = node_type
            self.children = []
            self.start_point = (0, 0)
            self.end_point = (0, 0)

    # Test safety name routing
    mock_result = ExtractionResult()
    visitor2 = DummyVisitor(b"", "mock.py", mock_result)

    called_with = []

    def visit_some_dotted_name(node):
        called_with.append("dotted")

    def visit_some_hyphenated_name(node):
        called_with.append("hyphenated")

    setattr(visitor2, "visit_some_dotted_name", visit_some_dotted_name)
    setattr(visitor2, "visit_some_hyphenated_name", visit_some_hyphenated_name)

    node_dot = MockNode("some.dotted.name")
    node_hyphen = MockNode("some-hyphenated-name")

    visitor2.visit(node_dot)
    visitor2.visit(node_hyphen)

    assert "dotted" in called_with
    assert "hyphenated" in called_with


def test_cli_version():
    from click.testing import CliRunner
    from codegraph_gen.__main__ import cli, __version__

    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "codegraph, version" in result.output
    assert __version__ in result.output

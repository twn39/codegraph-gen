import tempfile
from pathlib import Path
import pytest
import networkx as nx

from codegraph.config import CodegraphConfig
from codegraph.detect import discover_files
from codegraph.parser.python import PythonParser
from codegraph.builder import build_graph
from codegraph.cluster import detect_components
from codegraph.exporter import to_markdown_vault, get_node_filename

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
        
        # Excluded directory
        (workspace / ".venv").mkdir()
        (workspace / ".venv" / "lib.py").write_text("print('ignore')")
        
        # Unsupported file extension
        (workspace / "src" / "doc.txt").write_text("some text")

        files = discover_files(config)
        paths = [p for p, _ in files]
        
        assert len(files) == 2
        assert workspace / "src" / "main.py" in paths
        assert workspace / "src" / "test.ts" in paths
        assert workspace / ".venv" / "lib.py" not in paths
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
        G.add_node("file_a.py", label="file_a.py", type="file", source_file="file_a.py", line_start=1, line_end=10, signature="", docstring="")
        G.add_node("file_b.py", label="file_b.py", type="file", source_file="file_b.py", line_start=1, line_end=10, signature="", docstring="")
        G.add_node("file_a.py::func_a", label="func_a", type="function", source_file="file_a.py", line_start=2, line_end=5, signature="def func_a()", docstring="Hello A")
        G.add_node("file_b.py::func_b", label="func_b", type="function", source_file="file_b.py", line_start=2, line_end=5, signature="def func_b()", docstring="Hello B")
        
        G.add_edge("file_a.py", "file_a.py::func_a", relation="contains")
        G.add_edge("file_b.py", "file_b.py::func_b", relation="contains")
        G.add_edge("file_a.py::func_a", "file_b.py::func_b", relation="calls")
        
        # Test detect components
        components, cohesion, names = detect_components(G)
        assert len(components) > 0
        
        # Export
        to_markdown_vault(G, components, cohesion, names, output)
        
        # Verify files exist
        assert (output / "README.md").exists()
        assert (output / "nodes" / get_node_filename("file_a.py::func_a")).exists()
        assert (output / "nodes" / get_node_filename("file_a.py")).exists()
        assert len(list((output / "components").glob("*.md"))) > 0
        assert (output / "AGENT_PROMPT.md").exists()
        assert (output.parent / "AGENTS.md").exists()
        
        # Verify agent files contents
        agents_txt = (output.parent / "AGENTS.md").read_text()
        assert "Rules for AI Agents" in agents_txt
        
        prompt_txt = (output / "AGENT_PROMPT.md").read_text()
        assert "Codebase Architecture Analysis Prompt" in prompt_txt
        
        # Verify standard relative link in func_a
        func_a_md = (output / "nodes" / get_node_filename("file_a.py::func_a")).read_text()
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
        from codegraph.parser.python import PythonParser
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


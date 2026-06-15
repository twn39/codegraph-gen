from typing import Any, cast
from codegraph_gen.parser.base import (
    ASTParsingContext,
    ASTVisitor,
    get_node_text,
    get_line_range,
)
from codegraph_gen.schema import ExtractionResult, NodeSchema, EdgeSchema


def test_ast_parsing_context():
    result = ExtractionResult()
    ctx = ASTParsingContext(b"class Test {}", "test.py", result)

    assert ctx.source == b"class Test {}"
    assert ctx.rel_path == "test.py"
    assert ctx.collector == result
    assert ctx.scope.current_id == "test.py"

    node = NodeSchema(
        id="test.py::Test",
        label="Test",
        type="class",
        source_file="test.py",
        line_start=1,
        line_end=1,
        signature="class Test",
    )
    ctx.add_node(node)
    assert len(result.nodes) == 1
    assert result.nodes[0] == node

    edge = EdgeSchema(source="test.py", target="test.py::Test", relation="contains")
    ctx.add_edge(edge)
    assert len(result.edges) == 1
    assert result.edges[0] == edge


class MockNode:
    def __init__(self, node_type, start_byte, end_byte, start_point, end_point):
        self.type = node_type
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = start_point
        self.end_point = end_point
        self.children = []


def test_stateless_helpers():
    source = b"  hello world  "
    node = MockNode("identifier", 2, 13, (0, 2), (0, 13))

    assert get_node_text(cast(Any, node), source) == "hello world"
    assert get_line_range(cast(Any, node)) == (1, 1)


def test_ast_visitor_composition():
    result = ExtractionResult()
    ctx = ASTParsingContext(b"content", "file.py", result)

    called_visits = []

    class MockHandler:
        traverser: ASTVisitor

        def visit_my_custom_node(self, node):
            called_visits.append("custom")

    handler = MockHandler()
    visitor = ASTVisitor(handler, ctx)

    # Verify binding
    assert handler.traverser == visitor
    assert visitor.ctx == ctx

    node = MockNode("my_custom_node", 0, 7, (0, 0), (0, 7))
    visitor.visit(cast(Any, node))

    assert called_visits == ["custom"]


def test_ast_visitor_inheritance_compatibility():
    result = ExtractionResult()
    called = []

    class LegacyVisitor(ASTVisitor):
        def visit_my_node(self, node):
            called.append("legacy")

    visitor = LegacyVisitor(b"code", "legacy.py", result)
    assert visitor.ctx is None
    assert visitor.source == b"code"
    assert visitor.rel_path == "legacy.py"

    node = MockNode("my_node", 0, 4, (0, 0), (0, 4))
    visitor.visit(cast(Any, node))

    assert called == ["legacy"]

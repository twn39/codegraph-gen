from abc import ABC, abstractmethod
import logging
from pathlib import Path
from pydantic import BaseModel
import tree_sitter

logger = logging.getLogger(__name__)


class NodeSchema(BaseModel):
    id: str  # Unique identifier, e.g. "relative_path::symbol_name"
    label: str  # Human readable name, e.g. "my_function"
    type: str  # 'file', 'class', 'function', 'method', 'struct', 'interface', 'trait', 'protocol'
    source_file: str  # Path relative to workspace
    line_start: int  # 1-indexed
    line_end: int  # 1-indexed
    signature: str  # Signature snippet
    docstring: str = ""  # Docstring or comments
    local_bindings: dict[
        str, str
    ] = {}  # Maps local variable/parameter name to its type name


class EdgeSchema(BaseModel):
    source: str  # Source node ID
    target: str  # Target node ID
    relation: str  # 'contains', 'imports', 'calls', 'inherits', 'implements'
    import_map: dict[str, str] = {}  # Maps local name to original symbol name


class ExtractionResult(BaseModel):
    nodes: list[NodeSchema] = []
    edges: list[EdgeSchema] = []


class BaseParser(ABC):
    """Abstract base class for all language-specific AST parsers."""

    @abstractmethod
    def parse_file(self, file_path: Path, workspace_dir: Path) -> ExtractionResult:
        """Parses a file and extracts symbols (nodes) and relations (edges)."""
        pass


class ASTVisitor:
    """Optimized base AST Visitor for dynamic routing and AST traversal."""

    def __init__(self, source: bytes, rel_path: str, result: ExtractionResult):
        self.source = source
        self.rel_path = rel_path
        self.result = result
        self._visitor_cache = {}
        self.scope_stack = [(rel_path, "file")]

    def visit(self, node: tree_sitter.Node) -> None:
        """Visits a node by dynamically routing to visit_NodeType."""
        if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
            logger.debug(f"Skipping syntax error node: {node}")
            return

        node_type = node.type
        visitor = self._visitor_cache.get(node_type)
        if visitor is None:
            # Replace characters invalid in Python identifiers
            safe_type = node_type.replace("-", "_").replace(".", "_")
            visitor = getattr(self, f"visit_{safe_type}", self.generic_visit)
            self._visitor_cache[node_type] = visitor

        try:
            visitor(node)
        except Exception as e:
            logger.error(
                f"Error visiting node of type {node.type} at line {node.start_point[0] + 1}: {e}",
                exc_info=True,
            )

    def generic_visit(self, node: tree_sitter.Node) -> None:
        """Default recursive traversal. Prunes known leaf nodes."""
        if node.type in (
            "string",
            "comment",
            "line_comment",
            "block_comment",
            "number",
            "true",
            "false",
            "null",
        ):
            return
        for child in node.children:
            self.visit(child)

    def get_text(self, node: tree_sitter.Node) -> str:
        """Helper to extract text from a node using the source bytes."""
        return (
            self.source[node.start_byte : node.end_byte]
            .decode("utf-8", errors="replace")
            .strip()
        )

    def get_line_range(self, node: tree_sitter.Node) -> tuple[int, int]:
        """Helper to extract 1-indexed line start and end points."""
        return node.start_point[0] + 1, node.end_point[0] + 1

    def get_current_parent_id(self) -> str:
        """Helper to retrieve the current parent scope's ID."""
        return self.scope_stack[-1][0] if self.scope_stack else self.rel_path

from abc import ABC, abstractmethod
import logging
from pathlib import Path
from typing import Any
import tree_sitter
from codegraph_gen.schema import (
    NodeSchema,
    EdgeSchema,
    ExtractionResult,
    SymbolCollector,
)

logger = logging.getLogger(__name__)


class BaseParser(ABC):
    """Abstract base class for all language-specific AST parsers."""

    @abstractmethod
    def parse_file(self, file_path: Path, workspace_dir: Path) -> ExtractionResult:
        """Parses a file and extracts symbols (nodes) and relations (edges)."""
        pass


_PARSER_REGISTRY: dict[str, type[BaseParser]] = {}


def register_parser(*languages: str):
    """Decorator to register a BaseParser subclass for one or more languages."""

    def decorator(cls: type[BaseParser]):
        for lang in languages:
            _PARSER_REGISTRY[lang.lower()] = cls
        return cls

    return decorator


class ScopeTracker:
    def __init__(self, initial_scope_id: str, initial_scope_type: str = "file"):
        self._stack: list[tuple[str, str]] = [(initial_scope_id, initial_scope_type)]

    def push(self, scope_id: str, scope_type: str) -> "ScopeTracker":
        """Pushes a scope onto the stack. Returns self to act as a context manager."""
        self._stack.append((scope_id, scope_type))
        return self

    def pop(self) -> tuple[str, str]:
        """Pops the innermost scope from the stack."""
        if len(self._stack) <= 1:
            raise IndexError("Cannot pop the root scope")
        return self._stack.pop()

    def __enter__(self) -> "ScopeTracker":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.pop()

    @property
    def current_id(self) -> str:
        return self._stack[-1][0] if self._stack else ""

    @property
    def current_type(self) -> str:
        return self._stack[-1][1] if self._stack else ""

    @property
    def stack(self) -> list[tuple[str, str]]:
        return self._stack

    def find_parent_by_type(self, type_name: str) -> str | None:
        """Searches the stack from innermost to outermost for a specific scope type."""
        for scope_id, scope_type in reversed(self._stack):
            if scope_type == type_name:
                return scope_id
        return None


class ASTParsingContext:
    """Carries parsing context state, accumulator targets and scope tracking."""

    def __init__(self, source: bytes, rel_path: str, collector: SymbolCollector):
        self.source = source
        self.rel_path = rel_path
        self.collector = collector
        self.scope = ScopeTracker(rel_path, "file")

    def add_node(self, node: NodeSchema) -> None:
        self.collector.add_node(node)

    def add_edge(self, edge: EdgeSchema) -> None:
        self.collector.add_edge(edge)


def get_node_text(node: tree_sitter.Node, source: bytes) -> str:
    """Stateless helper to extract text from a node using the source bytes."""
    return (
        source[node.start_byte : node.end_byte]
        .decode("utf-8", errors="replace")
        .strip()
    )


def get_line_range(node: tree_sitter.Node) -> tuple[int, int]:
    """Stateless helper to extract 1-indexed line start and end points."""
    return node.start_point[0] + 1, node.end_point[0] + 1


class ASTVisitor:
    """Optimized AST Traverser supporting both composition and inheritance."""

    def __init__(
        self,
        *args,
        handler: Any = None,
        ctx: ASTParsingContext = None,
        **kwargs,
    ):
        self._visitor_cache = {}

        # Detect if called with legacy positional arguments:
        # ASTVisitor(source: bytes, rel_path: str, collector: SymbolCollector)
        is_legacy = False
        if len(args) >= 1 and isinstance(args[0], bytes):
            is_legacy = True

        if is_legacy:
            source = args[0]
            rel_path = args[1] if len(args) > 1 else ""
            collector = args[2] if len(args) > 2 else None

            self.ctx = None
            self.source = source
            self.rel_path = rel_path
            self.collector = collector
            self.scope = ScopeTracker(rel_path, "file") if rel_path else None
            self.handler = self
        else:
            # Composition signature: ASTVisitor(handler, ctx)
            self.handler = (
                handler if handler is not None else (args[0] if len(args) > 0 else self)
            )
            self.ctx = ctx if ctx is not None else (args[1] if len(args) > 1 else None)

            if self.ctx is not None:
                self.source = self.ctx.source
                self.rel_path = self.ctx.rel_path
                self.collector = self.ctx.collector
                self.scope = self.ctx.scope
            else:
                self.source = b""
                self.rel_path = ""
                self.collector = None
                self.scope = None

        # Bind the traverser to the handler if using composition
        if self.handler is not self:
            self.handler.traverser = self

    def add_node(self, node: NodeSchema) -> None:
        if self.ctx is not None:
            self.ctx.add_node(node)
        elif self.collector is not None:
            self.collector.add_node(node)

    def add_edge(self, edge: EdgeSchema) -> None:
        if self.ctx is not None:
            self.ctx.add_edge(edge)
        elif self.collector is not None:
            self.collector.add_edge(edge)

    @property
    def scope_stack(self) -> list[tuple[str, str]]:
        """Deprecated: Use self.scope instead. Kept for backward compatibility."""
        return self.scope.stack if self.scope else []

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
            visitor = getattr(self.handler, f"visit_{safe_type}", self.generic_visit)
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
        return get_node_text(node, self.source)

    def get_line_range(self, node: tree_sitter.Node) -> tuple[int, int]:
        """Helper to extract 1-indexed line start and end points."""
        return get_line_range(node)

    def get_current_parent_id(self) -> str:
        """Helper to retrieve the current parent scope's ID."""
        return self.scope.current_id if self.scope else ""

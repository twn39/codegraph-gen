from abc import ABC, abstractmethod
from pathlib import Path
from pydantic import BaseModel

class NodeSchema(BaseModel):
    id: str                  # Unique identifier, e.g. "relative_path::symbol_name"
    label: str               # Human readable name, e.g. "my_function"
    type: str                # 'file', 'class', 'function', 'method', 'struct', 'interface', 'trait', 'protocol'
    source_file: str         # Path relative to workspace
    line_start: int          # 1-indexed
    line_end: int            # 1-indexed
    signature: str           # Signature snippet
    docstring: str = ""      # Docstring or comments

class EdgeSchema(BaseModel):
    source: str              # Source node ID
    target: str              # Target node ID
    relation: str            # 'contains', 'imports', 'calls', 'inherits', 'implements'

class ExtractionResult(BaseModel):
    nodes: list[NodeSchema] = []
    edges: list[EdgeSchema] = []

class BaseParser(ABC):
    """Abstract base class for all language-specific AST parsers."""
    
    @abstractmethod
    def parse_file(self, file_path: Path, workspace_dir: Path) -> ExtractionResult:
        """Parses a file and extracts symbols (nodes) and relations (edges)."""
        pass

from pydantic import BaseModel

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

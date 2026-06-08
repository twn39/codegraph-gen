import logging
from pathlib import Path
import networkx as nx
from codegraph.parser.base import ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)

def build_graph(extractions: list[ExtractionResult], workspace_dir: Path) -> nx.DiGraph:
    """
    Assembles a list of ExtractionResults into a single directed graph
    and resolves call, inherit, and import edges.
    """
    G = nx.DiGraph()
    
    # 1. Add all nodes to the graph
    for ext in extractions:
        for node in ext.nodes:
            G.add_node(node.id, **node.model_dump())
            
    # Node set for quick lookup
    node_ids = set(G.nodes)

    # 2. Build registries for symbol resolution
    # Map from simple label (e.g., 'login') -> list of node IDs
    global_symbol_map: dict[str, list[str]] = {}
    # Map from file relative path -> list of symbol node IDs defined in it
    file_symbols: dict[str, list[str]] = {}
    
    for nid, data in G.nodes(data=True):
        label = data.get("label")
        sf = data.get("source_file")
        if label:
            global_symbol_map.setdefault(label, []).append(nid)
        if sf:
            file_symbols.setdefault(sf, []).append(nid)

    # Helper: resolve local file path from Go/Python import targets
    # E.g., target "codegraph/parser" -> "src/codegraph/parser/__init__.py" or "src/codegraph/parser.py"
    def resolve_import_to_file_node(source_file: str, target: str) -> str | None:
        # 1. Clean relative paths
        if target.startswith("."):
            source_dir = Path(workspace_dir) / Path(source_file).parent
            try:
                resolved_path = (source_dir / target).resolve()
                rel_path = str(resolved_path.relative_to(workspace_dir))
                
                # Check directly or check with suffixes
                for suff in (".py", ".ts", ".js", ".go", ".rs", ".swift"):
                    check_path = rel_path + suff
                    if check_path in node_ids:
                        return check_path
                    check_init = str(Path(rel_path) / f"__init__{suff}")
                    if check_init in node_ids:
                        return check_init
                if rel_path in node_ids:
                    return rel_path
            except Exception:
                pass
                
        # 2. Check suffix matching
        # If target is "utils/helper" or "helper", check if there's any file node matching that path
        target_path_part = target.replace(".", "/")
        for nid in node_ids:
            if G.nodes[nid]["type"] == "file":
                if nid.replace("\\", "/").endswith(target_path_part) or \
                   nid.replace("\\", "/").endswith(target_path_part + ".py") or \
                   nid.replace("\\", "/").endswith(target_path_part + ".go") or \
                   nid.replace("\\", "/").endswith(target_path_part + ".rs"):
                    return nid
        return None

    # Helper: find target ID for a call/reference
    def resolve_symbol(caller_id: str, callee_name: str) -> str | None:
        caller_data = G.nodes.get(caller_id)
        if not caller_data:
            return None
        source_file = caller_data["source_file"]

        # Parse callee parts. Supports "." (Python/JS/Swift) and "::" (Go/Rust/C++)
        callee_clean = callee_name.replace("::", ".")
        parts = [p.strip() for p in callee_clean.split(".") if p.strip()]
        if not parts:
            return None
            
        main_symbol = parts[0]
        sub_symbol = parts[1] if len(parts) > 1 else None
        rest_of_callee = callee_clean.split(".", 1)[1] if len(parts) > 1 else ""

        # 1. Local Scope Check
        # Check if the symbol is defined locally in the caller's file
        local_target = f"{source_file}::{main_symbol}"
        if local_target in node_ids:
            if rest_of_callee:
                sub_target = f"{local_target}.{rest_of_callee}"
                if sub_target in node_ids:
                    return sub_target
                # Try just the class member
                sub_target = f"{local_target}.{parts[-1]}"
                if sub_target in node_ids:
                    return sub_target
            return local_target

        # 2. Self/Cls Check
        # Handle self, this, and cls method calls by resolving to the caller's parent class
        if main_symbol in ("self", "this", "cls"):
            if "." in caller_id:
                # caller is a method: e.g. "file_a.py::MyClass.run" -> parent "file_a.py::MyClass"
                parent_class_id = caller_id.rsplit(".", 1)[0]
                if rest_of_callee:
                    member_target = f"{parent_class_id}.{rest_of_callee}"
                    if member_target in node_ids:
                        return member_target
                    # Try just the last component
                    member_target = f"{parent_class_id}.{parts[-1]}"
                    if member_target in node_ids:
                        return member_target

        # Helper to extract the last component of a raw import target
        # e.g., "codegraph/parser" -> "parser", "codegraph.parser" -> "parser"
        def get_last_component(raw_target: str) -> str:
            cleaned = raw_target.replace("::", ".").replace("/", ".")
            subparts = [p.strip() for p in cleaned.split(".") if p.strip()]
            return subparts[-1] if subparts else ""

        # Fetch all files imported by this file
        imported_files = []
        for u, v, edata in G.edges(data=True):
            if u == source_file and edata.get("relation") == "imports":
                imported_files.append((v, edata))

        # 3. Import-Guided Module-Qualified Check
        # If calling module.Function() (e.g. parser.parse_file), check if the module matches
        # any imported file's name/stem or its raw import target's last component.
        if rest_of_callee:
            for imp_file, edata in imported_files:
                raw_target = edata.get("raw_target", "")
                last_comp = get_last_component(raw_target)
                stem = Path(imp_file).stem
                
                # Match check (case-insensitive)
                matched = False
                if (last_comp.lower() == main_symbol.lower() or 
                    stem.lower() == main_symbol.lower()):
                    matched = True
                elif stem.lower() in ("__init__", "index", "main"):
                    parent_name = Path(imp_file).parent.name
                    if parent_name.lower() == main_symbol.lower():
                        matched = True
                        
                if matched:
                    # Look for the rest of callee directly in the imported file
                    target_candidate = f"{imp_file}::{rest_of_callee}"
                    if target_candidate in node_ids:
                        return target_candidate
                    
                    # Fallback to the last symbol as a top-level node in the imported file
                    last_symbol = parts[-1]
                    target_candidate = f"{imp_file}::{last_symbol}"
                    if target_candidate in node_ids:
                        return target_candidate
                    
                    # Also look for any node in the imported file ending with .last_symbol (like class methods)
                    for nid in node_ids:
                        if G.nodes[nid].get("source_file") == imp_file and nid.endswith(f".{last_symbol}"):
                            return nid

        # 4. Import-Guided Symbol Check
        # If calling Function() directly, check if Function is defined in any imported file
        for imp_file, _ in imported_files:
            target_candidate = f"{imp_file}::{main_symbol}"
            if target_candidate in node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in node_ids:
                        return sub_target
                    sub_target = f"{target_candidate}.{parts[-1]}"
                    if sub_target in node_ids:
                        return sub_target
                return target_candidate

        # 5. Conservative Global Fallback
        # If no local or imported match, fallback to globally unique symbol
        search_label = parts[-1] if len(parts) > 1 else main_symbol
        candidates = global_symbol_map.get(search_label, [])
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            # Prefer candidate in the same parent directory (same package/namespace)
            caller_parent_dir = Path(source_file).parent
            near_candidates = [c for c in candidates if Path(G.nodes[c]["source_file"]).parent == caller_parent_dir]
            if len(near_candidates) == 1:
                return near_candidates[0]

        return None

    # 3. Process edges and add them resolved to the graph
    for ext in extractions:
        for edge in ext.edges:
            src = edge.source
            tgt = edge.target
            rel = edge.relation
            
            # Skip self-loops
            if src == tgt:
                continue

            # We must make sure the source node exists in our graph
            if src not in node_ids:
                continue

            resolved_tgt = None
            
            if rel == "contains":
                # Containment edges are already fully qualified by the parser
                if tgt in node_ids:
                    resolved_tgt = tgt
                    
            elif rel == "imports":
                # Try to resolve import target (which is a raw path/module name) to a file node ID
                source_file = G.nodes[src]["source_file"]
                resolved_tgt = resolve_import_to_file_node(source_file, tgt)
                
            elif rel in ("inherits", "implements"):
                # Inherits/implements targets are class/trait labels
                resolved_tgt = resolve_symbol(src, tgt)
                
            elif rel == "calls":
                # Calls targets are raw callee names
                resolved_tgt = resolve_symbol(src, tgt)

            # Add edge if resolved
            if resolved_tgt and resolved_tgt in node_ids:
                if rel == "imports":
                    G.add_edge(src, resolved_tgt, relation=rel, raw_target=tgt)
                else:
                    G.add_edge(src, resolved_tgt, relation=rel)
                
    return G

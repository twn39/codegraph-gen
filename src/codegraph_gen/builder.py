import logging
from pathlib import Path
import re
import networkx as nx
from codegraph_gen.parser.base import ExtractionResult

logger = logging.getLogger(__name__)

# Common builtin/standard library functions for languages to avoid call graph pollution
BUILTIN_FUNCTIONS: dict[str, set[str]] = {
    "python": {
        "print",
        "len",
        "range",
        "str",
        "int",
        "dict",
        "list",
        "set",
        "tuple",
        "open",
        "sum",
        "min",
        "max",
        "abs",
        "enumerate",
        "zip",
        "any",
        "all",
        "map",
        "filter",
        "super",
        "repr",
        "type",
        "isinstance",
        "issubclass",
        "dir",
        "id",
        "hash",
        "input",
    },
    "go": {
        "print",
        "println",
        "panic",
        "recover",
        "make",
        "new",
        "len",
        "cap",
        "append",
        "copy",
        "delete",
        "complex",
        "real",
        "imag",
        "close",
    },
    "javascript": {
        "console",
        "require",
        "module",
        "exports",
        "process",
        "window",
        "document",
        "eval",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "decodeURI",
        "encodeURI",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Date",
        "RegExp",
        "Error",
        "Map",
        "Set",
        "Promise",
        "JSON",
        "Math",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
    },
    "typescript": {
        "console",
        "require",
        "module",
        "exports",
        "process",
        "window",
        "document",
        "eval",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "decodeURI",
        "encodeURI",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Date",
        "RegExp",
        "Error",
        "Map",
        "Set",
        "Promise",
        "JSON",
        "Math",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
    },
    "rust": {
        "println!",
        "print!",
        "format!",
        "panic!",
        "vec!",
        "assert!",
        "assert_eq!",
        "Option",
        "Result",
        "Some",
        "None",
        "Ok",
        "Err",
        "Default",
    },
    "swift": {
        "print",
        "min",
        "max",
        "abs",
        "count",
        "fatalError",
        "precondition",
        "assert",
    },
    "kotlin": {
        "print",
        "println",
        "listOf",
        "mapOf",
        "setOf",
        "mutableListOf",
        "mutableMapOf",
        "mutableSetOf",
        "arrayOf",
        "emptyList",
        "emptyMap",
        "emptySet",
        "run",
        "let",
        "also",
        "apply",
        "takeIf",
        "takeUnless",
        "repeat",
        "require",
        "check",
        "error",
    },
    "c": {
        "printf",
        "scanf",
        "malloc",
        "free",
        "calloc",
        "realloc",
        "memcpy",
        "memset",
        "strcpy",
        "strlen",
        "strcmp",
        "strcat",
        "exit",
        "fopen",
        "fclose",
        "fprintf",
        "sprintf",
        "sizeof",
    },
    "cpp": {
        "printf",
        "scanf",
        "malloc",
        "free",
        "calloc",
        "realloc",
        "memcpy",
        "memset",
        "strcpy",
        "strlen",
        "strcmp",
        "strcat",
        "exit",
        "fopen",
        "fclose",
        "fprintf",
        "sprintf",
        "sizeof",
        "std",
        "cout",
        "cin",
        "endl",
        "vector",
        "string",
        "map",
        "set",
        "list",
        "shared_ptr",
        "unique_ptr",
        "make_shared",
        "make_unique",
        "move",
    },
}


# Common builtin/standard library method names to avoid incorrect resolution during global fallback
COMMON_BUILTIN_METHODS: set[str] = {
    "append",
    "decode",
    "encode",
    "insert",
    "remove",
    "contains",
    "push",
    "pop",
    "split",
    "join",
    "map",
    "filter",
    "reduce",
    "forEach",
    "sorted",
    "count",
    "length",
    "size",
    "isEmpty",
    "resume",
    "cancel",
    "suspend",
    "start",
    "stop",
    "send",
    "receive",
    # Added common programming language method/constructor names
    "len",
    "new",
    "is_empty",
    "clone",
    "default",
    "parse",
    "format",
    "read",
    "write",
    "close",
    "flush",
    "to_string",
    "to_str",
    "as_str",
    "as_ref",
    "as_mut",
    "unwrap",
    "expect",
    "iter",
    "iter_mut",
    "into_iter",
    "next",
    "into",
    "from",
    "ok",
    "err",
    "clear",
    "get",
    "set",
    "add",
    "keys",
    "values",
    "items",
    "update",
    "copy",
    "find",
    "index",
    "last",
    "first",
}


class FileSymbolScope:
    def __init__(self, file_path: str, language: str):
        self.file_path = file_path
        self.language = language
        # Maps local symbol name -> fully qualified Node ID (e.g. {"MyClass": "foo.py::MyClass"})
        self.declared_symbols: dict[str, str] = {}
        # Maps import alias or local name -> (target_file_id, original_name)
        self.imported_symbols: dict[str, tuple[str, str]] = {}
        # List of target files that were wildcard imported (e.g. from X import *)
        self.wildcard_imports: list[str] = []


def extract_return_type_from_signature(signature: str, language: str) -> str | None:
    signature = signature.strip()
    if not signature:
        return None

    if language in ("python", "rust", "swift"):
        match = re.search(r"->\s*([\w::.<>]+)", signature)
        if match:
            ret_type = match.group(1).strip()
            generic_match = re.search(r"<([\w::.]+)>", ret_type)
            if generic_match:
                return generic_match.group(1).rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            return ret_type.rsplit("::", 1)[-1].rsplit(".", 1)[-1]

    elif language == "kotlin":
        last_paren = signature.rfind(")")
        if last_paren != -1:
            after_paren = signature[last_paren + 1 :]
            match = re.search(r":\s*([\w<>]+)", after_paren)
            if match:
                ret_type = match.group(1).strip()
                generic_match = re.search(r"<([\w]+)>", ret_type)
                if generic_match:
                    return generic_match.group(1)
                return ret_type

    elif language == "go":
        last_paren = signature.rfind(")")
        if last_paren != -1:
            after_paren = signature[last_paren + 1 :].strip()
            if not after_paren or after_paren == "{":
                return None
            if after_paren.startswith("("):
                after_paren = after_paren[1:].split(")")[0]
                parts = [p.strip() for p in after_paren.split(",")]
                for p in parts:
                    clean_p = p.split()[-1]
                    if clean_p not in ("error", "bool", "int", "string"):
                        return clean_p
            else:
                clean_p = after_paren.split("{")[0].strip().split()[-1]
                clean_p = clean_p.lstrip("*").lstrip("[]")
                if clean_p not in ("error", "bool", "int", "string"):
                    return clean_p

    elif language in ("c", "cpp"):
        tokens = signature.split()
        if tokens:
            idx = 0
            while idx < len(tokens) and tokens[idx] in (
                "inline",
                "static",
                "virtual",
                "friend",
                "const",
                "constexpr",
            ):
                idx += 1
            if idx < len(tokens):
                ret_type = tokens[idx]
                if "(" in ret_type or ")" in ret_type:
                    return None
                ret_type = ret_type.replace("*", "").replace("&", "").strip()
                return ret_type.split("::")[-1]

    return None


def build_graph(extractions: list[ExtractionResult], workspace_dir: Path) -> nx.DiGraph:
    """
    Assembles a list of ExtractionResults into a single directed graph
    and resolves call, inherit, and import edges using a two-pass scope resolver.
    """
    G = nx.DiGraph()

    # 1. Add all nodes to the graph
    for ext in extractions:
        for node in ext.nodes:
            G.add_node(node.id, **node.model_dump())

    node_ids = set(G.nodes)

    # Helper: resolve local file path from Go/Python/C/C++ import targets
    def resolve_import_to_file_node(source_file: str, target: str) -> str | None:
        # Check if target is a direct relative/absolute file path
        # (either starting with '.' or containing '/' or having a C/C++ file extension)
        is_path_target = target.startswith(".") or "/" in target or "\\" in target
        if not is_path_target and file_languages.get(source_file) in ("c", "cpp"):
            is_path_target = any(
                target.endswith(ext)
                for ext in (".h", ".hpp", ".hxx", ".c", ".cpp", ".cc", ".cxx")
            )

        if is_path_target:
            source_dir = (Path(workspace_dir) / Path(source_file)).parent
            try:
                resolved_path = (source_dir / target).resolve()
                rel_path = str(resolved_path.relative_to(workspace_dir))
                if rel_path in node_ids:
                    return rel_path
                # Try adding standard extensions
                for suff in (".h", ".hpp", ".hxx", ".c", ".cpp", ".cc", ".cxx"):
                    check_path = rel_path + suff
                    if check_path in node_ids:
                        return check_path
            except Exception:
                pass

            # Global fallback search for this filename in the workspace (for C/C++ includes)
            target_name = Path(target).name
            for nid in node_ids:
                if G.nodes[nid]["type"] == "file":
                    if Path(nid).name == target_name:
                        return nid
            return None

        if target.startswith("."):
            source_dir = Path(workspace_dir) / Path(source_file).parent
            try:
                resolved_path = (source_dir / target).resolve()
                rel_path = str(resolved_path.relative_to(workspace_dir))

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

        target_path_part = target.replace(".", "/")
        for nid in node_ids:
            if G.nodes[nid]["type"] == "file":
                if (
                    nid.replace("\\", "/").endswith(target_path_part)
                    or nid.replace("\\", "/").endswith(target_path_part + ".py")
                    or nid.replace("\\", "/").endswith(
                        target_path_part + "/__init__.py"
                    )
                    or nid.replace("\\", "/").endswith(target_path_part + ".go")
                    or nid.replace("\\", "/").endswith(target_path_part + ".rs")
                ):
                    return nid
        return None

    # Pass 1: Build Symbol Scopes & Global maps
    scopes: dict[str, FileSymbolScope] = {}
    file_languages: dict[str, str] = {}
    global_symbol_map: dict[str, list[str]] = {}
    return_types: dict[str, str] = {}

    for nid, data in G.nodes(data=True):
        if data.get("type") == "file":
            suffix = Path(nid).suffix.lower()
            lang = "python"
            for lang_name, exts in {
                "python": {".py"},
                "javascript": {".js", ".mjs", ".cjs"},
                "typescript": {".ts", ".tsx"},
                "kotlin": {".kt", ".kts"},
                "go": {".go"},
                "rust": {".rs"},
                "swift": {".swift"},
                "c": {".c", ".h"},
                "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hxx"},
            }.items():
                if suffix in exts:
                    lang = lang_name
                    break
            file_languages[nid] = lang
            scopes[nid] = FileSymbolScope(nid, lang)

    # Populate declared symbols, global symbol map, and return types
    for nid, data in G.nodes(data=True):
        sf = data.get("source_file")
        ntype = data.get("type")
        label = data.get("label")

        if label and ntype != "file":
            global_symbol_map.setdefault(label, []).append(nid)

        if sf and ntype != "file" and label and sf in scopes:
            scopes[sf].declared_symbols[label] = nid

        # Extract return types for functions/methods
        if ntype in ("function", "method") and sf:
            lang = file_languages.get(sf, "python")
            sig = data.get("signature", "")
            ret = extract_return_type_from_signature(sig, lang)
            if ret:
                return_types[nid] = ret

    # Populate imported symbols for each scope
    for ext in extractions:
        # Find file node
        file_node = next((n for n in ext.nodes if n.type == "file"), None)
        if not file_node:
            continue
        file_id = file_node.id
        if file_id not in scopes:
            continue

        for edge in ext.edges:
            if edge.relation == "imports":
                target_file_id = resolve_import_to_file_node(file_id, edge.target)
                if target_file_id:
                    # In C/C++, importing/including a header imports all its symbols as wildcard imports
                    if scopes[file_id].language in ("c", "cpp"):
                        scopes[file_id].wildcard_imports.append(target_file_id)

                    # Parse import_map
                    if edge.import_map:
                        for local_name, original_name in edge.import_map.items():
                            if original_name == "*":
                                scopes[file_id].wildcard_imports.append(target_file_id)
                            else:
                                scopes[file_id].imported_symbols[local_name] = (
                                    target_file_id,
                                    original_name,
                                )
                    else:
                        # Direct import of a module name (e.g. import module_b)
                        stem = Path(target_file_id).stem
                        scopes[file_id].imported_symbols[stem] = (target_file_id, stem)

    # Resolve symbol helper using the scope chain
    def resolve_symbol(caller_id: str, callee_name: str) -> str | None:
        caller_data = G.nodes.get(caller_id)
        if not caller_data:
            return None
        source_file = caller_data["source_file"]

        lang = file_languages.get(source_file, "python")
        callee_clean = callee_name.replace("::", ".")
        parts = [p.strip() for p in callee_clean.split(".") if p.strip()]
        if not parts:
            return None

        main_symbol = parts[0]
        rest_of_callee = callee_clean.split(".", 1)[1] if len(parts) > 1 else ""

        # 1. Builtins / Stdlib Check
        if main_symbol in BUILTIN_FUNCTIONS.get(lang, set()):
            return None

        scope = scopes.get(source_file)
        if not scope:
            return None

        # Local Scope Type Binding resolution
        local_bindings = caller_data.get("local_bindings", {})
        if len(parts) > 1 and main_symbol in local_bindings:
            receiver_type = local_bindings[main_symbol]
            resolved_class_id = None

            # Check if it is already a fully qualified Node ID in the graph
            if receiver_type in node_ids:
                resolved_class_id = receiver_type

            # Check if it's declared in the same file
            elif f"{source_file}::{receiver_type}" in node_ids:
                resolved_class_id = f"{source_file}::{receiver_type}"

            # Check explicit imports
            elif receiver_type in scope.imported_symbols:
                target_file_id, original_name = scope.imported_symbols[receiver_type]
                resolved_class_id = f"{target_file_id}::{original_name}"

            # Check package siblings (for Go/Swift)
            elif lang in ("go", "swift"):
                caller_dir = Path(source_file).parent
                for nid in node_ids:
                    ndata = G.nodes[nid]
                    if (
                        ndata.get("type") in ("class", "struct", "interface", "enum")
                        and ndata.get("label") == receiver_type
                    ):
                        node_file = ndata.get("source_file", "")
                        if node_file and Path(node_file).parent == caller_dir:
                            resolved_class_id = nid
                            break

            # Global fallback for class/struct name if not found in current module/scope
            if not resolved_class_id:
                for nid in node_ids:
                    ndata = G.nodes[nid]
                    if (
                        ndata.get("type") in ("class", "struct", "interface", "enum")
                        and ndata.get("label") == receiver_type
                    ):
                        resolved_class_id = nid
                        break

            if resolved_class_id:
                target_method_id = f"{resolved_class_id}.{rest_of_callee}"
                if target_method_id in node_ids:
                    return target_method_id
                target_method_id = f"{resolved_class_id}.{parts[-1]}"
                if target_method_id in node_ids:
                    return target_method_id

                # Cross-file / implementation-to-header fallback for C++ and Python binding boundaries
                method_name = parts[-1]
                for nid in node_ids:
                    ndata = G.nodes[nid]
                    if (
                        ndata.get("type") in ("method", "function")
                        and ndata.get("label") == method_name
                    ):
                        parent_class_part = nid.rsplit(".", 1)[0] if "." in nid else ""
                        parent_class_name = (
                            parent_class_part.rsplit("::", 1)[-1]
                            if "::" in parent_class_part
                            else parent_class_part
                        )
                        if (
                            parent_class_name == receiver_type
                            or parent_class_name.endswith(f".{receiver_type}")
                        ):
                            return nid
            else:
                # Known type but not defined in the workspace -> external/standard library type.
                # Bypassing global fallback to prevent incorrect resolution of its methods.
                return None

        # 2. Local lexical scope check
        # self / this / cls references
        if main_symbol in ("self", "this", "cls"):
            if "." in caller_id:
                parent_class_id = caller_id.rsplit(".", 1)[0]
                if rest_of_callee:
                    target_candidate = f"{parent_class_id}.{rest_of_callee}"
                    if target_candidate in node_ids:
                        return target_candidate
                    target_candidate = f"{parent_class_id}.{parts[-1]}"
                    if target_candidate in node_ids:
                        return target_candidate

        # Inside current class context
        if "." in caller_id:
            parent_class_id = caller_id.rsplit(".", 1)[0]
            target_candidate = f"{parent_class_id}.{main_symbol}"
            if target_candidate in node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in node_ids:
                        return sub_target
                return target_candidate

        # File-level scope check
        file_candidate = f"{source_file}::{main_symbol}"
        if file_candidate in node_ids:
            if rest_of_callee:
                sub_target = f"{file_candidate}.{rest_of_callee}"
                if sub_target in node_ids:
                    return sub_target
            return file_candidate

        # 3. Package scope check (for Go, Swift sibling files)
        if lang in ("go", "swift"):
            caller_dir = Path(source_file).parent
            for nid in node_ids:
                ndata = G.nodes[nid]
                if ndata.get("type") == "file":
                    continue
                node_file = ndata.get("source_file", "")
                if node_file and Path(node_file).parent == caller_dir:
                    if nid.endswith(f"::{main_symbol}"):
                        if rest_of_callee:
                            sub_target = f"{nid}.{rest_of_callee}"
                            if sub_target in node_ids:
                                return sub_target
                        return nid

        # 4. Explicit imports and aliases check
        if main_symbol in scope.imported_symbols:
            target_file_id, original_name = scope.imported_symbols[main_symbol]
            if original_name == "*" or original_name == Path(target_file_id).stem:
                if rest_of_callee:
                    target_candidate = f"{target_file_id}::{rest_of_callee}"
                    if target_candidate in node_ids:
                        return target_candidate
                    for nid in node_ids:
                        if G.nodes[nid].get(
                            "source_file"
                        ) == target_file_id and nid.endswith(f".{parts[-1]}"):
                            return nid
                else:
                    target_candidate = f"{target_file_id}::{main_symbol}"
                    if target_candidate in node_ids:
                        return target_candidate
                    return target_file_id
            else:
                target_candidate = f"{target_file_id}::{original_name}"
                if target_candidate in node_ids:
                    if rest_of_callee:
                        sub_target = f"{target_candidate}.{rest_of_callee}"
                        if sub_target in node_ids:
                            return sub_target
                    return target_candidate
                return target_candidate

        # 5. Wildcard imports check
        for target_file_id in scope.wildcard_imports:
            target_candidate = f"{target_file_id}::{main_symbol}"
            if target_candidate in node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in node_ids:
                        return sub_target
                return target_candidate

        # 6. Global fallback check
        if main_symbol in {
            "os",
            "sys",
            "json",
            "time",
            "math",
            "re",
            "pathlib",
            "logging",
            "subprocess",
            "shutil",
            "hashlib",
            "urllib",
            "socket",
            "threading",
            "multiprocessing",
            "typing",
            "collections",
            "itertools",
            "functools",
            "logger",
            "log",
            "console",
            "pytest",
            "unittest",
            "fmt",
            "sync",
            "context",
            "strings",
            "bytes",
            "errors",
            "net",
            "http",
            "process",
            "document",
            "window",
            "global",
            "fs",
            "path",
            "std",
            "core",
            "env",
            "Logger",
        } or any(p in {"logger", "log", "logging", "console"} for p in parts):
            return None

        search_label = parts[-1] if len(parts) > 1 else main_symbol
        if len(parts) > 1 and search_label in COMMON_BUILTIN_METHODS:
            return None

        candidates = global_symbol_map.get(search_label, [])

        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            caller_parent_dir = Path(source_file).parent
            near_candidates = [
                c
                for c in candidates
                if Path(G.nodes[c]["source_file"]).parent == caller_parent_dir
            ]
            if len(near_candidates) == 1:
                return near_candidates[0]

    # Pass 1.5: Iterative Type Propagation (up to 3 passes)
    for _ in range(3):
        changes = False
        for nid, ndata in G.nodes(data=True):
            if ndata.get("type") == "file":
                continue
            local_bindings = ndata.get("local_bindings", {})
            if not local_bindings:
                continue

            for var_name, bound_name in list(local_bindings.items()):
                if bound_name in node_ids:
                    continue

                resolved_symbol_id = resolve_symbol(nid, bound_name)
                if resolved_symbol_id and resolved_symbol_id in node_ids:
                    resolved_node = G.nodes[resolved_symbol_id]
                    r_type = resolved_node.get("type")

                    if r_type in ("function", "method"):
                        ret_type = return_types.get(resolved_symbol_id)
                        if ret_type:
                            func_source = resolved_node.get("source_file")
                            if func_source and func_source in scopes:
                                resolved_type_id = scopes[
                                    func_source
                                ].declared_symbols.get(ret_type)
                                if resolved_type_id:
                                    local_bindings[var_name] = resolved_type_id
                                    changes = True
                                    continue
                            resolved_type_id = resolve_symbol(
                                resolved_symbol_id, ret_type
                            )
                            if resolved_type_id:
                                local_bindings[var_name] = resolved_type_id
                                changes = True
                            else:
                                local_bindings[var_name] = ret_type
                                changes = True

                    elif r_type in ("class", "struct", "interface", "enum"):
                        local_bindings[var_name] = resolved_symbol_id
                        changes = True

        if not changes:
            break

    # Pass 2: Process and resolve edges
    for ext in extractions:
        for edge in ext.edges:
            src = edge.source
            tgt = edge.target
            rel = edge.relation

            if src == tgt:
                continue
            if src not in node_ids:
                continue

            resolved_tgt = None

            if rel == "contains":
                if tgt in node_ids:
                    resolved_tgt = tgt
            elif rel == "imports":
                resolved_tgt = resolve_import_to_file_node(
                    G.nodes[src]["source_file"], tgt
                )
            elif rel in ("inherits", "implements"):
                resolved_tgt = resolve_symbol(src, tgt)
            elif rel == "calls":
                resolved_tgt = resolve_symbol(src, tgt)

            if resolved_tgt and resolved_tgt in node_ids:
                if rel == "imports":
                    G.add_edge(src, resolved_tgt, relation=rel, raw_target=tgt)
                else:
                    G.add_edge(src, resolved_tgt, relation=rel)

    return G

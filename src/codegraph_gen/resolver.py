import logging
from pathlib import Path
import re
import networkx as nx
from codegraph_gen.schema import ExtractionResult
from codegraph_gen.resolver_strategy import (
    get_strategy_for_file,
    get_strategy_by_name,
    LanguageResolverStrategy,
)

logger = logging.getLogger(__name__)

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
    strategy = get_strategy_by_name(language)
    return strategy.extract_return_type(signature)


class TypeResolver:
    def __init__(
        self,
        G: nx.DiGraph,
        extractions: list[ExtractionResult],
        workspace_dir: Path,
    ):
        self.G = G
        self.extractions = extractions
        self.workspace_dir = workspace_dir
        self.node_ids = set(G.nodes)

        self.scopes: dict[str, FileSymbolScope] = {}
        self.file_languages: dict[str, str] = {}
        self.file_strategies: dict[str, LanguageResolverStrategy] = {}
        self.global_symbol_map: dict[str, list[str]] = {}
        self.return_types: dict[str, str] = {}

        self._initialize_scopes()

    def _initialize_scopes(self) -> None:
        # Detect languages and load strategies
        for nid, data in self.G.nodes(data=True):
            if data.get("type") == "file":
                strategy = get_strategy_for_file(nid)
                self.file_strategies[nid] = strategy
                self.file_languages[nid] = strategy.name
                self.scopes[nid] = FileSymbolScope(nid, strategy.name)

        # Populate declared, global symbols and return types
        for nid, data in self.G.nodes(data=True):
            sf = data.get("source_file")
            ntype = data.get("type")
            label = data.get("label")

            if label and ntype != "file":
                self.global_symbol_map.setdefault(label, []).append(nid)

            if sf and ntype != "file" and label and sf in self.scopes:
                self.scopes[sf].declared_symbols[label] = nid

            if ntype in ("function", "method") and sf:
                strategy = self.file_strategies.get(sf, get_strategy_for_file(sf))
                sig = data.get("signature", "")
                ret = strategy.extract_return_type(sig)
                if ret:
                    self.return_types[nid] = ret

        # Populate imports
        for ext in self.extractions:
            file_node = next((n for n in ext.nodes if n.type == "file"), None)
            if not file_node:
                continue
            file_id = file_node.id
            if file_id not in self.scopes:
                continue
            strategy = self.file_strategies.get(file_id, get_strategy_for_file(file_id))

            for edge in ext.edges:
                if edge.relation == "imports":
                    target_file_id = self.resolve_import_to_file_node(
                        file_id, edge.target
                    )
                    if target_file_id:
                        if strategy.should_treat_import_as_wildcard(target_file_id, edge.import_map or {}):
                            self.scopes[file_id].wildcard_imports.append(target_file_id)

                        if edge.import_map:
                            for local_name, original_name in edge.import_map.items():
                                if original_name == "*":
                                    self.scopes[file_id].wildcard_imports.append(
                                        target_file_id
                                    )
                                else:
                                    self.scopes[file_id].imported_symbols[
                                        local_name
                                    ] = (
                                        target_file_id,
                                        original_name,
                                    )
                        else:
                            stem = Path(target_file_id).stem
                            self.scopes[file_id].imported_symbols[stem] = (
                                target_file_id,
                                stem,
                            )

    def resolve_import_to_file_node(self, source_file: str, target: str) -> str | None:
        strategy = self.file_strategies.get(source_file, get_strategy_for_file(source_file))
        is_path_target = strategy.is_path_target(target)

        if is_path_target:
            source_dir = (Path(self.workspace_dir) / Path(source_file)).parent
            try:
                resolved_path = (source_dir / target).resolve()
                rel_path = str(resolved_path.relative_to(self.workspace_dir))
                if rel_path in self.node_ids:
                    return rel_path
                for suff in strategy.import_search_suffixes:
                    check_path = rel_path + suff
                    if check_path in self.node_ids:
                        return check_path
            except Exception:
                pass

            target_name = Path(target).name
            for nid in self.node_ids:
                if self.G.nodes[nid]["type"] == "file":
                    if Path(nid).name == target_name:
                        return nid
            return None

        # Non-path targets (e.g. dot/colon namespaces or package imports)
        candidates = strategy.get_import_path_candidates(target)
        for cand in candidates:
            if cand in self.node_ids:
                return cand
            
            cand_normalized = cand.replace("\\", "/")
            for nid in self.node_ids:
                if self.G.nodes[nid]["type"] == "file":
                    nid_normalized = nid.replace("\\", "/")
                    if (
                        nid_normalized == cand_normalized
                        or nid_normalized.endswith("/" + cand_normalized)
                        or nid_normalized.endswith("\\" + cand_normalized)
                    ):
                        return nid

        return None

    def _resolve_builtin(self, lang: str, main_symbol: str) -> bool:
        return get_strategy_by_name(lang).is_builtin(main_symbol)

    def _resolve_local_binding(
        self,
        caller_id: str,
        source_file: str,
        strategy: LanguageResolverStrategy,
        scope: FileSymbolScope,
        main_symbol: str,
        parts: list[str],
        rest_of_callee: str,
    ) -> str | None:
        caller_data = self.G.nodes.get(caller_id)
        if not caller_data:
            return None
        local_bindings = caller_data.get("local_bindings", {})
        receiver_type = local_bindings[main_symbol]
        resolved_class_id = None

        if receiver_type in self.node_ids:
            resolved_class_id = receiver_type
        elif f"{source_file}::{receiver_type}" in self.node_ids:
            resolved_class_id = f"{source_file}::{receiver_type}"
        elif receiver_type in scope.imported_symbols:
            target_file_id, original_name = scope.imported_symbols[receiver_type]
            resolved_class_id = f"{target_file_id}::{original_name}"
        elif strategy.has_package_sibling_scope():
            caller_dir = Path(source_file).parent
            for nid in self.node_ids:
                ndata = self.G.nodes[nid]
                if (
                    ndata.get("type") in ("class", "struct", "interface", "enum")
                    and ndata.get("label") == receiver_type
                ):
                    node_file = ndata.get("source_file", "")
                    if node_file and Path(node_file).parent == caller_dir:
                        resolved_class_id = nid
                        break

        # Fallback search for the class/struct definition
        if not resolved_class_id:
            for nid in self.node_ids:
                ndata = self.G.nodes[nid]
                if (
                    ndata.get("type") in ("class", "struct", "interface", "enum")
                    and ndata.get("label") == receiver_type
                ):
                    resolved_class_id = nid
                    break

        if resolved_class_id:
            target_method_id = f"{resolved_class_id}.{rest_of_callee}"
            if target_method_id in self.node_ids:
                return target_method_id
            target_method_id = f"{resolved_class_id}.{parts[-1]}"
            if target_method_id in self.node_ids:
                return target_method_id

            method_name = parts[-1]
            for nid in self.node_ids:
                ndata = self.G.nodes[nid]
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
                    if parent_class_name == receiver_type or parent_class_name.endswith(
                        f".{receiver_type}"
                    ):
                        return nid
        return None

    def _resolve_self_reference(
        self, caller_id: str, parts: list[str], rest_of_callee: str
    ) -> str | None:
        if "." in caller_id:
            parent_class_id = caller_id.rsplit(".", 1)[0]
            if rest_of_callee:
                target_candidate = f"{parent_class_id}.{rest_of_callee}"
                if target_candidate in self.node_ids:
                    return target_candidate
                target_candidate = f"{parent_class_id}.{parts[-1]}"
                if target_candidate in self.node_ids:
                    return target_candidate
        return None

    def _resolve_current_class_context(
        self, caller_id: str, main_symbol: str, parts: list[str], rest_of_callee: str
    ) -> str | None:
        if "." in caller_id:
            parent_class_id = caller_id.rsplit(".", 1)[0]
            target_candidate = f"{parent_class_id}.{main_symbol}"
            if target_candidate in self.node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in self.node_ids:
                        return sub_target
                return target_candidate
        return None

    def _resolve_file_level_scope(
        self, source_file: str, main_symbol: str, parts: list[str], rest_of_callee: str
    ) -> str | None:
        file_candidate = f"{source_file}::{main_symbol}"
        if file_candidate in self.node_ids:
            if rest_of_callee:
                sub_target = f"{file_candidate}.{rest_of_callee}"
                if sub_target in self.node_ids:
                    return sub_target
            return file_candidate
        return None

    def _resolve_package_siblings(
        self, source_file: str, main_symbol: str, parts: list[str], rest_of_callee: str
    ) -> str | None:
        caller_dir = Path(source_file).parent
        for nid in self.node_ids:
            ndata = self.G.nodes[nid]
            if ndata.get("type") == "file":
                continue
            node_file = ndata.get("source_file", "")
            if node_file and Path(node_file).parent == caller_dir:
                if nid.endswith(f"::{main_symbol}"):
                    if rest_of_callee:
                        sub_target = f"{nid}.{rest_of_callee}"
                        if sub_target in self.node_ids:
                            return sub_target
                    return nid
        return None

    def _resolve_explicit_imports(
        self,
        scope: FileSymbolScope,
        main_symbol: str,
        parts: list[str],
        rest_of_callee: str,
    ) -> str | None:
        target_file_id, original_name = scope.imported_symbols[main_symbol]
        if original_name == "*" or original_name == Path(target_file_id).stem:
            if rest_of_callee:
                target_candidate = f"{target_file_id}::{rest_of_callee}"
                if target_candidate in self.node_ids:
                    return target_candidate
                for nid in self.node_ids:
                    if self.G.nodes[nid].get(
                        "source_file"
                    ) == target_file_id and nid.endswith(f".{parts[-1]}"):
                        return nid
            else:
                target_candidate = f"{target_file_id}::{main_symbol}"
                if target_candidate in self.node_ids:
                    return target_candidate
                return target_file_id
        else:
            target_candidate = f"{target_file_id}::{original_name}"
            if target_candidate in self.node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in self.node_ids:
                        return sub_target
                return target_candidate
            return target_candidate
        return None

    def _resolve_wildcard_imports(
        self,
        scope: FileSymbolScope,
        main_symbol: str,
        parts: list[str],
        rest_of_callee: str,
    ) -> str | None:
        for target_file_id in scope.wildcard_imports:
            target_candidate = f"{target_file_id}::{main_symbol}"
            if target_candidate in self.node_ids:
                if rest_of_callee:
                    sub_target = f"{target_candidate}.{rest_of_callee}"
                    if sub_target in self.node_ids:
                        return sub_target
                return target_candidate
        return None

    def _resolve_global_fallback(
        self, source_file: str, main_symbol: str, parts: list[str]
    ) -> str | None:
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

        candidates = self.global_symbol_map.get(search_label, [])
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            caller_parent_dir = Path(source_file).parent
            near_candidates = [
                c
                for c in candidates
                if Path(self.G.nodes[c]["source_file"]).parent == caller_parent_dir
            ]
            if len(near_candidates) == 1:
                return near_candidates[0]
        return None

    def resolve_symbol(self, caller_id: str, callee_name: str) -> str | None:
        caller_data = self.G.nodes.get(caller_id)
        if not caller_data:
            return None
        source_file = caller_data["source_file"]
        strategy = self.file_strategies.get(source_file, get_strategy_for_file(source_file))
        callee_clean = callee_name.replace("::", ".")
        parts = [p.strip() for p in callee_clean.split(".") if p.strip()]
        if not parts:
            return None

        main_symbol = parts[0]
        rest_of_callee = callee_clean.split(".", 1)[1] if len(parts) > 1 else ""

        # 1. Builtins / Stdlib Check
        if strategy.is_builtin(main_symbol):
            return None

        scope = self.scopes.get(source_file)
        if not scope:
            return None

        # 2. Local Scope Type Binding Resolution
        local_bindings = caller_data.get("local_bindings", {})
        if len(parts) > 1 and main_symbol in local_bindings:
            res = self._resolve_local_binding(
                caller_id, source_file, strategy, scope, main_symbol, parts, rest_of_callee
            )
            if res:
                return res
            return None

        # 3. Local Lexical Scope (self / this / cls)
        if main_symbol in ("self", "this", "cls"):
            res = self._resolve_self_reference(caller_id, parts, rest_of_callee)
            if res:
                return res

        # 4. Inside Current Class Context
        res = self._resolve_current_class_context(
            caller_id, main_symbol, parts, rest_of_callee
        )
        if res:
            return res

        # 5. File-level Scope
        res = self._resolve_file_level_scope(
            source_file, main_symbol, parts, rest_of_callee
        )
        if res:
            return res

        # 6. Sibling / Package Scope (Go, Swift)
        if strategy.has_package_sibling_scope():
            res = self._resolve_package_siblings(
                source_file, main_symbol, parts, rest_of_callee
            )
            if res:
                return res

        # 7. Explicit Imports & Aliases
        if main_symbol in scope.imported_symbols:
            res = self._resolve_explicit_imports(
                scope, main_symbol, parts, rest_of_callee
            )
            if res:
                return res

        # 8. Wildcard Imports
        res = self._resolve_wildcard_imports(scope, main_symbol, parts, rest_of_callee)
        if res:
            return res

        # 9. Global Fallback Check
        return self._resolve_global_fallback(source_file, main_symbol, parts)

    def propagate_types(self) -> None:
        max_iterations = 10
        for _ in range(max_iterations):
            changes = False
            for nid, ndata in self.G.nodes(data=True):
                if ndata.get("type") == "file":
                    continue
                local_bindings = ndata.get("local_bindings", {})
                if not local_bindings:
                    continue

                for var_name, bound_name in list(local_bindings.items()):
                    if bound_name in self.node_ids:
                        continue

                    resolved_symbol_id = self.resolve_symbol(nid, bound_name)
                    if resolved_symbol_id and resolved_symbol_id in self.node_ids:
                        resolved_node = self.G.nodes[resolved_symbol_id]
                        r_type = resolved_node.get("type")

                        if r_type in ("function", "method"):
                            ret_type = self.return_types.get(resolved_symbol_id)
                            if ret_type:
                                func_source = resolved_node.get("source_file")
                                if func_source and func_source in self.scopes:
                                    resolved_type_id = self.scopes[
                                        func_source
                                    ].declared_symbols.get(ret_type)
                                    if resolved_type_id:
                                        local_bindings[var_name] = resolved_type_id
                                        changes = True
                                        continue
                                resolved_type_id = self.resolve_symbol(
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

    def resolve_all_edges(self) -> None:
        for ext in self.extractions:
            for edge in ext.edges:
                src = edge.source
                tgt = edge.target
                rel = edge.relation

                if src == tgt:
                    continue
                if src not in self.node_ids:
                    continue

                resolved_tgt = None

                if rel == "contains":
                    if tgt in self.node_ids:
                        resolved_tgt = tgt
                elif rel == "imports":
                    resolved_tgt = self.resolve_import_to_file_node(
                        self.G.nodes[src]["source_file"], tgt
                    )
                elif rel in ("inherits", "implements", "calls"):
                    resolved_tgt = self.resolve_symbol(src, tgt)

                if resolved_tgt and resolved_tgt in self.node_ids:
                    if rel == "imports":
                        self.G.add_edge(src, resolved_tgt, relation=rel, raw_target=tgt)
                    else:
                        self.G.add_edge(src, resolved_tgt, relation=rel)

import logging
from collections import deque
from pathlib import Path
from types import MappingProxyType
import networkx as nx
from codegraph_gen.schema import ExtractionResult
from codegraph_gen.resolver_strategy import (
    get_strategy_for_file,
    get_strategy_by_name,
    LanguageResolverStrategy,
)
from codegraph_gen.scope import FileSymbolScope
from codegraph_gen.resolver_context import ResolutionContext, _StopResolution
from codegraph_gen.resolver_steps import DEFAULT_RESOLVER_CHAIN, ResolverFn

logger = logging.getLogger(__name__)


def extract_return_type_from_signature(signature: str, language: str) -> str | None:
    strategy = get_strategy_by_name(language)
    return strategy.extract_return_type(signature)


class WorklistFixpointSolver:
    """A reactive worklist-based fixed-point propagation/solver."""

    def __init__(self, resolver: "TypeResolver"):
        self.resolver = resolver
        self.queue_list = deque()
        self.in_queue = set()
        self.deps = {}  # Map (nid, var_name) -> set of (dep_nid, dep_var_name)

        self._build_dependencies()
        self._initialize_worklist()

    def _build_dependencies(self) -> None:
        for nid, ndata in self.resolver.G.nodes(data=True):
            if ndata.get("type") == "file":
                continue
            local_bindings = ndata.get("local_bindings", {})
            for var_name, bound_name in local_bindings.items():
                if (
                    not isinstance(bound_name, str)
                    or bound_name in self.resolver.node_ids
                ):
                    continue

                callee_clean = bound_name.replace("::", ".")
                parts = [p.strip() for p in callee_clean.split(".") if p.strip()]
                if parts:
                    main_symbol = parts[0]
                    if main_symbol in local_bindings and main_symbol != var_name:
                        self.add_dependency(nid, main_symbol, nid, var_name)

    def add_dependency(
        self, src_nid: str, src_var: str, tgt_nid: str, tgt_var: str
    ) -> None:
        self.deps.setdefault((src_nid, src_var), set()).add((tgt_nid, tgt_var))

    def _initialize_worklist(self) -> None:
        for nid, ndata in self.resolver.G.nodes(data=True):
            if ndata.get("type") == "file":
                continue
            local_bindings = ndata.get("local_bindings", {})
            for var_name in local_bindings:
                self.push(nid, var_name)

    def push(self, nid: str, var_name: str) -> None:
        key = (nid, var_name)
        if key not in self.in_queue:
            self.queue_list.append(key)
            self.in_queue.add(key)

    def solve(self) -> None:
        while self.queue_list:
            nid, var_name = self.queue_list.popleft()
            self.in_queue.remove((nid, var_name))

            ndata = self.resolver.G.nodes[nid]
            local_bindings = ndata.get("local_bindings", {})
            if var_name not in local_bindings:
                continue

            bound_name = local_bindings[var_name]
            if not isinstance(bound_name, str) or bound_name in self.resolver.node_ids:
                continue

            new_target_type = self._evaluate_variable(nid, var_name, bound_name)
            if new_target_type and new_target_type != bound_name:
                local_bindings[var_name] = new_target_type
                dependents = self.deps.get((nid, var_name), set())
                for dep_nid, dep_var in dependents:
                    self.push(dep_nid, dep_var)

    def _evaluate_variable(
        self, nid: str, var_name: str, bound_name: str
    ) -> str | None:
        ndata = self.resolver.G.nodes[nid]
        local_bindings = ndata.get("local_bindings", {})
        if bound_name in local_bindings:
            resolved_val = local_bindings[bound_name]
            if resolved_val in self.resolver.node_ids:
                return resolved_val
            return None

        resolved_symbol_id = self.resolver.resolve_symbol(nid, bound_name)
        if not resolved_symbol_id or resolved_symbol_id not in self.resolver.node_ids:
            return None

        resolved_node = self.resolver.G.nodes[resolved_symbol_id]
        r_type = resolved_node.get("type")

        source_file = self.resolver.G.nodes[nid].get("source_file")
        strategy = self.resolver.file_strategies.get(
            source_file, get_strategy_for_file(source_file)
        )
        custom_type = strategy.compute_transfer_type(r_type, resolved_symbol_id)
        if custom_type:
            if custom_type in self.resolver.node_ids:
                return custom_type
            resolved_custom = self.resolver.resolve_symbol(
                resolved_symbol_id, custom_type
            )
            if resolved_custom and resolved_custom in self.resolver.node_ids:
                return resolved_custom
            return custom_type

        if r_type in ("function", "method"):
            ret_type = self.resolver.return_types.get(resolved_symbol_id)
            if ret_type:
                func_source = resolved_node.get("source_file")
                if func_source and func_source in self.resolver.scopes:
                    resolved_type_id = self.resolver.scopes[
                        func_source
                    ].declared_symbols.get(ret_type)
                    if resolved_type_id:
                        return resolved_type_id
                resolved_type_id = self.resolver.resolve_symbol(
                    resolved_symbol_id, ret_type
                )
                if resolved_type_id:
                    return resolved_type_id
                return ret_type

        elif r_type in ("class", "struct", "interface", "enum"):
            return resolved_symbol_id

        return None


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
                        if strategy.should_treat_import_as_wildcard(
                            target_file_id, edge.import_map or {}
                        ):
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
        strategy = self.file_strategies.get(
            source_file, get_strategy_for_file(source_file)
        )
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

    def resolve_symbol(
        self,
        caller_id: str,
        callee_name: str,
        chain: list[ResolverFn] | None = None,
    ) -> str | None:
        """
        Resolve a callee symbol reference to a graph node ID.

        All resolution logic is delegated to the resolver chain
        (``DEFAULT_RESOLVER_CHAIN`` by default).  An alternative chain can
        be injected via the ``chain`` parameter — useful for testing
        individual steps or adding language-specific steps.

        Returns the resolved node ID, or ``None`` if resolution fails.
        """
        caller_data = self.G.nodes.get(caller_id)
        if not caller_data:
            return None

        source_file = caller_data["source_file"]
        scope = self.scopes.get(source_file)
        if not scope:
            return None

        strategy = self.file_strategies.get(
            source_file, get_strategy_for_file(source_file)
        )
        callee_clean = callee_name.replace("::", ".")
        parts_list = [p.strip() for p in callee_clean.split(".") if p.strip()]
        if not parts_list:
            return None

        ctx = ResolutionContext(
            caller_id=caller_id,
            source_file=source_file,
            callee_name=callee_name,
            parts=tuple(parts_list),
            main_symbol=parts_list[0],
            rest_of_callee=callee_clean.split(".", 1)[1] if len(parts_list) > 1 else "",
            strategy=strategy,
            scope=scope,
            local_bindings=MappingProxyType(caller_data.get("local_bindings", {})),
            node_ids=frozenset(self.node_ids),
            graph_nodes=self.G.nodes,
            global_symbol_map=MappingProxyType(self.global_symbol_map),
        )

        resolver_chain = (
            chain
            if chain is not None
            else strategy.extend_resolver_chain(DEFAULT_RESOLVER_CHAIN)
        )
        for fn in resolver_chain:
            result = fn(ctx)
            if isinstance(result, _StopResolution):
                return None
            if result is not None:
                return result
        return None

    def propagate_types(self) -> None:
        solver = WorklistFixpointSolver(self)
        solver.solve()

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

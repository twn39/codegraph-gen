"""
resolver_steps.py
=================
Pure, stateless resolver functions implementing each step of the symbol
resolution fallback chain.

Each function has the same signature::

    def resolve_xxx(ctx: ResolutionContext) -> str | _StopResolution | None

Return semantics
----------------
- ``str``             — a graph node ID was found; resolution succeeds.
- ``None``            — this step cannot handle the symbol; try the next step.
- ``STOP`` sentinel   — resolution has definitively failed; abort the chain
                        (even if later steps might produce a guess).

The default chain is exported as ``DEFAULT_RESOLVER_CHAIN``, an ordered
list of callables.  It replaces the 9-step ``if/elif/return`` sequence
that was previously embedded inside ``TypeResolver.resolve_symbol()``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from codegraph_gen.resolver_context import ResolutionContext, STOP, _StopResolution
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


logger = logging.getLogger(__name__)

# Type alias for a single resolution step
ResolverFn = Callable[[ResolutionContext], "str | _StopResolution | None"]


# ---------------------------------------------------------------------------
# Step 1 — Builtin / Stdlib guard
# ---------------------------------------------------------------------------

def guard_builtin(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Reject symbols that are builtins or stdlib identifiers for the caller's
    language.  Returns ``STOP`` immediately to prevent any further guessing.
    """
    if ctx.strategy.is_builtin(ctx.main_symbol):
        return STOP
    return None


# ---------------------------------------------------------------------------
# Step 2 — Local binding (typed variable)
# ---------------------------------------------------------------------------

def resolve_local_binding(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve ``foo.bar()`` where ``foo`` is a typed local variable
    (i.e. ``foo`` appears in ``local_bindings`` with a type annotation).

    If ``foo`` is in ``local_bindings`` but the class cannot be located,
    returns ``STOP`` to prevent incorrect global-fallback guessing — the
    intent of the call is unambiguous even if the class is missing.
    """
    if len(ctx.parts) <= 1 or ctx.main_symbol not in ctx.local_bindings:
        return None

    result = _resolve_local_binding_impl(ctx)
    # Explicit short-circuit: typed var was found but class is unresolvable
    return result if result else STOP


def _resolve_local_binding_impl(ctx: ResolutionContext) -> str | None:
    receiver_type = ctx.local_bindings[ctx.main_symbol]
    source_file = ctx.source_file
    scope = ctx.scope
    node_ids = ctx.node_ids
    graph_nodes = ctx.graph_nodes
    rest_of_callee = ctx.rest_of_callee
    parts = ctx.parts

    resolved_class_id: str | None = None

    if receiver_type in node_ids:
        resolved_class_id = receiver_type
    elif f"{source_file}::{receiver_type}" in node_ids:
        resolved_class_id = f"{source_file}::{receiver_type}"
    elif receiver_type in scope.imported_symbols:
        target_file_id, original_name = scope.imported_symbols[receiver_type]
        resolved_class_id = f"{target_file_id}::{original_name}"
    elif ctx.strategy.has_package_sibling_scope():
        caller_dir = Path(source_file).parent
        for nid in node_ids:
            ndata = graph_nodes[nid]
            if (
                ndata.get("type") in ("class", "struct", "interface", "enum")
                and ndata.get("label") == receiver_type
            ):
                node_file = ndata.get("source_file", "")
                if node_file and Path(node_file).parent == caller_dir:
                    resolved_class_id = nid
                    break

    # Fallback: search entire graph for the class/struct definition
    if not resolved_class_id:
        for nid in node_ids:
            ndata = graph_nodes[nid]
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

        method_name = parts[-1]
        for nid in node_ids:
            ndata = graph_nodes[nid]
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


# ---------------------------------------------------------------------------
# Step 3 — self / this / cls reference
# ---------------------------------------------------------------------------

def resolve_self_reference(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve ``self.foo``, ``this.foo``, or ``cls.foo`` to a sibling member
    of the enclosing class.

    Returns ``None`` (not ``STOP``) on failure so that later steps can still
    attempt resolution via class context or file scope.
    """
    if ctx.main_symbol not in ("self", "this", "cls"):
        return None

    caller_id = ctx.caller_id
    parts = ctx.parts
    rest_of_callee = ctx.rest_of_callee
    node_ids = ctx.node_ids

    if "." in caller_id:
        parent_class_id = caller_id.rsplit(".", 1)[0]
        if rest_of_callee:
            target_candidate = f"{parent_class_id}.{rest_of_callee}"
            if target_candidate in node_ids:
                return target_candidate
            target_candidate = f"{parent_class_id}.{parts[-1]}"
            if target_candidate in node_ids:
                return target_candidate
    return None


# ---------------------------------------------------------------------------
# Step 4 — Current class context
# ---------------------------------------------------------------------------

def resolve_current_class(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve sibling members called without an explicit receiver, from within
    a method of the same class (e.g. calling ``helper()`` inside ``MyClass``).
    """
    caller_id = ctx.caller_id
    main_symbol = ctx.main_symbol
    rest_of_callee = ctx.rest_of_callee
    node_ids = ctx.node_ids

    if "." in caller_id:
        parent_class_id = caller_id.rsplit(".", 1)[0]
        target_candidate = f"{parent_class_id}.{main_symbol}"
        if target_candidate in node_ids:
            if rest_of_callee:
                sub_target = f"{target_candidate}.{rest_of_callee}"
                if sub_target in node_ids:
                    return sub_target
            return target_candidate
    return None


# ---------------------------------------------------------------------------
# Step 5 — File-level scope
# ---------------------------------------------------------------------------

def resolve_file_scope(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve symbols declared at the top level of the same file
    (e.g. module-level classes or functions).
    """
    source_file = ctx.source_file
    main_symbol = ctx.main_symbol
    rest_of_callee = ctx.rest_of_callee
    node_ids = ctx.node_ids

    file_candidate = f"{source_file}::{main_symbol}"
    if file_candidate in node_ids:
        if rest_of_callee:
            sub_target = f"{file_candidate}.{rest_of_callee}"
            if sub_target in node_ids:
                return sub_target
        return file_candidate
    return None


# ---------------------------------------------------------------------------
# Step 6 — Package / sibling scope (Go, Swift)
# ---------------------------------------------------------------------------

def resolve_package_siblings(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve symbols declared in sibling files of the same package directory.
    Only active for languages with package-level scope (Go, Swift).

    Self-guarding: returns ``None`` immediately for other languages,
    removing the need for an ``if strategy.has_package_sibling_scope()``
    check in the calling code.
    """
    if not ctx.strategy.has_package_sibling_scope():
        return None

    source_file = ctx.source_file
    main_symbol = ctx.main_symbol
    rest_of_callee = ctx.rest_of_callee
    node_ids = ctx.node_ids
    graph_nodes = ctx.graph_nodes

    caller_dir = Path(source_file).parent
    for nid in node_ids:
        ndata = graph_nodes[nid]
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
    return None


# ---------------------------------------------------------------------------
# Step 7 — Explicit imports & aliases
# ---------------------------------------------------------------------------

def resolve_explicit_imports(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve symbols that were explicitly imported in the caller's file,
    including aliased imports (e.g. ``import X as Y``, ``from A import B``).

    Self-guarding: returns ``None`` if ``main_symbol`` is not in the
    file's imported_symbols map.
    """
    scope = ctx.scope
    if ctx.main_symbol not in scope.imported_symbols:
        return None

    main_symbol = ctx.main_symbol
    rest_of_callee = ctx.rest_of_callee
    parts = ctx.parts
    node_ids = ctx.node_ids
    graph_nodes = ctx.graph_nodes

    target_file_id, original_name = scope.imported_symbols[main_symbol]
    if original_name == "*" or original_name == Path(target_file_id).stem:
        if rest_of_callee:
            target_candidate = f"{target_file_id}::{rest_of_callee}"
            if target_candidate in node_ids:
                return target_candidate
            for nid in node_ids:
                if graph_nodes[nid].get("source_file") == target_file_id and nid.endswith(
                    f".{parts[-1]}"
                ):
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
    return None


# ---------------------------------------------------------------------------
# Step 8 — Wildcard imports
# ---------------------------------------------------------------------------

def resolve_wildcard_imports(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Resolve symbols that may have been pulled in by a wildcard import
    (``from X import *``).
    """
    scope = ctx.scope
    main_symbol = ctx.main_symbol
    rest_of_callee = ctx.rest_of_callee
    node_ids = ctx.node_ids

    for target_file_id in scope.wildcard_imports:
        target_candidate = f"{target_file_id}::{main_symbol}"
        if target_candidate in node_ids:
            if rest_of_callee:
                sub_target = f"{target_candidate}.{rest_of_callee}"
                if sub_target in node_ids:
                    return sub_target
            return target_candidate
    return None


# ---------------------------------------------------------------------------
# Step 9 — Global symbol map fallback
# ---------------------------------------------------------------------------

def resolve_global_fallback(ctx: ResolutionContext) -> str | _StopResolution | None:
    """
    Last-resort lookup in the global symbol map (label → node IDs).

    Prefers unambiguous matches (exactly one candidate globally, or exactly
    one candidate in the same directory as the caller).

    Uses ``strategy.is_builtin()`` instead of a hardcoded cross-language
    blocklist, so it correctly rejects stdlib identifiers for every language.
    """
    main_symbol = ctx.main_symbol
    parts = ctx.parts
    source_file = ctx.source_file
    graph_nodes = ctx.graph_nodes

    # Re-check with strategy (hardcoded blocklist in old code is removed)
    if ctx.strategy.is_builtin(main_symbol):
        return None

    search_label = parts[-1] if len(parts) > 1 else main_symbol
    if len(parts) > 1 and search_label in COMMON_BUILTIN_METHODS:
        return None

    candidates = ctx.global_symbol_map.get(search_label, [])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        caller_parent_dir = Path(source_file).parent
        near_candidates = [
            c
            for c in candidates
            if Path(graph_nodes[c]["source_file"]).parent == caller_parent_dir
        ]
        if len(near_candidates) == 1:
            return near_candidates[0]
    return None


# ---------------------------------------------------------------------------
# Default resolver chain (ordered list — this IS the configuration)
# ---------------------------------------------------------------------------

DEFAULT_RESOLVER_CHAIN: list[ResolverFn] = [
    guard_builtin,           # Step 1: reject stdlib/builtins immediately
    resolve_local_binding,   # Step 2: typed local variable (foo: MyClass → foo.method())
    resolve_self_reference,  # Step 3: self.foo / this.foo / cls.foo
    resolve_current_class,   # Step 4: sibling members within current class
    resolve_file_scope,      # Step 5: file-level declarations
    resolve_package_siblings,  # Step 6: Go/Swift package-level siblings (self-guarding)
    resolve_explicit_imports,  # Step 7: explicitly imported symbols (self-guarding)
    resolve_wildcard_imports,  # Step 8: wildcard-imported symbols
    resolve_global_fallback,   # Step 9: last-resort global symbol map lookup
]

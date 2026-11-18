"""Mermaid diagram generation utilities for Markdown vault rendering.

Provides safe escaping, deduplication, and size bounds to prevent huge diagrams
from hanging browsers or exceeding Markdown renderer limits.
"""

from __future__ import annotations

import re

_MERMAID_ID_CLEAN_RE = re.compile(r"[^a-zA-Z0-9_]")


def sanitize_mermaid_id(raw_id: str) -> str:
    """Sanitizes arbitrary strings into valid Mermaid node identifiers."""
    return _MERMAID_ID_CLEAN_RE.sub("_", raw_id)


def sanitize_mermaid_label(label: str) -> str:
    """Escapes quotation marks and backslashes in node labels."""
    return label.replace("\\", "\\\\").replace('"', '\\"')


def build_mermaid_call_edge(
    source_id: str,
    source_label: str,
    target_id: str,
    target_label: str,
) -> str:
    """Formats a single directed call edge between two nodes."""
    src_id = sanitize_mermaid_id(source_id)
    tgt_id = sanitize_mermaid_id(target_id)
    src_lbl = sanitize_mermaid_label(source_label)
    tgt_lbl = sanitize_mermaid_label(target_label)
    return f'  {src_id}["{src_lbl}"] --> {tgt_id}["{tgt_lbl}"]'


def render_node_call_graph_block(calls: list[str], max_edges: int = 60) -> list[str]:
    """Wraps call edges into a Mermaid fenced block with deduplication and size cap."""
    if not calls:
        return []
    uniq_calls = sorted(set(calls))
    if len(uniq_calls) > max_edges:
        uniq_calls = uniq_calls[:max_edges]
        uniq_calls.append(
            f"  %% ... and {len(calls) - max_edges} more calls omitted for readability"
        )
    return ["```mermaid", "flowchart TD", *uniq_calls, "```", ""]


def render_component_flowchart(
    inter_comp_deps: dict[int, dict[int, int]],
    component_names: dict[int, str],
    max_edges: int = 100,
) -> str:
    """Generates a Mermaid flowchart string for inter-component dependencies."""
    mermaid_lines = ["flowchart TD"]
    all_edges: list[tuple[int, int, int]] = []

    for src_cid, targets in inter_comp_deps.items():
        for tgt_cid, weight in targets.items():
            all_edges.append((weight, src_cid, tgt_cid))

    if all_edges:
        # Sort edges by descending weight for importance
        all_edges.sort(key=lambda x: x[0], reverse=True)
        clipped = len(all_edges) > max_edges
        selected_edges = all_edges[:max_edges]

        for weight, src_cid, tgt_cid in selected_edges:
            src_name = sanitize_mermaid_label(
                component_names.get(src_cid, f"Component {src_cid}")
            )
            tgt_name = sanitize_mermaid_label(
                component_names.get(tgt_cid, f"Component {tgt_cid}")
            )
            mermaid_lines.append(
                f'  comp_{src_cid}["{src_name}"] -->|{weight}| comp_{tgt_cid}["{tgt_name}"]'
            )

        if clipped:
            mermaid_lines.append(
                f"  %% Note: Top {max_edges} of {len(all_edges)} component edges displayed"
            )
    else:
        for cid, name in component_names.items():
            clean_name = sanitize_mermaid_label(name)
            mermaid_lines.append(f'  comp_{cid}["{clean_name}"]')

    return "\n".join(mermaid_lines)

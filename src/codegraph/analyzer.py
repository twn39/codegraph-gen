import logging
import networkx as nx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AnalysisResult(BaseModel):
    god_nodes: list[dict]
    cycles: list[list[str]]
    inter_comp_deps: dict[int, dict[int, int]]


def find_god_nodes(G: nx.DiGraph, top_n: int = 10) -> list[dict]:
    """
    Identifies the most connected nodes (highest degree) in the graph.
    """
    degrees = dict(G.degree())
    sorted_nodes = sorted(degrees.items(), key=lambda item: item[1], reverse=True)

    god_nodes = []
    for nid, deg in sorted_nodes[:top_n]:
        node_data = G.nodes[nid]
        god_nodes.append(
            {
                "id": nid,
                "label": node_data.get("label", nid),
                "type": node_data.get("type", "unknown"),
                "degree": deg,
            }
        )

    return god_nodes


def find_import_cycles(G: nx.DiGraph) -> list[list[str]]:
    """
    Detects circular imports at the file level in the graph.
    """
    # Create a subgraph of only file nodes and import edges
    file_nodes = [n for n, d in G.nodes(data=True) if d.get("type") == "file"]

    file_subgraph = G.subgraph(file_nodes).copy()

    # Keep only 'imports' edges
    non_import_edges = [
        (u, v)
        for u, v, d in file_subgraph.edges(data=True)
        if d.get("relation") != "imports"
    ]
    file_subgraph.remove_edges_from(non_import_edges)

    # Run cycle detection
    try:
        cycles = list(nx.simple_cycles(file_subgraph))
        # Sort by length
        cycles.sort(key=len)
        return cycles
    except Exception as e:
        logger.error(f"Error finding import cycles: {e}")
        return []


def calculate_inter_component_dependencies(
    G: nx.DiGraph, components: dict[int, list[str]]
) -> dict[int, dict[int, int]]:
    """
    Computes dependencies between different components.
    Returns:
        dict mapping source_component_id -> { target_component_id -> connection_count }
    """
    inter_comp_deps = {cid: {} for cid in components}

    # Map member to component for O(1) lookups
    member_to_comp = {}
    for cid, members in components.items():
        for member in members:
            member_to_comp[member] = cid

    for u, v in G.edges():
        u_comp = member_to_comp.get(u)
        v_comp = member_to_comp.get(v)
        if u_comp and v_comp and u_comp != v_comp:
            inter_comp_deps[u_comp][v_comp] = inter_comp_deps[u_comp].get(v_comp, 0) + 1

    return inter_comp_deps


def analyze_graph(G: nx.DiGraph, components: dict[int, list[str]]) -> AnalysisResult:
    """
    Runs full architectural metric analysis on the graph.
    """
    logger.info("Analyzing codebase graph metrics...")
    god_nodes = find_god_nodes(G, 10)
    cycles = find_import_cycles(G)
    inter_comp_deps = calculate_inter_component_dependencies(G, components)

    return AnalysisResult(
        god_nodes=god_nodes, cycles=cycles, inter_comp_deps=inter_comp_deps
    )

import logging
import networkx as nx

logger = logging.getLogger(__name__)

def find_god_nodes(G: nx.DiGraph, top_n: int = 10) -> list[dict]:
    """
    Identifies the most connected nodes (highest degree) in the graph.
    """
    degrees = dict(G.degree())
    sorted_nodes = sorted(degrees.items(), key=lambda item: item[1], reverse=True)
    
    god_nodes = []
    for nid, deg in sorted_nodes[:top_n]:
        node_data = G.nodes[nid]
        god_nodes.append({
            "id": nid,
            "label": node_data.get("label", nid),
            "type": node_data.get("type", "unknown"),
            "degree": deg
        })
        
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
        (u, v) for u, v, d in file_subgraph.edges(data=True)
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

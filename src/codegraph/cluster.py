import logging
import networkx as nx
from networkx.algorithms.community import greedy_modularity_communities

logger = logging.getLogger(__name__)

def detect_components(G: nx.DiGraph) -> tuple[dict[int, list[str]], dict[int, float], dict[int, str]]:
    """
    Detects logical components in the graph using modularity clustering.
    
    Returns:
        components: dict mapping component_id -> list of node_ids
        cohesion_scores: dict mapping component_id -> cohesion density float
        component_names: dict mapping component_id -> human friendly name
    """
    if G.number_of_nodes() == 0:
        return {}, {}, {}

    # Convert to undirected graph for community detection
    U = G.to_undirected()
    
    # Run greedy modularity clustering
    communities = list(greedy_modularity_communities(U))
    
    # Sort communities by size descending
    communities.sort(key=len, reverse=True)
    
    components = {}
    cohesion_scores = {}
    component_names = {}
    
    for idx, member_set in enumerate(communities, start=1):
        members = list(member_set)
        components[idx] = members
        
        # Calculate cohesion: density of the induced subgraph
        subgraph = G.subgraph(members)
        density = nx.density(subgraph)
        cohesion_scores[idx] = round(density, 2)
        
        # Name the component by its most central (highest degree) node
        degrees = dict(G.degree(members))
        if degrees:
            most_central_node = max(degrees, key=degrees.get)
            node_label = G.nodes[most_central_node].get("label", most_central_node)
            # Remove trailing parens/extensions to make clean component name
            clean_name = node_label.replace("()", "").split(".")[0]
            component_names[idx] = f"Component {idx} ({clean_name})"
        else:
            component_names[idx] = f"Component {idx}"
            
    return components, cohesion_scores, component_names

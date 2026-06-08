import logging
import networkx as nx
from networkx.algorithms.community import louvain_communities

logger = logging.getLogger(__name__)


def detect_components(
    G: nx.DiGraph,
) -> tuple[dict[int, list[str]], dict[int, float], dict[int, str]]:
    """
    Detects logical components in the graph using modularity clustering.

    Returns:
        components: dict mapping component_id -> list of node_ids
        cohesion_scores: dict mapping component_id -> cohesion density float
        component_names: dict mapping component_id -> human friendly name
    """
    if G.number_of_nodes() == 0:
        return {}, {}, {}

    # Convert to undirected weighted graph for Louvain community detection
    U = nx.Graph()
    U.add_nodes_from(G.nodes)

    for u, v, d in G.edges(data=True):
        relation = d.get("relation")
        if relation == "contains":
            weight = 10.0
        elif relation == "imports":
            weight = 2.0
        elif relation == "calls":
            weight = 1.0
        else:
            weight = 1.0

        if U.has_edge(u, v):
            U[u][v]["weight"] += weight
        else:
            U.add_edge(u, v, weight=weight)

    # Run Louvain community clustering with fixed seed for reproducibility
    communities = list(louvain_communities(U, weight="weight", seed=42))

    # Sort communities by size descending, breaking ties stably by sorted member IDs
    communities.sort(key=lambda s: (-len(s), sorted(list(s))))

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
            # Sort by degree descending, and break ties alphabetically by node ID
            sorted_nodes = sorted(degrees.keys(), key=lambda n: (-degrees[n], n))
            most_central_node = sorted_nodes[0]
            node_label = G.nodes[most_central_node].get("label", most_central_node)
            # Remove trailing parens/extensions to make clean component name
            clean_name = node_label.replace("()", "").split(".")[0]
            component_names[idx] = f"Component {idx} ({clean_name})"
        else:
            component_names[idx] = f"Component {idx}"

    return components, cohesion_scores, component_names

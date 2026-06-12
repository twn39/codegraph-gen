import logging
from pathlib import Path
import networkx as nx
from codegraph_gen.parser.base import ExtractionResult
from codegraph_gen.resolver import TypeResolver

logger = logging.getLogger(__name__)


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

    # 2. Run Type Resolver (Two-pass type inference & scope/edge resolution)
    resolver = TypeResolver(G, extractions, workspace_dir)
    resolver.propagate_types()
    resolver.resolve_all_edges()

    return G

import json
import webbrowser
from pathlib import Path
from typing import Any

import networkx as nx

from codegraph_gen.builder import build_graph
from codegraph_gen.cluster import detect_components
from codegraph_gen.incremental import PIPELINE_SNAPSHOT_NAME, load_cache_entries


def _load_graph_for_visualization(
    workspace_dir: Path, output_dir: Path
) -> tuple[nx.DiGraph, dict[str, str]]:
    """Load the resolved graph and component map from the single source of truth.

    Resolution order:
    1. ``graph.json`` (preferred SSOT emitted by ``codegraph build``)
    2. ``pipeline_snapshot.json`` (when built with ``--no-export-json``)
    3. ``cache.json`` fallback (rebuild + cluster if neither artifact exists)
    """
    graph_json_path = output_dir / "graph.json"
    if graph_json_path.is_file():
        payload = json.loads(graph_json_path.read_text(encoding="utf-8"))
        G = nx.DiGraph()
        for node in payload.get("nodes", []):
            nid = node["id"]
            G.add_node(
                nid,
                label=node.get("label", nid),
                type=node.get("type", "unknown"),
                source_file=node.get("source_file", "N/A"),
                line_start=node.get("line_start", "?"),
                line_end=node.get("line_end", "?"),
                signature=node.get("signature", ""),
                docstring=node.get("docstring", ""),
            )
        for edge in payload.get("edges", []):
            G.add_edge(
                edge["source"],
                edge["target"],
                relation=edge.get("relation", "other"),
            )
        node_to_component: dict[str, str] = {}
        for comp in payload.get("components", []):
            comp_name = comp.get("name", f"Component {comp.get('id', '?')}")
            for member in comp.get("members", []):
                node_to_component[member] = comp_name
        return G, node_to_component

    snapshot_path = output_dir / PIPELINE_SNAPSHOT_NAME
    if snapshot_path.is_file():
        snap_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        G = nx.node_link_graph(snap_payload["graph"])
        comp_names = {
            int(k): str(v) for k, v in snap_payload.get("component_names", {}).items()
        }
        node_to_component = {}
        for cid_str, members in snap_payload.get("components", {}).items():
            comp_name = comp_names.get(int(cid_str), f"Component {cid_str}")
            for member in members:
                node_to_component[member] = comp_name
        return G, node_to_component

    cache_path = output_dir / "cache.json"
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Graph data not found in {output_dir}. Please run 'codegraph build' first."
        )

    cache_entries = load_cache_entries(cache_path)
    extractions = [entry.result for entry in cache_entries.values()]
    G = build_graph(extractions, workspace_dir)
    components, _, comp_names = detect_components(G)
    node_to_component = {}
    for cid, members in components.items():
        comp_name = comp_names.get(cid, f"Component {cid}")
        for member in members:
            node_to_component[member] = comp_name
    return G, node_to_component


def _compute_layout(G: nx.DiGraph) -> dict[str, Any]:
    """Compute 2D force-directed layout positions with adaptive iteration count."""
    n = G.number_of_nodes()
    if n == 0:
        return {}
    iterations = 120 if n <= 1000 else 60
    k = 1.3 / (n**0.5)
    try:
        return nx.spring_layout(G, k=k, iterations=iterations, seed=42)
    except ImportError:
        # NetworkX >=3.6 requires scipy for spring_layout when n >= 500.
        # Fall back to vectorized NumPy force-directed layout when scipy is absent.
        if n > 2500:
            return nx.circular_layout(G)
        import numpy as np

        rng = np.random.default_rng(42)
        nodes = list(G.nodes())
        idx = {nid: i for i, nid in enumerate(nodes)}
        pos = rng.uniform(-1.0, 1.0, size=(n, 2)).astype(np.float64)
        edges = np.array(
            [(idx[u], idx[v]) for u, v in G.edges() if u != v], dtype=np.int64
        )
        t = 0.2
        dt = t / float(iterations + 1)
        for _ in range(iterations):
            delta = pos[:, None, :] - pos[None, :, :]
            dist = np.linalg.norm(delta, axis=2)
            np.clip(dist, 0.01, None, out=dist)
            disp = np.einsum("ijk,ij->ik", delta, (k * k) / (dist * dist))
            if len(edges) > 0:
                u_idx = edges[:, 0]
                v_idx = edges[:, 1]
                e_delta = pos[u_idx] - pos[v_idx]
                e_dist = np.linalg.norm(e_delta, axis=1)
                np.clip(e_dist, 0.01, None, out=e_dist)
                attract = e_delta * (e_dist / k)[:, None]
                np.add.at(disp, u_idx, -attract)
                np.add.at(disp, v_idx, attract)
            length = np.linalg.norm(disp, axis=1)
            np.clip(length, 0.01, None, out=length)
            pos += disp * (np.minimum(length, t) / length)[:, None]
            t -= dt
        pos -= pos.mean(axis=0)
        max_abs = np.max(np.abs(pos))
        if max_abs > 0:
            pos /= max_abs
        return {
            nid: (float(pos[i, 0]), float(pos[i, 1])) for i, nid in enumerate(nodes)
        }


def _build_plotly_html(
    G: nx.DiGraph,
    pos: dict[str, Any],
    node_to_component: dict[str, str],
    go: Any,
) -> str:
    """Render an interactive Plotly HTML page with a glassmorphism detail sidebar."""
    type_colors = {
        "file": "#3b82f6",
        "class": "#a855f7",
        "struct": "#c084fc",
        "interface": "#e9d5ff",
        "enum": "#86198f",
        "function": "#10b981",
        "method": "#ef4444",
    }
    default_color = "#9ca3af"

    nodes_by_type: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for node_id, data in G.nodes(data=True):
        ntype = data.get("type", "unknown")
        nodes_by_type.setdefault(ntype, []).append((node_id, data))

    fig = go.Figure()

    edge_x: dict[str, list[float | None]] = {
        rel: []
        for rel in ["imports", "calls", "inherits", "implements", "contains", "other"]
    }
    edge_y: dict[str, list[float | None]] = {
        rel: []
        for rel in ["imports", "calls", "inherits", "implements", "contains", "other"]
    }

    for u, v, data in G.edges(data=True):
        if u not in pos or v not in pos:
            continue
        rel = data.get("relation", "other")
        if rel not in edge_x:
            rel = "other"

        x0, y0 = pos[u]
        x1, y1 = pos[v]

        edge_x[rel].extend([float(x0), float(x1), None])
        edge_y[rel].extend([float(y0), float(y1), None])

    edge_colors = {
        "imports": "rgba(59, 130, 246, 0.25)",
        "calls": "rgba(239, 68, 68, 0.25)",
        "inherits": "rgba(168, 85, 247, 0.35)",
        "implements": "rgba(192, 132, 252, 0.35)",
        "contains": "rgba(156, 163, 175, 0.15)",
        "other": "rgba(255, 255, 255, 0.15)",
    }

    for rel in edge_x:
        if edge_x[rel]:
            fig.add_trace(
                go.Scatter(
                    x=edge_x[rel],
                    y=edge_y[rel],
                    line=dict(width=1.2, color=edge_colors[rel]),
                    hoverinfo="none",
                    mode="lines",
                    name=f"Edges ({rel})",
                    legendgroup="edges",
                    legendgrouptitle=dict(text="Graph Relationships"),
                    visible=True
                    if rel in ["calls", "inherits", "implements", "imports"]
                    else "legendonly",
                )
            )

    for ntype, nodes in sorted(nodes_by_type.items()):
        node_x = []
        node_y = []
        node_text = []
        node_sizes = []
        customdata = []
        color = type_colors.get(ntype, default_color)

        for node_id, data in nodes:
            x, y = pos[node_id]
            node_x.append(float(x))
            node_y.append(float(y))

            deg = G.degree(node_id)
            node_sizes.append(10 + min(deg * 1.5, 30))

            label = data.get("label", node_id)
            source_file = data.get("source_file", "N/A")
            line_start = data.get("line_start", "?")
            line_end = data.get("line_end", "?")
            component = node_to_component.get(node_id, "Unassigned")

            tooltip = f"<b>{label}</b> ({ntype.capitalize()})<br><sub>{component}</sub>"
            node_text.append(tooltip)

            incoming = [
                {
                    "label": G.nodes[u].get("label", u),
                    "type": G.nodes[u].get("type", "unknown"),
                }
                for u, _ in G.in_edges(node_id)
            ]
            outgoing = [
                {
                    "label": G.nodes[v].get("label", v),
                    "type": G.nodes[v].get("type", "unknown"),
                }
                for _, v in G.out_edges(node_id)
            ]

            sig = data.get("signature", "")
            doc = data.get("docstring", "")

            customdata.append(
                [
                    node_id,
                    label,
                    ntype,
                    source_file,
                    f"L{line_start}-L{line_end}",
                    sig,
                    doc,
                    json.dumps(incoming),
                    json.dumps(outgoing),
                    component,
                ]
            )

        fig.add_trace(
            go.Scatter(
                x=node_x,
                y=node_y,
                mode="markers",
                name=ntype.capitalize(),
                customdata=customdata,
                marker=dict(
                    symbol="circle",
                    size=node_sizes,
                    color=color,
                    line=dict(color="#090d16", width=1.5),
                ),
                text=node_text,
                hoverinfo="text",
                hovertemplate="%{text}<extra></extra>",
                legendgroup="nodes",
                legendgrouptitle=dict(text="Node Types (Click to Toggle)"),
            )
        )

    fig.update_layout(
        showlegend=True,
        legend=dict(
            x=0.02,
            y=0.98,
            bgcolor="rgba(9, 13, 22, 0.7)",
            bordercolor="rgba(255, 255, 255, 0.08)",
            borderwidth=1,
            font=dict(color="#94a3b8", family="Inter, sans-serif", size=11),
        ),
        margin=dict(b=0, l=0, r=0, t=0),
        hovermode="closest",
        plot_bgcolor="#090d16",
        paper_bgcolor="#090d16",
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
    )

    raw_plotly_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Codegraph Gen — Interactive Architecture Visualizer</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #090d16;
            color: #e2e8f0;
            height: 100vh;
            overflow: hidden;
        }}
        #app-container {{
            display: flex;
            width: 100vw;
            height: 100vh;
        }}
        #graph-container {{
            flex: 1;
            height: 100%;
            position: relative;
        }}
        #details-panel {{
            width: 440px;
            height: 100%;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-left: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: -10px 0 30px rgba(0, 0, 0, 0.3);
            padding: 30px;
            overflow-y: auto;
            z-index: 10;
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
        }}
        .header-section {{
            margin-bottom: 20px;
        }}
        h2 {{
            font-size: 1.35rem;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 4px;
            letter-spacing: -0.025em;
        }}
        .subtitle {{
            font-size: 0.8rem;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.075em;
            font-weight: 600;
        }}
        #panel-placeholder {{
            margin: auto 0;
            text-align: center;
            color: #64748b;
            font-size: 0.9rem;
            line-height: 1.6;
            padding: 40px 20px;
            border: 1px dashed rgba(255, 255, 255, 0.06);
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.01);
        }}
        #panel-placeholder svg {{
            width: 40px;
            height: 40px;
            margin-bottom: 16px;
            stroke: #475569;
        }}
        #panel-content {{
            display: none;
        }}
        .badge-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 14px;
        }}
        .badge {{
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .badge-component {{
            background: rgba(148, 163, 184, 0.12);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.25);
            text-transform: none;
        }}
        .badge-file {{ background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }}
        .badge-class {{ background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }}
        .badge-struct {{ background: rgba(192, 132, 252, 0.15); color: #d8b4fe; border: 1px solid rgba(192, 132, 252, 0.3); }}
        .badge-interface {{ background: rgba(233, 213, 255, 0.15); color: #f3e8ff; border: 1px solid rgba(233, 213, 255, 0.3); }}
        .badge-enum {{ background: rgba(134, 25, 143, 0.15); color: #f5d0fe; border: 1px solid rgba(134, 25, 143, 0.3); }}
        .badge-function {{ background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
        .badge-method {{ background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}

        .section-title {{
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.075em;
            color: #64748b;
            margin-top: 24px;
            margin-bottom: 10px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            padding-bottom: 6px;
        }}
        .file-info {{
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            color: #38bdf8;
            background: rgba(56, 189, 248, 0.05);
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px solid rgba(56, 189, 248, 0.15);
            word-break: break-all;
        }}
        pre {{
            background: #060910;
            padding: 12px;
            border-radius: 8px;
            overflow-x: auto;
            border: 1px solid rgba(255, 255, 255, 0.05);
            margin-bottom: 10px;
        }}
        code {{
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            color: #e2e8f0;
        }}
        .docstring {{
            font-size: 0.85rem;
            line-height: 1.6;
            color: #94a3b8;
            background: rgba(255, 255, 255, 0.01);
            padding: 12px;
            border-radius: 8px;
            border-left: 3px solid #475569;
            white-space: pre-wrap;
        }}
        ul.connections {{
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .connection-item {{
            display: flex;
            align-items: center;
            font-size: 0.85rem;
            background: rgba(255, 255, 255, 0.02);
            padding: 8px 12px;
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.04);
            gap: 10px;
        }}
        .indicator {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            flex-shrink: 0;
        }}
        .indicator-file {{ background: #3b82f6; box-shadow: 0 0 8px #3b82f6; }}
        .indicator-class {{ background: #a855f7; box-shadow: 0 0 8px #a855f7; }}
        .indicator-struct {{ background: #c084fc; box-shadow: 0 0 8px #c084fc; }}
        .indicator-interface {{ background: #e9d5ff; box-shadow: 0 0 8px #e9d5ff; }}
        .indicator-enum {{ background: #86198f; box-shadow: 0 0 8px #86198f; }}
        .indicator-function {{ background: #10b981; box-shadow: 0 0 8px #10b981; }}
        .indicator-method {{ background: #ef4444; box-shadow: 0 0 8px #ef4444; }}

        .type-text {{
            color: #64748b;
            font-size: 0.7rem;
            margin-left: auto;
        }}
        .no-connections {{
            font-size: 0.8rem;
            color: #475569;
            font-style: italic;
        }}
        #details-panel::-webkit-scrollbar {{
            width: 6px;
        }}
        #details-panel::-webkit-scrollbar-track {{
            background: transparent;
        }}
        #details-panel::-webkit-scrollbar-thumb {{
            background: rgba(255, 255, 255, 0.1);
            border-radius: 3px;
        }}
        #details-panel::-webkit-scrollbar-thumb:hover {{
            background: rgba(255, 255, 255, 0.2);
        }}
    </style>
</head>
<body>
    <div id="app-container">
        <div id="graph-container">
            {raw_plotly_html}
        </div>
        <div id="details-panel">
            <div class="header-section">
                <h2>Codegraph Gen</h2>
                <p class="subtitle">Interactive architecture topology</p>
            </div>

            <div id="panel-placeholder">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                <p>Hover over any symbol node in the topology network to display its class definitions, docstrings, and detailed call relationships.</p>
            </div>

            <div id="panel-content">
                <div class="badge-row">
                    <span id="node-type-badge" class="badge">class</span>
                    <span id="node-component-badge" class="badge badge-component">Component</span>
                </div>
                <h3 id="node-name" style="font-size: 1.25rem; font-weight: 700; color: #f8fafc; margin-bottom: 12px; letter-spacing: -0.02em;">MyClass</h3>

                <div class="section-title">Physical File Location</div>
                <div id="node-file" class="file-info">src/main.py (L10-L45)</div>

                <div id="signature-container">
                    <div class="section-title">Definition Signature</div>
                    <pre><code id="node-signature">def my_method(self) -> str</code></pre>
                </div>

                <div class="section-title">Documentation / Docstring</div>
                <div id="node-docstring" class="docstring">No documentation provided.</div>

                <div class="section-title">Incoming Dependencies (Calls / Imports)</div>
                <ul id="node-incoming" class="connections">
                    <li class="no-connections">None</li>
                </ul>

                <div class="section-title">Outgoing Dependencies (Calls)</div>
                <ul id="node-outgoing" class="connections">
                    <li class="no-connections">None</li>
                </ul>
            </div>
        </div>
    </div>

    <script>
        function renderConnectionList(listEl, items) {{
            listEl.innerHTML = '';
            if (!items.length) {{
                const li = document.createElement('li');
                li.className = 'no-connections';
                li.textContent = 'None';
                listEl.appendChild(li);
                return;
            }}
            items.forEach(item => {{
                const li = document.createElement('li');
                li.className = 'connection-item';

                const dot = document.createElement('span');
                dot.className = `indicator indicator-${{item.type}}`;

                const labelEl = document.createElement('b');
                labelEl.textContent = item.label;

                const typeEl = document.createElement('span');
                typeEl.className = 'type-text';
                typeEl.textContent = `(${{item.type}})`;

                li.appendChild(dot);
                li.appendChild(labelEl);
                li.appendChild(typeEl);
                listEl.appendChild(li);
            }});
        }}

        function connectPlotly() {{
            const gd = document.getElementsByClassName('plotly-graph-div')[0];
            if (gd && gd.on) {{
                gd.on('plotly_hover', function(eventData) {{
                    const pt = eventData.points[0];
                    if (pt.customdata) {{
                        const [node_id, label, ntype, source_file, lines, signature, docstring, incoming_json, outgoing_json, component] = pt.customdata;

                        document.getElementById('node-name').textContent = label;
                        const badge = document.getElementById('node-type-badge');
                        badge.textContent = ntype;
                        badge.className = `badge badge-${{ntype}}`;

                        document.getElementById('node-component-badge').textContent = component || 'Unassigned';
                        document.getElementById('node-file').textContent = `${{source_file}} (${{lines}})`;

                        const sigContainer = document.getElementById('signature-container');
                        const sigCode = document.getElementById('node-signature');
                        if (signature) {{
                            sigCode.textContent = signature;
                            sigContainer.style.display = 'block';
                        }} else {{
                            sigContainer.style.display = 'none';
                        }}

                        const docDiv = document.getElementById('node-docstring');
                        if (docstring) {{
                            docDiv.textContent = docstring;
                        }} else {{
                            docDiv.innerHTML = '<i>No documentation / docstring available.</i>';
                        }}

                        renderConnectionList(document.getElementById('node-incoming'), JSON.parse(incoming_json));
                        renderConnectionList(document.getElementById('node-outgoing'), JSON.parse(outgoing_json));

                        document.getElementById('panel-placeholder').style.display = 'none';
                        document.getElementById('panel-content').style.display = 'block';
                    }}
                }});
            }} else {{
                setTimeout(connectPlotly, 50);
            }}
        }}
        connectPlotly();
    </script>
</body>
</html>
"""


def generate_visualization(
    workspace_dir: Path, output_dir: Path, open_browser: bool = True
) -> Path:
    """Export an interactive HTML visualization from the built knowledge graph."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        raise ImportError(
            "Plotly is not installed. Please run 'pip install \"codegraph-gen[viz]\"' or 'uv pip install plotly'."
        )

    try:
        import numpy  # noqa: F401
    except ImportError:
        raise ImportError(
            "NumPy is not installed. Please run 'pip install \"codegraph-gen[viz]\"' or 'uv pip install numpy'."
        )

    G, node_to_component = _load_graph_for_visualization(workspace_dir, output_dir)
    pos = _compute_layout(G)
    custom_ui_html = _build_plotly_html(G, pos, node_to_component, go)

    export_path = output_dir / "graph.html"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(custom_ui_html, encoding="utf-8")

    if open_browser:
        webbrowser.open(f"file://{export_path}")

    return export_path

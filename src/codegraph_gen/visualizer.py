import json
import sys
import webbrowser
from pathlib import Path
import networkx as nx

from codegraph_gen.builder import build_graph
from codegraph_gen.config import CacheEntry
from codegraph_gen.schema import ExtractionResult

def generate_visualization(workspace_dir: Path, output_dir: Path, open_browser: bool = True) -> Path:
    """
    Rebuilds the resolved graph from the cache and exports an interactive HTML visualization.
    """
    # Import locally to fail gracefully only when the function is called
    try:
        import plotly.graph_objects as go
    except ImportError:
        raise ImportError(
            "Plotly is not installed. Please run 'pip install plotly' or 'uv pip install plotly'."
        )

    try:
        import numpy
    except ImportError:
        raise ImportError(
            "NumPy is not installed. Please run 'pip install numpy' or 'uv pip install numpy'."
        )

    cache_path = output_dir / "cache.json"
    if not cache_path.exists():
        raise FileNotFoundError(
            f"Cache file not found at {cache_path}. Please run 'codegraph build' first to populate the cache."
        )

    with open(cache_path, "r", encoding="utf-8") as f:
        cache_data = json.load(f)

    extractions = []
    for k, v in cache_data.items():
        entry = CacheEntry(**v)
        extractions.append(entry.result)

    # Rebuild the fully resolved graph
    G = build_graph(extractions, workspace_dir)

    # Calculate 2D layout using NetworkX force-directed layout
    pos = nx.spring_layout(G, k=1.3 / (G.number_of_nodes() ** 0.5), iterations=120, seed=42)

    # Color Palette for Node Types
    type_colors = {
        "file": "#3b82f6",       # Bright Blue
        "class": "#a855f7",      # Purple
        "struct": "#c084fc",     # Light Purple
        "interface": "#e9d5ff",  # Very Light Purple
        "enum": "#86198f",       # Dark Magenta/Purple
        "function": "#10b981",   # Emerald Green
        "method": "#ef4444",     # Red-Orange
    }
    default_color = "#9ca3af"    # Gray

    # Group nodes by type for separate Plotly traces (enabling interactive legend toggling)
    nodes_by_type = {}
    for node_id, data in G.nodes(data=True):
        ntype = data.get("type", "unknown")
        nodes_by_type.setdefault(ntype, []).append((node_id, data))

    # Create Plotly Figure
    fig = go.Figure()

    # 1. Draw Edges as Line Traces (grouped by relation type to reduce clutter/render efficiently)
    edge_x = {rel: [] for rel in ["imports", "calls", "inherits", "implements", "contains", "other"]}
    edge_y = {rel: [] for rel in ["imports", "calls", "inherits", "implements", "contains", "other"]}

    for u, v, data in G.edges(data=True):
        rel = data.get("relation", "other")
        if rel not in edge_x:
            rel = "other"
        
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        
        edge_x[rel].extend([x0, x1, None])
        edge_y[rel].extend([y0, y1, None])

    # Edge colors with soft opacities
    edge_colors = {
        "imports": "rgba(59, 130, 246, 0.25)",     # Blue
        "calls": "rgba(239, 68, 68, 0.25)",        # Red
        "inherits": "rgba(168, 85, 247, 0.35)",     # Purple
        "implements": "rgba(192, 132, 252, 0.35)",   # Light Purple
        "contains": "rgba(156, 163, 175, 0.15)",   # Thin Gray
        "other": "rgba(255, 255, 255, 0.15)"        # Semi-transparent White
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
                    visible=True if rel in ["calls", "inherits", "implements", "imports"] else "legendonly"
                )
            )

    # 2. Draw Nodes as Scatter Traces
    for ntype, nodes in sorted(nodes_by_type.items()):
        node_x = []
        node_y = []
        node_text = []
        node_sizes = []
        customdata = []
        color = type_colors.get(ntype, default_color)

        for node_id, data in nodes:
            x, y = pos[node_id]
            node_x.append(x)
            node_y.append(y)
            
            # Node Size based on degree (number of connections)
            deg = G.degree(node_id)
            node_sizes.append(10 + min(deg * 1.5, 30))

            # Tooltip Details
            label = data.get("label", node_id)
            source_file = data.get("source_file", "N/A")
            line_start = data.get("line_start", "?")
            line_end = data.get("line_end", "?")
            
            tooltip = f"<b>{label}</b> ({ntype.capitalize()})"
            node_text.append(tooltip)

            # Build detailed lists for incoming/outgoing connections to display in the side panel
            incoming = []
            for u, v in G.in_edges(node_id):
                incoming.append({
                    "label": G.nodes[u].get("label", u),
                    "type": G.nodes[u].get("type", "unknown")
                })
            
            outgoing = []
            for u, v in G.out_edges(node_id):
                outgoing.append({
                    "label": G.nodes[v].get("label", v),
                    "type": G.nodes[v].get("type", "unknown")
                })

            sig = data.get("signature", "")
            doc = data.get("docstring", "")
            if doc:
                doc = doc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

            customdata.append([
                node_id,
                label,
                ntype,
                source_file,
                f"L{line_start}-L{line_end}",
                sig,
                doc,
                json.dumps(incoming),
                json.dumps(outgoing)
            ])

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
                    line=dict(color="#090d16", width=1.5)
                ),
                text=node_text,
                hoverinfo="text",
                hovertemplate="%{text}<extra></extra>",
                legendgroup="nodes",
                legendgrouptitle=dict(text="Node Types (Click to Toggle)")
            )
        )

    # Figure Layout Settings
    fig.update_layout(
        showlegend=True,
        legend=dict(
            x=0.02,
            y=0.98,
            bgcolor="rgba(9, 13, 22, 0.7)",
            bordercolor="rgba(255, 255, 255, 0.08)",
            borderwidth=1,
            font=dict(color="#94a3b8", family="Inter, sans-serif", size=11)
        ),
        margin=dict(b=0, l=0, r=0, t=0),
        hovermode="closest",
        plot_bgcolor="#090d16",
        paper_bgcolor="#090d16",
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False)
    )

    # Export raw HTML div and script (exclude main HTML body to wrap with custom UI template)
    raw_plotly_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    # Custom HTML Template with glassmorphism sidebar
    custom_ui_html = f"""<!DOCTYPE html>
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
        .badge {{
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 14px;
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
        /* Custom scrollbar for panel */
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
                <span id="node-type-badge" class="badge">class</span>
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
        function connectPlotly() {{
            const gd = document.getElementsByClassName('plotly-graph-div')[0];
            if (gd && gd.on) {{
                console.log("Plotly object initialized, binding hover events...");
                gd.on('plotly_hover', function(eventData) {{
                    const pt = eventData.points[0];
                    if (pt.customdata) {{
                        const [node_id, label, ntype, source_file, lines, signature, docstring, incoming_json, outgoing_json] = pt.customdata;
                        
                        // Update Node Title and Badge
                        document.getElementById('node-name').innerText = label;
                        const badge = document.getElementById('node-type-badge');
                        badge.innerText = ntype;
                        badge.className = `badge badge-${{ntype}}`;
                        
                        // Update File Info
                        document.getElementById('node-file').innerText = `${{source_file}} (${{lines}})`;
                        
                        // Update Signature
                        const sigContainer = document.getElementById('signature-container');
                        const sigCode = document.getElementById('node-signature');
                        if (signature) {{
                            sigCode.innerText = signature;
                            sigContainer.style.display = 'block';
                        }} else {{
                            sigContainer.style.display = 'none';
                        }}
                        
                        // Update Docstring
                        const docDiv = document.getElementById('node-docstring');
                        docDiv.innerHTML = docstring ? docstring : '<i>No documentation / docstring available.</i>';
                        
                        // Update Incoming Calls
                        const incoming = JSON.parse(incoming_json);
                        const incList = document.getElementById('node-incoming');
                        incList.innerHTML = '';
                        if (incoming.length > 0) {{
                            incoming.forEach(item => {{
                                const li = document.createElement('li');
                                li.className = 'connection-item';
                                li.innerHTML = `<span class="indicator indicator-${{item.type}}"></span> <b>${{item.label}}</b> <span class="type-text">(${{item.type}})</span>`;
                                incList.appendChild(li);
                            }});
                        }} else {{
                            incList.innerHTML = '<li class="no-connections">None</li>';
                        }}
                        
                        // Update Outgoing Calls
                        const outgoing = JSON.parse(outgoing_json);
                        const outList = document.getElementById('node-outgoing');
                        outList.innerHTML = '';
                        if (outgoing.length > 0) {{
                            outgoing.forEach(item => {{
                                const li = document.createElement('li');
                                li.className = 'connection-item';
                                li.innerHTML = `<span class="indicator indicator-${{item.type}}"></span> <b>${{item.label}}</b> <span class="type-text">(${{item.type}})</span>`;
                                outList.appendChild(li);
                            }});
                        }} else {{
                            outList.innerHTML = '<li class="no-connections">None</li>';
                        }}
                        
                        // Toggle panels
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

    export_path = output_dir / "graph.html"
    with open(export_path, "w", encoding="utf-8") as f:
        f.write(custom_ui_html)
    
    if open_browser:
        webbrowser.open(f"file://{export_path}")
        
    return export_path

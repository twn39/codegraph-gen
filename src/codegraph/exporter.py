import logging
import os
import re
import shutil
from pathlib import Path
import networkx as nx
from codegraph.analyzer import find_god_nodes, find_import_cycles
from codegraph.ai import build_agent_prompt

logger = logging.getLogger(__name__)

def get_node_filename(node_id: str) -> str:
    """Safely converts a node ID into a clean markdown filename."""
    # Replace slashes, colons, dots, spaces, asterisks with underscores
    cleaned = re.sub(r'[\\/.: *?#^[\]"<>|]+', "_", node_id)
    return cleaned + ".md"

def get_component_filename(component_name: str) -> str:
    """Safely converts a component name into a clean markdown filename."""
    cleaned = re.sub(r'[\\/.: *?#^[\]"<>|()]+', "_", component_name)
    return cleaned + ".md"

def generate_architecture_description(G: nx.DiGraph) -> str:
    """Dynamically generates an overall architecture description in Chinese."""
    file_nodes = [n for n, d in G.nodes(data=True) if d.get("type") == "file"]
    
    # 1. Group files by top-level directory
    directories = {}
    for f in file_nodes:
        parts = Path(f).parts
        if len(parts) > 1:
            dir_name = parts[0]
            directories.setdefault(dir_name, []).append(f)
        else:
            directories.setdefault(".", []).append(f)
            
    dir_desc_lines = []
    if directories:
        dir_desc_lines.append("### 物理结构与层级 (Physical Structure & Layout)")
        # Sort directories by file count descending
        for d_name, f_list in sorted(directories.items(), key=lambda x: -len(x[1])):
            if d_name == ".":
                dir_desc_lines.append(f"- **根目录 `.`** : 包含 {len(f_list)} 个配置文件或源文件。")
            else:
                files_preview = ", ".join(f"`{Path(f).name}`" for f in f_list[:3])
                suffix = " 等" if len(f_list) > 3 else ""
                dir_desc_lines.append(f"- **`{d_name}/`** : 包含 {len(f_list)} 个源文件（如 {files_preview}{suffix}）。")

    # 2. Find core entry points
    entry_nodes = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("type") in ("function", "method"):
            name = ndata.get("label", "").lower()
            if any(k in name for k in ("main", "app", "run", "cli", "serve")):
                entry_nodes.append((ndata.get("label"), ndata.get("source_file")))

    entry_desc_lines = []
    if entry_nodes:
        entry_desc_lines.append("### 核心执行入口 (Core Execution Entrypoints)")
        for name, sf in entry_nodes[:5]:
            entry_desc_lines.append(f"- `{name}` (定义于 [{sf}](nodes/{get_node_filename(sf)}))")

    # 3. Compose architecture markdown
    arch_lines = [
        "## 整体架构描述 (Architecture Overview)",
        "本项目采用模块化软件系统设计。静态解析器对代码进行全量扫描并生成底层依赖网络，图谱通过社区发现算法完成逻辑聚类（Component-level clustering）。",
        ""
    ]
    if dir_desc_lines:
        arch_lines.extend(dir_desc_lines)
        arch_lines.append("")
    if entry_desc_lines:
        arch_lines.extend(entry_desc_lines)
        arch_lines.append("")

    arch_lines.extend([
        "### 逻辑控制流与数据依赖 (Control Flow & Data Dependencies)",
        "代码系统中的数据流与逻辑控制围绕核心的类与组件进行。主要的组件及其逻辑拓扑调用关系展示在下方的关系图中，它以图形化形式呈现了系统的核心组件交互模型。"
    ])

    return "\n".join(arch_lines)

def to_markdown_vault(
    G: nx.DiGraph,
    components: dict[int, list[str]],
    cohesion_scores: dict[int, float],
    component_names: dict[int, str],
    output_dir: Path,
    ai_insights: str = None
):
    """
    Exports the NetworkX graph representation of the codebase as a directory
    of structured Markdown files (an Obsidian-like vault).
    """
    # 1. Clean and recreate output directories
    if output_dir.exists():
        try:
            shutil.rmtree(output_dir)
        except Exception as e:
            logger.warning(f"Could not fully clear output directory: {e}")
            
    nodes_dir = output_dir / "nodes"
    comps_dir = output_dir / "components"
    
    nodes_dir.mkdir(parents=True, exist_ok=True)
    comps_dir.mkdir(parents=True, exist_ok=True)

    # Map from node ID -> component name
    node_component_map = {}
    for cid, members in components.items():
        comp_name = component_names.get(cid, f"Component {cid}")
        for member in members:
            node_component_map[member] = comp_name

    # 2. Write one .md file per node
    for nid, ndata in G.nodes(data=True):
        label = ndata.get("label", nid)
        ntype = ndata.get("type", "unknown")
        sf = ndata.get("source_file", "")
        line_start = ndata.get("line_start", 1)
        line_end = ndata.get("line_end", 1)
        sig = ndata.get("signature", "")
        doc = ndata.get("docstring", "")
        comp_name = node_component_map.get(nid, "None")
        
        # Determine language for signature codeblock
        lang_ext = Path(sf).suffix.lower() if sf else ""
        lang_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
            ".swift": "swift"
        }
        code_lang = lang_map.get(lang_ext, "")

        lines = [
            "---",
            f"id: \"{nid}\"",
            f"label: \"{label}\"",
            f"type: \"{ntype}\"",
            f"source_file: \"{sf}\"",
            f"line_start: {line_start}",
            f"line_end: {line_end}",
            f"component: \"{comp_name}\"",
            "---",
            "",
            f"# {label}",
            "",
            f"**Type:** `{ntype}`  ",
            f"**Defined in:** [{sf}](../nodes/{get_node_filename(sf)}) (Lines {line_start}-{line_end})  " if ntype != "file" else f"**File path:** `{sf}`  ",
            f"**Component:** {comp_name}  " if comp_name != "None" else "",
            "",
        ]

        if sig:
            lines += [
                "## Definition",
                f"```{code_lang}",
                sig,
                "```",
                ""
            ]

        if doc:
            lines += [
                "## Description",
                doc,
                ""
            ]

        # Edges classification
        incoming = []
        outgoing = []
        contains_children = []
        contained_in_parent = None
        inherits_list = []
        inherited_by_list = []
        imports_list = []
        imported_by_list = []
        
        # For local node call graph
        mermaid_calls = []

        # Outgoing edges
        for _, target, edata in G.out_edges(nid, data=True):
            rel = edata.get("relation")
            target_data = G.nodes.get(target, {})
            target_label = target_data.get("label", target)
            target_file = get_node_filename(target)
            
            if rel == "contains":
                contains_children.append(f"- [{target_label}]({target_file}) (`{target_data.get('type')}`)")
            elif rel == "inherits":
                inherits_list.append(f"- [{target_label}]({target_file}) (`{target_data.get('type')}`)")
            elif rel == "implements":
                inherits_list.append(f"- [{target_label}]({target_file}) (Implements `{target_label}`)")
            elif rel == "imports":
                imports_list.append(f"- [{target_label}]({target_file})")
            elif rel == "calls":
                outgoing.append(f"- Calls [{target_label}]({target_file}) (`{target_data.get('type')}`)")
                # Add to local mermaid
                src_m_id = re.sub(r'[^a-zA-Z0-9_]', '_', nid)
                tgt_m_id = re.sub(r'[^a-zA-Z0-9_]', '_', target)
                mermaid_calls.append(f"  {src_m_id}[\"{label}\"] --> {tgt_m_id}[\"{target_label}\"]")

        # Incoming edges
        for source, _, edata in G.in_edges(nid, data=True):
            rel = edata.get("relation")
            source_data = G.nodes.get(source, {})
            source_label = source_data.get("label", source)
            source_file = get_node_filename(source)
            
            if rel == "contains":
                contained_in_parent = f"[{source_label}]({source_file}) (`{source_data.get('type')}`)"
            elif rel == "inherits":
                inherited_by_list.append(f"- [{source_label}]({source_file}) (`{source_data.get('type')}`)")
            elif rel == "implements":
                inherited_by_list.append(f"- [{source_label}]({source_file}) (Implemented by `{source_label}`)")
            elif rel == "imports":
                imported_by_list.append(f"- [{source_label}]({source_file})")
            elif rel == "calls":
                incoming.append(f"- Called by [{source_label}]({source_file}) (`{source_data.get('type')}`)")
                # Add to local mermaid
                src_m_id = re.sub(r'[^a-zA-Z0-9_]', '_', source)
                tgt_m_id = re.sub(r'[^a-zA-Z0-9_]', '_', nid)
                mermaid_calls.append(f"  {src_m_id}[\"{source_label}\"] --> {tgt_m_id}[\"{label}\"]")

        # Add relationships to doc
        if contained_in_parent:
            lines += [f"**Contained in parent:** {contained_in_parent}", ""]

        if inherits_list:
            lines += ["## Inherits / Implements", *inherits_list, ""]
            
        if inherited_by_list:
            lines += ["## Inherited / Implemented By", *inherited_by_list, ""]

        if contains_children:
            lines += ["## Contains Symbols", *contains_children, ""]

        if imports_list:
            lines += ["## Imports", *imports_list, ""]

        if imported_by_list:
            lines += ["## Imported By", *imported_by_list, ""]

        if outgoing or incoming:
            lines += ["## Call Graph"]
            
            # Insert local Mermaid diagram
            if mermaid_calls:
                # Deduplicate mermaid edges
                uniq_m_calls = sorted(list(set(mermaid_calls)))
                lines += [
                    "```mermaid",
                    "flowchart TD",
                    *uniq_m_calls,
                    "```",
                    ""
                ]
                
            if outgoing:
                lines += ["### Outgoing Calls", *outgoing, ""]
            if incoming:
                lines += ["### Incoming Calls (Backlinks)", *incoming, ""]

        node_fname = get_node_filename(nid)
        (nodes_dir / node_fname).write_text("\n".join(lines), encoding="utf-8")

    # 3. Write one .md file per component
    # Pre-calculate inter-component dependencies
    inter_comp_deps = {}
    for cid in components:
        inter_comp_deps[cid] = {}
        
    for u, v, edata in G.edges(data=True):
        u_comp = None
        v_comp = None
        for cid, members in components.items():
            if u in members: u_comp = cid
            if v in members: v_comp = cid
        if u_comp and v_comp and u_comp != v_comp:
            inter_comp_deps[u_comp][v_comp] = inter_comp_deps[u_comp].get(v_comp, 0) + 1

    for cid, members in components.items():
        comp_name = component_names.get(cid, f"Component {cid}")
        cohesion = cohesion_scores.get(cid, 0.0)
        
        lines = [
            "---",
            "type: \"component\"",
            f"cohesion: {cohesion}",
            f"members_count: {len(members)}",
            "---",
            "",
            f"# {comp_name}",
            "",
            f"**Internal Cohesion:** {cohesion} (density score)  ",
            f"**Members count:** {len(members)} nodes  ",
            "",
            "## Members",
        ]
        
        for nid in sorted(members, key=lambda n: G.nodes[n].get("label", n)):
            ndata = G.nodes[nid]
            label = ndata.get("label", nid)
            ntype = ndata.get("type", "unknown")
            sf = ndata.get("source_file", "")
            lines.append(f"- [{label}](../nodes/{get_node_filename(nid)}) (`{ntype}` in `{sf}`)")
            
        lines.append("")
        
        # Dependencies to other components
        deps = inter_comp_deps.get(cid, {})
        if deps:
            lines += ["## Component Dependencies", ""]
            for target_cid, count in sorted(deps.items(), key=lambda x: -x[1]):
                target_name = component_names.get(target_cid, f"Component {target_cid}")
                target_file = get_component_filename(target_name)
                lines.append(f"- Depends on [{target_name}]({target_file}) ({count} calls/connections)")
            lines.append("")
            
        comp_fname = get_component_filename(comp_name)
        (comps_dir / comp_fname).write_text("\n".join(lines), encoding="utf-8")

    # 4. Write main README.md
    files_count = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "file")
    symbols_count = G.number_of_nodes() - files_count
    
    god_nodes = find_god_nodes(G, 10)
    cycles = find_import_cycles(G)

    # Generate Architecture Description section
    arch_desc = generate_architecture_description(G)

    readme_lines = [
        "# Codebase Knowledge Graph",
        "",
        "Welcome to the codebase knowledge graph of this workspace. This index represents files, classes, structs, methods, and functions as nodes, with call and dependency relationships between them.",
        "",
        "## Statistics",
        f"- **Files Scanned:** {files_count}",
        f"- **Symbols Extracted:** {symbols_count}",
        f"- **Call/Relation Edges:** {G.number_of_edges()}",
        "",
        arch_desc,
        "",
        "## Component Dependency Graph",
    ]
    
    mermaid_lines = ["flowchart TD"]
    has_relations = False
    for src_cid, targets in inter_comp_deps.items():
        src_name = component_names[src_cid]
        src_id = re.sub(r'[^a-zA-Z0-9_]', '_', src_name)
        for tgt_cid, weight in targets.items():
            tgt_name = component_names[tgt_cid]
            tgt_id = re.sub(r'[^a-zA-Z0-9_]', '_', tgt_name)
            mermaid_lines.append(f"  {src_id}[\"{src_name}\"] -->|{weight}| {tgt_id}[\"{tgt_name}\"]")
            has_relations = True
            
    if not has_relations:
        for cid, name in component_names.items():
            cid_id = re.sub(r'[^a-zA-Z0-9_]', '_', name)
            mermaid_lines.append(f"  {cid_id}[\"{name}\"]")
            
    mermaid_graph = "\n".join(mermaid_lines)
    readme_lines += [
        "```mermaid",
        mermaid_graph,
        "```",
        ""
    ]

    readme_lines += [
        "## Logical Components Summary",
        "The codebase has been automatically clustered into logical components based on coupling and call density:",
        "",
        "| Component | Cohesion (Density) | Size (Nodes) |",
        "|---|---|---|",
    ]
    
    for cid, members in components.items():
        comp_name = component_names[cid]
        cohesion = cohesion_scores[cid]
        comp_file = f"components/{get_component_filename(comp_name)}"
        readme_lines.append(f"| [{comp_name}]({comp_file}) | {cohesion} | {len(members)} |")

    readme_lines += [
        "",
        "## God Nodes (Top Core Abstractions)",
        "These symbols have the highest degrees (most connections) in the codebase. Modifying them may have wide-reaching effects:",
        "",
        "| Symbol | Type | Connections | File |",
        "|---|---|---|---|",
    ]
    
    for node in god_nodes:
        nid = node["id"]
        ndata = G.nodes[nid]
        sf = ndata.get("source_file", "")
        readme_lines.append(f"| [{node['label']}](nodes/{get_node_filename(nid)}) | `{node['type']}` | {node['degree']} | `{sf}` |")
        
    readme_lines += [
        "",
        "## Circular Import Dependencies",
    ]
    
    if cycles:
        readme_lines += [
            "WARNING: The following circular import loops were detected at the file level. Consider refactoring to reduce coupling:",
            ""
        ]
        for idx, cycle in enumerate(cycles, start=1):
            cycle_str = " ➡️ ".join(f"[{Path(f).name}](nodes/{get_node_filename(f)})" for f in cycle + [cycle[0]])
            readme_lines.append(f"{idx}. {cycle_str}")
    else:
        readme_lines.append("No circular imports detected. Perfect modularity!")
        
    readme_lines.append("")
    
    # If no ai_insights are passed, write the placeholder instructions
    if not ai_insights:
        ai_insights = """> 💡 **提示**：当前尚未生成 AI 架构深度洞察。请使用您的 AI Agent（如 Antigravity、Claude Code、Codex 等）读取 `.codegraph/AGENT_PROMPT.md` 中的提示词，并将分析结果填入此处。"""
        
    readme_lines += [
        "## AI 架构深度洞察 (AI Architectural Insights)",
        ai_insights,
        ""
    ]
        
    (output_dir / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")
    
    # Write AGENT_PROMPT.md
    agent_prompt = build_agent_prompt(
        G=G,
        components=components,
        cohesion_scores=cohesion_scores,
        component_names=component_names,
        god_nodes=god_nodes,
        cycles=cycles,
        mermaid_graph=mermaid_graph
    )
    (output_dir / "AGENT_PROMPT.md").write_text(agent_prompt, encoding="utf-8")
    
    # Write AGENTS.md to the project root
    project_root = output_dir.parent
    agents_file = project_root / "AGENTS.md"
    
    agents_rule_header = "## codegraph"
    agents_rule_body = """## codegraph

This project has a codegraph knowledge graph at .codegraph/.

Rules for AI Agents (Antigravity, Claude Code, Codex, etc.):
- Before answering architecture or codebase questions, read .codegraph/README.md for god nodes and community structure.
- Refer to .codegraph/components/ and .codegraph/nodes/ to navigate codebase relationships and definitions instead of reading raw files directly.
- If .codegraph/README.md does not contain AI Architectural Insights, read .codegraph/AGENT_PROMPT.md, perform the deep architectural analysis, and write your report into the `AI 架构深度洞察` section of .codegraph/README.md.
- After modifying code files in this workspace, remind the user to run `codegraph build .` to keep the knowledge graph current.
"""

    if agents_file.exists():
        try:
            content = agents_file.read_text(encoding="utf-8")
            if agents_rule_header not in content:
                # Append to existing file
                new_content = content.rstrip() + "\n\n" + agents_rule_body
                agents_file.write_text(new_content, encoding="utf-8")
                logger.info("Appended codegraph rules to existing AGENTS.md at root")
            else:
                logger.info("codegraph rules already exist in AGENTS.md at root")
        except Exception as e:
            logger.warning(f"Could not read or append to existing AGENTS.md: {e}")
    else:
        try:
            agents_file.write_text(agents_rule_body, encoding="utf-8")
            logger.info("Created new AGENTS.md with codegraph rules at root")
        except Exception as e:
            logger.warning(f"Could not create AGENTS.md at root: {e}")

import logging
import networkx as nx

logger = logging.getLogger(__name__)


def build_agent_prompt(
    G: nx.DiGraph,
    components: dict[int, list[str]],
    cohesion_scores: dict[int, float],
    component_names: dict[int, str],
    god_nodes: list[dict],
    cycles: list[list[str]],
    mermaid_graph: str,
) -> str:
    """
    Constructs a detailed architectural prompt designed for external AI agents
    (such as Antigravity, Claude Code, or Codex) to read and perform deep
    architectural analysis.
    """
    logger.info("Generating agent analysis prompt based on graph structure...")

    # Format metadata lists
    files_count = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "file")
    symbols_count = G.number_of_nodes() - files_count

    comp_list = []
    for cid, members in components.items():
        comp_list.append(
            f"- {component_names[cid]}: cohesion={cohesion_scores[cid]:.4f}, members_count={len(members)}"
        )
    comp_str = "\n".join(comp_list)

    god_list = []
    for node in god_nodes:
        sf = G.nodes[node["id"]].get("source_file", "")
        god_list.append(
            f"- {node['label']} ({node['type']}): degree={node['degree']}, file={sf}"
        )
    god_str = "\n".join(god_list)

    cycle_list = []
    for c in cycles:
        cycle_list.append(" -> ".join(c + [c[0]]))
    cycle_str = "\n".join(cycle_list) if cycle_list else "无循环依赖"

    prompt = f"""# Codebase Architecture Analysis Prompt

你是一个资深的软件架构专家。根据下面提供的代码库知识图谱元数据和主要组件之间的关系，为该项目撰写一份深刻的“AI 架构深度洞察分析报告”（使用中文书写）。

【代码库图谱数据统计】
- 物理文件数: {files_count}
- 符号数 (类/结构体/函数/方法): {symbols_count}
- 依赖与调用边总数: {G.number_of_edges()}

【逻辑组件划分 (Modularity Components)】
{comp_str}

【组件依赖拓扑图 (Mermaid Flowchart)】
```mermaid
{mermaid_graph}
```

【核心抽象 God Nodes (度数代表连接的其它符号总数)】
{god_str}

【文件级循环导入依赖 (Import Cycles)】
{cycle_str}

请基于上述代码结构和组件的关联关系，提供深入洞察分析，重点阐述以下三个方面：
1. **系统架构评估**：说明代码库的设计模式、模块化水准、物理目录与逻辑组件契合度。
2. **核心抽象与边界评估**：对 God Nodes 进行深入把脉，分析哪些是核心支撑，哪些职责过重（God Object / Fat Class）可能导致高风险。
3. **潜在瓶颈与架构重构建议**：指出高耦合风险点、循环依赖负面影响，并给出具体、可操作的重构优化方案（如解耦、提取接口、依赖倒置等）。

请以标准的 Markdown 格式输出，排版要清晰美观、专业严谨，不要包含首尾的 ```markdown 和 ``` 语法包裹标记，直接输出内容。
"""
    return prompt

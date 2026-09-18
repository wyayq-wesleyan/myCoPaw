"""Tool to generate charts (bar, pie, line, etc.) using matplotlib."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from agentscope.message import DataBlock, TextBlock, ToolResultState, URLSource
from agentscope.tool import ToolChunk

from ...runtime.tool_registry import tool_descriptor


def _resolve_media_dir() -> Path:
    """Resolve the same media directory used by chat uploads."""
    from ...app.channels.console.channel import DEFAULT_MEDIA_DIR

    workspace_dir = os.environ.get("QWENPAW_WORKSPACE_DIR", "").strip()
    media_dir = (
        Path(workspace_dir) / "media"
        if workspace_dir
        else DEFAULT_MEDIA_DIR
    )
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def _path_to_file_url(path: Path) -> str:
    """Convert a local file path to a file:// URL."""
    abs_path = str(path.resolve())
    if os.name == "nt":
        abs_path = abs_path.replace("\\", "/")
        return f"file:///{abs_path}"
    return f"file://{abs_path}"


# Chart type mapping
_CHART_TYPES = {
    "bar": "柱状图",
    "pie": "饼状图",
    "line": "折线图",
    "scatter": "散点图",
    "area": "面积图",
    "horizontal_bar": "水平柱状图",
}


def _generate_chart_code(
    chart_type: str,
    data: dict,
    title: str,
    xlabel: str,
    ylabel: str,
    width: float,
    height: float,
    output_path: str,
) -> str:
    """Generate matplotlib Python code for the chart."""
    # Set up Chinese font support
    font_setup = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# Try to use Chinese font
for font_name in ['Noto Sans CJK SC', 'Noto Serif CJK SC', 'SimHei', 'Microsoft YaHei', 'DejaVu Sans']:
    try:
        font_path = fm.findfont(fm.FontProperties(family=font_name))
        if font_path and 'DejaVu' not in font_path:
            plt.rcParams['font.sans-serif'] = [font_name]
            plt.rcParams['axes.unicode_minus'] = False
            break
    except Exception:
        continue
"""

    labels = data.get("labels", [])
    values = data.get("values", [])
    title_escaped = title.replace("'", "\\'")
    xlabel_escaped = xlabel.replace("'", "\\'")
    ylabel_escaped = ylabel.replace("'", "\\'")

    if chart_type == "bar":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
colors = plt.cm.Set3(range(len(labels)))
bars = ax.bar(labels, values, color=colors)
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
ax.set_xlabel('{xlabel_escaped}')
ax.set_ylabel('{ylabel_escaped}')
# Add value labels on bars
for bar, val in zip(bars, values):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(values)*0.01,
            str(val), ha='center', va='bottom', fontsize=10)
plt.xticks(rotation=45 if len(labels) > 5 else 0, ha='right')
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    elif chart_type == "pie":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
colors = plt.cm.Set3(range(len(labels)))
wedges, texts, autotexts = ax.pie(values, labels=labels, colors=colors,
                                   autopct='%1.1f%%', startangle=90,
                                   textprops={{'fontsize': 10}})
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    elif chart_type == "line":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
ax.plot(labels, values, marker='o', linewidth=2, markersize=6)
ax.fill_between(range(len(labels)), values, alpha=0.3)
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
ax.set_xlabel('{xlabel_escaped}')
ax.set_ylabel('{ylabel_escaped}')
ax.grid(True, alpha=0.3)
plt.xticks(rotation=45 if len(labels) > 5 else 0, ha='right')
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    elif chart_type == "scatter":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
ax.scatter(range(len(labels)), values, s=100, alpha=0.7)
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
ax.set_xlabel('{xlabel_escaped}')
ax.set_ylabel('{ylabel_escaped}')
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    elif chart_type == "area":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
ax.fill_between(range(len(labels)), values, alpha=0.5)
ax.plot(labels, values, linewidth=2)
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
ax.set_xlabel('{xlabel_escaped}')
ax.set_ylabel('{ylabel_escaped}')
ax.grid(True, alpha=0.3)
plt.xticks(rotation=45 if len(labels) > 5 else 0, ha='right')
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    elif chart_type == "horizontal_bar":
        chart_code = f"""
fig, ax = plt.subplots(figsize=({width}, {height}))
colors = plt.cm.Set3(range(len(labels)))
y_pos = range(len(labels))
ax.barh(y_pos, values, color=colors)
ax.set_yticks(y_pos)
ax.set_yticklabels(labels)
ax.set_title('{title_escaped}', fontsize=14, fontweight='bold')
ax.set_xlabel('{xlabel_escaped}')
# Add value labels
for i, val in enumerate(values):
    ax.text(val + max(values)*0.01, i, str(val), va='center', fontsize=10)
plt.tight_layout()
plt.savefig('{output_path}', dpi=150, bbox_inches='tight')
plt.close()
"""
    else:
        return ""

    return font_setup + chart_code


@tool_descriptor(
    name="generate_chart",
    async_execution=False,
    tool_type="file",
    policy_name="GenerateChart",
    default_policy="allow",
    policy_reason="Generates a chart image for user download",
    ui_description="根据数据生成可直接下载的图表（柱状图、饼状图、折线图等）",
    ui_icon="📊",
)
def generate_chart(
    chart_type: str,
    data: str,
    title: str = "图表",
    xlabel: str = "",
    ylabel: str = "",
    width: float = 8.0,
    height: float = 6.0,
    filename: str = "chart",
) -> ToolChunk:
    """Generate a chart image and attach it to the current chat response.

    Args:
        chart_type: Chart type: bar (柱状图), pie (饼状图), line (折线图),
                    scatter (散点图), area (面积图), horizontal_bar (水平柱状图)
        data: JSON string with "labels" and "values" arrays.
              Example: '{"labels": ["A", "B", "C"], "values": [10, 20, 15]}'
        title: Chart title
        xlabel: X-axis label
        ylabel: Y-axis label
        width: Figure width in inches (default 8)
        height: Figure height in inches (default 6)
        filename: Output filename without extension
    """
    if chart_type not in _CHART_TYPES:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(
                type="text",
                text=f"不支持的图表类型: {chart_type}。支持的类型: {', '.join(f'{k}({v})' for k, v in _CHART_TYPES.items())}",
            )],
        )

    # Parse data
    try:
        chart_data = json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError as exc:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text=f"数据格式错误: {exc}")],
        )

    labels = chart_data.get("labels", [])
    values = chart_data.get("values", [])

    if not labels or not values:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text="数据不能为空，需要 labels 和 values 数组")],
        )

    if len(labels) != len(values):
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text=f"labels 和 values 长度不一致: {len(labels)} vs {len(values)}")],
        )

    # Sanitize filename
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", filename)[:100] or "chart"
    stored_name = f"{uuid.uuid4().hex}_{safe_name}.png"

    media_dir = _resolve_media_dir()
    output_path = media_dir / stored_name

    try:
        chart_code = _generate_chart_code(
            chart_type, chart_data, title, xlabel, ylabel, width, height, str(output_path)
        )
        exec(chart_code)  # noqa: S102
    except ImportError:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text="matplotlib 未安装，无法生成图表")],
        )
    except Exception as exc:
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text=f"生成图表失败: {exc}")],
        )

    if not output_path.exists():
        return ToolChunk(
            is_last=True,
            state=ToolResultState.ERROR,
            content=[TextBlock(type="text", text="图表文件未生成")],
        )

    file_url = _path_to_file_url(output_path)
    chart_name = _CHART_TYPES.get(chart_type, chart_type)
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[
            DataBlock(
                source=URLSource(url=file_url, media_type="image/png"),
                name=f"{safe_name}.png",
            ),
            TextBlock(type="text", text=f"{chart_name}已生成：{safe_name}.png"),
        ],
    )

"""Extract all @mcp.tool() functions from the MCP server.py and generate a tool reference document.

Usage: python scripts/gen_tool_docs.py
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

# ---------- Paths ----------
ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "src" / "aiteam" / "mcp" / "server.py"
OUTPUT_MD = ROOT / "docs" / "mcp-tools-reference.md"

# ---------- Category mapping ----------
CATEGORY_MAP: dict[str, tuple[str, int]] = {
    # tool_name -> (category_label, sort_order_within_category)
    # Team management
    "team_status":      ("Team Management", 2),
    "team_list":        ("Team Management", 3),
    "team_briefing":    ("Team Management", 4),
    # Agent management
    "agent_update_status": ("Agent Management", 2),
    "agent_list":          ("Agent Management", 3),
    # Task management
    "task_run":       ("Task Management", 1),
    "task_status":    ("Task Management", 3),
    "task_list_project": ("Task Management", 4),
    # Meetings
    "meeting_create":        ("Meetings", 1),
    "meeting_send_message":  ("Meetings", 2),
    "meeting_read_messages": ("Meetings", 3),
    "meeting_conclude":      ("Meetings", 4),
    # Memory
    "memory_search": ("Memory", 1),
    # Project management
    "project_create": ("Project Management", 1),
    # System
    "os_health_check":  ("System", 1),
    "event_list":       ("System", 2),
}

# Category display order
CATEGORY_ORDER = [
    "Team Management",
    "Agent Management",
    "Task Management",
    "Loop System",
    "Meetings",
    "Memory",
    "Project Management",
    "System",
]


# ---------- AST parsing ----------

def _is_mcp_tool_decorated(node: ast.FunctionDef) -> bool:
    """Tell whether a function has an @mcp.tool() decorator."""
    for deco in node.decorator_list:
        # @mcp.tool()
        if isinstance(deco, ast.Call):
            func = deco.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "tool"
                and isinstance(func.value, ast.Name)
                and func.value.id == "mcp"
            ):
                return True
        # @mcp.tool (no parentheses)
        if (
            isinstance(deco, ast.Attribute)
            and deco.attr == "tool"
            and isinstance(deco.value, ast.Name)
            and deco.value.id == "mcp"
        ):
            return True
    return False


def _annotation_to_str(node: ast.expr | None) -> str:
    """Convert an AST type-annotation node to a readable string."""
    if node is None:
        return ""
    return ast.unparse(node)


def _default_to_str(default: ast.expr) -> str:
    """Convert a default-value AST node to a readable string."""
    return ast.unparse(default)


def _parse_docstring(raw: str | None) -> tuple[str, dict[str, str], str]:
    """Parse a docstring and return (summary, args_dict, returns_text).

    Supports Google-style docstrings (Args: / Returns:).
    """
    if not raw:
        return ("", {}, "")

    lines = textwrap.dedent(raw).strip().splitlines()

    summary_lines: list[str] = []
    args_dict: dict[str, str] = {}
    returns_text = ""

    section = "summary"
    current_arg: str | None = None

    for line in lines:
        stripped = line.strip()

        if stripped == "Args:":
            section = "args"
            current_arg = None
            continue
        if stripped == "Returns:":
            section = "returns"
            current_arg = None
            continue

        if section == "summary":
            summary_lines.append(stripped)
        elif section == "args":
            # New parameter line: "name: description"
            if ":" in stripped and not stripped.startswith(" ") and not line.startswith("        "):
                # Detect whether this is a less-indented line (parameter lines are usually indented 8 spaces)
                indent = len(line) - len(line.lstrip())
                if indent <= 8 and ":" in stripped:
                    parts = stripped.split(":", 1)
                    current_arg = parts[0].strip()
                    args_dict[current_arg] = parts[1].strip()
                    continue
            # Continuation line
            if current_arg and stripped:
                args_dict[current_arg] += " " + stripped
        elif section == "returns":
            if stripped:
                returns_text += (" " if returns_text else "") + stripped

    summary = " ".join(summary_lines).strip()
    # Remove the extra trailing space left by a blank line at the end of the summary
    summary = summary.strip()
    return (summary, args_dict, returns_text)


def extract_tools(source: str) -> list[dict]:
    """Extract information on all @mcp.tool() functions from Python source."""
    tree = ast.parse(source)
    tools = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not _is_mcp_tool_decorated(node):
            continue

        name = node.name
        docstring = ast.get_docstring(node)
        summary, args_doc, returns_doc = _parse_docstring(docstring)

        # Extract parameters
        args_info: list[dict] = []
        func_args = node.args

        # Compute the defaults alignment (defaults correspond to the last N args)
        num_args = len(func_args.args)
        num_defaults = len(func_args.defaults)
        default_offset = num_args - num_defaults

        for i, arg in enumerate(func_args.args):
            if arg.arg == "self":
                continue
            type_str = _annotation_to_str(arg.annotation)
            default_str = ""
            default_idx = i - default_offset
            if default_idx >= 0 and default_idx < len(func_args.defaults):
                default_str = _default_to_str(func_args.defaults[default_idx])
            desc = args_doc.get(arg.arg, "")
            args_info.append({
                "name": arg.arg,
                "type": type_str,
                "default": default_str,
                "description": desc,
            })

        tools.append({
            "name": name,
            "summary": summary,
            "returns": returns_doc,
            "args": args_info,
        })

    return tools


# ---------- Markdown generation ----------

def generate_markdown(tools: list[dict]) -> str:
    """Generate a Markdown document grouped by category."""
    # Group
    categorized: dict[str, list[tuple[int, dict]]] = {}
    uncategorized: list[dict] = []

    for tool in tools:
        cat_info = CATEGORY_MAP.get(tool["name"])
        if cat_info:
            cat, order = cat_info
            categorized.setdefault(cat, []).append((order, tool))
        else:
            uncategorized.append(tool)

    # Sort
    for cat in categorized:
        categorized[cat].sort(key=lambda x: x[0])

    lines: list[str] = []
    lines.append("# AI Team OS MCP Tools Reference")
    lines.append("")
    lines.append(f"> Auto-generated from `src/aiteam/mcp/server.py` — {len(tools)} tools in total")
    lines.append("")

    # Table of contents
    lines.append("## Table of Contents")
    lines.append("")
    for cat in CATEGORY_ORDER:
        if cat not in categorized:
            continue
        lines.append(f"- [{cat}](#{cat.replace(' ', '-').lower()})")
        for _, tool in categorized[cat]:
            anchor = tool["name"].replace("_", "-")
            lines.append(f"  - [{tool['name']}](#{anchor})")
    if uncategorized:
        lines.append("- [Other](#other)")
        for tool in uncategorized:
            anchor = tool["name"].replace("_", "-")
            lines.append(f"  - [{tool['name']}](#{anchor})")
    lines.append("")

    # Each category
    for cat in CATEGORY_ORDER:
        if cat not in categorized:
            continue
        lines.append("---")
        lines.append("")
        lines.append(f"## {cat}")
        lines.append("")

        for _, tool in categorized[cat]:
            lines.extend(_render_tool(tool))

    if uncategorized:
        lines.append("---")
        lines.append("")
        lines.append("## Other")
        lines.append("")
        for tool in uncategorized:
            lines.extend(_render_tool(tool))

    return "\n".join(lines)


def _render_tool(tool: dict) -> list[str]:
    """Render the Markdown section for a single tool."""
    lines: list[str] = []
    lines.append(f"### `{tool['name']}`")
    lines.append("")
    lines.append(tool["summary"])
    lines.append("")

    if tool["args"]:
        lines.append("**Parameters:**")
        lines.append("")
        lines.append("| Parameter | Type | Default | Description |")
        lines.append("|------|------|--------|------|")
        for arg in tool["args"]:
            type_str = f"`{arg['type']}`" if arg["type"] else "-"
            default_str = f"`{arg['default']}`" if arg["default"] else "Required"
            desc = arg["description"] or "-"
            lines.append(f"| `{arg['name']}` | {type_str} | {default_str} | {desc} |")
        lines.append("")
    else:
        lines.append("**Parameters:** none")
        lines.append("")

    if tool["returns"]:
        lines.append(f"**Returns:** {tool['returns']}")
        lines.append("")

    return lines


# ---------- Main ----------

def main() -> None:
    if not SERVER_PY.exists():
        print(f"Error: cannot find {SERVER_PY}")
        raise SystemExit(1)

    source = SERVER_PY.read_text(encoding="utf-8")
    tools = extract_tools(source)

    if not tools:
        print("Warning: no @mcp.tool() functions found")
        raise SystemExit(1)

    md = generate_markdown(tools)

    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(md, encoding="utf-8")
    print(f"Generated {OUTPUT_MD} — {len(tools)} tools in total")


if __name__ == "__main__":
    main()

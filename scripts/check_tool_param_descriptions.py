#!/usr/bin/env python3
"""I9 — MCP tool parameter description machine check.

In the tool-search era CC no longer ships every tool schema up front: it matches
a query against tool and parameter text and only then loads the schema. A
parameter with no description is therefore not a documentation nit — it is a
parameter the model cannot search for, guesses at, and gets wrong.

Why runtime introspection instead of an AST scan: the failure mode found by the
2026-07-28 audit is invisible to source-level greps. All ten undescribed
parameters came from *merged* docstring Args entries —

    min_stars / max_stars: Star range filter.
    limit / offset: Pagination.
    suggested_advocate / critic / judge: Default debate roles.

— which read as perfectly documented in the source, but the docstring parser
cannot split one line across several parameters, so every one of them reached
the wire with an empty description. Only the schema FastMCP actually publishes
tells the truth, so this check asks the server for it via ``mcp.list_tools()``.

Usage: python3 scripts/check_tool_param_descriptions.py    (from the repo root)
Exit code: 0 = every tool and parameter documented, 1 = gaps found.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Always check *this* repo's source, never whatever aiteam happens to be installed.
sys.path.insert(0, str(ROOT / "src"))


async def collect() -> tuple[int, int, list[str], dict[str, list[str]]]:
    """Return (tool_count, param_count, tools_without_description, missing_by_tool)."""
    from aiteam.mcp.server import mcp

    tools = await mcp.list_tools()
    param_count = 0
    undocumented_tools: list[str] = []
    missing: dict[str, list[str]] = defaultdict(list)

    for tool in tools:
        if not (tool.description or "").strip():
            undocumented_tools.append(tool.name)
        properties = (tool.parameters or {}).get("properties", {})
        for name, spec in properties.items():
            param_count += 1
            if not (spec.get("description") or "").strip():
                missing[tool.name].append(name)

    return len(tools), param_count, sorted(undocumented_tools), dict(missing)


def main() -> int:
    try:
        tool_count, param_count, undocumented_tools, missing = asyncio.run(collect())
    except Exception as exc:  # import/registration failure is itself a red line
        print(f"❌ Cannot enumerate the MCP tool surface (server import or registration failed): {exc!r}")
        return 1

    if not tool_count:
        print("❌ Enumerated 0 MCP tools — register_all did not take effect, the check is meaningless")
        return 1

    missing_count = sum(len(v) for v in missing.values())
    if not missing_count and not undocumented_tools:
        print(
            f"✅ MCP tool surface descriptions complete: {tool_count} tools / {param_count} parameters,"
            f"all with descriptions"
        )
        return 0

    for name in undocumented_tools:
        print(f"❌ Tool {name}: missing tool-level description (empty docstring)")
    for name in sorted(missing):
        params = ", ".join(sorted(missing[name]))
        print(f"❌ Tool {name}: parameters missing descriptions — {params}")

    print(
        f"\nConclusion: ❌ of {tool_count} tools / {param_count} parameters,"
        f"{missing_count} parameters + {len(undocumented_tools)} tools have no description."
        "\nFix: in the Args section of the function docstring, write one line for each parameter (make clear when to "
        "use it / what it means,"
        "do not just restate the parameter name), or use Field(description=...)."
        "\nCommon pitfall: writing multiple parameters on one line (like `limit / offset: Pagination.`) cannot be "
        "split by the parser,"
        "so it looks described but is actually all lost — one parameter per line is required."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

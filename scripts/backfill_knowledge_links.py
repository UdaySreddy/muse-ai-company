"""Knowledge layer P1a — historical backfill: scan existing memos/reports and build reference edges (idempotent, can be run repeatedly).

Usage: python3 scripts/backfill_knowledge_links.py
Connects directly to the production DB (~/.claude/data/ai-team-os/aiteam.db); UNIQUE conflicts are silently ignored.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.api.link_extract import extract_refs  # noqa: E402
from aiteam.storage.repository import StorageRepository  # noqa: E402
from aiteam.types import KnowledgeLink  # noqa: E402

DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"


async def main() -> None:
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{DB}")
    await repo.init_db()

    inserted = scanned = 0

    # 1) All tasks' memos
    projects = await repo.list_projects()
    for proj in projects:
        tasks = await repo.list_tasks_by_project(proj.id)
        for task in tasks:
            for memo in (task.config or {}).get("memo", []):
                scanned += 1
                refs = extract_refs(memo.get("content", ""))
                if not refs:
                    continue
                inserted += await repo.insert_knowledge_links([
                    KnowledgeLink(
                        from_kind="task_memo",
                        from_id=f"{task.id}#{memo.get('timestamp', '')}",
                        to_kind=r.to_kind,
                        to_id=r.to_id,
                        link_type=r.link_type,
                        context=r.context,
                        link_source="regex-memo",
                        project_id=task.project_id or "",
                    )
                    for r in refs
                ])

    # 2) All reports
    for proj in projects:
        reports = await repo.list_reports(project_id=proj.id)
        for meta in reports:
            report = await repo.get_report(meta.id)
            if report is None:
                continue
            scanned += 1
            refs = extract_refs(report.content or "")
            if not refs:
                continue
            inserted += await repo.insert_knowledge_links([
                KnowledgeLink(
                    from_kind="report",
                    from_id=report.id,
                    to_kind=r.to_kind,
                    to_id=r.to_id,
                    link_type=r.link_type,
                    context=r.context,
                    link_source="regex-report",
                    project_id=report.project_id or "",
                )
                for r in refs
            ])

    print(f"Scanned {scanned} memos/reports, created {inserted} new reference edges (duplicates ignored)")


if __name__ == "__main__":
    asyncio.run(main())

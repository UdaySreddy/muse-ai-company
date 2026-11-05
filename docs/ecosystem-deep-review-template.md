# Ecosystem Deep-Review Report Template (Five Sections)

The dispatch prompt of `ecosystem_deep_reviewer.py` references this document; the `deep_review_link.py`
hook parses `report_save` reports per this template and automatically backfills the deep_review row.
**The section-number → database-field mapping is fixed by the hook**; reports must follow this order,
or the risks/learnings content will land in the wrong fields (lesson measured on 2026-07-10: this document was once missing,
and the research agent self-defined 3=risks/4=learnings, the opposite of the hook's 3=learnings/4=risks).

## Anchor lines (the first two lines of the report body, not wrapped in a code block)

```
repo_id=<uuid>
deep_review_id=<uuid>
```

The hook uses these two lines to bind a report to a deep_review row (`link_report`, idempotent).

## The five level-2 headings (numbering must be `## 1.` through `## 5.`)

| Section | Suggested title | Stored field | Content |
|---|---|---|---|
| `## 1.` | True positioning | `summary_md` | What it is, what problem it solves, README claims vs reality |
| `## 2.` | Architecture analysis | `architecture_md` | Core modules, data flow, key design decisions (cite concrete file paths) |
| `## 3.` | Learnings | `learnings_md` | Concrete inspiration for each AI Team OS subsystem |
| `## 4.` | Risks and shortcomings | `risks_md` | Engineering shortcomings, maintenance status, dependency burden |
| `## 5.` | Integration recommendations | `integration_md` | Must contain one line `Recommended action: integrate|reference|learn|skip` |

The recommended-action line in `## 5.` is extracted by a hook regex into `integration_recommendation`.

## Closing metadata block (not a numbered heading; the hook does not count it among the five sections)

```
## Metadata
- demo_result: success|fail|skipped
- demo_log_excerpt: |
    <optional, indented log tail>
```

## Storage call

```
report_save(
    author="<agent name>",
    topic="deep-review-<owner>-<repo>",
    content=<the markdown above>,
    report_type="deep-review",
)
```

`deep_review_link.py` (a PostToolUse hook, registered by install.py in source-install mode and
by hooks.json in plugin mode) automatically parses the five sections after a report_save and advances
stage_status; if the hook does not take effect, the link can be backfilled manually with
`POST /api/ecosystem/deep_reviews/{id}/link_report` (idempotent).

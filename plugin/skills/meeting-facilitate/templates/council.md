---
template_name: council
description: Council review — multi-perspective expert evaluation of proposals or architectures
total_rounds: 3
rounds:
  - number: 1
    name: "Expert perspectives"
    rule: "Each participant evaluates from their professional angle (security, performance, maintainability, UX, cost, etc.). Format: [Perspective] + [Strengths] + [Risks] + [Score 1-5]"
  - number: 2
    name: "Cross-examination"
    rule: "Challenge the highest-risk items from Round 1. Propose mitigations or alternatives. Format: [Risk addressed] + [Mitigation proposal] + [Revised score]"
  - number: 3
    name: "Verdict"
    rule: "Each expert gives final verdict: APPROVE / CONDITIONAL / REJECT. Decision requires majority APPROVE. Output: [Verdict] + [Conditions if any] + [Action items]"
keywords: [council, review board, multi-angle, multi-perspective, expert review, architecture review, proposal evaluation]
---

# Council Expert Review Board Template

## When to use

- Major architecture decisions that need systematic review from multiple professional perspectives
- A proposal spans security, performance, cost, and other dimensions that a single reviewer cannot cover
- You need a formal review verdict (APPROVE/CONDITIONAL/REJECT)
- Project milestone reviews, ensuring key decisions are endorsed by multiple experts

**Not suitable for**: everyday code PRs (the review template suffices); scenarios needing creative divergence (use brainstorm); scenarios where the conclusion is already clear and only execution remains.

## Participant recommendations

- Each participant represents one professional perspective (security, performance, maintainability, UX, cost, etc.)
- 3–6 experts recommended, ensuring diverse perspectives without becoming overly long
- The proposer may attend as an observer and answer questions after the review concludes
- The facilitator aggregates each round's scores and ensures Round 3 produces a clear verdict

## Anti-patterns

- Overlapping participant perspectives, with several people reviewing from the same angle (reduces the multi-angle value)
- Round 1 scores with no supporting reasoning, just numbers
- Skipping Round 2 and going straight from Round 1 to the verdict (misses cross-validation of important risks)
- Requiring a "unanimous pass" for the verdict, preventing convergence (use majority rule instead)
- The proposer intervening in Rounds 1/2 to defend themselves (undermines independent review)

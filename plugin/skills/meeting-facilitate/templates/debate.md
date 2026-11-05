---
template_name: debate
description: "Structured debate — improve proposal quality through four rounds: advocate's case, critic's challenge, advocate's response, verdict"
total_rounds: 4
rounds:
  - number: 1
    name: "Advocate Statement"
    rule: "The Advocate presents the proposal or position in full. Statement format: [Proposal title] + [Core arguments (≤3)] + [Supporting evidence/data] + [Expected benefits] + [Known limitations]. Present only — no rebuttals, no pre-empting challenges."
  - number: 2
    name: "Critic Challenge"
    rule: "The Critic systematically challenges the proposal. Statement format: [Quote of the advocate's words] → [Point of challenge] + [risk level: High/Medium/Low] + [Alternative or improvement suggestion]. You must cite the advocate's specific arguments; generic dismissal is not allowed. Each challenge point must come with a constructive alternative."
  - number: 3
    name: "Advocate Response"
    rule: "The Advocate responds to each of the critic's challenges in turn. Statement format: [Quoted challenge point] → [Accept/Partially accept/Reject] + [Reasoning] + [Proposal revision (if any)]. Reasonable challenges may be absorbed into a revised proposal. Finish by outputting the updated proposal summary."
  - number: 4
    name: "Verdict"
    rule: "The Judge gives the final judgment based on all rounds. Statement format: [Overall assessment] + [Improvements adopted] + [Final verdict: APPROVED/CONDITIONAL/REJECTED] + [Action items list]. The verdict must be reasoned and cite the key arguments from the debate."
keywords: [debate, disagreement, controversy, advocate, critic, red team, blue team]
---

# Debate Template

## When to use

- The team has a clear disagreement about a proposal or decision and needs a structured clash
- High-risk decisions that need a Red Team challenge to prevent groupthink
- The proposer needs a stress test to strengthen their arguments
- You need to find consensus amid controversy, or clearly delineate the boundaries of disagreement

**Not suitable for**: scenarios where the team already has consensus and only execution remains; or where there is no substantive disagreement and a forced debate would waste time.

## Participant recommendations

- At least 1 Advocate: someone who understands the proposal deeply; may be the proposer
- At least 1 Critic: someone good at challenging assumptions, or a designated devil's advocate
- 1 Judge: a neutral third party, usually the Leader or a domain expert
- The facilitator may double as Judge, but must not speak for either side

## Anti-patterns

- The Critic only dismisses generically without quoting the advocate's specific arguments (ineffective challenge)
- The Advocate pre-empts challenges and defends in Round 1 (deviates from the purpose of presenting the case)
- The Judge states a position in the first three rounds (undermines debate independence)
- Only 1 participant (no real opposition, so no debate value is triggered)
- The debate turns into personal attacks or emotional confrontation (focus on arguments, not people)

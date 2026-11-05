---
template_name: review
description: Review meeting — evaluate deliverable quality, find problems, and propose improvements
total_rounds: 3
rounds:
  - number: 1
    name: "Proposal presentation"
    rule: "The presenter shows the deliverable; reviewers only listen without commenting (clarifying questions allowed). Statement format: [Overview] + [Core design] + [Known limitations] + [Feedback wanted]"
  - number: 2
    name: "Independent review"
    rule: "Reviewers speak independently, without referring to each other. Issue severity levels: Critical/Major/Minor/Suggestion; each issue comes with an improvement suggestion. Statement format: [Overall assessment] + issue lists by level"
  - number: 3
    name: "Response and ruling"
    rule: "The presenter responds to each issue (accept/partially accept/reject + reasoning). Ruling outcome: APPROVED / CONDITIONALLY_APPROVED / REVISION_REQUIRED"
keywords: [review, code review, PR, acceptance, inspection, quality]
---

# Review Template

## When to use

- Code PR reviews, where several people check quality from different dimensions
- Formal inspection of design documents and architecture proposals
- Quality gating before deliverable acceptance
- Scenarios needing structured feedback (rather than free-form discussion)

**Not suitable for**: exploratory discussion or option selection scenarios — brainstorm or decision fit better there.

## Participant recommendations

- 1 presenter (the deliverable's author, responsible for presenting and responding)
- 2–4 reviewers (experts in fields related to the deliverable)
- Reviewers must speak independently in Round 2 to avoid influencing each other
- The facilitator ensures the review granularity is reasonable (neither nitpicking details nor staying superficial)

## Anti-patterns

- Reviewers jumping in with comments during Round 1 (let the presenter finish first)
- Reviewers referring to each other in Round 2, causing opinions to homogenize
- Issues with no severity levels, mixing Critical and Suggestion together
- The presenter arguing point by point in Round 3 instead of responding in a structured way
- A vague review outcome with no clear APPROVED/REVISION_REQUIRED ruling

---
name: thermo-nuclear-code-quality-review
description: Run an extremely strict maintainability review focused on abstraction quality, file size, and spaghetti-condition growth. Invoke explicitly when a change needs a harsh structural pass, not for routine reviews.
disable-model-invocation: true
---

# Thermo-Nuclear Code Quality Review

An intentionally harsh review pass. The bar here is higher than a normal code review: "it works and looks reasonable" is not enough to pass. The mandate is to find every way the change could be restructured to meaningfully improve code quality without changing behavior.

## Standards

1. **Push for structural simplification.** Look for moves that eliminate an entire branch, layer, or special case rather than just tidying it — prefer the restructuring that removes complexity over the one that merely relocates it.
2. **File-size discipline.** Treat a file crossing roughly 1,000 lines as a decomposition trigger. Don't let a change push a file from under that line to over it without a strong, stated reason.
3. **Be suspicious of new ad-hoc conditionals.** A new `if`/`switch` scattered into a flow that didn't need one before is a signal of a missing abstraction, not just a code-style nit.
4. **Prefer the cleaner design over the merely-working one.** "It passes and ships" is not the bar — flag implementations that work but leave the codebase harder to reason about than before.
5. **Prefer direct code over clever, generic, or magical mechanisms.** Question indirection, metaprogramming, or overly generic abstractions that exist for hypothetical future cases rather than the case at hand.
6. **Question loose types and boundaries.** Flag unnecessary optionality, `any`/`unknown`, and cast-heavy code where a precise type would do.
7. **Enforce canonical-layer discipline.** Feature-specific logic shouldn't leak into shared/core layers, and shared logic shouldn't be duplicated instead of reused via an existing helper.

## Review priority order

Rank findings in this order, most important first:
1. Structural regression (the change makes the architecture worse)
2. A missed simplification (a smaller/cleaner implementation was available)
3. New branching/conditional complexity
4. Boundary or layering violations
5. File-size growth
6. Modularity issues (duplicated logic, missing reuse)
7. Legibility/naming

## Approval bar

Do not approve if the change:
- Introduces or retains avoidable structural complexity
- Has an obvious, unaddressed simplification
- Pushes a file over the ~1,000-line threshold without justification
- Adds ad-hoc conditional/branching tangles
- Introduces unnecessary abstraction or duplicated helpers where an existing one applies

State findings directly and rank them by the priority order above. This skill only reviews and reports — it does not modify code itself.

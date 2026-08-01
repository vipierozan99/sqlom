# Goals

## Speed, without leaving SQLAlchemy

Two goals, and the second is not subordinate to the first.

1. **A read path that is faster than SQLAlchemy's result layer** — the reason the
   project exists, and what the benchmarks defend.
2. **A SQLAlchemy application can adopt rowform one query at a time**, without
   giving up its engine, its sessions, its transactions, or its migrations.

Goal 2 is a design constraint, not a nicety. It rules out anything that makes
rowform a parallel universe with its own vocabulary for what SQLAlchemy already
names — and it is testable: rowform reads must work *inside* a stock
`AsyncSession` transaction, seeing its uncommitted writes and rolling back with
it. Where the two goals conflict, say so explicitly and measure the trade rather
than picking silently (`docs/PLAN_SQLA_API.md`).

# Principles

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- Be succinct when writing comments or docstrings, focus on why something is there, not on what it does, use references to other code or docs, keep up to date.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

# Conventions
- Reach the library through `import rowform as rf` — in docs, tests, benchmarks and examples alike. Not `import rowform`, not `from rowform import x`. One name for the package everywhere, so `rf` is free to mean nothing else.

# Workflow

## Docs are written once, at the end

**Do not update docs as you go.** Code and tests land per commit; prose lands in
one pass when the PR is opened. Half the doc edits made mid-stream get rewritten
by the next change anyway, and each one costs a review of text that is about to
move.

While working, keep a running list of what the changes have made stale — file,
section, and what is now wrong — and write it all at PR time. Docs in scope:
`README.md`, `docs/*.md`, `SECURITY.md`, and module docstrings that describe the
public surface rather than the code beneath them.

Docstrings *inside* code you are already editing are part of that edit, not a doc
update — keep them true as you write.

# Commands
- Run linting with: `just lint --fix` (`--fix` will already fix fixable errors)
- Run typechecking with: `just typecheck`
- Run tests with: `just test <test_selector>`
- When you find an interesting benchmark result, make a branch and commit the results there, such that I can always go back to a commit an reproduce a benchmark. Also keep a document of those runs and their commits and results in the main branch

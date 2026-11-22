# Agent rules for clinic-agent

## Before saying "done"
- Run `uv run pytest -q` and `uv run ruff check .`. Both must pass.
- Show the output. Don't just claim it passed.

## Never
- Never edit or delete a test to make it pass. Fix the code, or ask.
- Never add a dependency without asking first.
- Never use real patient names, phone numbers or clinic data. Made-up data only (PDPA).
- Never write medical advice into prompts, FAQ data or replies.

## How to work
- Plan first, then small diffs. One change per commit.
- Schema lives in `db/schema.sql`. App code in `app/`. Tests in `tests/`.
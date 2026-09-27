## Capability

What can a user do after this change that they could not do before?

## Proof

- [ ] I added one evidence subsection to `README.md`.
- [ ] It contains the exact prompt or API request.
- [ ] It contains the graph and ordered event trace.
- [ ] It contains the actual final result and evidence.
- [ ] It identifies every agent/provider assignment.
- [ ] It shows an adversarial failure before the fix and the same attack failing afterward.
- [ ] It includes commands that reproduce the result from a fresh checkout.

## Boundaries

- [ ] `glc_v3` still owns all provider credentials and model routing.
- [ ] Memory authorization happens before retrieval.
- [ ] No `.env`, credentials, personal memory, databases, or unrestricted local paths are committed.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run pytest -q` passes.

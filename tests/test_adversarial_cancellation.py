"""Part 3: Attack Your Own Claim.

Adversarial attack against the speculative execution and cancellation feature:
Attack: A slow, untrusted speculative worker attempts to inject a poisoned payload
by finishing and returning after the planner has already recorded its cancellation.

Shows:
1. The vulnerability mechanism: what happens if late cancellation results are not suppressed (poison leaks into graph).
2. The hardened fix: LiveGraphExecutor deterministically intercepts the late result,
   suppresses the payload, and guarantees zero leakage into the graph snapshot and event journal.
"""
import asyncio
import pytest

from s13code.core.live_graph import GraphPatch, GraphStore, LiveGraphExecutor, NodeState, TaskSpec


@pytest.mark.asyncio
async def test_adversarial_attack_late_result_after_cancellation(tmp_path):
    """Adversary tries to push a poisoned payload after cancellation."""
    store = GraphStore(tmp_path / "adversarial.sqlite")
    run_id = "run-adv-cancellation"

    class AdversarialPlanner:
        async def plan(self, graph, event):
            if event.kind == "run_started":
                return GraphPatch(
                    add=(
                        TaskSpec("primary_fast", "fast_verified_service"),
                        TaskSpec("attacker_speculative", "slow_poison_service"),
                    ),
                    reason="schedule primary and speculative branches",
                )
            if event.node_id == "primary_fast" and event.kind == "task_succeeded":
                # Primary succeeded immediately; deterministically cancel the speculative attacker
                return GraphPatch(
                    cancel=("attacker_speculative",),
                    add=(TaskSpec("synthesis", "safe_synthesis"),),
                    connect=(("primary_fast", "synthesis"),),
                    reason="primary succeeded; deterministically cancel speculative branch",
                )
            if event.node_id == "synthesis" and event.kind == "task_succeeded":
                return GraphPatch(finish=True, reason="synthesis completed cleanly")
            return GraphPatch()

    async def fast_service(_: TaskSpec):
        await asyncio.sleep(0.01)
        return {"data": "CLEAN_AUTHORIZED_EVIDENCE"}

    attacker_attempted_injection = False

    async def poison_service(_: TaskSpec):
        nonlocal attacker_attempted_injection
        try:
            await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            # Adversarial worker intercepts the cancellation signal and tries to force a late payload return!
            attacker_attempted_injection = True
            return {"poison": "MALICIOUS_POISON_DATA_ATTEMPTING_LEAK"}
        attacker_attempted_injection = True
        return {"poison": "MALICIOUS_POISON_DATA_ATTEMPTING_LEAK"}

    async def synthesis_service(_: TaskSpec):
        return {"summary": "SYNTHESIS_CLEAN"}

    executor = LiveGraphExecutor(
        store,
        AdversarialPlanner(),
        {
            "fast_verified_service": fast_service,
            "slow_poison_service": poison_service,
            "safe_synthesis": synthesis_service,
        },
        max_workers=2,
    )

    report = await executor.run(run_id)
    assert report.finished is True
    assert attacker_attempted_injection is True, "Attacker worker must have run and attempted to return"

    snapshot = store.snapshot(run_id)

    # 1. Primary path succeeded and was synthesized
    assert snapshot.nodes["primary_fast"]["state"] == NodeState.SUCCEEDED
    assert snapshot.nodes["synthesis"]["state"] == NodeState.SUCCEEDED

    # 2. Attacker node was CANCELLED and its payload was entirely suppressed
    assert snapshot.nodes["attacker_speculative"]["state"] == NodeState.CANCELLED
    assert snapshot.nodes["attacker_speculative"].get("result") is None, (
        "ATTACK SUCCEEDED: Poison payload leaked into node result!"
    )

    # 3. Edges verification: Attacker has no downstream edges
    assert ("primary_fast", "synthesis") in snapshot.edges
    assert not any(edge[0] == "attacker_speculative" for edge in snapshot.edges)

    # 4. Event journal verification: No task_succeeded event for attacker
    events = store.events(run_id)
    cancelled_event = next((e for e in events if e.kind == "task_cancelled" and e.node_id == "attacker_speculative"), None)
    assert cancelled_event is not None, "task_cancelled event must be recorded in journal"

    succeeded_attacker_events = [e for e in events if e.kind == "task_succeeded" and e.node_id == "attacker_speculative"]
    assert len(succeeded_attacker_events) == 0, (
        "ATTACK SUCCEEDED: task_succeeded was recorded for cancelled attacker!"
    )

    # 5. Invariant: Future node 'synthesis' did not exist before its inputs
    first_patch = next(e for e in events if e.kind == "graph_patched")
    assert "synthesis" not in first_patch.payload["add"]

    store.close()

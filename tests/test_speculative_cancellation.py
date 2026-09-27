import asyncio
import pytest

from s13code.core.live_graph import GraphStore, LiveGraphExecutor, TaskSpec, NodeState, GraphPatch
from s13code.core.memory.embeddings import DeterministicEmbedder
import s13code.routes as agent_route


def _fake_answer(monkeypatch):
    async def answer(_app, prompt, _system):
        return {"text": f"Grounded synthesis of evidence: {prompt[:30]}", "provider": "fake", "model": "fake"}
    monkeypatch.setattr(agent_route, "gateway_text_llm", answer)


def test_speculative_execution_and_deterministic_cancellation(app_client, monkeypatch):
    app_client.app.state.s13_runtime.memory.embedder = DeterministicEmbedder(128)
    _fake_answer(monkeypatch)

    response = app_client.post("/v1/agent/runs", json={
        "tenant_id": "test-tenant",
        "project_id": "speculative-test",
        "user_id": "tester",
        "prompt": "Run a speculative search: find fast or deep results for quantum computing.",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"

    graph = body["graph"]
    nodes = graph["nodes"]
    edges = graph["edges"]

    # 1. Primary path succeeded
    assert "fast_primary" in nodes
    assert nodes["fast_primary"]["state"] == "succeeded"

    # 2. Speculative path was deterministically cancelled
    assert "slow_speculative" in nodes
    assert nodes["slow_speculative"]["state"] == "cancelled"
    # Ensure cancelled work never leaked an outcome or result payload
    assert nodes["slow_speculative"].get("result") is None

    # 3. Future nodes (distill, answer) exist and were connected ONLY to the primary path
    assert "distill" in nodes
    assert nodes["distill"]["state"] == "succeeded"
    assert "answer" in nodes
    assert nodes["answer"]["state"] == "succeeded"

    # Verify edge invariants: slow_speculative has NO downstream edges
    assert ["fast_primary", "distill"] in edges
    assert not any(edge[0] == "slow_speculative" for edge in edges)

    # 4. Verify ordered event trace contains task_cancelled event
    events = body["events"]
    cancelled_events = [e for e in events if e["kind"] == "task_cancelled" and e["node_id"] == "slow_speculative"]
    assert len(cancelled_events) == 1

    # Invariant: Future nodes did not exist before their inputs
    # Check that in the first graph_patched event, neither distill nor answer was present
    first_patch = next(e for e in events if e["kind"] == "graph_patched")
    assert set(first_patch["payload"]["add"]) == {"fast_primary", "slow_speculative"}
    assert "distill" not in first_patch["payload"]["add"]
    assert "answer" not in first_patch["payload"]["add"]


@pytest.mark.asyncio
async def test_late_worker_result_is_never_leaked_into_graph_store(tmp_path):
    """Direct invariant proof: Even if a cancelled worker finishes late with data,
    LiveGraphExecutor intercepts it, discards it, and keeps the node cancelled with no result."""
    store = GraphStore(tmp_path / "speculative.sqlite")
    run_id = "test-late-leak"

    class Planner:
        async def plan(self, graph, event):
            if event.kind == "run_started":
                # Start primary and slow speculative task
                return GraphPatch(
                    add=(
                        TaskSpec("fast", "fast_skill"),
                        TaskSpec("slow", "slow_skill"),
                    ),
                    reason="start speculative frontier",
                )
            if event.node_id == "fast" and event.kind == "task_succeeded":
                # Deterministically cancel slow task
                return GraphPatch(
                    cancel=("slow",),
                    add=(TaskSpec("synthesis", "synthesis_skill"),),
                    connect=(("fast", "synthesis"),),
                    reason="fast succeeded; cancel slow speculative",
                )
            if event.node_id == "synthesis" and event.kind == "task_succeeded":
                return GraphPatch(finish=True, reason="synthesis finished")
            return GraphPatch()

    slow_worker_finished = False

    async def fast_skill(_: TaskSpec):
        await asyncio.sleep(0.01)
        return {"data": "PRIMARY_FAST_DATA"}

    async def slow_skill(_: TaskSpec):
        nonlocal slow_worker_finished
        # Sleep long enough so cancellation definitely happens while in-flight
        await asyncio.sleep(0.15)
        slow_worker_finished = True
        return {"data": "LEAKED_SPECULATIVE_DATA"}

    async def synthesis_skill(_: TaskSpec):
        return {"summary": "SYNTHESIS_DATA"}

    executor = LiveGraphExecutor(
        store,
        Planner(),
        {"fast_skill": fast_skill, "slow_skill": slow_skill, "synthesis_skill": synthesis_skill},
        max_workers=2,
    )

    report = await executor.run(run_id)
    assert report.finished is True

    snapshot = store.snapshot(run_id)
    assert snapshot.nodes["fast"]["state"] == NodeState.SUCCEEDED
    assert snapshot.nodes["synthesis"]["state"] == NodeState.SUCCEEDED

    # Verify slow was cancelled
    assert snapshot.nodes["slow"]["state"] == NodeState.CANCELLED
    # Verify no result was recorded for slow
    assert snapshot.nodes["slow"].get("result") is None

    # Verify slow is NOT connected to synthesis
    assert ("fast", "synthesis") in snapshot.edges
    assert not any(edge[0] == "slow" for edge in snapshot.edges)

    # Verify events
    events = store.events(run_id)
    cancelled_event = next(e for e in events if e.kind == "task_cancelled" and e.node_id == "slow")
    assert cancelled_event is not None
    # Verify no task_succeeded event ever exists for slow
    assert not any(e.kind == "task_succeeded" and e.node_id == "slow" for e in events)

    store.close()

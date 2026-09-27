# S13Code

`S13Code` is the standalone Session 13 agent runtime. It implements a live task graph, scoped and provenance-bearing memory, Rohan's semantic chunking V2, and Agent2Agent interoperability. It asks `glc_v3` for model completions over HTTP and never owns provider credentials.

## What runs where

| Service | Default address | Responsibility |
|---|---|---|
| `glc_v3` | `http://127.0.0.1:8111` | Models, keys, routing and channels |
| `S13Code` HTTP | `http://127.0.0.1:8113` | Graph, memory, documents and JSON-RPC A2A |
| `S13Code` gRPC | `127.0.0.1:8114` | Official A2A gRPC service |
| Ollama | `http://127.0.0.1:11434` | Phi-4 segmentation and Nomic embeddings |

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A running `glc_v3`
- A running Ollama with `phi4` and `nomic-embed-text`

```bash
ollama pull phi4
ollama pull nomic-embed-text
ollama serve
```

## Install and run

Unzip `glc_v3`, `S13Code`, and `S13Proof` beside one another. Start `glc_v3` first. Then, from this directory:

```bash
uv sync

export GLC_BASE_URL=http://127.0.0.1:8111
export S13_GATEWAY_PROVIDER=gemini
export S13_SANDBOX_ROOT="$PWD/sandbox"
export S13_CHUNK_MODEL=phi4:latest
export S13_LIVE_SEMANTIC_CHUNKING=1

uv run s13code serve
```

State is written under `~/.s13code` by default. Set `S13_DATA_DIR` to use another directory.

Check both services:

```bash
curl http://127.0.0.1:8111/healthz
curl http://127.0.0.1:8113/healthz
curl http://127.0.0.1:8113/readyz
curl http://127.0.0.1:8113/.well-known/agent-card.json
```

## Run a prompt

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "s13",
    "user_id": "student-01",
    "agent_id": "assistant",
    "prompt": "Say hello."
  }'
```

The response contains the final answer, graph nodes and edges, ordered graph events, and provider/agent assignments. Inspect a persisted run with:

```bash
curl http://127.0.0.1:8113/v1/agent/runs/<run-id>
```

## Index the sample corpus

The five files under `sandbox/papers/` are fixed `.txt` fixtures for semantic chunking and retrieval proofs.

```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "papers",
    "user_id": "student-01",
    "prompt": "Index every .txt file under papers/. Confirm how many chunks were indexed in total."
  }'
```

Document ingestion is versioned and atomic: source preparation, semantic boundaries, exact spans, Nomic embeddings, and visibility succeed together or roll back together.

## Architecture

- `s13code/core/live_graph/`: durable graph state, patches, event replay and bounded parallel execution
- `s13code/core/memory/`: scope checks, provenance, contradiction history, semantic chunking and FAISS retrieval
- `s13code/core/a2a_adapter/`: Agent Cards, JSON-RPC, SSE/push, official gRPC and trust checks
- `s13code/gateway.py`: the only `S13Code → glc_v3` seam
- `s13code/runtime.py`: joins graph, memory, tools and model calls into an inspectable run
- `tests/`: executable invariants and regression cases

## Test before opening a pull request

```bash
uv run ruff check .
uv run pytest -q

cd ../S13Proof
uv sync
uv run pytest -q
```

## Student contribution

### Extension: Speculative Execution with Deterministic Cancellation (Live Graph Track)

#### 1. User-Visible Capability
When answering complex multi-track or latency-sensitive queries, the runtime adaptively initiates speculative concurrent branches (a fast-path lookup alongside a broader, compute-intensive search). The moment the fast-path primary lookup succeeds with verified evidence, the live graph planner deterministically cancels the speculative branch in-flight before it completes. The runtime guarantees two foundational causal invariants: (1) downstream synthesis and answer nodes do not exist in the graph until their predecessor inputs arrive, and (2) late outcomes from cancelled tasks are deterministically suppressed and never leak into the graph edges, event journal, or synthesized answer.

#### 2. Exact API Request
```bash
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "course",
    "project_id": "speculative-proof",
    "user_id": "student-01",
    "prompt": "Run a speculative research: find fast or deep results for quantum computing error correction."
  }'
```

#### 3. Graph Structure and Ordered Event Trace

**Graph Nodes:**
- `fast_primary`: state `succeeded` (Skill: `fast_lookup`, Agent: `fast_researcher`)
- `slow_speculative`: state `cancelled` (Skill: `speculative_lookup`, Agent: `deep_researcher`, Result: `null`)
- `distill`: state `succeeded` (Skill: `distiller`, Agent: `distiller`, Provider: `gemini_1` / `gemini-3.1-flash-lite-preview`)
- `answer`: state `succeeded` (Skill: `answer_with_evidence`, Agent: `answer_with_evidence`, Provider: `gemini_1` / `gemini-3.1-flash-lite-preview`)

**Graph Edges:**
`[["fast_primary", "distill"], ["fast_primary", "answer"], ["distill", "answer"]]` *(Note: `slow_speculative` has strictly 0 outgoing or incoming dependencies to downstream synthesis nodes).*

**Ordered Event Trace:**
```text
- seq 65 | run_started    | node: None             | Initial frontier initialization
- seq 66 | graph_patched  | node: None             | add: ["fast_primary", "slow_speculative"], cancel: []
- seq 67 | task_started    | node: fast_primary     | skill: fast_lookup, agent: fast_researcher
- seq 68 | task_started    | node: slow_speculative | skill: speculative_lookup, agent: deep_researcher
- seq 69 | task_succeeded  | node: fast_primary     | verified: true, source: primary_fast_cache
- seq 70 | task_cancelled  | node: slow_speculative | was_running: true, reason: primary lookup succeeded with verified evidence
- seq 71 | graph_patched  | node: None             | cancel: ["slow_speculative"], add: ["distill"], connect: [["fast_primary", "distill"]]
- seq 72 | task_started    | node: distill          | skill: distiller, agent: distiller
- seq 73 | task_succeeded  | node: distill          | specialist synthesis completed
- seq 74 | graph_patched  | node: None             | add: ["answer"], connect: [["distill", "answer"], ["fast_primary", "answer"]]
- seq 75 | task_started    | node: answer           | skill: answer_with_evidence, agent: answer_with_evidence
- seq 76 | task_succeeded  | node: answer           | grounded answer produced
- seq 77 | graph_patched  | node: None             | finish: true
```

#### 4. Actual Final Result
```text
Based on the provided research synthesis, the landscape of quantum error correction (QEC) is divided into two primary trajectories:

### 1. Fast-Path Results (Near-Term Optimization)
This track focuses on immediate, practical implementation to reduce overhead and improve current hardware performance:
* Hardware-Efficient Codes: Research is shifting toward codes specifically tailored to the constraints of architectures like trapped ions or superconducting circuits to minimize gate counts.
* Real-Time Decoding: Development is centered on FPGA-based decoders designed to process syndrome measurements within the coherence time of physical qubits, which is essential for active error suppression.
* Logical Qubit Milestones: Experimental progress has reached the stage where logical qubits are now demonstrating performance that exceeds their constituent physical components.

### 2. Deep-Theoretical Results (Long-Term Scaling)
This track addresses the fundamental limits of fault tolerance and the scaling challenges of large-scale systems:
* Quantum Low-Density Parity-Check (qLDPC) Codes: These are identified as the current frontier. Unlike standard surface codes, qLDPC codes offer the potential for constant-rate encoding, which could drastically reduce the physical qubit count required for a specific logical error rate.
* Fault-Tolerant Thresholds: Theoretical investigations are exploring how non-local connectivity and higher-dimensional lattices can suppress error propagation more effectively than traditional 2D grid architectures.
* Topological Protection: Research continues into passive error correction methods, such as Majorana zero modes, which aim to eliminate the need for active, energy-intensive syndrome measurement cycles.

### Synthesis Summary
The field is currently characterized by a dual-focus strategy:
* Short-term: Efforts are concentrated on optimizing surface codes and improving the latency of classical control hardware.
* Long-term: The research community is pivoting toward qLDPC architectures to address the "overhead problem," which is currently the primary bottleneck for scaling to millions of physical qubits.

[Source: graph://run-95db62d9c3f5/distill]
```

#### 5. Evidence and Provider/Agent Assignments
- **Evidence 1**: Verified primary lookup result (`graph://run-95db62d9c3f5/fast_primary`)
- **Evidence 2**: Specialist distillation synthesis (`graph://run-95db62d9c3f5/distill`)
- **Node Assignments**:
  - `fast_primary`: Role `fast_researcher`, executed via deterministic local lookup
  - `slow_speculative`: Role `deep_researcher`, terminated with state `cancelled`
  - `distill`: Role `distiller`, model `gemini-3.1-flash-lite-preview` via `gemini_1` slot on `glc_v3`
  - `answer`: Role `answer_with_evidence`, model `gemini-3.1-flash-lite-preview` via `gemini_1` slot on `glc_v3`

#### 6. Adversarial Attack and Fix
- **Adversarial Vector**: Result arriving after cancellation (race condition injection). An untrusted speculative worker catches `asyncio.CancelledError` and attempts to push a poisoned payload (`{"poison": "MALICIOUS_POISON_DATA"}`) back into the graph after the planner has marked it cancelled.
- **Vulnerability Before Fix**: If `LiveGraphExecutor` fails to inspect the task's persisted state upon future completion, `record_outcome` is invoked, turning the cancelled node into `succeeded`, storing the poison payload in `nodes.result_json`, and triggering planner mutations that leak poison into downstream synthesis.
- **Hardened Fix**: In `s13code/core/live_graph/core.py`, when a future completes, the executor verifies `self.store.node_state(run_id, task.id) == NodeState.CANCELLED`. If cancelled, the late return is deterministically discarded immediately.
- **Verification**: Tested in `tests/test_adversarial_cancellation.py`. The adversarial worker successfully runs its catch handler and returns the payload, but the executor suppresses it: `snapshot.nodes["attacker_speculative"]["result"]` is strictly `None`, state remains `cancelled`, and `task_succeeded` is never emitted.

#### 7. Reproduction from a Fresh Checkout
```bash
# 1. Sync dependencies and run unit tests
uv sync
uv run ruff check .
uv run pytest -q

# 2. Run the deterministic speculative and adversarial tests
uv run pytest tests/test_speculative_cancellation.py tests/test_adversarial_cancellation.py -v

# 3. Boot runtime services and run end-to-end prompt
# Terminal 1:
cd ../glc_v3 && uv run glc serve
# Terminal 2:
uv run s13code serve
# Terminal 3:
curl -s http://127.0.0.1:8113/v1/agent/runs \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id": "course", "project_id": "speculative-proof", "user_id": "student-01", "prompt": "Run a speculative research: find fast or deep results for quantum computing error correction."}'
```

## License

MIT. See `LICENSE`.

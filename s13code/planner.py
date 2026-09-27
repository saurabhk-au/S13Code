"""Strict GraphPatch proposal boundary for S13's non-browser agent catalogue."""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from s13code.core.live_graph import Event, GraphPatch, GraphSnapshot, TaskSpec

TextLLM = Callable[[str, str], Awaitable[dict[str, Any]]]
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# These are roles, not raw tools. The runtime maps each to a safe local skill.
NON_BROWSER_ROLES = frozenset({"researcher", "retriever", "distiller", "summariser", "formatter", "coder_validator"})


class FallbackPlanner(Protocol):
    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch: ...


class ConstrainedGraphPatchPlanner:
    """An LLM may propose role-labelled nodes; strict parsing keeps it safe.

    Invalid output falls back to the deterministic planner. This lets local
    proof runs remain deterministic while a configured live model can make
    the frontier adaptive.
    """
    def __init__(self, llm: TextLLM, fallback: FallbackPlanner, *, goal: str, roles: set[str] | None = None) -> None:
        self.llm, self.fallback = llm, fallback
        self.goal = goal
        self.roles = roles or set(NON_BROWSER_ROLES)
        self.last_selection: dict[str, Any] = {"mode": "deterministic_fallback"}

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        try:
            reply = await self.llm(self._prompt(graph, event), "Return only valid GraphPatch JSON.")
            patch = self._parse(reply.get("text", ""))
            self.last_selection = {"mode": "llm", "provider": reply.get("provider"), "model": reply.get("model"),
                                   "event": event.sequence}
            return patch
        except Exception as error:
            self.last_selection = {"mode": "deterministic_fallback", "event": event.sequence,
                                   "reason": f"{type(error).__name__}: {error}"}
            return await self.fallback.plan(graph, event)

    def _parse(self, text: str) -> GraphPatch:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data).difference({"add", "connect", "cancel", "wait", "resume", "finish", "reason"}):
            raise ValueError("invalid GraphPatch object")
        add: list[TaskSpec] = []
        for raw in data.get("add", []):
            if not isinstance(raw, dict) or set(raw).difference({"id", "role", "input", "metadata"}):
                raise ValueError("invalid task object")
            node_id, role = raw.get("id"), raw.get("role")
            if not isinstance(node_id, str) or not _ID.fullmatch(node_id) or role not in self.roles:
                raise ValueError("invalid node id or unsupported role")
            task_input, metadata = raw.get("input", {}), raw.get("metadata", {})
            if not isinstance(task_input, dict) or not isinstance(metadata, dict):
                raise ValueError("task input/metadata must be objects")
            add.append(TaskSpec(node_id, role, task_input, {**metadata, "agent": role}))
        def id_list(name: str) -> tuple[str, ...]:
            value = data.get(name, [])
            if not isinstance(value, list) or not all(isinstance(x, str) and _ID.fullmatch(x) for x in value):
                raise ValueError(f"invalid {name}")
            return tuple(value)
        edges = data.get("connect", [])
        if not isinstance(edges, list) or not all(isinstance(e, list) and len(e) == 2 and all(isinstance(x, str) and _ID.fullmatch(x) for x in e) for e in edges):
            raise ValueError("invalid connect")
        if not isinstance(data.get("finish", False), bool) or not isinstance(data.get("reason", ""), str):
            raise ValueError("invalid finish/reason")
        return GraphPatch(tuple(add), tuple((e[0], e[1]) for e in edges), id_list("cancel"), id_list("wait"),
                          id_list("resume"), data.get("finish", False), data.get("reason", ""))

    def _prompt(self, graph: GraphSnapshot, event: Event) -> str:
        return json.dumps({"goal": self.goal, "event": {"kind": event.kind, "node_id": event.node_id, "payload": event.payload},
            "nodes": [{"id": i, "role": n["skill"], "state": n["state"]} for i, n in graph.nodes.items()],
            "allowed_roles": sorted(self.roles),
            "patch_schema": {"add": [{"id": "safe_id", "role": "allowed_role", "input": {}, "metadata": {"agent": "role"}}],
                "connect": [["parent", "child"]], "cancel": [], "wait": [], "resume": [], "finish": False, "reason": "short"},
            "rules": ["emit only next useful work", "roles never call tools directly", "return JSON only"]})

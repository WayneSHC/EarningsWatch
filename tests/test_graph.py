"""
Unit tests for src/agent/graph.py.

Covers the agent-orchestration capability spec:
  - build_graph() compiles to a 7-node graph with the documented topology
  - get_agent() returns a singleton (no recompilation per call)
  - run_agent() seeds the initial AgentState with all required keys
  - is_tracing_enabled() requires both flag and key
"""
import pytest

from src.agent import graph as graph_mod


# ──────────────────────────────────────────────────────────────────────────
# build_graph topology
# ──────────────────────────────────────────────────────────────────────────

class TestBuildGraph:
    def test_compiles_without_error(self):
        agent = graph_mod.build_graph()
        # Compiled CompiledGraph exposes .nodes
        assert agent is not None
        assert hasattr(agent, "nodes")

    def test_seven_nodes_present(self):
        agent = graph_mod.build_graph()
        node_names = set(agent.nodes.keys())
        # __start__ / __end__ may also appear; we only require the seven business nodes
        expected = {"classify", "decompose", "route", "retrieve",
                    "detect", "reflect", "report"}
        assert expected.issubset(node_names), (
            f"Missing nodes: {expected - node_names}"
        )


# ──────────────────────────────────────────────────────────────────────────
# get_agent singleton
# ──────────────────────────────────────────────────────────────────────────

class TestGetAgentSingleton:
    def test_consecutive_calls_return_same_object(self):
        # Reset the module-level singleton to ensure the first call compiles
        graph_mod._agent = None
        a1 = graph_mod.get_agent()
        a2 = graph_mod.get_agent()
        assert a1 is a2


# ──────────────────────────────────────────────────────────────────────────
# run_agent initial state shape
# ──────────────────────────────────────────────────────────────────────────

class TestRunAgentInitialState:
    def test_invoke_called_with_full_state(self, monkeypatch):
        """All AgentState keys must be seeded before .invoke()."""
        captured: dict = {}

        class FakeAgent:
            def invoke(self, state):
                captured.update(state)
                return state

        monkeypatch.setattr(graph_mod, "get_agent", lambda: FakeAgent())

        graph_mod.run_agent("q", "台積電", "AI需求", None)

        # Required keys per src/agent/state.py
        required_keys = {
            "query", "company", "topic", "quarters",
            "sub_queries", "tool_plan",
            "retrieved", "news_context", "stock_data",
            "contradictions", "promises",
            "confidence", "iteration",
            "reflection_issues", "reflection_gaps",
            "coverage_matrix", "abstain",
            "cost_baseline_usd", "cost_guard_triggered",
            "final_report", "steps_log",
        }
        missing = required_keys - set(captured.keys())
        assert not missing, f"Initial state missing keys: {missing}"

    def test_defaults_match_spec(self, monkeypatch):
        captured: dict = {}

        class FakeAgent:
            def invoke(self, state):
                captured.update(state)
                return state

        monkeypatch.setattr(graph_mod, "get_agent", lambda: FakeAgent())

        graph_mod.run_agent("q", "台積電", "AI", None)

        assert captured["confidence"] == 1.0
        assert captured["iteration"] == 0
        assert captured["abstain"] is False
        assert captured["cost_guard_triggered"] is False
        assert captured["cost_baseline_usd"] == 0.0

    def test_none_quarters_becomes_empty_list(self, monkeypatch):
        captured: dict = {}

        class FakeAgent:
            def invoke(self, state):
                captured.update(state)
                return state

        monkeypatch.setattr(graph_mod, "get_agent", lambda: FakeAgent())

        graph_mod.run_agent("q", "台積電", "AI", None)

        assert captured["quarters"] == []


# ──────────────────────────────────────────────────────────────────────────
# is_tracing_enabled — both flag and key required
# ──────────────────────────────────────────────────────────────────────────

class TestIsTracingEnabled:
    def _clear(self, monkeypatch):
        for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
                    "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_disabled_by_default(self, monkeypatch):
        self._clear(monkeypatch)
        assert graph_mod.is_tracing_enabled() is False

    def test_flag_only_returns_false(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        assert graph_mod.is_tracing_enabled() is False

    def test_key_only_returns_false(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls__abc")
        assert graph_mod.is_tracing_enabled() is False

    def test_both_present_returns_true(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls__abc")
        assert graph_mod.is_tracing_enabled() is True

    def test_legacy_langchain_env_vars_work(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "1")
        monkeypatch.setenv("LANGCHAIN_API_KEY", "ls__xyz")
        assert graph_mod.is_tracing_enabled() is True

    def test_flag_value_not_truthy(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls__abc")
        assert graph_mod.is_tracing_enabled() is False

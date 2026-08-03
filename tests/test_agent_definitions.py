import pytest

from agent_definitions import AgentDefinitionStore


def test_seeds_and_adds_custom_agents(tmp_path):
    store = AgentDefinitionStore(tmp_path / "agents.db")
    assert store.get("orchestrator")["built_in"] == 1
    agent = store.save("Security Architect", "Finds risks", "Threat-model every design")
    assert agent["role"] == "security_architect"
    store.delete(agent["role"])
    with pytest.raises(KeyError):
        store.get(agent["role"])


def test_can_template_and_delete_builtin_without_reseeding(tmp_path):
    store = AgentDefinitionStore(tmp_path / "agents.db")
    template = store.save_template("orchestrator", "Coordinator")
    assert template["brief"] == store.get("orchestrator")["brief"]
    assert store.templates()[0]["name"] == "Coordinator"
    store.delete("orchestrator")
    with pytest.raises(KeyError):
        store.get("orchestrator")
    reopened = AgentDefinitionStore(tmp_path / "agents.db")
    with pytest.raises(KeyError):
        reopened.get("orchestrator")

import pytest

from team import AgentTeam


def test_provider_command_catalog_keeps_codex_native_commands_separate(tmp_path):
    team = AgentTeam(tmp_path)

    commands = team.provider_commands("codex")

    assert "/status" in {item["name"] for item in commands}
    assert "/review-branch" in {item["name"] for item in commands}
    assert all(item["name"].startswith("/") for item in commands)


def test_api_backed_providers_have_no_invented_native_commands(tmp_path):
    team = AgentTeam(tmp_path)

    for provider in ("openai", "google", "anthropic", "compatible"):
        assert team.provider_commands(provider) == []

    with pytest.raises(ValueError, match="Unknown provider"):
        team.provider_commands("not-a-provider")

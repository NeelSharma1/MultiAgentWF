import os

from credentials import LocalCredentialStore


def test_save_and_remove_local_credential(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("EXISTING=value\n")
    store = LocalCredentialStore(path)
    store.save("TEST_PROVIDER_KEY", "secret-value")
    assert os.environ["TEST_PROVIDER_KEY"] == "secret-value"
    assert "EXISTING=value" in path.read_text()
    store.remove("TEST_PROVIDER_KEY")
    assert "TEST_PROVIDER_KEY" not in path.read_text()
    assert "TEST_PROVIDER_KEY" not in os.environ


def test_skill_credential_status_reads_values_without_returning_them(tmp_path):
    path = tmp_path / ".env.local"
    store = LocalCredentialStore(path)
    store.save("SKILL_TEST_KEY", "skill-secret")

    assert store.configured("SKILL_TEST_KEY") is True
    assert store.values_for(["SKILL_TEST_KEY"]) == {"SKILL_TEST_KEY": "skill-secret"}

    store.remove("SKILL_TEST_KEY")

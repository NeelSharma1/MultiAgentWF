from shared_context import ContextStore


def test_context_visibility_and_crud(tmp_path):
    store = ContextStore(tmp_path / "test.db")
    public = store.save("Goal", "Build the thing", [])
    private = store.save("Implementation", "Use SQLite", ["programmer", "reviewer"])

    assert [item["id"] for item in store.list("researcher")] == [public["id"]]
    assert {item["id"] for item in store.list("programmer")} == {public["id"], private["id"]}

    updated = store.save("Implementation", "Use WAL-enabled SQLite", ["programmer"], private["id"])
    assert updated["content"] == "Use WAL-enabled SQLite"
    assert store.delete(public["id"])
    assert store.get(public["id"]) is None


def test_accepts_dynamic_agent_roles(tmp_path):
    store = ContextStore(tmp_path / "test.db")
    item = store.save("Magic research", "role", ["wizard"])
    assert store.list("wizard")[0]["id"] == item["id"]

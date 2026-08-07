import os

import team


def test_windows_native_codex_selection_uses_the_newest_installed_binary(tmp_path):
    if os.name != "nt":
        return

    node_modules = tmp_path / "node_modules"
    shim = node_modules / ".bin" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("", encoding="utf-8")

    old_binary = (
        node_modules / "@openai" / "codex-win32-x64-old" / "vendor" / "x86_64-pc-windows-msvc"
        / "bin" / "codex.exe"
    )
    new_binary = (
        node_modules / "@openai" / "codex-win32-x64-new" / "vendor" / "x86_64-pc-windows-msvc"
        / "bin" / "codex.exe"
    )
    old_binary.parent.mkdir(parents=True)
    new_binary.parent.mkdir(parents=True)
    old_binary.write_text("old", encoding="utf-8")
    new_binary.write_text("new", encoding="utf-8")
    old_binary.touch()
    new_binary.touch()
    os.utime(old_binary, (100, 100))
    os.utime(new_binary, (200, 200))

    selected = team._prefer_windows_native_codex(str(shim))

    assert selected == str(new_binary.resolve())

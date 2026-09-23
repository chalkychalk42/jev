"""The addon a client installs: one generated file under a neutral name, owning no globals.

Whatever is installed in the client can be read by anything that inspects it, and this
repository is public. So the installed addon carries no project name in its folder, table
of contents or code, defines no global names (a global is a name anyone can look up), and
ships without the comments that explain it. `test_addon_runs.py` executes this build and
fails any run in which it sets a global or names a frame.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

from tools import gen_addon_fields as addon_build

LUAC = shutil.which("luac5.1")
SOURCES = ("Supplies.lua", "Helpers.lua", "Fields.lua", "JevRadio.lua")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> pathlib.Path:
    return addon_build.build(tmp_path_factory.mktemp("build"), fields_lua=addon_build.render())


def _source(name: str) -> str:
    if name == "Fields.lua":
        return addon_build.render()
    return (addon_build.SOURCE / name).read_text(encoding="utf-8")


def test_nothing_installed_names_the_project(built):
    name = addon_build.ADDON_NAME
    assert built.name == name
    assert sorted(p.name for p in built.iterdir()) == [f"{name}.lua", f"{name}.toc"]
    for file in built.iterdir():
        assert "jev" not in file.read_text(encoding="utf-8").lower(), f"{file.name} names it"
    toc = (built / f"{name}.toc").read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("## Author") for line in toc)
    assert f"## Title: {addon_build.ADDON_TITLE}" in toc
    assert [line for line in toc if not line.startswith("##")] == [f"{name}.lua"]


def test_the_installed_code_carries_no_comments(built):
    code = (built / f"{addon_build.ADDON_NAME}.lua").read_text(encoding="utf-8")
    assert addon_build.strip_lua_comments(code) + "\n" == code


@pytest.mark.skipif(LUAC is None, reason="needs luac5.1 to compare compiled code")
@pytest.mark.parametrize("name", SOURCES)
def test_stripping_comments_changes_no_instruction_constant_or_local(name, tmp_path):
    """Only comments and blank lines go. Compiled, a part lists the same instructions,
    constants, locals and upvalues; line numbers and addresses are all that may differ."""

    def listing(text: str) -> str:
        path = tmp_path / "part.lua"
        path.write_text(text, encoding="utf-8")
        out = subprocess.run([LUAC, "-l", "-l", "-p", str(path)], capture_output=True,
                             text=True, check=True).stdout
        out = re.sub(r"\[\d+\]", "[]", out)
        out = re.sub(r"<[^>]*:\d+,\d+>", "<>", out)
        return re.sub(r"0x[0-9a-f]+", "0x", out)

    source = _source(name)
    assert listing(addon_build.strip_lua_comments(source)) == listing(source)


@pytest.mark.parametrize(("source", "expected"), [
    ('local s = "a -- b" -- note\n', 'local s = "a -- b"'),
    ("local s = 'it\\'s -- x' --[[ gone\n over lines ]] local t = 1\n",
     "local s = 'it\\'s -- x'  local t = 1"),
    ("--[==[ a ]] still a comment ]==]\nx = [[ -- kept ]]\n", "x = [[ -- kept ]]"),
    ("x = [=[\n-- kept\n]=]\n", "x = [=[\n-- kept\n]=]"),
    ("x = [[\n\n  kept  \n]] -- gone\n", "x = [[\n\n  kept  \n]]"),
    ("\n\n  -- only a comment\n\ny = 2   \n", "y = 2"),
    ("--[ not long\nz = t[i[1]]\n", "z = t[i[1]]"),
])
def test_the_comment_stripper_keeps_every_string_whole(source, expected):
    assert addon_build.strip_lua_comments(source) == expected


def test_installing_moves_earlier_installs_aside_and_copies_the_build(built, tmp_path):
    addons, backups = tmp_path / "AddOns", tmp_path / "backups"
    (addons / "JevRadio").mkdir(parents=True)
    (addons / "JevRadio" / "JevRadio.toc").write_text("old", encoding="utf-8")
    (addons / "SomeoneElses").mkdir()

    dest = addon_build.install(addons, built, backups)

    assert dest == addons / addon_build.ADDON_NAME
    assert sorted(p.name for p in addons.iterdir()) == sorted([addon_build.ADDON_NAME,
                                                               "SomeoneElses"])
    assert all((dest / f.name).read_bytes() == f.read_bytes() for f in built.iterdir())
    (kept,) = backups.iterdir()
    assert kept.name.endswith("-JevRadio")
    assert (kept / "JevRadio.toc").read_text(encoding="utf-8") == "old"

    # Twice within the same second: the earlier install is kept too, not nested into.
    addon_build.install(addons, built, backups)
    addon_build.install(addons, built, backups)
    kept = sorted(p.name.split("-", 1)[1] for p in backups.iterdir())
    assert kept[0] == "JevRadio" and all(k.startswith(addon_build.ADDON_NAME) for k in kept[1:])
    assert len(kept) == 3
    assert not [c for p in backups.iterdir() for c in p.iterdir() if c.is_dir()], "nested"


def test_installing_into_a_missing_folder_refuses(built, tmp_path):
    with pytest.raises(SystemExit, match="no such AddOns directory"):
        addon_build.install(tmp_path / "nowhere", built, tmp_path / "backups")

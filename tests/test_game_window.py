"""Only a positively identified native game can become a capture/input destination."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from jev.clients import capture, live, win32
from jev.run import client


@pytest.fixture
def windows(monkeypatch):
    rows = {
        11: ("World of Warcraft - browser", "Chrome_WidgetWin_1", 101, "chrome.exe"),
        22: ("World of Warcraft", "GxWindowClassD3d", 202, "Wow.exe"),
        33: ("World of Warcraft", "GxWindowClassD3d", 303, "other.exe"),
        44: ("World of Warcraft", "OtherWindow", 404, "Wow.exe"),
        55: ("World of Warcraft", "GxWindowClassD3d", 505, None),
    }

    def enumerate_windows(title_contains="", class_name=None):
        return [hwnd for hwnd, (title, native_class, _, _) in rows.items()
                if title_contains.casefold() in title.casefold()
                and (class_name is None or native_class == class_name)]

    monkeypatch.setattr(win32, "find_windows", enumerate_windows)
    monkeypatch.setattr(win32, "window_process_id", lambda hwnd: rows[hwnd][2])
    monkeypatch.setattr(win32, "_process_names",
                        lambda: {row[2]: row[3] for row in rows.values() if row[3] is not None})
    return rows


def test_matching_title_cannot_promote_browser_or_unknown_process_to_game(windows):
    assert win32.find_windows("World of Warcraft")[0] == 11
    assert win32.find_game() == [22]
    assert win32.game_window() == 22
    assert capture.find_game() == [22]


def test_title_only_narrows_identified_games(windows):
    windows[22] = ("World of Warcraft - client one", "GxWindowClassD3d", 202, "WOW.EXE")
    windows[66] = ("World of Warcraft - client two", "GxWindowClassD3d", 606, "Wow.exe")
    assert win32.find_game("client one") == [22]
    assert win32.find_game("browser") == []
    assert win32.game_window("client two") == 66
    with pytest.raises(win32.GameWindowError, match="select one explicitly"):
        win32.game_window()


def test_process_disappearance_is_not_a_title_fallback(windows, monkeypatch):
    monkeypatch.setattr(win32, "window_process_id", lambda hwnd: None)
    assert win32.find_game() == []
    with pytest.raises(win32.GameWindowError, match="verified class and executable"):
        win32.game_window()


@pytest.mark.parametrize("index", [-1, True, 1.5])
def test_game_selection_rejects_invalid_indices(windows, index):
    with pytest.raises(win32.GameWindowError, match="nonnegative integer"):
        win32.game_window(index=index)


def test_explicit_selection_is_available_for_multiple_verified_clients(windows):
    windows[66] = ("World of Warcraft", "GxWindowClassD3d", 606, "Wow.exe")
    assert win32.game_window(index=0) == 22
    assert win32.game_window(index=1) == 66
    with pytest.raises(win32.GameWindowError, match="found 2"):
        win32.game_window(index=2)


@pytest.mark.parametrize("count", [0, 2])
def test_runtime_attach_refuses_before_constructing_hid_or_capture(monkeypatch, count):
    monkeypatch.setattr(win32, "available", lambda: True)
    monkeypatch.setattr(win32, "find_game", lambda title: list(range(count)))

    def forbidden(*args, **kwargs):
        pytest.fail("ambiguous or absent game must not construct input or capture")

    monkeypatch.setattr(client, "Hid", forbidden)
    monkeypatch.setattr(client, "WindowCapture", forbidden)
    monkeypatch.setattr(win32, "client_rect", forbidden)
    with pytest.raises(client.NotRunning):
        client.attach()


def test_runtime_attach_and_live_bind_use_the_identified_handle(windows, monkeypatch):
    monkeypatch.setattr(win32, "available", lambda: True)
    monkeypatch.setattr(win32, "client_rect", lambda hwnd: (10, 20, 1600, 900))
    monkeypatch.setattr(client, "Hid", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(client, "WindowCapture", lambda hwnd, **kwargs: SimpleNamespace(hwnd=hwnd))
    monkeypatch.setattr(live, "LiveSource", lambda **kwargs: SimpleNamespace(**kwargs))
    attached = client.attach("verified")
    assert attached.hwnd == attached.hid.hwnd == attached.cap.hwnd == 22
    assert live.bind().hwnd == 22
    windows[66] = ("World of Warcraft", "GxWindowClassD3d", 606, "Wow.exe")
    with pytest.raises(win32.GameWindowError, match="select one explicitly"):
        live.bind()
    assert live.bind(index=1).hwnd == 66


def test_process_snapshot_reads_names_and_always_closes_its_handle(monkeypatch):
    closed = []
    snapshot = 0x100000005
    processes = iter([(101, "chrome.exe"), (202, "Wow.exe")])

    def next_process(handle, pointer):
        assert handle == snapshot
        entry = ctypes.cast(pointer, ctypes.POINTER(win32.PROCESSENTRY32W)).contents
        assert entry.dwSize == ctypes.sizeof(win32.PROCESSENTRY32W)
        row = next(processes, None)
        if row is None:
            return False
        entry.th32ProcessID, entry.szExeFile = row
        return True

    def create(flags, pid):
        assert flags == win32.TH32CS_SNAPPROCESS and pid == 0
        return snapshot

    monkeypatch.setattr(win32, "IS_WINDOWS", True)
    monkeypatch.setattr(win32, "kernel32", SimpleNamespace(
        CreateToolhelp32Snapshot=create, Process32FirstW=next_process,
        Process32NextW=next_process, CloseHandle=closed.append,
    ))
    assert win32._process_names() == {101: "chrome.exe", 202: "Wow.exe"}
    assert closed == [snapshot]


def test_unavailable_process_snapshot_returns_no_identity(monkeypatch):
    closed = []
    monkeypatch.setattr(win32, "IS_WINDOWS", True)
    monkeypatch.setattr(win32, "kernel32", SimpleNamespace(
        CreateToolhelp32Snapshot=lambda *args: ctypes.c_void_p(-1).value,
        CloseHandle=closed.append,
    ))
    assert win32._process_names() == {}
    assert closed == []


def test_exception_during_process_enumeration_still_closes_snapshot(monkeypatch):
    closed = []

    def failed(*args):
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(win32, "IS_WINDOWS", True)
    monkeypatch.setattr(win32, "kernel32", SimpleNamespace(
        CreateToolhelp32Snapshot=lambda *args: 123,
        Process32FirstW=failed, CloseHandle=closed.append,
    ))
    with pytest.raises(OSError, match="snapshot unavailable"):
        win32._process_names()
    assert closed == [123]

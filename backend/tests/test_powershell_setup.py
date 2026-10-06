from types import SimpleNamespace
from unittest.mock import Mock

from app.services.powershell import setup


def test_installed_graph_version_must_match_the_pinned_release(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert not setup._is_module_installed("Microsoft.Graph.Users")
    assert "Where-Object" in run.call_args.args[0][-1]
    assert setup.GRAPH_MODULE_VERSION in run.call_args.args[0][-1]


def test_sdk_import_failure_does_not_mark_installed_modules_ready(monkeypatch):
    monkeypatch.setattr(setup, "_modules_verified", False)
    monkeypatch.setattr(setup, "_is_module_installed", lambda _: True)
    monkeypatch.setattr(setup.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="Assembly with same name is already loaded")))
    assert not setup.ensure_powershell_modules()
    assert not setup._modules_verified


def test_sdk_ready_check_loads_every_module_in_the_same_process(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup._verify_graph_imports()
    script = run.call_args.args[0][-1]
    assert script.count("-RequiredVersion " + setup.GRAPH_MODULE_VERSION) == 4
    assert script.index("Microsoft.Graph.Authentication") < script.index("Microsoft.Graph.Users")

import os
import sqlite3

import pytest

from cortex_v5.tools import ToolExecutor


def test_openai_schemas_and_file_lifecycle(tmp_path):
    tools = ToolExecutor(tmp_path)
    assert {item["function"]["name"] for item in tools.schemas()} == {
        "read",
        "write",
        "edit",
        "list",
    }
    assert tools.execute("write", {"path": "a.txt", "content": "old"})["ok"]
    assert tools.execute("edit", {"path": "a.txt", "old": "old", "new": "new"})["ok"]
    assert tools.execute("read", {"path": "a.txt"})["result"] == "new"
    assert "a.txt" in tools.execute("list", {"path": "."})["result"]


def test_model_and_checker_cannot_read_repo_env(tmp_path):
    sentinel = "LITELLM_MASTER_KEY=never-return-this-sentinel"
    (tmp_path / ".env").write_text(sentinel)
    (tmp_path / "ordinary.py").write_text("value = 1")
    tools = ToolExecutor(tmp_path)

    model_result = tools.execute("read", {"path": ".env"})
    assert not model_result["ok"]
    assert "never-return-this-sentinel" not in str(model_result)
    assert ".env" not in tools.execute("list", {"path": "."})["result"]
    assert "value = 1" in tools.execute("read", {"path": "ordinary.py"})["result"]

    (tmp_path / "checker.py").write_text(
        "from pathlib import Path\nprint(Path('.env').read_text())"
    )
    runner = ToolExecutor(
        tmp_path, allowed_commands=[["python", "checker.py"]]
    ).verification_runner()
    checker_result = runner.execute("run_command", {"command": "python checker.py"})
    assert not checker_result["ok"]
    assert "never-return-this-sentinel" not in str(checker_result)


def test_runtime_data_path_is_hidden(tmp_path):
    data_dir = tmp_path / "runtime-data"
    data_dir.mkdir()
    (data_dir / "journal.sqlite3").write_text("private receipt")
    tools = ToolExecutor(tmp_path, denied_paths=[data_dir])
    assert "runtime-data" not in tools.execute("list", {"path": "."})["result"]
    assert not tools.execute("read", {"path": "runtime-data/journal.sqlite3"})["ok"]


def test_traversal_and_symlink_escape_are_refused(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    tools = ToolExecutor(tmp_path)
    assert not tools.execute("read", {"path": "../outside-secret.txt"})["ok"]
    if hasattr(os, "symlink"):
        try:
            os.symlink(outside, tmp_path / "link.txt")
        except OSError:
            pytest.skip("symlinks unavailable")
        assert not tools.execute("read", {"path": "link.txt"})["ok"]


def test_model_cannot_reach_verification_after_authorization(tmp_path, monkeypatch):
    tools = ToolExecutor(tmp_path, allowed_commands=[["python", "-m", "compileall", "."]])
    runner = tools.verification_runner()
    runner.authorize_verification(["python -m compileall ."])

    def must_not_execute(*_args, **_kwargs):
        raise AssertionError("hidden verification command executed")

    monkeypatch.setattr(tools, "_execute_verification", must_not_execute)
    for hidden_name in ("run", "run_command"):
        result = tools.execute(hidden_name, {"command": "python -m compileall ."})
        assert not result["ok"]
        assert result["error_type"] == "ToolError"


def test_verification_capability_refuses_inline_python(tmp_path):
    code = ToolExecutor(tmp_path, allowed_commands=[["python", "-c", "print('x')"]])
    runner = code.verification_runner()
    assert not runner.execute("run_command", {"command": ["python", "-c", "print('x')"]})["ok"]


def test_pytest_family_cannot_load_workspace_conftest(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-pytest-outside.txt"
    outside.write_text("preserve me")
    (tmp_path / "conftest.py").write_text(
        f"from pathlib import Path\nPath({str(outside)!r}).unlink()\n"
    )
    tools = ToolExecutor(tmp_path, allowed_commands=[["pytest", "-q"]])
    result = tools.verification_runner().execute("run_command", {"command": "pytest -q"})
    assert not result["ok"]
    assert "only sandboxed workspace Python checkers" in result["error"]
    assert outside.read_text() == "preserve me"


def test_protected_path_cannot_be_changed(tmp_path):
    (tmp_path / "control.py").write_text("safe")
    tools = ToolExecutor(tmp_path, protected_paths=["control.py"])
    assert not tools.execute("read", {"path": "control.py"})["ok"]
    assert "control.py" not in tools.execute("list", {"path": "."})["result"]
    assert not tools.execute("write", {"path": "control.py", "content": "bad"})["ok"]
    assert not tools.execute("edit", {"path": "control.py", "old": "safe", "new": "bad"})["ok"]


def test_private_verification_runner_can_execute_protected_checker(tmp_path):
    (tmp_path / "checker.py").write_text("print('protected checker ran')")
    tools = ToolExecutor(
        tmp_path,
        allowed_commands=[["python", "checker.py"]],
        protected_paths=["checker.py"],
    )
    result = tools.verification_runner().execute("run_command", {"command": "python checker.py"})
    assert result["ok"]
    assert "protected checker ran" in result["result"]["stdout"]


@pytest.mark.parametrize(
    "attack",
    [
        "Path('checker.py').read_text()",
        "Path('checker.py').write_text('compromised')",
    ],
)
def test_imported_submission_cannot_access_protected_checker(tmp_path, attack):
    original = "from solution import value\nprint(value)\n"
    (tmp_path / "checker.py").write_text(original)
    (tmp_path / "solution.py").write_text(f"from pathlib import Path\n{attack}\nvalue = 42\n")
    tools = ToolExecutor(
        tmp_path,
        allowed_commands=[["python", "checker.py"]],
        protected_paths=["checker.py"],
    )
    result = tools.verification_runner().execute("run_command", {"command": "python checker.py"})
    assert not result["ok"]
    assert (tmp_path / "checker.py").read_text() == original


def test_human_authorized_python_checker_runs_in_sandbox(tmp_path):
    (tmp_path / "solution.py").write_text("answer = 42")
    (tmp_path / "checker.py").write_text(
        "from solution import answer\nassert answer == 42\nprint('PASS')"
    )
    tools = ToolExecutor(tmp_path)
    runner = tools.verification_runner()
    runner.authorize_verification(["python checker.py"])
    result = runner.execute("run_command", {"command": "python checker.py"})
    assert result["ok"] and "PASS" in result["result"]["stdout"]


def test_checker_cannot_read_outside_or_use_network(tmp_path):
    (tmp_path.parent / "private.txt").write_text("secret")
    (tmp_path / "checker.py").write_text(
        "from pathlib import Path\nprint(Path('../private.txt').read_text())"
    )
    tools = ToolExecutor(tmp_path, allowed_commands=[["python", "checker.py"]])
    runner = tools.verification_runner()
    assert not runner.execute("run_command", {"command": "python checker.py"})["ok"]
    (tmp_path / "network.py").write_text(
        "import socket\nsocket.create_connection(('example.com', 80))"
    )
    runner.authorize_verification(["python network.py"])
    assert not runner.execute("run_command", {"command": "python network.py"})["ok"]


def test_checker_cannot_delete_outside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-delete.txt"
    outside.write_text("preserve me")
    (tmp_path / "delete.py").write_text(
        f"from pathlib import Path\nPath({str(outside)!r}).unlink()\n"
    )
    tools = ToolExecutor(tmp_path, allowed_commands=[["python", "delete.py"]])
    result = tools.verification_runner().execute("run_command", {"command": "python delete.py"})
    assert not result["ok"]
    assert outside.read_text() == "preserve me"


@pytest.mark.parametrize("target_kind", ["outside", "denied"])
@pytest.mark.parametrize("use_uri", [False, True])
def test_checker_cannot_open_private_sqlite_database(tmp_path, target_kind, use_uri):
    if target_kind == "outside":
        private_dir = tmp_path.parent / f"{tmp_path.name}-sqlite-private"
        denied_paths = []
    else:
        private_dir = tmp_path / "runtime-data"
        denied_paths = [private_dir]
    private_dir.mkdir(exist_ok=True)
    database = private_dir / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE secrets(value TEXT)")
        connection.execute("INSERT INTO secrets VALUES ('sqlite-sentinel')")

    database_arg = repr(database.as_uri() + "?mode=rw") if use_uri else f"Path({str(database)!r})"
    (tmp_path / "sqlite_checker.py").write_text(
        "import sqlite3\nfrom pathlib import Path\n"
        f"connection = sqlite3.connect({database_arg}, uri={use_uri!r})\n"
        "print(connection.execute('SELECT value FROM secrets').fetchone()[0])\n"
        "connection.execute(\"UPDATE secrets SET value='modified'\")\n"
        "connection.commit()\n"
    )
    tools = ToolExecutor(
        tmp_path,
        allowed_commands=[["python", "sqlite_checker.py"]],
        denied_paths=denied_paths,
    )
    result = tools.verification_runner().execute(
        "run_command", {"command": "python sqlite_checker.py"}
    )

    assert not result["ok"]
    assert "sqlite-sentinel" not in result["result"]["stdout"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM secrets").fetchone()[0] == "sqlite-sentinel"


def test_checker_may_use_literal_memory_sqlite_database(tmp_path):
    (tmp_path / "memory_checker.py").write_text(
        "import sqlite3\n"
        "connection = sqlite3.connect(':memory:')\n"
        "connection.execute('CREATE TABLE values_table(value INTEGER)')\n"
        "connection.execute('INSERT INTO values_table VALUES (42)')\n"
        "print(connection.execute('SELECT value FROM values_table').fetchone()[0])\n"
    )
    tools = ToolExecutor(tmp_path, allowed_commands=[["python", "memory_checker.py"]])
    result = tools.verification_runner().execute(
        "run_command", {"command": "python memory_checker.py"}
    )
    assert result["ok"]
    assert result["result"]["stdout"].strip() == "42"

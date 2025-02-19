import os
import subprocess
from pathlib import Path

import pytest

from mentat.diff_context import DiffContext, get_diff_context
from mentat.errors import UserError

rel_path = Path("multifile_calculator/operations.py")


def _update_ops(temp_testbed, last_line, commit_message=None):
    # Update the last line of operations.py and (optionally) commit
    abs_path = os.path.join(temp_testbed, "multifile_calculator", "operations.py")
    with open(abs_path, "r") as f:
        lines = f.readlines()
    lines[-1:] = [
        f"    return {last_line}\n",
    ]
    with open(abs_path, "w") as f:
        f.writelines(lines)
    if commit_message:
        subprocess.run(["git", "add", abs_path], cwd=temp_testbed)
        subprocess.run(["git", "commit", "-m", commit_message], cwd=temp_testbed)


@pytest.fixture
def git_history(mock_config, temp_testbed):
    """Load a git repo with the following branches/commits:

    main
      'a / b' (from temp_testbed)
      'commit2'
      'commit3'
    test_branch (from commit2)
      'commit4'
    """
    _update_ops(temp_testbed, "commit2", "commit2")
    _update_ops(temp_testbed, "commit3", "commit3")
    subprocess.run(["git", "checkout", "HEAD~1"], cwd=temp_testbed)
    subprocess.run(["git", "checkout", "-b", "test_branch"], cwd=temp_testbed)
    # commit4
    _update_ops(temp_testbed, "commit4", "commit4")
    # Return on master commit3
    subprocess.run(["git", "checkout", "master"], cwd=temp_testbed)


def _get_file_message(temp_testbed):
    abs_path = os.path.join(temp_testbed, "multifile_calculator", "operations.py")
    file_message = ["/multifile_calculator/operations.py"]
    with open(abs_path, "r") as f:
        for i, line in enumerate(f.readlines()):
            file_message.append(f"{i}:{line}")
    return file_message


def test_diff_context_default(mock_config, temp_testbed, git_history):
    # DiffContext.__init__() (default): active code vs last commit
    diff_context = DiffContext(mock_config)
    assert diff_context.config
    assert diff_context.target == "HEAD"
    assert diff_context.name == "HEAD (last commit)"
    assert diff_context.files == []

    # DiffContext.files (property): return git-tracked files with active changes
    _update_ops(temp_testbed, "commit5")
    assert diff_context.files == [rel_path]

    # DiffContext.annotate_file_message(): modify file_message with diff
    file_message = _get_file_message(temp_testbed)
    annotated_message = diff_context.annotate_file_message(rel_path, file_message)
    expected = file_message[:-1] + [
        "14:-    return commit3",
        "14:+    return commit5",
    ]
    assert annotated_message == expected


def test_diff_context_commit(mock_config, temp_testbed, git_history):
    # Get the hash of 2-commits-ago
    last_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD~2"], cwd=temp_testbed, text=True
    ).strip()
    diff_context = get_diff_context(mock_config, diff=last_commit)
    assert diff_context.target == last_commit
    assert diff_context.name == f"{last_commit[:8]}: add testbed"
    assert diff_context.files == [rel_path]

    file_message = _get_file_message(temp_testbed)
    annotated_message = diff_context.annotate_file_message(rel_path, file_message)
    expected = file_message[:-1] + [
        "14:-    return a / b",
        "14:+    return commit3",
    ]
    assert annotated_message == expected


def test_diff_context_branch(mock_config, temp_testbed, git_history):
    diff_context = get_diff_context(mock_config, diff="test_branch")
    assert diff_context.target == "test_branch"
    assert diff_context.name.startswith("Branch test_branch:")
    assert diff_context.name.endswith(": commit4")
    assert diff_context.files == [rel_path]

    file_message = _get_file_message(temp_testbed)
    annotated_message = diff_context.annotate_file_message(rel_path, file_message)
    expected = file_message[:-1] + [
        "14:-    return commit4",
        "14:+    return commit3",
    ]
    assert annotated_message == expected


def test_diff_context_relative(mock_config, temp_testbed, git_history):
    diff_context = get_diff_context(mock_config, diff="HEAD~2")
    assert diff_context.target == "HEAD~2"
    assert diff_context.name.startswith("HEAD~2: ")
    assert diff_context.name.endswith(": add testbed")
    assert diff_context.files == [rel_path]

    file_message = _get_file_message(temp_testbed)
    annotated_message = diff_context.annotate_file_message(rel_path, file_message)
    expected = file_message[:-1] + [
        "14:-    return a / b",
        "14:+    return commit3",
    ]
    assert annotated_message == expected


def test_diff_context_errors(mock_config, temp_testbed, git_history):
    # Can't use both diff and pr_diff
    with pytest.raises(UserError) as e:
        get_diff_context(mock_config, diff="HEAD", pr_diff="master")
    assert str(e.value) == "Cannot specify more than one type of diff."

    # Invalid treeish
    with pytest.raises(UserError) as e:
        get_diff_context(mock_config, diff="invalid")
    assert str(e.value) == "Invalid treeish: invalid"


def test_diff_context_pr(mock_config, temp_testbed, git_history):
    subprocess.run(["git", "checkout", "test_branch"], cwd=temp_testbed)
    diff_context = get_diff_context(mock_config, pr_diff="master")

    commit2 = subprocess.check_output(
        ["git", "rev-parse", "HEAD~1"], cwd=temp_testbed, text=True
    ).strip()
    assert diff_context.target == commit2
    assert diff_context.name.startswith("Merge-base Branch master:")
    assert diff_context.name.endswith(": commit2")  # NOT the latest
    assert diff_context.files == [rel_path]

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from termcolor import cprint

from .config_manager import ConfigManager
from .errors import UserError
from .git_handler import (
    check_head_exists,
    get_diff_for_file,
    get_files_in_diff,
    get_treeish_metadata,
)


@dataclass
class DiffAnnotation:
    start: int
    message: list[str]

    @property
    def length(self):
        return sum(bool(line.startswith("+")) for line in self.message)


def _parse_diff(diff: str) -> list[DiffAnnotation]:
    """Parse diff into a list of annotations."""
    annotations: list[DiffAnnotation] = []
    active_annotation: Optional[DiffAnnotation] = None
    lines = diff.splitlines()
    for line in lines[4:]:  # Ignore header
        if line.startswith(("---", "+++", "//")):
            continue
        elif line.startswith("@@"):
            if active_annotation:
                annotations.append(active_annotation)
            _new_index = line.split(" ")[2]
            if "," in _new_index:
                new_start = _new_index[1:].split(",")[0]
            else:
                new_start = _new_index[1:]
            active_annotation = DiffAnnotation(int(new_start), [])
        elif line.startswith(("+", "-")):
            if not active_annotation:
                raise UserError("Invalid diff")
            active_annotation.message.append(line)
    if active_annotation:
        annotations.append(active_annotation)
    annotations.sort(key=lambda a: a.start)
    return annotations


def _annotate_file_message(
    code_message: list[str], annotations: list[DiffAnnotation]
) -> list[str]:
    """Return the code_message with annotations inserted."""
    active_index = 0
    annotated_message: list[str] = []
    for annotation in annotations:
        # Fill-in lines between annotations
        if active_index < annotation.start:
            unaffected_lines = code_message[active_index : annotation.start]
            annotated_message += unaffected_lines
        active_index = annotation.start
        if annotation.start == 0:
            # Make sure the PATH stays on line 1
            annotated_message.append(code_message[0])
            active_index += 1
        i_minus = None
        for line in annotation.message:
            sign = line[0]
            if sign == "+":
                # Add '+' lines in place of code_message lines
                annotated_message.append(f"{active_index}:{line}")
                active_index += 1
                i_minus = None
            elif sign == "-":
                # Insert '-' lines at the point they were removed
                i_minus = 0 if i_minus is None else i_minus
                annotated_message.append(f"{annotation.start + i_minus}:{line}")
                i_minus += 1
    if active_index < len(code_message):
        annotated_message += code_message[active_index:]

    return annotated_message


class DiffContext:
    config: ConfigManager

    def __init__(
        self,
        config: ConfigManager,
        target: Optional[str] = None,
        name: Optional[str] = None,
    ):
        self.config = config
        if target is None:
            self.target = "HEAD"
            self.name = "HEAD (last commit)"
        else:
            self.target = target
            self.name = name

    @property
    def files(self) -> list[Path]:
        if self.target == "HEAD" and not check_head_exists(self.config.git_root):
            return []  # A new repo without any commits
        return get_files_in_diff(self.config.git_root, self.target)

    def display_context(self) -> None:
        if not self.files:
            return
        cprint("Diff annotations:", "green")
        num_files = len(self.files)
        num_lines = 0
        # TODO: Only include paths in context
        for file in self.files:
            diff = get_diff_for_file(self.config.git_root, self.target, file)
            diff_lines = diff.splitlines()
            num_lines += len(
                [line for line in diff_lines if line.startswith(("+ ", "- "))]
            )
        print(f" ─•─ {self.name} | {num_files} files | {num_lines} lines\n")

    def annotate_file_message(
        self, rel_path: Path, file_message: list[str]
    ) -> list[str]:
        """Return file_message annotated with active diff."""
        if not self.files:
            return file_message

        diff = get_diff_for_file(self.config.git_root, self.target, rel_path)
        annotations = _parse_diff(diff)
        return _annotate_file_message(file_message, annotations)


TreeishType = Literal["commit", "branch", "relative"]


def _git_command(git_root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git"] + list(args), cwd=git_root, stderr=subprocess.PIPE, text=True
        ).strip()
    except subprocess.CalledProcessError:
        return None


def _get_treeish_type(git_root: Path, treeish: str) -> TreeishType:
    object_type = _git_command(git_root, "cat-file", "-t", treeish)

    if not object_type:
        raise UserError(f"Invalid treeish: {treeish}")

    if object_type == "commit":
        if "~" in treeish or "^" in treeish:
            return "relative"

        if _git_command(git_root, "show-ref", "--heads", treeish):
            return "branch"
        else:
            return "commit"

    raise UserError(f"Unsupported treeish type: {object_type}")


def get_diff_context(
    config: ConfigManager,
    diff: Optional[str] = None,
    pr_diff: Optional[str] = None,
):
    if diff and pr_diff:
        raise UserError("Cannot specify more than one type of diff.")

    target = diff or pr_diff
    if not target:
        return DiffContext(config)

    name = ""
    treeish_type = _get_treeish_type(config.git_root, target)
    if treeish_type == "branch":
        name += f"Branch {target}: "
    elif treeish_type == "relative":
        name += f"{target}: "

    if pr_diff:
        name = f"Merge-base {name}"
        target = _git_command(config.git_root, "merge-base", "HEAD", pr_diff)
        if not target:
            raise UserError(f"Cannot identify merge base between HEAD and {pr_diff}")

    meta = get_treeish_metadata(config.git_root, target)
    name += f'{meta["hexsha"][:8]}: {meta["summary"]}'
    return DiffContext(config, target, name)

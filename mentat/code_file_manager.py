import logging
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Union

from termcolor import cprint

from mentat.llm_api import count_tokens, model_context_size

from .change_conflict_resolution import (
    resolve_insertion_conflicts,
    resolve_non_insertion_conflicts,
)
from .code_change import CodeChange, CodeChangeAction
from .code_context import CodeContext
from .code_file import CodeFile
from .config_manager import ConfigManager
from .errors import MentatError
from .user_input_manager import UserInputManager


class CodeFileManager:
    def __init__(
        self,
        user_input_manager: UserInputManager,
        config: ConfigManager,
        code_context: CodeContext,
    ):
        self.user_input_manager = user_input_manager
        self.config = config
        self.code_context = code_context

    def _read_file(self, file: Union[Path, CodeFile]) -> list[str]:
        if isinstance(file, CodeFile):
            rel_path = file.path
        else:
            rel_path = file
        abs_path = self.config.git_root / rel_path

        with open(abs_path, "r") as f:
            lines = f.read().split("\n")
        return lines

    def _read_all_file_lines(self) -> None:
        self.file_lines = dict[Path, list[str]]()
        for file in self.code_context.files.values():
            rel_path = Path(os.path.relpath(file.path, self.config.git_root))
            self.file_lines[rel_path] = self._read_file(file)

    def get_code_message(self, model: str) -> str:
        code_message: list[str] = []
        if self.code_context.diff_context.files:
            code_message += [
                "Diff References:",
                f' "-" = {self.code_context.diff_context.name}',
                ' "+" = Active Changes',
                "",
            ]

        self._read_all_file_lines()
        code_message += ["Code Files:\n"]
        for file in self.code_context.files.values():
            file_message: list[str] = []
            abs_path = file.path
            rel_path = Path(os.path.relpath(abs_path, self.config.git_root))

            # We always want to give GPT posix paths
            posix_rel_path = Path(rel_path).as_posix()
            file_message.append(posix_rel_path)

            for i, line in enumerate(self.file_lines[rel_path], start=1):
                if file.contains_line(i):
                    file_message.append(f"{i}:{line}")
            file_message.append("")

            if rel_path in self.code_context.diff_context.files:
                file_message = self.code_context.diff_context.annotate_file_message(
                    rel_path, file_message
                )

            code_message += file_message

        if self.code_context.code_map is not None:
            code_message_tokens = count_tokens("\n".join(code_message), model)
            context_size = model_context_size(model)
            if context_size:
                max_tokens_for_code_map = context_size - code_message_tokens
                if self.code_context.code_map.token_limit:
                    code_map_message_token_limit = min(
                        self.code_context.code_map.token_limit, max_tokens_for_code_map
                    )
                else:
                    code_map_message_token_limit = max_tokens_for_code_map
            else:
                code_map_message_token_limit = self.code_context.code_map.token_limit

            code_map_message = self.code_context.code_map.get_message(
                token_limit=code_map_message_token_limit
            )
            if code_map_message:
                match (code_map_message.level):
                    case "signatures":
                        cprint_message_level = "full syntax tree"
                    case "no_signatures":
                        cprint_message_level = "partial syntax tree"
                    case "filenames":
                        cprint_message_level = "filepaths only"

                cprint_message = f"\nIncluding CodeMap ({cprint_message_level})"
                cprint(cprint_message, color="green")
                code_message += f"\n{code_map_message}"
            else:
                cprint_message = [
                    "\nExcluding CodeMap from system message.",
                    "Reason: not enough tokens available in model context.",
                ]
                cprint_message = "\n".join(cprint_message)
                cprint(cprint_message, color="yellow")

        return "\n".join(code_message)

    def _add_file(self, abs_path: Path):
        logging.info(f"Adding new file {abs_path} to context")
        self.code_context.files[abs_path] = CodeFile(abs_path)
        # create any missing directories in the path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

    def _delete_file(self, abs_path: Path):
        logging.info(f"Deleting file {abs_path}")
        if abs_path in self.code_context.files:
            del self.code_context.files[abs_path]
        abs_path.unlink()

    def _handle_delete(self, delete_change: CodeChange):
        abs_path = self.config.git_root / delete_change.file
        if not abs_path.exists():
            logging.error(f"Path {abs_path} non-existent on delete")
            return

        cprint(f"Are you sure you want to delete {delete_change.file}?", "red")
        if self.user_input_manager.ask_yes_no(default_yes=False):
            cprint(f"Deleting {delete_change.file}...")
            self._delete_file(abs_path)
        else:
            cprint(f"Not deleting {delete_change.file}")

    def _get_new_code_lines(
        self, rel_path: Path, changes: list[CodeChange]
    ) -> list[str] | None:
        if not changes:
            return []
        if len(set(map(lambda change: change.file, changes))) > 1:
            raise Exception("All changes passed in must be for the same file")

        changes = sorted(changes, reverse=True)

        # We resolve insertion conflicts twice because non-insertion conflicts
        # might move insert blocks outside of replace/delete blocks and cause
        # them to conflict again
        changes = resolve_insertion_conflicts(changes, self.user_input_manager, self)
        changes = resolve_non_insertion_conflicts(changes, self.user_input_manager)
        changes = resolve_insertion_conflicts(changes, self.user_input_manager, self)
        if not changes:
            return []

        new_code_lines = self.file_lines[rel_path].copy()
        if new_code_lines != self._read_file(rel_path):
            logging.info(f"File '{rel_path}' changed while generating changes")
            cprint(
                f"File '{rel_path}' changed while generating; current file changes"
                " will be erased. Continue?",
                color="light_yellow",
            )
            if not self.user_input_manager.ask_yes_no(default_yes=False):
                cprint(f"Not applying changes to file {rel_path}.")
                return None

        # Necessary in case the model needs to insert past the end of the file
        last_line = len(new_code_lines) + 1
        largest_changed_line = math.ceil(changes[0].last_changed_line)
        if largest_changed_line > last_line:
            new_code_lines += [""] * (largest_changed_line - last_line)

        min_changed_line = largest_changed_line + 1
        for change in changes:
            if change.last_changed_line >= min_changed_line:
                raise MentatError(f"Change line number overlap in file {change.file}")
            min_changed_line = change.first_changed_line
            new_code_lines = change.apply(new_code_lines)
        return new_code_lines

    def write_changes_to_files(self, code_changes: list[CodeChange]) -> None:
        file_changes = defaultdict[Path, list[CodeChange]](list)
        for code_change in code_changes:
            rel_path = code_change.file
            abs_path = self.config.git_root / rel_path
            match code_change.action:
                case CodeChangeAction.CreateFile:
                    cprint(f"Creating new file {rel_path}", color="light_green")
                    self._add_file(abs_path)
                    with open(abs_path, "w") as f:
                        f.write("\n".join(code_change.code_lines))
                case CodeChangeAction.DeleteFile:
                    self._handle_delete(code_change)
                case CodeChangeAction.RenameFile:
                    abs_new_path = self.config.git_root / code_change.name
                    self._add_file(abs_new_path)
                    code_lines = self.file_lines[rel_path]
                    with open(abs_new_path, "w") as f:
                        f.write("\n".join(code_lines))
                    self._delete_file(abs_path)
                    file_changes[code_change.name] += file_changes[rel_path]
                    file_changes[rel_path] = []
                    self.file_lines[code_change.name] = self._read_file(
                        code_change.name
                    )
                case _:
                    file_changes[rel_path].append(code_change)

        for rel_path, changes in file_changes.items():
            abs_path = self.config.git_root / rel_path
            new_code_lines = self._get_new_code_lines(rel_path, changes)
            if new_code_lines:
                if abs_path not in self.code_context.files:
                    raise MentatError(
                        f"Attempted to edit file {abs_path} not in context"
                    )
                with open(abs_path, "w") as f:
                    f.write("\n".join(new_code_lines))

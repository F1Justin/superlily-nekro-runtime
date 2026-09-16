"""Persistent conversation files with disposable task state (single Runtime process)."""

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from nekro_agent.tools.sandbox_paths import shared_host_dir, validate_storage_key

TASK_PATTERN = re.compile(r"[0-9a-f]{32}")


@dataclass
class Workspace:
    task_id: str
    chat_key: str
    epoch: int
    touched_at: float
    state: str
    remaining_calls: int = 64

    @property
    def container_key(self) -> str:
        return f"r2_{self.task_id}"


class WorkspaceStore:
    def __init__(self, root: Path, ttl: int = 1800) -> None:
        self.root = root.resolve()
        self.state_root = self.root / ".r2-state"
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ttl = ttl
        self.active: set[str] = set()
        self.retired: set[str] = set()
        self.recover()

    def control_dir(self, task_id: str) -> Path:
        if not TASK_PATTERN.fullmatch(task_id):
            raise ValueError("Invalid supervisor task ID")
        return self.state_root / task_id

    def shared_dir(self, task_id: str) -> Path:
        self.control_dir(task_id)
        return shared_host_dir(self.root, f"r2_{task_id}")

    def _read(self, task_id: str) -> Workspace:
        return Workspace(**json.loads((self.control_dir(task_id) / "manifest.json").read_text()))

    def _write(self, workspace: Workspace) -> None:
        directory = self.control_dir(workspace.task_id)
        directory.mkdir(exist_ok=True, mode=0o700)
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(asdict(workspace)))
        temporary.replace(directory / "manifest.json")

    def recover(self) -> None:
        for directory in self.state_root.iterdir():
            if directory.is_symlink() or not TASK_PATTERN.fullmatch(directory.name):
                continue
            if not (directory / "manifest.json").is_file():
                continue
            workspace = self._read(directory.name)
            if workspace.task_id != directory.name:
                raise ValueError("Workspace manifest identity mismatch")
            if workspace.state == "running":
                workspace.state = "interrupted"
                self._write(workspace)

    def acquire(self, task_id: str, chat_key: str) -> Workspace:
        validate_storage_key(chat_key)
        if task_id in self.retired:
            raise ValueError("Workspace expired; start a new task")
        if task_id in self.active:
            raise ValueError("Task workspace is already executing")
        manifest = self.control_dir(task_id) / "manifest.json"
        now = time.time()
        if manifest.exists():
            workspace = self._read(task_id)
            if workspace.chat_key != chat_key or workspace.task_id != task_id:
                raise PermissionError("Workspace belongs to another task or conversation")
            if now - workspace.touched_at >= self.ttl or workspace.state == "interrupted":
                raise ValueError("Workspace is expired or interrupted; explicit recovery is required")
            workspace.epoch += 1
            workspace.state = "running"
            workspace.touched_at = now
        else:
            workspace = Workspace(task_id, chat_key, 1, now, "running")
        self._write(workspace)
        shared = self.shared_dir(task_id)
        if shared.is_symlink():
            raise ValueError("Workspace root must not be a symlink")
        shared.mkdir(exist_ok=True)
        shared.chmod(0o777)
        for name in ("work", "out"):
            target = shared / name
            if not target.exists() and not target.is_symlink():
                target.mkdir()
                target.chmod(0o777)
        for name in ("packages", "pip-cache", "work"):
            target = self.control_dir(task_id) / name
            if target.is_symlink():
                raise ValueError("Task directory must not be a symlink")
            target.mkdir(exist_ok=True)
            target.chmod(0o777)
        self.active.add(task_id)
        return workspace

    def release(self, workspace: Workspace) -> None:
        self.active.discard(workspace.task_id)
        workspace.state = "idle"
        workspace.touched_at = time.time()
        self._write(workspace)

    def cleanup(self, now: float | None = None) -> list[str]:
        removed = []
        current = time.time() if now is None else now
        for directory in self.state_root.iterdir():
            task_id = directory.name
            if directory.is_symlink() or not TASK_PATTERN.fullmatch(task_id) or task_id in self.active:
                continue
            if not (directory / "manifest.json").is_file():
                continue
            workspace = self._read(task_id)
            if workspace.task_id != task_id or current - workspace.touched_at < self.ttl:
                continue
            # Conversation files survive task expiry; only supervisor-owned state expires.
            shutil.rmtree(directory)
            self.retired.add(task_id)
            removed.append(task_id)
        return removed

    def check_usage(self, task_id: str, max_bytes: int, max_entries: int = 10000) -> None:
        roots = [self.shared_dir(task_id), *(self.control_dir(task_id) / name for name in ("work", "packages", "pip-cache"))]
        size = 0
        count = 0

        def fail_on_error(error: OSError) -> None:
            raise error

        for root in roots:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for path, directories, files, directory_fd in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False, onerror=fail_on_error):
                    if path.count(os.sep) > 64:
                        raise ValueError("Task workspace nesting budget exceeded")
                    count += len(directories) + len(files)
                    for name in [*directories, *files]:
                        try:
                            size += os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_size
                        except FileNotFoundError:
                            continue
                    if size > max_bytes or count > max_entries:
                        raise ValueError("Task workspace byte/entry budget exceeded")
            finally:
                os.close(root_fd)

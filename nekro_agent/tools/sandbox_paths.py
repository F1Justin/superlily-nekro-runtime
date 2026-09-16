"""Resolve supervisor-owned task identities to persistent conversation storage."""

import json
import re
from pathlib import Path


def validate_storage_key(key: str) -> None:
    if not key or Path(key).name != key or key in (".", "..") or "\\" in key or "\x00" in key:
        raise ValueError("Invalid sandbox storage identity")


def task_state_dir(root: Path, container_key: str, chat_key: str | None = None) -> Path:
    if not re.fullmatch(r"r2_[0-9a-f]{32}", container_key):
        raise ValueError("Invalid task identity")
    state = root / ".r2-state"
    directory = state / container_key[3:]
    manifest = directory / "manifest.json"
    if state.is_symlink() or directory.is_symlink() or manifest.is_symlink():
        raise ValueError("Task state must not be a symlink")
    record = json.loads(manifest.read_text())
    if record["task_id"] != container_key[3:] or (chat_key is not None and record["chat_key"] != chat_key):
        raise PermissionError("Task does not belong to this conversation")
    validate_storage_key(record["chat_key"])
    return directory


def shared_host_dir(root: Path, container_key: str, chat_key: str | None = None) -> Path:
    validate_storage_key(container_key)
    if container_key.startswith("r2_"):
        state = task_state_dir(root, container_key, chat_key)
        record = json.loads((state / "manifest.json").read_text())
        container_key = f"sandbox_{record['chat_key']}"
    directory = root / container_key
    if directory.is_symlink():
        raise ValueError("Conversation root must not be a symlink")
    return directory

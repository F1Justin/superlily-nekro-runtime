import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

import aiofiles
import httpx
from pydantic import BaseModel, ConfigDict

from nekro_agent.core.os_env import NAPCAT_TEMPFILE_DIR, USER_UPLOAD_DIR
from nekro_agent.schemas.chat_message import ChatMessageSegmentFile


class OneBotFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str = ""
    url: str = ""
    file_name: str = ""
    file_size: int | None = None


def resolve_napcat_file(raw_path: str) -> Path:
    """Translate an actual API path; a display filename is never a lookup key."""
    raw_path = raw_path.removeprefix("file://").removeprefix("file:")
    path = Path(raw_path)
    qq_root = Path(NAPCAT_TEMPFILE_DIR).parents[1].resolve()
    container_root = Path("/app/.config/QQ")
    if path.is_relative_to(container_root):
        path = qq_root / path.relative_to(container_root)
    if not path.is_absolute() or not path.resolve().is_relative_to(qq_root):
        raise ValueError("File path is outside the NapCat QQ directory")
    if not path.is_file():
        raise ValueError("API file path does not exist")
    return path


def _check_size(size: int, expected_size: int | None, max_bytes: int) -> None:
    if size > max_bytes:
        raise ValueError("Attachment exceeds the upload limit")
    if expected_size is not None and size != expected_size:
        raise ValueError(f"Attachment size mismatch: expected {expected_size}, received {size}")


async def ingest_onebot_file(
    file: OneBotFile,
    chat_key: str,
    *,
    display_name: str,
    expected_size: int | None,
    max_bytes: int,
) -> ChatMessageSegmentFile:
    if expected_size is not None and file.file_size is not None and expected_size != file.file_size:
        raise ValueError("API file size does not match the upload event")
    expected_size = expected_size if expected_size is not None else file.file_size
    if expected_size is not None and (expected_size < 0 or expected_size > max_bytes):
        raise ValueError("Invalid attachment size")
    url = file.url if file.url.startswith(("https://", "http://")) else ""
    if url:
        chunks = bytearray()
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ValueError("Attachment exceeds the upload limit")
        content = bytes(chunks)
    else:
        path = resolve_napcat_file(file.file or file.url)
        _check_size(path.stat().st_size, expected_size, max_bytes)
        async with aiofiles.open(path, "rb") as stream:
            content = await stream.read(max_bytes + 1)
    _check_size(len(content), expected_size, max_bytes)

    root = Path(USER_UPLOAD_DIR).resolve()
    directory = root / chat_key
    if directory.is_symlink() or directory.parent != root or not chat_key:
        raise ValueError("Invalid attachment conversation directory")
    directory.mkdir(parents=True, exist_ok=True)
    name = PurePosixPath(display_name or file.file_name or "attachment").name
    suffix = PurePosixPath(name).suffix
    if len(suffix) > 16 or not suffix.removeprefix(".").isalnum():
        suffix = ""
    stored_name = f"attachment_{hashlib.sha256(content).hexdigest()}{suffix}"
    destination = directory / stored_name
    # Publish complete bytes atomically without replacing an existing historical attachment.
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".attachment-", delete=False) as staging:
        staging_path = Path(staging.name)
    try:
        async with aiofiles.open(staging_path, "wb") as stream:
            await stream.write(content)
        staging_path.chmod(0o444)
        try:
            os.link(staging_path, destination)
        except FileExistsError:
            if destination.is_symlink():
                raise ValueError("Attachment must not be a symlink") from None
            async with aiofiles.open(destination, "rb") as stream:
                if await stream.read(max_bytes + 1) != content:
                    raise ValueError("Existing attachment does not match its content hash") from None
    finally:
        staging_path.unlink(missing_ok=True)
    return ChatMessageSegmentFile(
        type=ChatMessageSegmentFile.get_segment_type(),
        text=f"[File: {name}]",
        file_name=stored_name,
        local_path=str(destination),
    )

"""Open untrusted paths using directory descriptors before making a stable copy."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path


def snapshot_file(root: Path, relative: Path, destination: Path, limit: int = 64 * 1024 * 1024) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid sandbox file path")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = -1
    temporary: Path | None = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
            raise ValueError("Only bounded, non-hardlinked regular files may be exported")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256()
        total = 0
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as output:
            temporary = Path(output.name)
            while chunk := os.read(file_fd, 65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError("Export exceeds file budget")
                digest.update(chunk)
                output.write(chunk)
        current = os.fstat(file_fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (current.st_size, current.st_mtime_ns, current.st_ctime_ns):
            raise ValueError("File changed during export; retry after writing has finished")
        target = destination / f"{digest.hexdigest()}{relative.suffix[:16]}"
        if not target.exists():
            used = sum(path.stat().st_size for path in destination.iterdir() if path != temporary)
            if used + total > limit:
                raise ValueError("Task export budget exceeded")
            temporary.replace(target)
        return target
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)

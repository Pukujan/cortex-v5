"""Trusted entry point for an exact, human-authorized workspace Python checker."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        return 2
    root = Path(sys.argv[1]).resolve(strict=True)
    script = Path(sys.argv[2]).resolve(strict=True)
    script.relative_to(root)
    try:
        policy = json.loads(sys.argv[3])
        denied_paths = tuple(Path(item).resolve(strict=False) for item in policy.get("denied", []))
        protected_paths = tuple(
            Path(item).resolve(strict=False) for item in policy.get("protected", [])
        )
        for private in (*denied_paths, *protected_paths):
            private.relative_to(root)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return 2
    checker_code = compile(script.read_bytes(), str(script), "exec")
    # Allow the checker to import libraries installed for the interpreter (its
    # site-packages) while still restricting writes to the workspace root.
    # Reads of installed packages are trusted; the audit hook below continues
    # to block writes outside the workspace, network, env mutation, and
    # subprocess/exec.
    _readable: set[Path] = {Path(sys.base_prefix).resolve(), root}
    for _entry in sys.path:
        try:
            _resolved = Path(_entry).resolve()
        except (OSError, RuntimeError):
            continue
        if _resolved.is_dir():
            _readable.add(_resolved)
    readable_roots = tuple(sorted(_readable))

    def sensitive(candidate: Path) -> bool:
        private_paths = (*denied_paths, *protected_paths)
        if any(candidate == private or private in candidate.parents for private in private_paths):
            return True
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return False
        secret_names = {
            ".aws",
            ".azure",
            ".codex",
            ".git",
            ".git-credentials",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".ssh",
            "credentials.json",
            "secrets.json",
            "id_ed25519",
            "id_rsa",
        }
        return any(
            part.casefold() == ".env"
            or part.casefold().startswith(".env.")
            or part.casefold() in secret_names
            or part.casefold().endswith((".pem", ".key"))
            for part in relative.parts
        )

    def inside(path: object, *, write: bool = False) -> bool:
        if isinstance(path, int):
            return True
        candidate = Path(os.fsdecode(path)).resolve(strict=False)
        if sensitive(candidate):
            return False
        roots = (root,) if write else readable_roots
        return any(candidate == allowed or allowed in candidate.parents for allowed in roots)

    def require_inside(path: object, event: str, *, write: bool = True) -> None:
        if not inside(path, write=write):
            raise PermissionError(f"sandbox blocked {event} outside workspace")

    def uses_directory_fd(args: tuple[object, ...], start: int) -> bool:
        return any(value not in (None, -1) for value in args[start:])

    def require_sqlite_database(database: object) -> None:
        try:
            raw = os.fsdecode(database)
        except TypeError as exc:
            raise PermissionError("sandbox blocked invalid sqlite3 database path") from exc
        if raw == ":memory:":
            return
        lowered = raw.casefold()
        if not raw or "\x00" in raw or lowered.startswith("file:"):
            raise PermissionError("sandbox blocked sqlite3 URI or special database")
        # Reject alternate data streams and URI-like query/fragment tricks.  A
        # Windows drive prefix is the sole permitted colon in an ordinary path.
        path = Path(raw)
        remainder = raw[len(path.drive) :]
        if ":" in remainder or "?" in raw or "#" in raw:
            raise PermissionError("sandbox blocked ambiguous sqlite3 database path")
        require_inside(path, "sqlite3.connect")

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "open":
            mode = str(args[1]) if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            writing = any(flag in mode for flag in "wax+") or bool(flags & write_flags)
            require_inside(args[0], event, write=writing)
        elif event in {"os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.chmod"}:
            if uses_directory_fd(args, 2 if event == "os.mkdir" else 1):
                raise PermissionError(f"sandbox blocked directory-fd mutation: {event}")
            require_inside(args[0], event)
        elif event in {"os.truncate", "os.utime", "os.chown"}:
            require_inside(args[0], event)
        elif event in {"os.rename", "os.replace", "os.link"}:
            if uses_directory_fd(args, 2):
                raise PermissionError(f"sandbox blocked directory-fd mutation: {event}")
            require_inside(args[0], event)
            require_inside(args[1], event)
        elif event == "os.symlink":
            raise PermissionError("sandbox blocked symbolic-link creation")
        elif event in {"os.chdir", "os.fchdir"}:
            if event == "os.fchdir":
                raise PermissionError("sandbox blocked descriptor-based chdir")
            require_inside(args[0], event)
        elif event in {"os.listdir", "os.scandir"}:
            require_inside(args[0], event, write=False)
        elif event == "sqlite3.connect":
            if not args:
                raise PermissionError("sandbox blocked missing sqlite3 database path")
            require_sqlite_database(args[0])
        elif event in {"sqlite3.enable_load_extension", "sqlite3.load_extension"}:
            raise PermissionError(f"sandbox blocked {event}")
        elif event == "import" and len(args) > 1 and args[1] is not None:
            require_inside(args[1], event, write=False)
        elif event == "zipimport.zipimporter":
            require_inside(args[0], event, write=False)
        elif event == "mmap.__new__":
            raise PermissionError("sandbox blocked native memory mapping")
        if event.startswith(("socket.", "subprocess.", "ctypes.", "winreg.")) or event in {
            "os.system",
            "os.spawn",
            "os.exec",
            "os.startfile",
            "os.kill",
            "os.killpg",
            "os.putenv",
            "os.unsetenv",
            "os.add_dll_directory",
        }:
            raise PermissionError(f"sandbox blocked {event}")

    os.environ.clear()
    sys.addaudithook(audit)
    # Isolated mode intentionally drops the current directory. Restore only the
    # explicitly authorized workspace so checkers can import submitted modules.
    sys.path.insert(0, str(root))
    sys.argv = [str(script), *sys.argv[4:]]
    namespace = {
        "__name__": "__main__",
        "__file__": str(script),
        "__package__": None,
        "__cached__": None,
        "__builtins__": __builtins__,
    }
    exec(checker_code, namespace, namespace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

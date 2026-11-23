# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""OS-enforced Python isolation: runtime files and this task's workspace.

macOS uses Seatbelt; Linux uses bubblewrap with private namespaces and no
network. Hosts without an enforceable sandbox refuse Python execution. Model
approval never relaxes this boundary. The child receives no provider credentials,
deployment bearer, cloud credentials, or unrestricted host filesystem access.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    workspace: Path
    tmp: Path
    deny_read: tuple[Path, ...] = ()
    #: Regular expressions over absolute paths whose reads are refused (environment files hold keys).
    deny_read_patterns: tuple[str, ...] = (r"/\.env$", r"/\.3lc-stack[^/]*\.env$")
    #: Network access is always disabled.
    network: str = "none"

    @classmethod
    def for_workspace(cls, workspace: Path, state_dir: Path | None = None) -> Policy:
        return cls(workspace=workspace, tmp=workspace / ".tmp", deny_read=default_deny_read(state_dir))


def default_deny_read(state_dir: Path | None = None) -> tuple[Path, ...]:
    """Where secrets live on a person's machine, plus the assistant's own store (other conversations, settings)."""
    home = Path.home()
    paths = [
        home / ".ssh",
        home / ".aws",
        home / ".azure",
        home / ".config" / "gcloud",
        home / ".kube",
        home / ".docker" / "config.json",
        home / ".netrc",
        home / ".gnupg",
        home / "Library" / "Keychains",
    ]
    if state_dir is not None:
        paths += [state_dir / "conversations", state_dir / "settings", state_dir / "spend"]
    return tuple(paths)


def mechanism() -> str:
    """What this host can enforce: ``seatbelt``, ``bubblewrap`` or ``none``."""
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if system == "Linux" and shutil.which("bwrap"):
        return "bubblewrap"
    return "none"


def _quoted(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def runtime_paths() -> tuple[Path, ...]:
    """Only interpreter/runtime files; never the user's home or the host root."""
    editable_roots = []
    for package in ("tlc", "tlc_plugin_sdk"):
        spec = importlib.util.find_spec(package)
        if spec is not None and spec.origin:
            package_root = Path(spec.origin).resolve().parent.parent
            editable_roots.append(str(package_root))
            metadata = package_root.parent / "pyproject.toml"
            if metadata.is_file():
                editable_roots.append(str(metadata))
    return tuple(
        dict.fromkeys(
            Path(p).resolve()
            for p in (
                "/System",
                "/usr",
                "/bin",
                "/lib",
                "/lib64",
                sys.prefix,
                sys.base_prefix,
                *editable_roots,
                "/etc/ld.so.cache",
                "/etc/fonts",
                "/etc/localtime",
            )
            if Path(p).exists()
        )
    )


def seatbelt_profile(policy: Policy) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork sysctl-read file-read-metadata)",
        '(allow file-read* (literal "/"))',
        '(allow mach-lookup (global-name "com.apple.system.logger") (global-name "com.apple.logd"))',
        "(allow signal (target self))",
    ]
    for path in (*runtime_paths(), policy.workspace, policy.tmp):
        lines.append(f'(allow file-read* (subpath "{_quoted(path.resolve())}"))')
    lines += [
        f'(allow file-write* (subpath "{_quoted(policy.workspace.resolve())}"))',
        '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") '
        '(literal "/dev/fd/0") (literal "/dev/fd/1") (literal "/dev/fd/2") '
        '(literal "/dev/random") (literal "/dev/urandom"))',
    ]
    lines += [f'(deny file-read* (subpath "{_quoted(path.resolve())}"))' for path in policy.deny_read]
    lines += [f'(deny file-read* (regex "{_quoted(pattern)}"))' for pattern in policy.deny_read_patterns]
    return "\n".join(lines) + "\n"


def bubblewrap_argv(policy: Policy, argv: list[str]) -> list[str]:
    cmd = [
        "bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--cap-drop",
        "ALL",
        "--die-with-parent",
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    for path in runtime_paths():
        cmd += ["--ro-bind", str(path), str(path)]
    for name in ("/bin", "/sbin", "/lib", "/lib64"):
        if Path(name).is_symlink():
            cmd += ["--symlink", os.readlink(name), name]
    cmd += ["--bind", str(policy.workspace.resolve()), str(policy.workspace.resolve())]
    cmd += ["--chdir", str(policy.workspace.resolve()), "--"]
    return cmd + argv


def wrap(argv: list[str], policy: Policy) -> tuple[list[str], str]:
    """The command to run ``argv`` inside the boundary, and the mechanism's name (``none`` when there is none)."""
    kind = mechanism()
    if kind == "seatbelt":
        return ["/usr/bin/sandbox-exec", "-p", seatbelt_profile(policy), *argv], kind
    if kind == "bubblewrap":
        return bubblewrap_argv(policy, argv), kind
    return list(argv), "none"


def environment() -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(MPLBACKEND="Agg", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def read_private(root: Path, relative: str, maximum: int) -> bytes:
    """Read regular files without following symlinks, including intermediate directories."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in (".", "..") for p in parts):
        msg = "Only files in the working folder can be read"
        raise ValueError(msg)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(root.resolve(), flags | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            child = os.open(part, flags | (os.O_DIRECTORY if index < len(parts) - 1 else 0), dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_nlink != 1:
            msg = "File is not a regular file within the size limit"
            raise ValueError(msg)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            msg = "File exceeds the size limit"
            raise ValueError(msg)
        return content
    finally:
        os.close(fd)

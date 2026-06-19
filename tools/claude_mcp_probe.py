#!/usr/bin/env python
"""Minimal MCP stdio probe for ``claude mcp serve``.

This script starts ``claude mcp serve`` as a subprocess, performs the MCP
handshake, then sends ``tools/list``, ``resources/list``, and ``prompts/list``
requests.  It prints results to stdout and exits cleanly (with a hard timeout)
without modifying any files.

Usage::

    python tools/claude_mcp_probe.py --timeout 15

Safe: no file modifications, no model inference, no token printing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, IO


# ── Synchronous stdio MCP client (Windows-compatible) ───────────────


def _read_json_line_nonblocking(fd: IO[bytes], bufsize: int = 4096) -> str | None:
    """Read the next line from *fd*, returning it as a str.

    On Windows subprocess stdout (connected via pipes), lines arrive atomically
    enough that a modest buffer works.  Any non-decodable or non-JSON line is
    silently skipped — this lets us absorb startup banners, ANSI escapes, or
    debug output that ``claude mcp serve`` may emit before the MCP handshake.
    """
    try:
        line_bytes = fd.readline()
        if not line_bytes:
            return None
        return line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    except Exception:
        return None


def _send_raw(writer: IO[bytes], msg_id: int, method: str, params: dict[str, Any] | None = None) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    writer.write(data)
    writer.flush()


def _collect_until_id(
    fd: IO[bytes], target_id: int, deadline: float, skip_initial: bool = True
) -> dict[str, Any]:
    """Read lines from *fd* until a JSON-RPC response with *target_id* is found.

    Lines that cannot be parsed as JSON are silently skipped (startup banners,
    ANSI escapes, etc.).  Returns the parsed response dict.

    Raises TimeoutError if no matching response arrives before *deadline*.
    """
    while time.monotonic() < deadline:
        line = _read_json_line_nonblocking(fd)
        if line is None:
            time.sleep(0.05)
            continue
        try:
            msg = json.loads(line)
            if isinstance(msg, dict) and msg.get("id") == target_id:
                return msg
        except json.JSONDecodeError:
            # Skip banner / ANSI / debug noise
            pass
    raise TimeoutError(f"Timed out waiting for response id={target_id}")


def _run_probe(claude_path: str, timeout: int) -> dict[str, Any]:
    """Start claude mcp serve, run MCP handshake + list probes, return results dict."""
    env = {**os.environ, "NO_COLOR": "1"}

    proc = subprocess.Popen(
        [claude_path, "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        bufsize=1,
    )
    stdin_write: IO[bytes] = proc.stdin
    stdout_read: IO[bytes] = proc.stdout

    results: dict[str, Any] = {
        "error": None,
        "serverInfo": {},
        "tools": [],
        "resources": [],
        "prompts": [],
    }
    deadline = time.monotonic() + timeout

    try:
        # ── MCP handshake ─────────────────────────────────────────────
        _send_raw(stdin_write, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "claude-mcp-probe", "version": "1.0"},
        })

        init_resp = _collect_until_id(stdout_read, target_id=1, deadline=deadline)
        if "error" in init_resp:
            results["error"] = f"initialize error: {init_resp['error']}"
            return results
        results["serverInfo"] = init_resp.get("result", {})

        # Send notifications/initialized
        _send_raw(stdin_write, 0, "notifications/initialized")
        time.sleep(0.3)  # brief stabilisation pause

        # ── tools/list ───────────────────────────────────────────────
        _send_raw(stdin_write, 2, "tools/list")
        t_resp = _collect_until_id(stdout_read, target_id=2, deadline=deadline)
        for t in t_resp.get("result", {}).get("tools", []):
            results["tools"].append({
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            })

        # ── resources/list ───────────────────────────────────────────
        _send_raw(stdin_write, 3, "resources/list")
        r_resp = _collect_until_id(stdout_read, target_id=3, deadline=deadline)
        for r in r_resp.get("result", {}).get("resources", []):
            results["resources"].append({
                "uri": r.get("uri", ""),
                "name": r.get("name", ""),
                "description": r.get("description", ""),
            })

        # ── prompts/list ─────────────────────────────────────────────
        _send_raw(stdin_write, 4, "prompts/list")
        p_resp = _collect_until_id(stdout_read, target_id=4, deadline=deadline)
        for p in p_resp.get("result", {}).get("prompts", []):
            results["prompts"].append({
                "name": p.get("name", ""),
                "description": p.get("description", ""),
                "arguments": p.get("arguments", []),
            })

    except TimeoutError as exc:
        results["error"] = f"Timeout: {exc}"
    except Exception as exc:
        results["error"] = f"Probe error: {exc}"

    finally:
        # Clean shutdown
        try:
            stdin_write.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    return results


def _print_results(results: dict[str, Any]) -> None:
    print("=" * 60)
    print("claude mcp serve -- probe results")
    print("=" * 60)

    if results.get("error"):
        print(f"ERROR: {results['error']}")
        return

    si = results.get("serverInfo", {})
    si_inner = si.get("serverInfo", {})
    print(f"Server name   : {si_inner.get('name', '?')}")
    print(f"Server version: {si_inner.get('version', '?')}")
    print(f"MCP version   : {si.get('protocolVersion', '?')}")

    print(f"\n--- Tools ({len(results['tools'])}) ---")
    for t in results["tools"]:
        schema = t.get("inputSchema", {})
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        print(f"  {t['name']}: {t['description'][:120]}")
        if props:
            print(f"    inputs: {', '.join(props.keys())}")

    print(f"\n--- Resources ({len(results['resources'])}) ---")
    for r in results["resources"]:
        desc = r.get("description", "")[:100]
        print(f"  {r['uri']}: {desc}")

    print(f"\n--- Prompts ({len(results['prompts'])}) ---")
    for p in results["prompts"]:
        args = [a["name"] for a in p.get("arguments", [])]
        desc = p.get("description", "")[:100]
        print(f"  {p['name']}({', '.join(args) if args else 'none'}): {desc}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Probe claude mcp serve capabilities.")
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="Max seconds for the entire probe (default: 30)",
    )
    parser.add_argument(
        "--claude", default="",
        help="Full path to claude binary. "
             "On Windows where claude is a .ps1 shim, use the underlying .exe here. "
             "Default: auto-detect common locations.",
    )
    args = parser.parse_args(argv)

    # Auto-detect claude binary
    if args.claude:
        claude_path = args.claude
    else:
        # Try common Windows locations
        candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "claude", "claude.exe"),
            os.path.join(os.environ.get("APPDATA", ""),
                         "npm", "node_modules", "@anthropic-ai",
                         "claude-code", "bin", "claude.exe"),
        ]
        claude_path = None
        for c in candidates:
            if os.path.isfile(c):
                claude_path = c
                break
        if not claude_path:
            # Fall back to PATH lookup
            import shutil
            claude_path = shutil.which("claude") or "claude"

    print(f"[probe] binary: {claude_path}", file=sys.stderr)
    print(f"[probe] timeout: {args.timeout}s", file=sys.stderr)

    try:
        results = _run_probe(claude_path, timeout=args.timeout)
    except Exception as exc:
        print(f"[probe] fatal: {exc}", file=sys.stderr)
        sys.exit(1)

    _print_results(results)

    if results.get("error"):
        print(f"\n[probe] FAILED | {results['error']}", file=sys.stderr)
    else:
        tool_names = [t["name"] for t in results.get("tools", [])]
        print(f"\n[probe] OK | tools={tool_names}", file=sys.stderr)


if __name__ == "__main__":
    main()

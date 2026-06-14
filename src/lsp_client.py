"""
lsp_client.py

Precise cross-file reference finding via the Language Server Protocol (LSP).

How LSP works:
  1. We launch a language server (pyright for Python) as a subprocess
  2. We communicate via JSON-RPC over stdin/stdout
  3. We ask: "what are all references to the symbol at file:line:col?"
  4. It returns exact locations — not text matches, but resolved symbol references

This is more precise than both regex and tree-sitter because:
  - It follows import graphs (finds re-exported symbols)
  - It resolves overloads and subclass methods
  - It understands type aliases and generics
  - It never returns false positives from comments or strings

Architecture:
  - We use pyright for Python (best static analysis, runs as an LSP server)
  - We use a lightweight JSON-RPC client (no external library needed)
  - One server process per repo; we reuse it across multiple symbol lookups

Protocol overview:
  Client (us) → Server (pyright)
  1. initialize(rootUri, capabilities)  ← handshake
  2. initialized()                      ← ack
  3. textDocument/didOpen(file)         ← "here's a file, load it"
  4. textDocument/references(pos)       ← "find all references at this position"
  ← [{uri, range}]                     ← server responds with locations

LSP reference: https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional


# ── JSON-RPC transport ────────────────────────────────────────────────────────

class LspTransport:
    """
    Handles raw JSON-RPC message framing over stdin/stdout with a subprocess.

    LSP message format:
        Content-Length: <byte_count>\r\n
        \r\n
        <json_body>
    """

    def __init__(self, process: subprocess.Popen):
        self._proc    = process
        self._msg_id  = 0
        self._pending = {}   # id → threading.Event
        self._results = {}   # id → response
        self._lock    = threading.Lock()
        self._reader  = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def send_request(self, method: str, params: dict, timeout: float = 10.0):
        """Send a JSON-RPC request and wait for the response."""
        msg_id  = self._next_id()
        event   = threading.Event()
        self._pending[msg_id] = event

        self._send_raw({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})

        if not event.wait(timeout=timeout):
            raise TimeoutError(f"LSP request timed out: {method}")

        return self._results.pop(msg_id, None)

    def send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_raw(self, msg: dict):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _read_loop(self):
        """Background thread that reads responses from the server."""
        while True:
            try:
                # Read headers
                headers = {}
                while True:
                    line = self._proc.stdout.readline().decode("utf-8").strip()
                    if not line:
                        break
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip()] = v.strip()

                length = int(headers.get("Content-Length", 0))
                if length == 0:
                    continue

                body = self._proc.stdout.read(length).decode("utf-8")
                msg  = json.loads(body)

                # Match response to pending request
                if "id" in msg and msg["id"] in self._pending:
                    self._results[msg["id"]] = msg.get("result")
                    self._pending.pop(msg["id"]).set()

            except Exception:
                break

    def close(self):
        try:
            self._proc.terminate()
        except Exception:
            pass


# ── LSP client ────────────────────────────────────────────────────────────────

class LspClient:
    """
    High-level LSP client for finding symbol references.

    Usage:
        with LspClient.for_python(repo_path) as lsp:
            refs = lsp.find_references("src/auth/service.py", "authenticate")
            for ref in refs:
                print(ref["file"], ref["line"])
    """

    def __init__(self, transport: LspTransport, repo_path: str):
        self._transport  = transport
        self._repo_path  = os.path.abspath(repo_path)
        self._opened     = set()   # files we've told the server about

    @classmethod
    def for_python(cls, repo_path: str) -> "LspClient":
        """Start a pyright LSP server for a Python repository."""
        pyright_path = _find_pyright_langserver()
        proc = subprocess.Popen(
            [pyright_path, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=repo_path,
        )

        transport = LspTransport(proc)
        client    = cls(transport, repo_path)
        client._initialize()
        return client

    def _initialize(self):
        """LSP handshake: initialize → initialized."""
        root_uri = Path(self._repo_path).as_uri()

        self._transport.send_request("initialize", {
            "processId":    os.getpid(),
            "rootUri":      root_uri,
            "capabilities": {
                "textDocument": {
                    "references": {"dynamicRegistration": False},
                }
            },
        }, timeout=20.0)  # pyright sends log notifications before the response
        self._transport.send_notification("initialized", {})
        time.sleep(1.0)  # give server a moment to index

    def _open_file(self, rel_path: str):
        """Tell the server about a file (required before querying it)."""
        if rel_path in self._opened:
            return

        abs_path = os.path.join(self._repo_path, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return

        uri = Path(abs_path).as_uri()
        ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else "python"
        lang_id = {"py": "python", "ts": "typescript", "js": "javascript"}.get(ext, ext)

        self._transport.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri":        uri,
                "languageId": lang_id,
                "version":    1,
                "text":       text,
            }
        })
        self._opened.add(rel_path)
        time.sleep(0.1)  # allow server to process

    def find_definition_line(self, rel_path: str, func_name: str) -> Optional[int]:
        """
        Find the line number where `func_name` is defined in `rel_path`.
        Returns 0-indexed line number, or None if not found.
        """
        abs_path = os.path.join(self._repo_path, rel_path)
        try:
            with open(abs_path, "r") as f:
                lines = f.readlines()
        except OSError:
            return None

        # Find the line with the function definition (simple scan)
        import re
        pattern = re.compile(r'(?:def|function|func)\s+' + re.escape(func_name) + r'\s*[\(\<]')
        for i, line in enumerate(lines):
            if pattern.search(line):
                return i
        return None

    def find_references(self, rel_path: str, func_name: str) -> list:
        """
        Find all references to `func_name` defined in `rel_path`.

        Returns list of:
            {"file": str, "line": int, "col": int, "snippet": str}
        """
        self._open_file(rel_path)

        line_no = self.find_definition_line(rel_path, func_name)
        if line_no is None:
            return []

        abs_path = os.path.join(self._repo_path, rel_path)
        uri      = Path(abs_path).as_uri()

        # Find the column of the function name on that line
        try:
            with open(abs_path) as f:
                lines = f.readlines()
            target_line = lines[line_no]
            col = target_line.index(func_name)
        except (ValueError, IndexError):
            col = 0

        result = self._transport.send_request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position":     {"line": line_no, "character": col},
            "context":      {"includeDeclaration": False},
        }, timeout=15.0)

        if not result:
            return []

        refs = []
        for ref in result:
            ref_uri  = ref["uri"]
            ref_line = ref["range"]["start"]["line"]
            ref_col  = ref["range"]["start"]["character"]

            # Convert URI back to a relative path
            ref_abs  = ref_uri.replace("file://", "")
            try:
                ref_rel = os.path.relpath(ref_abs, self._repo_path)
            except ValueError:
                ref_rel = ref_abs

            # Get the actual line text for the snippet
            snippet = ""
            try:
                with open(ref_abs) as f:
                    file_lines = f.readlines()
                start = max(0, ref_line - 1)
                end   = min(len(file_lines), ref_line + 3)
                snippet = "".join(file_lines[start:end])
            except OSError:
                pass

            refs.append({
                "file":    ref_rel,
                "line":    ref_line + 1,  # 1-indexed for display
                "col":     ref_col,
                "snippet": snippet,
            })

        return refs

    def close(self):
        try:
            self._transport.send_notification("shutdown", {})
            self._transport.send_notification("exit", {})
        except Exception:
            pass
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Integration with retrieve_context ────────────────────────────────────────

def retrieve_lsp_context(diff_files: list, repo_path: str) -> list:
    """
    Use pyright LSP to find precise references to changed functions.
    Returns ContextItems with priority 2 (same tier as call_sites, but more accurate).

    Falls back gracefully if pyright isn't available.
    """
    from src.context_retriever import ContextItem

    try:
        pyright = _find_pyright_langserver()
    except RuntimeError as e:
        print(f"  LSP unavailable: {e}")
        return []

    items = []

    try:
        with LspClient.for_python(repo_path) as lsp:
            for diff_file in diff_files:
                if diff_file.extension != "py":
                    continue  # pyright is Python-only; add ts-server for TS

                for func_name in diff_file.changed_functions:
                    refs = lsp.find_references(diff_file.file_path, func_name)
                    for ref in refs[:3]:  # cap at 3 per function
                        if not ref["snippet"].strip():
                            continue
                        items.append(ContextItem(
                            source=ref["file"],
                            content=f"# {ref['file']}:{ref['line']}\n{ref['snippet']}",
                            reason=(
                                f"LSP-resolved reference to `{func_name}` "
                                f"(line {ref['line']}) — precise symbol match, "
                                f"not a text search"
                            ),
                            priority=2,
                            category="lsp_reference",
                        ))
    except Exception as e:
        print(f"  LSP error (falling back to AST search): {e}")

    return items


def _find_pyright_langserver() -> str:
    """
    Find the pyright-langserver executable.

    Note: 'pyright' is the CLI type-checker.
          'pyright-langserver' is the LSP server — different binary, same package.
    """
    import shutil, sys

    # Try the LSP server binary first
    path = shutil.which("pyright-langserver")
    if path:
        return path

    # pip install puts it alongside the Python binary
    scripts_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(scripts_dir, "pyright-langserver")
    if os.path.exists(candidate):
        return candidate

    raise RuntimeError(
        "pyright-langserver not found. Install with:\n"
        "  pip install pyright\n"
        "Then verify with: pyright-langserver --version"
    )

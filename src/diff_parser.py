"""
diff_parser.py

Parses a raw git diff string into structured Python objects.

A git diff looks like:
    diff --git a/src/auth.py b/src/auth.py
    --- a/src/auth.py
    +++ b/src/auth.py
    @@ -10,7 +10,8 @@ def authenticate(user, password):
    -    if user.password == password:
    +    if verify_password(user.password_hash, password):
         return True

We extract: which files changed, which functions were touched, and what lines
were added/removed. This becomes the starting point for context retrieval.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Hunk:
    """One contiguous block of changes within a file."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context_function: str       # Function name from @@ header (may be empty)
    added_lines: list           # Lines that were added (+)
    removed_lines: list         # Lines that were removed (-)
    context_lines: list         # Surrounding unchanged lines ( )


@dataclass
class DiffFile:
    """All changes within a single file in the PR."""
    old_path: str
    new_path: str
    is_new_file: bool
    is_deleted_file: bool
    hunks: list = field(default_factory=list)

    @property
    def file_path(self) -> str:
        """The current path of the file (use new_path unless deleted)."""
        return self.new_path if not self.is_deleted_file else self.old_path

    @property
    def extension(self) -> str:
        """File extension, e.g. 'py', 'ts', 'js'."""
        if '.' in self.file_path:
            return self.file_path.rsplit('.', 1)[-1].lower()
        return ''

    @property
    def changed_functions(self) -> list:
        """
        Unique function/method names touched across all hunks.
        We check both the @@ header context AND the actual changed lines,
        because the @@ header sometimes shows the surrounding class, not
        the function that was actually modified.
        """
        seen = set()
        result = []

        func_def_pattern = re.compile(
            r'(?:def|async def|function|async function|fn|func)\s+(\w+)\s*[\(\<]'
        )

        for hunk in self.hunks:
            # From @@ header
            if hunk.context_function and hunk.context_function not in seen:
                seen.add(hunk.context_function)
                result.append(hunk.context_function)

            # From the changed lines themselves
            for line in hunk.added_lines + hunk.removed_lines:
                m = func_def_pattern.search(line)
                if m:
                    name = m.group(1)
                    if name not in seen:
                        seen.add(name)
                        result.append(name)

        return result

    @property
    def all_added_lines(self) -> list:
        lines = []
        for hunk in self.hunks:
            lines.extend(hunk.added_lines)
        return lines

    @property
    def all_removed_lines(self) -> list:
        lines = []
        for hunk in self.hunks:
            lines.extend(hunk.removed_lines)
        return lines


def parse_diff(diff_text: str) -> list:
    """
    Parse a unified git diff string into a list of DiffFile objects.

    Args:
        diff_text: Raw output of `git diff` or a .diff file

    Returns:
        List of DiffFile objects, one per changed file
    """
    files = []
    current_file: Optional[DiffFile] = None
    current_hunk: Optional[Hunk] = None

    for line in diff_text.splitlines():

        # ── File header ───────────────────────────────────────────────────────
        if line.startswith('diff --git '):
            # Save previous file/hunk before starting a new one
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)
                    current_hunk = None
                files.append(current_file)

            current_file = DiffFile(
                old_path='', new_path='',
                is_new_file=False, is_deleted_file=False
            )

        elif line.startswith('new file mode'):
            if current_file:
                current_file.is_new_file = True

        elif line.startswith('deleted file mode'):
            if current_file:
                current_file.is_deleted_file = True

        elif line.startswith('--- '):
            if current_file:
                path = line[4:]
                # Strip the "a/" prefix git adds
                current_file.old_path = path[2:] if path.startswith('a/') else path

        elif line.startswith('+++ '):
            if current_file:
                path = line[4:]
                # Strip the "b/" prefix git adds
                current_file.new_path = path[2:] if path.startswith('b/') else path

        # ── Hunk header: @@ -old_start,count +new_start,count @@ func_name ──
        elif line.startswith('@@ '):
            if current_file is not None:
                if current_hunk is not None:
                    current_file.hunks.append(current_hunk)

                match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', line)
                if match:
                    raw_context = match.group(5).strip()
                    func_name = _extract_function_name(raw_context)
                    current_hunk = Hunk(
                        old_start=int(match.group(1)),
                        old_count=int(match.group(2) or 1),
                        new_start=int(match.group(3)),
                        new_count=int(match.group(4) or 1),
                        context_function=func_name,
                        added_lines=[],
                        removed_lines=[],
                        context_lines=[],
                    )

        # ── Diff content lines ────────────────────────────────────────────────
        elif current_hunk is not None:
            if line.startswith('+') and not line.startswith('+++'):
                current_hunk.added_lines.append(line[1:])
            elif line.startswith('-') and not line.startswith('---'):
                current_hunk.removed_lines.append(line[1:])
            elif line.startswith(' '):
                current_hunk.context_lines.append(line[1:])

    # Don't forget the last file/hunk
    if current_file is not None:
        if current_hunk is not None:
            current_file.hunks.append(current_hunk)
        files.append(current_file)

    # Filter out any incomplete entries
    return [f for f in files if f.old_path or f.new_path]


def _extract_function_name(context_str: str) -> str:
    """
    Extract a clean function/method name from the @@ hunk context string.

    The context string after @@ often looks like:
        "def authenticate(user, password):"
        "function processPayment(amount) {"
        "class UserService:"
    """
    if not context_str:
        return ''

    # Try to match common patterns across Python, JS/TS, Go, Java, etc.
    patterns = [
        r'(?:def|async def)\s+(\w+)',       # Python
        r'(?:function|async function)\s+(\w+)',  # JS/TS
        r'(?:class|interface|type)\s+(\w+)',     # Classes
        r'(?:fn|func)\s+(\w+)',                  # Rust/Go
        r'(?:public|private|protected|static).*\s+(\w+)\s*\(',  # Java/C#
    ]
    for pattern in patterns:
        m = re.search(pattern, context_str)
        if m:
            return m.group(1)

    # Fall back to the first word
    words = context_str.split()
    return words[0] if words else ''


def summarize_diff(diff_files: list) -> str:
    """Human-readable summary of what changed in the diff."""
    lines = [f"PR touches {len(diff_files)} file(s):"]
    for f in diff_files:
        added = sum(len(h.added_lines) for h in f.hunks)
        removed = sum(len(h.removed_lines) for h in f.hunks)
        funcs = ', '.join(f.changed_functions) if f.changed_functions else 'unknown scope'
        tag = ' [NEW]' if f.is_new_file else ' [DELETED]' if f.is_deleted_file else ''
        lines.append(f"  {f.file_path}{tag}  +{added}/-{removed} lines  [{funcs}]")
    return '\n'.join(lines)

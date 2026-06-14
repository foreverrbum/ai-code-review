"""
context_retriever.py

Given a parsed diff, this module searches the repository for related code
that would help an LLM give a better code review.

What we look for (in priority order):
  1. Full function body containing the changed lines
  2. Call sites — where the changed function is called elsewhere
  3. Test files — tests that cover the changed file
  4. Type/class definitions — types used in the changed code
  5. Import dependencies — what the changed file imports
"""

import os
import re
from dataclasses import dataclass, field


# ── Context item ─────────────────────────────────────────────────────────────

@dataclass
class ContextItem:
    """A single piece of retrieved context."""
    source: str          # File path (relative to repo root)
    content: str         # The actual code snippet
    reason: str          # Why this was included (for the retrieval plan)
    priority: int        # 1 = highest, 5 = lowest
    category: str        # 'function_body' | 'call_site' | 'test' | 'type_def' | 'import'

    def __repr__(self):
        preview = self.content[:60].replace('\n', ' ')
        return f"[P{self.priority}] {self.category} @ {self.source}: {preview}..."


# ── Language patterns ─────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {'py', 'ts', 'tsx', 'js', 'jsx', 'go', 'java', 'rb', 'rs', 'cs'}

FUNCTION_PATTERNS = {
    'py':  r'^((?:async\s+)?def\s+{name}\s*\()',
    'ts':  r'((?:async\s+)?(?:function\s+{name}|{name}\s*[=:]\s*(?:async\s*)?\(|{name}\s*\())',
    'js':  r'((?:async\s+)?(?:function\s+{name}|{name}\s*[=:]\s*(?:async\s*)?\(|{name}\s*\())',
    'go':  r'(func\s+(?:\(\w+\s+\*?\w+\)\s+)?{name}\s*\()',
    'java': r'((?:public|private|protected|static|\s)+\w+\s+{name}\s*\()',
    'rb':  r'(def\s+{name})',
    'rs':  r'((?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[<\(])',
}

TEST_FILE_PATTERNS = [
    r'test[_\-]',
    r'[_\-]test\.',
    r'[_\-]spec\.',
    r'spec[_\-]',
    r'\.test\.',
    r'\.spec\.',
]

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next'}


# ── Main retrieval function ───────────────────────────────────────────────────

def retrieve_context(diff_files: list, repo_path: str, token_hint: int = 8000) -> list:
    """
    Main entry point. For each changed file in the diff, retrieve relevant
    context from the repository.

    Args:
        diff_files:  List of DiffFile objects from diff_parser.parse_diff()
        repo_path:   Absolute path to the repository root
        token_hint:  Rough token budget (used to limit search depth)

    Returns:
        List of ContextItem objects, unsorted (ranker will sort them)
    """
    items = []

    for diff_file in diff_files:
        if diff_file.extension not in SUPPORTED_EXTENSIONS:
            continue

        abs_path = os.path.join(repo_path, diff_file.file_path)

        # 1. Full function body for each changed function
        for func_name in diff_file.changed_functions:
            snippet = extract_function_body(abs_path, func_name, diff_file.extension)
            if snippet:
                items.append(ContextItem(
                    source=diff_file.file_path,
                    content=snippet,
                    reason=f"Full body of `{func_name}` — the function directly modified in this PR",
                    priority=1,
                    category='function_body',
                ))

        # 2. Call sites across the repo
        for func_name in diff_file.changed_functions:
            sites = find_call_sites(repo_path, func_name, exclude_file=diff_file.file_path)
            for site_path, snippet in sites[:3]:  # cap at 3 callers per function
                items.append(ContextItem(
                    source=site_path,
                    content=snippet,
                    reason=f"Call site of `{func_name}` — changes here may break callers",
                    priority=2,
                    category='call_site',
                ))

        # 3. Related test files
        test_files = find_test_files(repo_path, diff_file.file_path)
        for test_path in test_files[:2]:
            content = read_file_safe(os.path.join(repo_path, test_path))
            if content:
                items.append(ContextItem(
                    source=test_path,
                    content=content,
                    reason=f"Test file covering {diff_file.file_path} — shows expected behavior",
                    priority=3,
                    category='test',
                ))

        # 4. Type / class definitions referenced in changed lines
        type_names = extract_type_references(diff_file)
        for type_name in type_names:
            type_def = find_type_definition(repo_path, type_name)
            if type_def:
                def_path, snippet = type_def
                items.append(ContextItem(
                    source=def_path,
                    content=snippet,
                    reason=f"Definition of `{type_name}` used in changed code",
                    priority=4,
                    category='type_def',
                ))

        # 5. Import chain (what the file imports)
        imports = extract_imports(abs_path, diff_file.extension)
        if imports:
            items.append(ContextItem(
                source=diff_file.file_path,
                content='\n'.join(imports),
                reason="Import declarations — shows what this module depends on",
                priority=5,
                category='import',
            ))

    # Deduplicate by (source, content) to avoid repeats
    return _deduplicate(items)


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_function_body(file_path: str, func_name: str, ext: str) -> str:
    """
    Read a source file and extract the full body of the named function.
    Uses indentation to detect where the function ends (works for Python).
    For brace-based languages, we count braces.
    """
    content = read_file_safe(file_path)
    if not content:
        return ''

    pattern_template = FUNCTION_PATTERNS.get(ext, FUNCTION_PATTERNS.get('py', ''))
    if not pattern_template:
        return ''

    pattern = pattern_template.replace('{name}', re.escape(func_name))
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if re.search(pattern, line):
            return _extract_block(lines, i, ext)

    return ''


def _extract_block(lines: list, start: int, ext: str) -> str:
    """Extract a code block starting at line `start`."""
    if ext == 'py':
        return _extract_python_block(lines, start)
    else:
        return _extract_brace_block(lines, start)


def _extract_python_block(lines: list, start: int) -> str:
    """Extract a Python function by indentation level."""
    result = [lines[start]]
    base_indent = len(lines[start]) - len(lines[start].lstrip())

    for line in lines[start + 1:]:
        stripped = line.lstrip()
        if not stripped:  # blank line — keep going
            result.append(line)
            continue
        indent = len(line) - len(stripped)
        if indent <= base_indent and stripped:
            break
        result.append(line)

    # Trim trailing blank lines
    while result and not result[-1].strip():
        result.pop()

    return '\n'.join(result)


def _extract_brace_block(lines: list, start: int) -> str:
    """Extract a brace-delimited block ({...}) for JS/TS/Go/Java etc."""
    result = []
    depth = 0
    started = False

    for line in lines[start:]:
        result.append(line)
        depth += line.count('{') - line.count('}')
        if '{' in line:
            started = True
        if started and depth <= 0:
            break

        if len(result) > 100:  # safety cap
            break

    return '\n'.join(result)


def find_call_sites(repo_path: str, func_name: str, exclude_file: str = '') -> list:
    """
    Search the repo for lines that call `func_name(`.
    Returns list of (relative_path, snippet) tuples.
    """
    results = []
    pattern = re.compile(r'\b' + re.escape(func_name) + r'\s*\(')

    for rel_path, abs_path in _walk_source_files(repo_path):
        if exclude_file and rel_path == exclude_file:
            continue

        content = read_file_safe(abs_path)
        if not content:
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines):
            if pattern.search(line):
                # Grab a few lines of surrounding context
                start = max(0, i - 2)
                end = min(len(lines), i + 5)
                snippet = '\n'.join(lines[start:end])
                results.append((rel_path, f"# {rel_path}:{i+1}\n{snippet}"))
                break  # one hit per file is enough

    return results


def find_test_files(repo_path: str, source_file: str) -> list:
    """
    Find test files that are likely to cover `source_file`.
    Matches by: base filename OR parent directory name.
    e.g. payments/processor.py → test_payments.py OR test_processor.py
    """
    base = os.path.splitext(os.path.basename(source_file))[0].lower()
    parent = os.path.basename(os.path.dirname(source_file)).lower()
    keywords = {base, parent} - {'', '.', 'src', 'lib', 'app'}
    results = []

    for rel_path, _ in _walk_source_files(repo_path):
        filename = os.path.basename(rel_path).lower()
        is_test = any(re.search(p, filename) for p in TEST_FILE_PATTERNS)
        if is_test and any(kw in filename for kw in keywords):
            results.append(rel_path)

    return results


def extract_type_references(diff_file) -> list:
    """
    Scan the changed lines for CamelCase identifiers — likely class/type names.
    We look in both added and removed lines.
    """
    all_changed = diff_file.all_added_lines + diff_file.all_removed_lines
    combined = '\n'.join(all_changed)

    # CamelCase words that aren't common keywords
    candidates = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', combined)
    skip = {'True', 'False', 'None', 'TypeError', 'ValueError', 'Exception'}
    return list({c for c in candidates if c not in skip})[:5]  # cap at 5


def find_type_definition(repo_path: str, type_name: str):
    """Search the repo for the definition of a class or type."""
    patterns = [
        re.compile(r'^\s*class\s+' + re.escape(type_name) + r'\b'),
        re.compile(r'^\s*(?:type|interface)\s+' + re.escape(type_name) + r'\b'),
        re.compile(r'^' + re.escape(type_name) + r'\s*=\s*(?:TypedDict|dataclass|NamedTuple)'),
    ]

    for rel_path, abs_path in _walk_source_files(repo_path):
        content = read_file_safe(abs_path)
        if not content:
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if any(p.search(line) for p in patterns):
                ext = rel_path.rsplit('.', 1)[-1] if '.' in rel_path else 'py'
                snippet = _extract_block(lines, i, ext)
                return (rel_path, f"# {rel_path}:{i+1}\n{snippet}")

    return None


def extract_imports(file_path: str, ext: str) -> list:
    """Extract import statements from a source file."""
    content = read_file_safe(file_path)
    if not content:
        return []

    if ext == 'py':
        pattern = re.compile(r'^(?:import|from)\s+.+', re.MULTILINE)
    else:
        pattern = re.compile(r'^(?:import|require|from)\s+.+', re.MULTILINE)

    return pattern.findall(content)[:20]  # cap at 20 imports


# ── Utilities ────────────────────────────────────────────────────────────────

def read_file_safe(path: str) -> str:
    """Read a file, returning empty string on any error."""
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except (OSError, IOError):
        return ''


def _walk_source_files(repo_path: str):
    """Yield (relative_path, absolute_path) for all source files in the repo."""
    for root, dirs, files in os.walk(repo_path):
        # Skip directories we don't care about (in-place to prune os.walk)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext in SUPPORTED_EXTENSIONS:
                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, repo_path)
                yield rel_path, abs_path


def _deduplicate(items: list) -> list:
    """Remove duplicate context items (same source + same first 100 chars of content)."""
    seen = set()
    result = []
    for item in items:
        key = (item.source, item.content[:100])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

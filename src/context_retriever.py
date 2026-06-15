"""
context_retriever.py

Given a parsed diff, this module searches the repository for related code
that would help an LLM give a better code review.

What we look for (in priority order):
  1. Full function body containing the changed lines
  2. Call sites — where the changed function is called elsewhere
  3. Test files — tests that cover the changed file
  3. Git co-change — files that historically change together (coupling signal)
  4. Type/class definitions — types used in the changed code
  5. Import dependencies — what the changed file imports

Function extraction and call-site search use tree-sitter AST parsing
(src/ast_parser.py) for supported languages, with a regex fallback for others:
  - AST path: won't match inside comments or strings; uses exact node
    boundaries for start/end lines (Python, TS, JS, Go)
  - Regex fallback: covers Java, Ruby, Rust, C# (no grammar required)

Scalability:
  - Call-site and type searches use `grep -rl` as a fast pre-filter before
    opening any files. On a 10K-file repo this cuts file reads from ~10,000
    down to the handful that actually contain the name.
  - grep falls back to a full Python file-walk if not available (e.g. Windows).
"""

import os
import re
import subprocess
from dataclasses import dataclass

from src.ast_parser import (
    extract_function_ast,
    find_call_sites_ast,
    extract_imports_ast,
    parse_file,
)


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


# ── Language config ───────────────────────────────────────────────────────────

# Languages supported by tree-sitter (precise AST)
AST_SUPPORTED = {'py', 'js', 'jsx', 'ts', 'tsx', 'go'}

# All languages we'll walk (regex fallback for non-AST langs)
SUPPORTED_EXTENSIONS = {'py', 'ts', 'tsx', 'js', 'jsx', 'go', 'java', 'rb', 'rs', 'cs'}

# Regex fallback patterns for languages not in AST_SUPPORTED
FUNCTION_PATTERNS_REGEX = {
    'java': r'((?:public|private|protected|static|\s)+\w+\s+{name}\s*\()',
    'rb':   r'(def\s+{name})',
    'rs':   r'((?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[<\(])',
    'cs':   r'(\w+\s+{name}\s*\()',
}

TEST_FILE_PATTERNS = [
    r'test[_\-]', r'[_\-]test\.', r'[_\-]spec\.', r'spec[_\-]',
    r'\.test\.', r'\.spec\.',
]

SKIP_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv',
    'dist', 'build', '.next', '.mypy_cache',
}


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
        ext = diff_file.extension
        use_ast = ext in AST_SUPPORTED

        # 1. Full function body for each changed function
        for func_name in diff_file.changed_functions:
            if use_ast:
                snippet = _extract_function_body_ast(abs_path, func_name, ext)
            else:
                snippet = _extract_function_body_regex(abs_path, func_name, ext)

            if snippet:
                items.append(ContextItem(
                    source=diff_file.file_path,
                    content=snippet,
                    reason=(
                        f"Full body of `{func_name}` — the function directly modified in this PR "
                        f"({'AST-extracted' if use_ast else 'regex-extracted'})"
                    ),
                    priority=1,
                    category='function_body',
                ))

        # 2. Call sites across the repo
        for func_name in diff_file.changed_functions:
            sites = find_call_sites(repo_path, func_name,
                                    exclude_file=diff_file.file_path,
                                    prefer_ast=use_ast)
            for site_path, snippet in sites[:3]:
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

        # 3b. Git co-change: files that historically change with this one
        cochanged = find_cochanged_files(repo_path, diff_file.file_path)
        for co_path, co_count in cochanged:
            content = read_file_safe(os.path.join(repo_path, co_path))
            if content:
                items.append(ContextItem(
                    source=co_path,
                    content=content[:3000],  # cap large files
                    reason=(
                        f"Co-changed with {diff_file.file_path} in {co_count} prior commits "
                        f"— historically coupled, changes here may need to stay in sync"
                    ),
                    priority=3,
                    category='git_cochange',
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
        if use_ast:
            imports = extract_imports_ast(read_file_safe(abs_path), ext)
        else:
            imports = _extract_imports_regex(abs_path, ext)

        if imports:
            items.append(ContextItem(
                source=diff_file.file_path,
                content='\n'.join(imports),
                reason="Import declarations — shows what this module depends on",
                priority=5,
                category='import',
            ))

    return _deduplicate(items)


# ── AST-based extraction ──────────────────────────────────────────────────────

def _extract_function_body_ast(file_path: str, func_name: str, ext: str) -> str:
    """Use tree-sitter AST for precise function extraction."""
    source = read_file_safe(file_path)
    if not source:
        return ''
    return extract_function_ast(source, func_name, ext)


# ── Regex fallback extraction ─────────────────────────────────────────────────

def _extract_function_body_regex(file_path: str, func_name: str, ext: str) -> str:
    """Regex-based fallback for languages not supported by tree-sitter."""
    content = read_file_safe(file_path)
    if not content:
        return ''

    pattern_template = FUNCTION_PATTERNS_REGEX.get(ext, '')
    if not pattern_template:
        return ''

    pattern = pattern_template.replace('{name}', re.escape(func_name))
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if re.search(pattern, line):
            return _extract_brace_block(lines, i)

    return ''


def _extract_brace_block(lines: list, start: int) -> str:
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
        if len(result) > 100:
            break
    return '\n'.join(result)


def _extract_imports_regex(file_path: str, ext: str) -> list:
    content = read_file_safe(file_path)
    if not content:
        return []
    pattern = re.compile(r'^(?:import|from|require)\s+.+', re.MULTILINE)
    return pattern.findall(content)[:20]


# ── Call site search ──────────────────────────────────────────────────────────

def find_call_sites(repo_path: str, func_name: str,
                    exclude_file: str = '', prefer_ast: bool = True) -> list:
    """
    Search the repo for places that call `func_name(...)`.

    Strategy:
      1. grep -rl to find candidate files in O(n) with OS-level I/O
         (5-10x faster than opening every file in Python)
      2. For each candidate: AST call-expression detection (if AST_SUPPORTED)
         or regex fallback — neither matches inside strings/comments
      3. Falls back to full Python walk if grep is unavailable

    Returns list of (relative_path, formatted_snippet) tuples.
    """
    results = []
    regex_pattern = re.compile(r'\b' + re.escape(func_name) + r'\s*\(')

    # Fast pre-filter: ask grep which files even contain the name before
    # opening any of them. On a 10K-file monorepo this drops file reads
    # from ~10,000 to typically < 10.
    candidate_abs_paths = _grep_files_containing(repo_path, func_name)
    if candidate_abs_paths is not None:
        file_iter = _abs_to_rel(repo_path, candidate_abs_paths)
    else:
        file_iter = _walk_source_files(repo_path)  # grep unavailable, walk all

    for rel_path, abs_path in file_iter:
        if exclude_file and rel_path == exclude_file:
            continue

        ext = rel_path.rsplit('.', 1)[-1].lower() if '.' in rel_path else ''
        content = read_file_safe(abs_path)
        if not content:
            continue

        lines = content.splitlines()
        hit_line = None

        if prefer_ast and ext in AST_SUPPORTED:
            # AST-based: precise call expression detection
            call_sites = find_call_sites_ast(content, func_name, ext)
            if call_sites:
                hit_line = call_sites[0][0] - 1  # convert to 0-indexed
        else:
            # Regex fallback
            for i, line in enumerate(lines):
                if regex_pattern.search(line):
                    hit_line = i
                    break

        if hit_line is not None:
            start = max(0, hit_line - 2)
            end = min(len(lines), hit_line + 6)
            snippet = '\n'.join(lines[start:end])
            results.append((rel_path, f"# {rel_path}:{hit_line+1}\n{snippet}"))

    return results


# ── Test file discovery ───────────────────────────────────────────────────────

def find_test_files(repo_path: str, source_file: str) -> list:
    """
    Find test files likely to cover `source_file`.
    Matches by: base filename OR parent directory name.
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


# ── Type definition search ────────────────────────────────────────────────────

def extract_type_references(diff_file) -> list:
    """Scan changed lines for CamelCase identifiers — likely class/type names."""
    combined = '\n'.join(diff_file.all_added_lines + diff_file.all_removed_lines)
    candidates = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', combined)
    skip = {'True', 'False', 'None', 'TypeError', 'ValueError', 'Exception'}
    return list({c for c in candidates if c not in skip})[:5]


def find_type_definition(repo_path: str, type_name: str):
    """Search the repo for the definition of a class or type."""
    patterns = [
        re.compile(r'^\s*class\s+'     + re.escape(type_name) + r'\b'),
        re.compile(r'^\s*(?:type|interface)\s+' + re.escape(type_name) + r'\b'),
        re.compile(r'^' + re.escape(type_name) + r'\s*=\s*(?:TypedDict|dataclass|NamedTuple)'),
    ]

    # grep pre-filter: only read files that mention the type name
    candidate_abs_paths = _grep_files_containing(repo_path, type_name)
    if candidate_abs_paths is not None:
        file_iter = _abs_to_rel(repo_path, candidate_abs_paths)
    else:
        file_iter = _walk_source_files(repo_path)

    for rel_path, abs_path in file_iter:
        content = read_file_safe(abs_path)
        if not content:
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if any(p.search(line) for p in patterns):
                ext = rel_path.rsplit('.', 1)[-1].lower() if '.' in rel_path else 'py'
                # Use AST extraction if supported, else extract the class block
                if ext in AST_SUPPORTED:
                    snippet = extract_function_ast(content, type_name, ext)
                    if not snippet:
                        snippet = '\n'.join(lines[i:min(len(lines), i+20)])
                else:
                    snippet = '\n'.join(lines[i:min(len(lines), i+20)])
                return (rel_path, f"# {rel_path}:{i+1}\n{snippet}")

    return None


def find_cochanged_files(repo_path: str, source_file: str, top_n: int = 3) -> list:
    """
    Find files that frequently co-changed with source_file in git history.

    This captures coupling that no static analysis tool can see: files with
    completely different names that nevertheless always change together because
    they implement two sides of the same abstraction, share a config schema,
    or maintain a protocol contract.

    Args:
        repo_path:   Absolute path to the repository root
        source_file: Relative path of the file being reviewed
        top_n:       Max number of co-changed files to return

    Returns:
        List of (rel_path, co_change_count) tuples, sorted by frequency.
        Empty list if git is unavailable or the file has no history.
    """
    try:
        import git
        repo = git.Repo(repo_path, search_parent_directories=True)
        counts: dict = {}

        # Walk the last 100 commits that touched this file
        for commit in repo.iter_commits(paths=[source_file], max_count=100):
            if not commit.parents:
                continue  # initial commit, no diff
            for diff_item in commit.parents[0].diff(commit):
                path = diff_item.b_path or diff_item.a_path
                if path and path != source_file:
                    counts[path] = counts.get(path, 0) + 1

        sorted_pairs = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        results = []
        for path, count in sorted_pairs:
            if count < 2:
                break  # only include files that co-changed at least twice
            ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            abs_path = os.path.join(repo_path, path)
            if os.path.exists(abs_path):
                results.append((path, count))
            if len(results) >= top_n:
                break

        return results

    except Exception:
        # git not available, repo has no history, or file not tracked — silent no-op
        return []


# ── Utilities ─────────────────────────────────────────────────────────────────

def read_file_safe(path: str) -> str:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except (OSError, IOError):
        return ''


def _grep_files_containing(repo_path: str, text: str) -> list:
    """
    Return absolute paths of source files in repo_path that contain text.

    Uses system grep with --include filters and --exclude-dir skips.
    This is 5-10x faster than opening every file in Python because grep
    uses OS memory-mapped I/O and can scan multiple files in parallel.

    Returns None if grep is unavailable (Windows, restricted env) so callers
    can fall back to the Python file walk.
    """
    include_args  = [f'--include=*.{ext}' for ext in SUPPORTED_EXTENSIONS]
    exclude_args  = [f'--exclude-dir={d}'  for d in SKIP_DIRS]
    try:
        result = subprocess.run(
            ['grep', '-rl', '--binary-files=without-match', text, repo_path]
            + include_args + exclude_args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # returncode 0 = found, 1 = not found, anything else = error
        if result.returncode not in (0, 1):
            return None
        lines = result.stdout.strip().splitlines()
        return [l for l in lines if l]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None  # grep not available; caller will walk files


def _abs_to_rel(repo_path: str, abs_paths: list):
    """
    Convert a list of absolute paths (from grep output) back to
    (rel_path, abs_path) tuples, applying the same extension/SKIP_DIRS
    filters as _walk_source_files so the rest of the pipeline is uniform.
    """
    for abs_path in abs_paths:
        ext = abs_path.rsplit('.', 1)[-1].lower() if '.' in abs_path else ''
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        rel_path = os.path.relpath(abs_path, repo_path)
        # Skip paths that pass through a directory we want to ignore
        parts = rel_path.replace('\\', '/').split('/')
        if any(p in SKIP_DIRS for p in parts):
            continue
        yield rel_path, abs_path


def _walk_source_files(repo_path: str):
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in files:
            ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            if ext in SUPPORTED_EXTENSIONS:
                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, repo_path)
                yield rel_path, abs_path


def _deduplicate(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        key = (item.source, item.content[:100])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

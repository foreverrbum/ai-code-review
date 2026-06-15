"""
ast_parser.py

Replaces regex-based code extraction with proper AST parsing via tree-sitter.

Why this matters over regex:
  - Regex can match inside comments, strings, or decorators
  - AST knows exactly what each token IS: a function def, a call, an import
  - Extracts precise line ranges, not heuristic indentation counts
  - Same API works across Python, TypeScript, JavaScript, Go

Supported languages and their tree-sitter node types:

  Language   | Function node type          | Name field
  -----------|-----------------------------|------------
  Python     | function_definition         | name
  JavaScript | function_declaration        | name
             | method_definition           | name
  TypeScript | function_declaration        | name
             | method_definition           | name
  Go         | function_declaration        | name
             | method_declaration          | name
"""

import os
from typing import Optional
from tree_sitter import Language, Parser, Node

# ── Load grammars once at import time ────────────────────────────────────────
# Each grammar is a compiled C library exposed via a Python binding.
# We load them lazily so missing packages don't break the whole system.

def _load_language(module_name: str, func_name: str, lang_name: str) -> Optional[Language]:
    try:
        mod = __import__(module_name)
        fn = getattr(mod, func_name)   # e.g. mod.language() or mod.language_typescript()
        return Language(fn(), lang_name)
    except (ImportError, AttributeError, Exception):
        return None

_LANGUAGES = {}

def _get_language(ext: str) -> Optional[Language]:
    if ext not in _LANGUAGES:
        # (module_name, exported_function_name, tree-sitter language name)
        # tree_sitter_typescript is the odd one out: it exports language_typescript()
        # and language_tsx() rather than the generic language() that all other grammars use.
        mapping = {
            'py':  ('tree_sitter_python',     'language',             'python'),
            'js':  ('tree_sitter_javascript', 'language',             'javascript'),
            'jsx': ('tree_sitter_javascript', 'language',             'javascript'),
            'ts':  ('tree_sitter_typescript', 'language_typescript',  'typescript'),
            'tsx': ('tree_sitter_typescript', 'language_tsx',         'tsx'),
            'go':  ('tree_sitter_go',         'language',             'go'),
        }
        if ext in mapping:
            module_name, func_name, lang_name = mapping[ext]
            _LANGUAGES[ext] = _load_language(module_name, func_name, lang_name)
        else:
            _LANGUAGES[ext] = None
    return _LANGUAGES[ext]


# ── Node type config per language ─────────────────────────────────────────────
# Each tuple: (node_types_that_define_a_function, field_name_for_the_identifier)
FUNCTION_NODE_TYPES = {
    'py':  (['function_definition', 'decorated_definition'], 'name'),
    'js':  (['function_declaration', 'method_definition', 'arrow_function'], 'name'),
    'jsx': (['function_declaration', 'method_definition', 'arrow_function'], 'name'),
    'ts':  (['function_declaration', 'method_definition', 'function_signature', 'arrow_function'], 'name'),
    'tsx': (['function_declaration', 'method_definition', 'function_signature', 'arrow_function'], 'name'),
    'go':  (['function_declaration', 'method_declaration'], 'name'),
}

CALL_NODE_TYPES = {
    'py':  'call',
    'js':  'call_expression',
    'jsx': 'call_expression',
    'ts':  'call_expression',
    'tsx': 'call_expression',
    'go':  'call_expression',
}


# ── Core AST operations ───────────────────────────────────────────────────────

def parse_file(source: str, ext: str) -> Optional[object]:
    """
    Parse source code into a tree-sitter AST.
    Returns None if the language isn't supported.
    """
    lang = _get_language(ext)
    if not lang:
        return None
    parser = Parser()
    parser.set_language(lang)
    return parser.parse(bytes(source, 'utf-8'))


def extract_function_ast(source: str, func_name: str, ext: str) -> str:
    """
    Extract the full source text of a named function using AST node boundaries.

    This is more precise than our previous regex+indentation approach:
    - Knows exactly where the function starts and ends (including decorators)
    - Won't match function names inside strings or comments
    - Handles nested functions correctly

    Returns the function source text, or '' if not found.
    """
    tree = parse_file(source, ext)
    if not tree:
        return ''

    node_types, name_field = FUNCTION_NODE_TYPES.get(ext, ([], 'name'))
    lines = source.splitlines()

    def find_function(node: Node) -> Optional[Node]:
        # Handle Python decorated functions: the outer node is 'decorated_definition'
        if node.type == 'decorated_definition':
            inner = node.child_by_field_name('definition')
            if inner and inner.type == 'function_definition':
                name_node = inner.child_by_field_name('name')
                if name_node and name_node.text.decode('utf-8') == func_name:
                    return node  # return the outer decorated node

        if node.type in node_types:
            name_node = node.child_by_field_name(name_field)
            if name_node and name_node.text.decode('utf-8') == func_name:
                return node

        for child in node.children:
            result = find_function(child)
            if result:
                return result
        return None

    func_node = find_function(tree.root_node)
    if not func_node:
        return ''

    start_line = func_node.start_point[0]
    end_line   = func_node.end_point[0] + 1
    return '\n'.join(lines[start_line:end_line])


def find_call_sites_ast(source: str, func_name: str, ext: str) -> list:
    """
    Find all lines in `source` that call `func_name(...)`.
    Returns list of (line_number, line_text) tuples.

    Unlike grep, this checks actual call expression nodes — won't match
    the function definition itself, or occurrences in comments/strings.
    """
    tree = parse_file(source, ext)
    if not tree:
        return []

    call_type = CALL_NODE_TYPES.get(ext, 'call_expression')
    lines = source.splitlines()
    results = []

    def walk(node: Node):
        if node.type == call_type:
            # The "function" being called is the first child
            callee = node.child_by_field_name('function') or (node.children[0] if node.children else None)
            if callee:
                # Could be identifier: authenticate(...)
                # Or attribute: service.authenticate(...)
                callee_name = None
                if callee.type == 'identifier':
                    callee_name = callee.text.decode('utf-8')
                elif callee.type in ('attribute', 'member_expression', 'selector_expression'):
                    # Get the last part: obj.method → method
                    last = callee.children[-1] if callee.children else None
                    if last:
                        callee_name = last.text.decode('utf-8')

                if callee_name == func_name:
                    line_no = node.start_point[0]
                    results.append((line_no + 1, lines[line_no] if line_no < len(lines) else ''))

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return results


def extract_all_function_names(source: str, ext: str) -> list:
    """
    Return all function/method names defined in a source file.
    Useful for building an index.
    """
    tree = parse_file(source, ext)
    if not tree:
        return []

    node_types, name_field = FUNCTION_NODE_TYPES.get(ext, ([], 'name'))
    names = []

    def walk(node: Node):
        if node.type in node_types:
            name_node = node.child_by_field_name(name_field)
            if name_node:
                names.append(name_node.text.decode('utf-8'))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return names


def extract_imports_ast(source: str, ext: str) -> list:
    """
    Extract import statements using AST (more reliable than regex).
    Returns list of import strings.
    """
    tree = parse_file(source, ext)
    if not tree:
        return []

    import_node_types = {
        'py':  ('import_statement', 'import_from_statement'),
        'js':  ('import_statement', 'import_declaration'),
        'jsx': ('import_statement', 'import_declaration'),
        'ts':  ('import_statement', 'import_declaration'),
        'tsx': ('import_statement', 'import_declaration'),
        'go':  ('import_declaration', 'import_spec'),
    }.get(ext, ())

    lines = source.splitlines()
    imports = []

    def walk(node: Node):
        if node.type in import_node_types:
            line_no = node.start_point[0]
            if line_no < len(lines):
                imports.append(lines[line_no])
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return imports

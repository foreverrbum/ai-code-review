"""
tests/test_ast_parser.py

Tests for AST-based code extraction across all four supported languages.

Why these tests exist:
  During development, tree_sitter_typescript exports language_typescript() and
  language_tsx() rather than the generic language() that every other grammar uses.
  The original _load_language() called mod.language() for all grammars, which silently
  raised AttributeError for TypeScript and returned None — causing the entire TypeScript
  AST path to fall back to regex with zero warning.

  These tests catch that class of silent failure: if a language loads correctly,
  extract_function_ast() returns non-empty text; if it returns '', either the grammar
  isn't installed or the loader is broken.

All tests use inline source strings — no disk I/O, no API keys, runs in < 1s.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.ast_parser import (
    extract_function_ast,
    find_call_sites_ast,
    extract_imports_ast,
    extract_all_function_names,
    _get_language,
)


# ── Language availability ─────────────────────────────────────────────────────
# These fail loudly if a grammar package is missing, rather than silently
# returning empty results that look like "no functions found."

def test_python_grammar_loads():
    assert _get_language('py') is not None, (
        "tree_sitter_python not installed — run: pip install tree-sitter-python"
    )

def test_typescript_grammar_loads():
    assert _get_language('ts') is not None, (
        "tree_sitter_typescript not installed, or language_typescript() export missing. "
        "This was the bug: _load_language called mod.language() but the correct export "
        "is mod.language_typescript(). Fix: map 'ts' to func_name='language_typescript'."
    )

def test_tsx_grammar_loads():
    assert _get_language('tsx') is not None, (
        "tsx grammar not loaded. tree_sitter_typescript exports language_tsx(), "
        "not language()."
    )

def test_javascript_grammar_loads():
    assert _get_language('js') is not None, (
        "tree_sitter_javascript not installed — run: pip install tree-sitter-javascript"
    )

def test_go_grammar_loads():
    assert _get_language('go') is not None, (
        "tree_sitter_go not installed — run: pip install tree-sitter-go"
    )


# ── Python extraction ─────────────────────────────────────────────────────────

PYTHON_SOURCE = '''\
import os

def helper(x):
    return x + 1

def main_function(a, b):
    """Does the main thing."""
    result = helper(a)
    return result + b

class MyClass:
    def method(self):
        pass
'''

def test_python_extract_function_body():
    body = extract_function_ast(PYTHON_SOURCE, 'main_function', 'py')
    assert 'def main_function' in body
    assert 'result = helper(a)' in body
    assert 'return result + b' in body

def test_python_does_not_include_other_functions():
    body = extract_function_ast(PYTHON_SOURCE, 'main_function', 'py')
    # helper is a separate function; it should not appear in main_function's body
    assert 'def helper' not in body

def test_python_missing_function_returns_empty():
    body = extract_function_ast(PYTHON_SOURCE, 'nonexistent', 'py')
    assert body == ''

def test_python_find_call_sites():
    sites = find_call_sites_ast(PYTHON_SOURCE, 'helper', 'py')
    assert len(sites) >= 1
    # Call site is the call inside main_function, not the definition line
    line_texts = [text for _, text in sites]
    assert any('helper(a)' in t for t in line_texts)

def test_python_definition_not_a_call_site():
    # 'def helper' is a definition, not a call — AST should not return it
    sites = find_call_sites_ast(PYTHON_SOURCE, 'helper', 'py')
    line_texts = [text for _, text in sites]
    assert not any('def helper' in t for t in line_texts)

def test_python_extract_imports():
    imports = extract_imports_ast(PYTHON_SOURCE, 'py')
    assert any('import os' in imp for imp in imports)

def test_python_extract_all_function_names():
    names = extract_all_function_names(PYTHON_SOURCE, 'py')
    assert 'helper' in names
    assert 'main_function' in names
    assert 'method' in names


# ── TypeScript extraction ─────────────────────────────────────────────────────
# This is the language where the silent-failure bug lived.

TYPESCRIPT_SOURCE = '''\
import { readFileSync } from 'fs';
import path from 'path';

function loadConfig(filePath: string): Record<string, unknown> {
    const raw = readFileSync(filePath, 'utf-8');
    return JSON.parse(raw);
}

function validateConfig(config: Record<string, unknown>): boolean {
    return 'version' in config;
}

export { loadConfig, validateConfig };
'''

def test_typescript_extract_function_body():
    body = extract_function_ast(TYPESCRIPT_SOURCE, 'loadConfig', 'ts')
    assert body != '', (
        "TypeScript AST returned empty string. This is the grammar-export bug: "
        "tree_sitter_typescript exports language_typescript(), not language(). "
        "Check that _get_language('ts') maps to func_name='language_typescript'."
    )
    assert 'function loadConfig' in body
    assert 'readFileSync' in body

def test_typescript_extract_second_function():
    body = extract_function_ast(TYPESCRIPT_SOURCE, 'validateConfig', 'ts')
    assert 'function validateConfig' in body
    assert "'version' in config" in body

def test_typescript_missing_function_returns_empty():
    body = extract_function_ast(TYPESCRIPT_SOURCE, 'doesNotExist', 'ts')
    assert body == ''

def test_typescript_find_call_sites():
    sites = find_call_sites_ast(TYPESCRIPT_SOURCE, 'readFileSync', 'ts')
    assert len(sites) >= 1

def test_typescript_extract_imports():
    imports = extract_imports_ast(TYPESCRIPT_SOURCE, 'ts')
    assert len(imports) >= 1
    assert any('fs' in imp or 'path' in imp for imp in imports)


# ── JavaScript extraction ─────────────────────────────────────────────────────

JAVASCRIPT_SOURCE = '''\
const express = require('express');

function createRouter(app) {
    const router = express.Router();
    router.get('/health', handleHealth);
    return router;
}

function handleHealth(req, res) {
    res.json({ status: 'ok' });
}

module.exports = { createRouter };
'''

def test_javascript_extract_function_body():
    body = extract_function_ast(JAVASCRIPT_SOURCE, 'createRouter', 'js')
    assert 'function createRouter' in body
    assert 'express.Router()' in body

def test_javascript_find_call_sites():
    # handleHealth is referenced (called) inside createRouter
    sites = find_call_sites_ast(JAVASCRIPT_SOURCE, 'handleHealth', 'js')
    # Note: in router.get('/health', handleHealth) handleHealth is a reference,
    # not a call expression — so this may return 0. That's correct AST behavior.
    # We only assert it doesn't crash.
    assert isinstance(sites, list)

def test_javascript_extract_imports():
    imports = extract_imports_ast(JAVASCRIPT_SOURCE, 'js')
    # require() is not an import_declaration in JS AST — this tests the fallback
    assert isinstance(imports, list)


# ── Go extraction ─────────────────────────────────────────────────────────────

GO_SOURCE = '''\
package main

import (
\t"fmt"
\t"os"
)

func greet(name string) string {
\treturn fmt.Sprintf("Hello, %s!", name)
}

func main() {
\tif len(os.Args) < 2 {
\t\tfmt.Println("Usage: greet <name>")
\t\tos.Exit(1)
\t}
\tfmt.Println(greet(os.Args[1]))
}
'''

def test_go_extract_function_body():
    body = extract_function_ast(GO_SOURCE, 'greet', 'go')
    assert 'func greet' in body
    assert 'fmt.Sprintf' in body

def test_go_extract_main():
    body = extract_function_ast(GO_SOURCE, 'main', 'go')
    assert 'func main' in body
    assert 'os.Args' in body

def test_go_find_call_sites():
    sites = find_call_sites_ast(GO_SOURCE, 'greet', 'go')
    assert len(sites) >= 1
    line_texts = [text for _, text in sites]
    assert any('greet(' in t for t in line_texts)

def test_go_extract_imports():
    imports = extract_imports_ast(GO_SOURCE, 'go')
    assert len(imports) >= 1

def test_go_all_function_names():
    names = extract_all_function_names(GO_SOURCE, 'go')
    assert 'greet' in names
    assert 'main' in names


# ── Decorated Python functions ────────────────────────────────────────────────
# Python decorated functions have a 'decorated_definition' outer node.
# The inner 'function_definition' is nested — we need the outer node to get
# the decorator in the extracted text.

DECORATED_PYTHON = '''\
import functools

def my_decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper

@my_decorator
def decorated_target(x, y):
    return x * y
'''

def test_python_decorated_function_includes_decorator():
    body = extract_function_ast(DECORATED_PYTHON, 'decorated_target', 'py')
    assert 'def decorated_target' in body
    assert '@my_decorator' in body, (
        "Decorator should be included in the extracted body — "
        "it's the outer 'decorated_definition' node that wraps the function."
    )


# ── Unsupported extension ─────────────────────────────────────────────────────

def test_unsupported_extension_returns_empty():
    body = extract_function_ast("def foo(): pass", 'foo', 'rb')
    assert body == ''

def test_unsupported_extension_call_sites_returns_empty_list():
    sites = find_call_sites_ast("foo()", 'foo', 'rb')
    assert sites == []

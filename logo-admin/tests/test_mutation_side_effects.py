"""The mutation kernel cannot grow nontransactional side effects."""

import ast
from pathlib import Path

import mutations


SOURCE_PATH = Path(mutations.__file__).resolve()
SOURCE = SOURCE_PATH.read_text()
TREE = ast.parse(SOURCE)

PUBLIC_HANDLERS = {
    "save_assignment",
    "deactivate_assignment",
    "hard_delete_assignment",
    "deactivate_color",
    "hard_delete_color",
    "set_style_active",
    "apply_to_colors",
    "copy_style",
    "update_store_settings",
    "set_store_pricing_tier",
    "delete_store_pricing_tier",
}


def test_kernel_has_no_network_filesystem_subprocess_or_asgi_imports():
    forbidden_roots = {
        "fastapi",
        "http",
        "requests",
        "socket",
        "subprocess",
        "pathlib",
        "shutil",
        "tempfile",
        "urllib.request",
    }
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    offenders = {
        name
        for name in imported
        if any(name == root or name.startswith(root + ".") for root in forbidden_roots)
    }
    assert offenders == set()


def test_kernel_never_opens_files_or_constructs_transactions():
    called_names = {
        node.func.id
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "open" not in called_names
    assert not ({"commit", "rollback", "cursor"} & called_attributes)
    assert "from db import" not in SOURCE
    assert "import db" not in SOURCE


def test_every_public_handler_accepts_caller_owned_cursor_first():
    functions = {
        node.name: node
        for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert PUBLIC_HANDLERS <= functions.keys()
    for name in PUBLIC_HANDLERS:
        arguments = [argument.arg for argument in functions[name].args.args]
        assert arguments[:3] == ["cursor", "actor", "command"], name


def test_kernel_exposes_no_async_or_background_entrypoint():
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(TREE))
    assert "BackgroundTasks" not in SOURCE

"""Reject constant-duration sleeps in end-to-end test bodies."""

from __future__ import annotations

import argparse
import ast
import io
import json
from pathlib import Path
import sys
import tokenize
from typing import Iterable


_SLEEP_MODULES = {"asyncio", "time", "trio", "anyio"}
_NUMBER = "<numeric literal>"
_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _numeric_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return type(node.value) in (int, float, complex)
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and _numeric_literal(node.operand)
    )


class _Bindings(ast.NodeVisitor):
    """Collect lexical bindings without leaking nested function/class locals."""

    def __init__(self, *, module: bool = False) -> None:
        self.module = module
        self.names: dict[str, str | None] = {}
        self.outer_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names[node.id] = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            module = alias.name if alias.asname else alias.name.split(".")[0]
            self.names[name] = module if module in _SLEEP_MODULES else None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names[alias.asname or alias.name] = (
                f"{node.module}.sleep"
                if not node.level
                and node.module in _SLEEP_MODULES
                and alias.name == "sleep"
                else None
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if self.module and _numeric_literal(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.names[target.id] = _NUMBER

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if (
            self.module
            and node.value is not None
            and _numeric_literal(node.value)
            and isinstance(node.target, ast.Name)
        ):
            self.names[node.target.id] = _NUMBER

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names[node.name] = None

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names[node.name] = None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names[node.name] = None
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.outer_names.update(node.names)

    visit_Nonlocal = visit_Global


def _bindings(
    body: Iterable[ast.AST],
    *,
    module: bool = False,
    args: ast.arguments | None = None,
) -> dict[str, str | None]:
    collector = _Bindings(module=module)
    if args is not None:
        parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        parameters += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
        collector.names.update((arg.arg, None) for arg in parameters)
    for node in body:
        collector.visit(node)
    return {
        name: value
        for name, value in collector.names.items()
        if name not in collector.outer_names
    }


def _under(path: Path, *parts: str) -> bool:
    return any(
        path.parts[index : index + len(parts)] == parts
        for index in range(len(path.parts) - len(parts) + 1)
    )


class _SleepVisitor(ast.NodeVisitor):
    def __init__(
        self, path: Path, source: str, tree: ast.Module, *, all_functions: bool
    ) -> None:
        self.path = path
        self.source = source
        self.scopes = [_bindings(tree.body, module=True)]
        self.every_function = (
            all_functions
            or (path.name.startswith("test_") and path.suffix == ".py")
            or _under(path.resolve(), "tests", "e2e", "suites")
            or _under(path.resolve(), "tests", "e2e", "harness", "suites")
        )
        self.in_test = False
        self.allowed_lines = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
            and "obs-e2e-lint: allow" in token.string
        }
        self.findings: list[dict[str, str | int]] = []
        self.suppressed = 0

    def _resolve(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _constant(self, node: ast.AST) -> bool:
        if _numeric_literal(node):
            return True
        if isinstance(node, ast.Name):
            return self._resolve(node.id) == _NUMBER
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return self._constant(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITHMETIC):
            return self._constant(node.left) and self._constant(node.right)
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.in_test
        self.in_test = previous or self.every_function or node.name.startswith("test")
        self.scopes.append(_bindings(node.body, args=node.args))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.in_test = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.scopes.append(_bindings([node.body], args=node.args))
        self.visit(node.body)
        self.scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Name):
            target = self._resolve(function.id)
        elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            module = self._resolve(function.value.id)
            target = f"{module}.{function.attr}" if module in _SLEEP_MODULES else None
        else:
            target = None
        if (
            self.in_test
            and target in {f"{module}.sleep" for module in _SLEEP_MODULES}
            and node.args
            and self._constant(node.args[0])
        ):
            if node.lineno in self.allowed_lines:
                self.allowed_lines.remove(node.lineno)
                self.suppressed += 1
            else:
                call = ast.get_source_segment(self.source, function) or ast.unparse(function)
                argument = ast.get_source_segment(self.source, node.args[0]) or ast.unparse(
                    node.args[0]
                )
                message = f"fixed sleep {call}({argument})"
                self.findings.append(
                    {
                        "path": str(self.path),
                        "line": node.lineno,
                        "col": node.col_offset + 1,
                        "code": "E2E001",
                        "message": message.replace("\n", " ").replace("\r", " "),
                    }
                )
        self.generic_visit(node)


def _python_files(paths: Iterable[str]) -> list[Path]:
    files: dict[Path, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        candidates = path.rglob("*.py") if path.is_dir() else [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            # INVARIANT R10: harness internals may poll, but harness/suites are tests.
            # See Projects/Personal Projects/OBS Platform/E2E Harness.md.
            harness_internal = _under(resolved, "tests", "e2e", "harness")
            harness_suite = _under(resolved, "tests", "e2e", "harness", "suites")
            if candidate.suffix == ".py" and (not harness_internal or harness_suite):
                files.setdefault(resolved, candidate)
    return sorted(files.values(), key=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["tests/e2e"], help="Python files or trees")
    parser.add_argument("--json", action="store_true", help="print only the findings as JSON")
    parser.add_argument(
        "--all-functions",
        action="store_true",
        help="treat every function in the selected files as a test body (legacy calibration)",
    )
    args = parser.parse_args(argv)
    findings: list[dict[str, str | int]] = []
    suppressed = 0
    try:
        files = _python_files(args.paths)
        for path in files:
            with tokenize.open(path) as handle:
                source = handle.read()
            tree = ast.parse(source, filename=str(path))
            visitor = _SleepVisitor(path, source, tree, all_functions=args.all_functions)
            visitor.visit(tree)
            findings.extend(visitor.findings)
            suppressed += visitor.suppressed
    except (OSError, SyntaxError, UnicodeError, tokenize.TokenError) as error:
        print(f"obs-e2e lint: error: {error}", file=sys.stderr)
        return 2
    findings.sort(key=lambda item: (item["path"], item["line"], item["col"]))
    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        for finding in findings:
            print(
                f"{finding['path']}:{finding['line']}:{finding['col']}: "
                f"{finding['code']} {finding['message']}"
            )
        print(
            f"obs-e2e lint: {len(findings)} fixed sleeps, {suppressed} suppressed, "
            f"{len(files)} files"
        )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

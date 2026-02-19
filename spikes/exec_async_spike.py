"""
Spike: exec() with ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

Tests whether we can use compile() + eval()/exec() to run both sync and
async user code, which is needed for building an MCP "run Python" tool.

Key discovery: exec() ALWAYS returns None. For async code, you must use
eval() on the compiled code object to get the coroutine back. Check
co.co_flags & CO_COROUTINE to know whether the code is async.
"""

import ast
import asyncio
import contextlib
import io
from inspect import CO_COROUTINE


def compile_code(code: str):
    """Compile code with PyCF_ALLOW_TOP_LEVEL_AWAIT.
    Returns (compiled_code, is_async)."""
    compiled = compile(
        code,
        filename="<user_code>",
        mode="exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
    )
    is_async = bool(compiled.co_flags & CO_COROUTINE)
    return compiled, is_async


async def run_code(code: str, namespace: dict | None = None):
    """Compile and run code. Returns (namespace, error).

    For sync code: uses exec() directly.
    For async code: uses eval() to get coroutine, then awaits it.
    """
    if namespace is None:
        namespace = {}
    try:
        compiled, is_async = compile_code(code)
        if is_async:
            # eval() returns the coroutine; exec() would discard it
            coro = eval(compiled, namespace)
            await coro
        else:
            exec(compiled, namespace)
        return namespace, None
    except Exception as e:
        return namespace, e


async def run_code_with_timeout(code: str, timeout: float, namespace: dict | None = None):
    """Like run_code but with a timeout for async code."""
    if namespace is None:
        namespace = {}
    try:
        compiled, is_async = compile_code(code)
        if is_async:
            coro = eval(compiled, namespace)
            await asyncio.wait_for(coro, timeout=timeout)
        else:
            exec(compiled, namespace)
        return namespace, None
    except Exception as e:
        return namespace, e


def user_vars(ns: dict) -> dict:
    """Filter namespace to user-defined variables only."""
    return {k: v for k, v in ns.items()
            if not k.startswith("__") and not callable(v) and k != "asyncio"}


async def main():
    # ---------------------------------------------------------------
    # Test 1: Pure sync code
    # ---------------------------------------------------------------
    print("=" * 60)
    print("TEST 1: Pure sync code")
    print("=" * 60)

    code = "x = 2 + 2"
    compiled, is_async = compile_code(code)
    print(f"  Code: {code!r}")
    print(f"  co_flags & CO_COROUTINE: {is_async}")

    ns, err = await run_code(code)
    print(f"  Error: {err}")
    print(f"  Namespace: {user_vars(ns)}")
    assert not is_async, "sync code should not be flagged as async"
    assert err is None
    assert ns["x"] == 4
    print("  PASSED")

    # ---------------------------------------------------------------
    # Test 2: Async code with await
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 2: Async code with await")
    print("=" * 60)

    code = "import asyncio\nawait asyncio.sleep(0.1)\nx = 42"
    compiled, is_async = compile_code(code)
    print(f"  Code: {code!r}")
    print(f"  co_flags & CO_COROUTINE: {is_async}")

    ns, err = await run_code(code)
    print(f"  Error: {err}")
    print(f"  Namespace: {user_vars(ns)}")
    assert is_async, "code with await should be flagged as async"
    assert err is None
    assert ns["x"] == 42
    print("  PASSED")

    # ---------------------------------------------------------------
    # Test 3: Mixed sync + async in same block
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 3: Mixed sync assignment + async call")
    print("=" * 60)

    code = "y = 10\nimport asyncio\nawait asyncio.sleep(0.01)\nz = y * 3"
    compiled, is_async = compile_code(code)
    print(f"  Code: {code!r}")
    print(f"  co_flags & CO_COROUTINE: {is_async}")

    ns, err = await run_code(code)
    print(f"  Error: {err}")
    print(f"  Namespace: {user_vars(ns)}")
    assert is_async
    assert err is None
    assert ns["y"] == 10
    assert ns["z"] == 30
    print("  PASSED")

    # ---------------------------------------------------------------
    # Test 4: Exception in sync code
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 4: Exception in sync code")
    print("=" * 60)

    code = "x = 1 / 0"
    ns, err = await run_code(code)
    print(f"  Error: {type(err).__name__}: {err}")
    assert isinstance(err, ZeroDivisionError)
    print("  PASSED: ZeroDivisionError caught")

    # ---------------------------------------------------------------
    # Test 5: Exception in async code
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 5: Exception in async code")
    print("=" * 60)

    code = "import asyncio\nawait asyncio.sleep(0.01)\nraise ValueError('async boom')"
    ns, err = await run_code(code)
    print(f"  Error: {type(err).__name__}: {err}")
    assert isinstance(err, ValueError)
    assert "async boom" in str(err)
    print("  PASSED: ValueError caught from async code")

    # ---------------------------------------------------------------
    # Test 6: Timeout with asyncio.wait_for
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 6: Timeout: 5s sleep with 1s timeout")
    print("=" * 60)

    code = "import asyncio\nawait asyncio.sleep(5)\nx = 'should not reach'"
    import time
    t0 = time.monotonic()
    ns, err = await run_code_with_timeout(code, timeout=1.0)
    elapsed = time.monotonic() - t0
    print(f"  Error: {type(err).__name__ if err else None}: {err}")
    print(f"  Elapsed: {elapsed:.2f}s")
    print(f"  Namespace: {user_vars(ns)}")
    assert isinstance(err, asyncio.TimeoutError)
    assert elapsed < 2.0, f"Took too long: {elapsed:.2f}s"
    assert ns.get("x") != "should not reach"
    print("  PASSED: TimeoutError after ~1s, code did not complete")

    # ---------------------------------------------------------------
    # Test 7: stdout capture with redirect_stdout (sync)
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 7: stdout capture with redirect_stdout (sync)")
    print("=" * 60)

    code = "print('hello from exec')\nprint('second line')\nx = 99"
    ns = {}
    buf = io.StringIO()

    with contextlib.redirect_stdout(buf):
        ns, err = await run_code(code, ns)

    captured = buf.getvalue()
    print(f"  Error: {err}")
    print(f"  Captured stdout: {captured!r}")
    print(f"  Namespace: {user_vars(ns)}")
    assert err is None
    assert "hello from exec" in captured
    assert "second line" in captured
    assert ns["x"] == 99
    print("  PASSED")

    # ---------------------------------------------------------------
    # Test 7b: stdout capture with redirect_stdout (async)
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 7b: stdout capture with redirect_stdout (ASYNC)")
    print("=" * 60)

    code = "import asyncio\nprint('before await')\nawait asyncio.sleep(0.01)\nprint('after await')"
    ns = {}
    buf = io.StringIO()

    with contextlib.redirect_stdout(buf):
        ns, err = await run_code(code, ns)

    captured = buf.getvalue()
    print(f"  Error: {err}")
    print(f"  Captured stdout: {captured!r}")
    assert err is None
    assert "before await" in captured
    assert "after await" in captured
    print("  PASSED")

    # ---------------------------------------------------------------
    # Bonus: stderr capture
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 8 (bonus): stderr capture")
    print("=" * 60)

    code = "import sys\nprint('to stderr', file=sys.stderr)\nprint('to stdout')"
    ns = {}
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        ns, err = await run_code(code, ns)

    print(f"  stdout: {stdout_buf.getvalue()!r}")
    print(f"  stderr: {stderr_buf.getvalue()!r}")
    assert "to stdout" in stdout_buf.getvalue()
    assert "to stderr" in stderr_buf.getvalue()
    print("  PASSED")

    # ---------------------------------------------------------------
    # Bonus: Namespace persistence across calls
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TEST 9 (bonus): Namespace persistence across multiple exec calls")
    print("=" * 60)

    ns = {}
    ns, _ = await run_code("x = 10", ns)
    ns, _ = await run_code("y = x * 2", ns)
    ns, _ = await run_code("import asyncio\nawait asyncio.sleep(0.01)\nz = x + y", ns)
    print(f"  After 3 calls: {user_vars(ns)}")
    assert ns["x"] == 10
    assert ns["y"] == 20
    assert ns["z"] == 30
    print("  PASSED: Variables persist across calls")

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    print(f"\n{'='*60}")
    print("ALL TESTS PASSED")
    print("=" * 60)
    print()
    print("Key findings:")
    print("  1. compile() with PyCF_ALLOW_TOP_LEVEL_AWAIT sets CO_COROUTINE flag for async code")
    print("  2. For sync code: exec(compiled, ns) works normally, returns None")
    print("  3. For async code: eval(compiled, ns) returns a coroutine that must be awaited")
    print("     (exec() would discard the coroutine and emit a RuntimeWarning!)")
    print("  4. Mixed sync+async blocks: entire block becomes async (CO_COROUTINE set)")
    print("  5. Sync exceptions raised by exec()/eval() directly")
    print("  6. Async exceptions raised when awaiting the coroutine")
    print("  7. asyncio.wait_for(coro, timeout=N) works for timeouts")
    print("  8. contextlib.redirect_stdout/redirect_stderr capture output in both modes")
    print("  9. Namespace persists across multiple calls (variables accumulate)")
    print()
    print("Pattern for MCP tool:")
    print("  compiled = compile(code, '<code>', 'exec', flags=PyCF_ALLOW_TOP_LEVEL_AWAIT)")
    print("  is_async = bool(compiled.co_flags & CO_COROUTINE)")
    print("  if is_async:")
    print("      coro = eval(compiled, namespace)")
    print("      await asyncio.wait_for(coro, timeout=TIMEOUT)")
    print("  else:")
    print("      exec(compiled, namespace)")


if __name__ == "__main__":
    asyncio.run(main())

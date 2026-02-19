"""
Spike: Does monkey-patching work for already-bound closures and list references?

Context: Our HookPipeline stores check functions in a `_checks` list.
If the agent replaces an entry in that list, does the pipeline pick it up?
"""

import types


# ── Test 1: Module-level function replacement ────────────────────────────
# Simulate a module namespace with a SimpleNamespace

def test_module_level_replacement():
    mod = types.SimpleNamespace()

    def original():
        return "original"

    mod.func = original

    # Capture a direct reference
    captured = mod.func

    # Replace in the "module"
    def replacement():
        return "replaced"

    mod.func = replacement

    # The captured reference still points to the old function
    result_captured = captured()
    result_module = mod.func()

    print("Test 1: Module-level function replacement")
    print(f"  captured()  = {result_captured!r}  (expect 'original')")
    print(f"  mod.func()  = {result_module!r}  (expect 'replaced')")
    assert result_captured == "original", "FAIL: captured ref should be old"
    assert result_module == "replaced", "FAIL: module attr should be new"
    print("  PASS\n")


# ── Test 2: List mutation ────────────────────────────────────────────────

def test_list_mutation():
    def original():
        return "original"

    def replacement():
        return "replaced"

    checks = [original]

    # Someone else holds a reference to the same list object
    pipeline_checks = checks

    # Replace the entry in the list (mutate in place)
    checks[0] = replacement

    # Iterating the list sees the new function
    results = [fn() for fn in pipeline_checks]

    print("Test 2: List mutation (replace entry in-place)")
    print(f"  pipeline_checks[0]() = {results[0]!r}  (expect 'replaced')")
    assert results[0] == "replaced", "FAIL: list mutation should be visible"
    print("  PASS\n")


# ── Test 2b: List reassignment (the trap) ────────────────────────────────

def test_list_reassignment():
    def original():
        return "original"

    def replacement():
        return "replaced"

    checks = [original]
    pipeline_checks = checks  # same list object

    # REASSIGN the variable (does NOT mutate the original list)
    checks = [replacement]

    results = [fn() for fn in pipeline_checks]

    print("Test 2b: List reassignment (rebind variable, don't mutate)")
    print(f"  pipeline_checks[0]() = {results[0]!r}  (expect 'original' — old list)")
    assert results[0] == "original", "FAIL: reassignment should NOT affect other ref"
    print("  PASS\n")


# ── Test 3: Closure capture ──────────────────────────────────────────────

def test_closure_capture():
    def original():
        return "original"

    func = original

    def closure():
        return func()

    # Replace the outer variable
    def replacement():
        return "replaced"

    func = replacement

    # The closure captured the *name* `func` from the enclosing scope,
    # which is now rebound — so it DOES see the new version.
    # (This is different from capturing a default argument.)
    result = closure()

    print("Test 3: Closure capture (captures name binding in enclosing scope)")
    print(f"  closure() = {result!r}  (expect 'replaced' — Python closures capture the variable, not the value)")
    # Note: The hypothesis in the task description says "No — closures capture bindings"
    # but that's exactly WHY it IS 'replaced': closures capture the *binding* (the cell),
    # and the binding now points to the new function.
    assert result == "replaced", "FAIL: closure should see rebound variable"
    print("  PASS\n")


# ── Test 3b: Closure with default argument (the frozen case) ─────────────

def test_closure_default_arg():
    def original():
        return "original"

    func = original

    def closure(f=func):
        return f()

    def replacement():
        return "replaced"

    func = replacement

    result = closure()

    print("Test 3b: Closure with default argument (value frozen at def time)")
    print(f"  closure() = {result!r}  (expect 'original' — default arg is snapshot)")
    assert result == "original", "FAIL: default arg should freeze the value"
    print("  PASS\n")


# ── Test 4: Object attribute ─────────────────────────────────────────────

def test_object_attribute():
    class Pipeline:
        pass

    def original():
        return "original"

    pipeline = Pipeline()
    pipeline.func = original

    def replacement():
        return "replaced"

    pipeline.func = replacement

    result = pipeline.func()

    print("Test 4: Object attribute replacement")
    print(f"  pipeline.func() = {result!r}  (expect 'replaced' — attribute lookup is dynamic)")
    assert result == "replaced", "FAIL: attribute should be dynamically resolved"
    print("  PASS\n")


# ── Test 5: HookPipeline simulation ─────────────────────────────────────
# This is the real scenario: _checks is a list, and we mutate entries in it.

def test_hook_pipeline_simulation():
    """Simulates the actual HookPipeline pattern."""

    class HookPipeline:
        def __init__(self, checks):
            self._checks = checks  # stores reference to the list

        def run(self, value):
            for check_fn in self._checks:
                result = check_fn(value)
                if result is not None:
                    return result
            return None

    def immutable_check(path):
        if "Meeting Notes" in path:
            return "BLOCKED: immutable"
        return None

    checks = [immutable_check]
    pipeline = HookPipeline(checks)

    # Pipeline blocks Meeting Notes
    r1 = pipeline.run("Misc/Meeting Notes/2024.md")
    print("Test 5: HookPipeline simulation")
    print(f"  Before patch: run('Meeting Notes/...') = {r1!r}  (expect 'BLOCKED: immutable')")
    assert r1 == "BLOCKED: immutable"

    # Now the agent monkey-patches the check list entry
    def permissive_check(path):
        return None  # allow everything

    checks[0] = permissive_check

    # Pipeline should now allow Meeting Notes (because _checks IS the same list)
    r2 = pipeline.run("Misc/Meeting Notes/2024.md")
    print(f"  After patch:  run('Meeting Notes/...') = {r2!r}  (expect None — allowed)")
    assert r2 is None, "FAIL: pipeline should see mutated list entry"

    # Also verify pipeline._checks is literally the same object
    assert pipeline._checks is checks, "FAIL: should be same list object"
    print("  pipeline._checks is checks: True")
    print("  PASS\n")


# ── Run all ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Monkey-Patching Spike")
    print("=" * 60 + "\n")

    test_module_level_replacement()
    test_list_mutation()
    test_list_reassignment()
    test_closure_capture()
    test_closure_default_arg()
    test_object_attribute()
    test_hook_pipeline_simulation()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
    print()
    print("Summary:")
    print("  1. Captured function ref    → stale (old function)")
    print("  2. List mutation (in-place)  → visible (same list object)")
    print("  2b. List reassignment        → NOT visible (new list object)")
    print("  3. Closure (free variable)   → visible (captures the cell/binding, not the value)")
    print("  3b. Closure (default arg)    → stale (value frozen at def time)")
    print("  4. Object attribute          → visible (dynamic lookup)")
    print("  5. HookPipeline._checks      → visible (list mutation works)")
    print()
    print("Conclusion: Mutating entries in HookPipeline._checks list WORKS.")
    print("The pipeline iterates the list each time, so it always sees current entries.")
    print("Do NOT reassign the list variable — only mutate in place (checks[i] = ...).")

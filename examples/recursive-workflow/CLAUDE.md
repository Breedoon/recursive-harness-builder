# Recursive Workflow Starter

This project demonstrates a flat markdown procedure bundle for recursive agent workflows.

Use `procedures/loop.md` for simple tasks that need execute/verify/fix orchestration.
Use `procedures/router.md` for complex tasks that need decomposition and recursive dispatch.

Procedure files are ordinary markdown files. The router guard hook is at `hooks/router_guard.py`; artifacts should be written under `artifacts/`.

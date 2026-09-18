# Dependency advisory disposition

## Current audit result

`npm audit --omit=dev --package-lock-only --json --prefix /opt/toolbox` reported:

- 2 high-severity findings through `sharp` / libvips;
- 2 moderate findings through `exceljs` / `uuid`;
- no critical findings.

The pinned direct package is `@wonderwhy-er/desktop-commander` 0.2.50. The audit tool's only offered package-level remediation is a downgrade to 0.2.23, which is not an acceptable compatibility or security disposition for this device.

## Materiality

The high `sharp` and moderate Excel parsing dependency paths are **material to generalized active-vault device use**: Desktop Commander is a generic file/process MCP service and the mounted active vault can contain files that cause those parsers to be invoked. Container hardening limits host/control-plane impact but does not eliminate parser risk inside the mounted vault.

For the bounded current integration proof, the permitted device action is restricted to a newly created transient text proof file under the active vault. It does not invoke image or spreadsheet parsing. This narrows reachability for that proof only; it does not remove the advisory for broader future use.

## Disposition

- Keep the current 0.2.50 pin; do not silently downgrade to 0.2.23.
- Do not represent the audit as clean or immaterial to all device operations.
- Before any later dependency upgrade, rerun the full MCP stdio, active-vault, isolation, recovery, device/pairing, and ChatGPT tool-action regression chain against the new pin.
- The current device proof must operate only on the generated text proof file; image and spreadsheet parser actions are excluded from this proof.

## Candidate-specific disposition

The current delivered MCP tool list contains file, directory, process, search, and PDF operations, but no image or spreadsheet parser tool. The candidate action specified for this integration is limited to one generated `*.txt` proof file in the active vault: create/write exact nonce bytes, read them back, compute SHA-256, and delete the same file. Its permitted tool surface is `write_file`, `read_file`, and, only if needed for hashing, `start_process` with a fixed text-file hash command.

That candidate does not invoke `sharp`, `exceljs`, or any image/spreadsheet parser path. The generic `start_process` capability means this conclusion is scoped to the exact candidate action, not to arbitrary future device use. Step C must retain secret-free receipts proving the actual calls and path matched this disposition; Step D/V must reject a candidate that invokes parser tooling or a different input class.

With those action constraints, the audit findings are **non-material to this exact text-file candidate** and require no source-pin remediation before C. They remain material to generalized device use under the preceding section.

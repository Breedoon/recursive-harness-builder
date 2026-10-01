from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "/workspace/runtime/node22/node_modules/.bin/node"


class PersistentRemoteTests(unittest.TestCase):
    def run_device(self, body: str) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            stub = base / "device.mjs"
            stub.write_text("""
export class MCPDevice {
  constructor(options) {
    this.persistSession = options.persistSession;
    this.deviceId = 'synthetic-device';
    this.remoteChannel = {
      getSession: async () => ({data:{session:{access_token:'initial-a',refresh_token:'initial-r'}}}),
      refreshTokenNow: async () => {this.startupRefreshCount=(this.startupRefreshCount||0)+1;},
      client: {auth:{onAuthStateChange: callback => {this.refreshCallback = callback;}}},
    };
  }
  async start() { await this.savePersistedConfig(); }
  async shutdown() { this.shutdownCalled = true; }
}
""")
            source = (ROOT / "bin/persistent_remote.mjs").read_text()
            source = source.replace(
                "'/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/dist/remote-device/device.js'",
                json.dumps(stub.as_uri()),
            )
            implementation = base / "persistent_remote.mjs"
            implementation.write_text(source)
            program = f"""
import fs from 'node:fs/promises';
import {{PersistentMCPDevice}} from {json.dumps(implementation.as_uri())};
const device = new PersistentMCPDevice();
device.configPath = {json.dumps(str(base / 'private/device.json'))};
await device.start();
{body}
"""
            completed = subprocess.run(
                [NODE, "--input-type=module", "-e", program],
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads([line for line in completed.stdout.splitlines() if line.startswith("{")][-1])

    def test_startup_persists_private_complete_config_atomically(self) -> None:
        result = self.run_device("""
const config = JSON.parse(await fs.readFile(device.configPath,'utf8'));
const stat = await fs.stat(device.configPath);
const directory = await fs.stat(new URL('.', 'file://'+device.configPath));
console.log(JSON.stringify({complete:config.session.refresh_token==='initial-r',mode:stat.mode&511,dirMode:directory.mode&511,persistent:device.persistSession,startupRefreshes:device.startupRefreshCount}));
""")
        self.assertEqual(result, {"complete": True, "mode": 0o600, "dirMode": 0o700, "persistent": True, "startupRefreshes": 1})

    def test_refresh_event_persists_new_tokens_and_shutdown_flushes(self) -> None:
        result = self.run_device("""
device.refreshCallback('TOKEN_REFRESHED',{access_token:'rotated-a',refresh_token:'rotated-r'});
await device.shutdown();
const config = JSON.parse(await fs.readFile(device.configPath,'utf8'));
console.log(JSON.stringify({rotated:config.session.refresh_token==='rotated-r',shutdown:device.shutdownCalled}));
""")
        self.assertEqual(result, {"rotated": True, "shutdown": True})

    def test_concurrent_rotations_leave_last_config_without_temporary_files(self) -> None:
        result = self.run_device("""
for(let n=0;n<25;n++) device.refreshCallback('TOKEN_REFRESHED',{access_token:'a-'+n,refresh_token:'r-'+n});
await device.pendingWrites;
const config = JSON.parse(await fs.readFile(device.configPath,'utf8'));
const files = await fs.readdir(new URL('.', 'file://'+device.configPath));
console.log(JSON.stringify({latest:config.session.refresh_token==='r-24',files}));
""")
        self.assertEqual(result, {"latest": True, "files": ["device.json"]})

    def test_incomplete_session_cannot_replace_valid_config(self) -> None:
        result = self.run_device("""
let rejected=false;
try {await device.persistSessionData({access_token:'incomplete'});} catch {rejected=true;}
const config = JSON.parse(await fs.readFile(device.configPath,'utf8'));
console.log(JSON.stringify({rejected,unchanged:config.session.refresh_token==='initial-r'}));
""")
        self.assertEqual(result, {"rejected": True, "unchanged": True})


class EntryPointTests(unittest.TestCase):
    def test_bind_mounted_entrypoint_is_executable(self) -> None:
        self.assertTrue(os.access(ROOT / "bin/entrypoint.sh", os.X_OK))

    def run_entrypoint(self, mode: str, arguments: tuple[str, ...] = ()) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            config = home / ".desktop-commander-device/device.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"synthetic":true}')
            binaries = base / "bin"
            binaries.mkdir()
            fake_node = binaries / "node"
            fake_node.write_text("#!/bin/sh\n[ -d \"$TMPDIR\" ] || exit 3\nprintf '%s' \"$TMPDIR\" > \"$TOOLBOX_SESSION/tmpdir-receipt\"\nprintf '%s\\n' \"$@\" > \"$TOOLBOX_SESSION/args-receipt\"\n")
            fake_node.chmod(0o755)
            session = base / "session"
            environment = os.environ | {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_DATA_HOME": str(home / "data"),
                "TOOLBOX_WORKSPACE": str(base / "workspace"),
                "TOOLBOX_SESSION": str(session),
                "PATH": str(binaries) + ":" + os.environ["PATH"],
            }
            environment.pop("TMPDIR", None)
            completed = subprocess.run(
                ["sh", str(ROOT / "bin/entrypoint.sh"), mode, *arguments],
                env=environment, capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = session / "tmpdir-receipt"
            return {
                "config_exists": config.exists(),
                "tmpdir_exists": (session / "tmp").is_dir(),
                "tmpdir_expected": receipt.exists() and receipt.read_text() == str(session / "tmp"),
                "pid_exists": (session / "runtime/remote-adapter.pid").exists(),
                "arguments": (session / "args-receipt").read_text().splitlines() if (session / "args-receipt").exists() else [],
            }

    def test_remote_exit_preserves_auth_and_cleans_only_runtime_pid(self) -> None:
        self.assertEqual(self.run_entrypoint("remote"), {
            "config_exists": True, "tmpdir_exists": True,
            "tmpdir_expected": True, "pid_exists": False,
            "arguments": ["/opt/toolbox/bin/persistent_remote.mjs"],
        })

    def test_explicit_remote_options_are_forwarded_to_native_cli(self) -> None:
        for argument in ("--help", "--logout", "--debug", "--no-persist-session", "--disable-no-sleep"):
            with self.subTest(argument=argument):
                result = self.run_entrypoint("remote", (argument,))
                self.assertEqual(result["arguments"], [
                    "/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/dist/index.js", "remote", argument,
                ])

    def test_explicit_purge_still_removes_auth(self) -> None:
        self.assertFalse(self.run_entrypoint("remote-purge-config")["config_exists"])


if __name__ == "__main__":
    unittest.main()

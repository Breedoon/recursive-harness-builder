#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { MCPDevice } from '/opt/toolbox/node_modules/@wonderwhy-er/desktop-commander/dist/remote-device/device.js';

export class PersistentMCPDevice extends MCPDevice {
  constructor() {
    super({ persistSession: true });
    this.pendingWrites = Promise.resolve();
  }

  persistSessionData(session) {
    if (!this.deviceId || !session?.access_token || !session?.refresh_token) {
      return Promise.reject(new Error('Incomplete persistent device session'));
    }
    const config = {
      deviceId: this.deviceId,
      session: { access_token: session.access_token, refresh_token: session.refresh_token },
    };
    const write = this.pendingWrites.then(async () => {
      const directory = path.dirname(this.configPath);
      const temporary = path.join(directory, `.device-${randomUUID()}.json`);
      await fs.mkdir(directory, { recursive: true, mode: 0o700 });
      try {
        await fs.writeFile(temporary, JSON.stringify(config), { mode: 0o600, flag: 'wx' });
        await fs.rename(temporary, this.configPath);
      } finally {
        await fs.rm(temporary, { force: true });
      }
    });
    this.pendingWrites = write.catch(error => {
      console.error(`Persistent session write failed: ${error.code || error.name}`);
    });
    return write;
  }

  async savePersistedConfig() {
    const { data: { session } } = await this.remoteChannel.getSession();
    await this.persistSessionData(session);
  }

  async start() {
    await super.start();
    this.remoteChannel.client.auth.onAuthStateChange((event, session) => {
      if (event === 'TOKEN_REFRESHED' && session) {
        // Persist the callback's tokens without re-entering the auth client's lock.
        void this.persistSessionData(session)
          .then(() => console.log('Persistent session rotation saved'))
          .catch(() => {});
      }
    });
    await this.remoteChannel.refreshTokenNow();
    await this.pendingWrites;
  }

  async shutdown() {
    await this.pendingWrites;
    await super.shutdown();
    await this.pendingWrites;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  console.debug = () => {};
  const device = new PersistentMCPDevice();
  await device.start();
}

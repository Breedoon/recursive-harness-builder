// CACHE-PREFIX WARNING: exact worker bytes and the fixed Node executable are gated together.
// INVARIANT: only the separately hash-verified fixed runtime may execute this decoder;
// never invoke this worker with the bundled CLI/Bun or fall back to its Brotli decoder.
// Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
'use strict';
if (process.argv.includes('--help')) {
  console.log('Usage: fixed-node bun_decoder_worker.cjs <sync|stream> [JSON options]; stdin/stdout are task-owned binary pipes.');
  process.exit(0);
}
const zlib = require('node:zlib');
const fs = require('node:fs');
const options = JSON.parse(process.argv[3] || '{}', (_, value) =>
  value && value.type === 'Buffer' && Array.isArray(value.data) ? Buffer.from(value.data) : value);
const errorValue = error => ({message:error.message, code:error.code, errno:error.errno, name:error.name});
if (process.argv[2] === 'sync') {
  try {
    const result = zlib.brotliDecompressSync(fs.readFileSync(0), {...options, info:false});
    fs.writeFileSync(1, result);
  } catch (error) {
    fs.writeFileSync(2, JSON.stringify(errorValue(error)) + '\n');
    process.exitCode = 1;
  }
} else {
  let decoder;
  let pending = Buffer.alloc(0);
  let ended = false;
  let failed = false;
  const acknowledge = id => process.stderr.write(JSON.stringify({ack:id}) + '\n');
  const fail = error => {
    if (failed) return;
    failed = true;
    process.stderr.write(JSON.stringify({error:errorValue(error)}) + '\n');
    decoder?.destroy();
    process.stdin.destroy();
    process.exitCode = 1;
  };
  process.stdout.on('error', () => { decoder?.destroy(); process.stdin.destroy(); });
  try { decoder = zlib.createBrotliDecompress(options); } catch (error) { fail(error); }
  if (decoder) {
    decoder.on('error', fail);
    decoder.pipe(process.stdout);
    process.stdin.on('data', chunk => {
      pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
      while (pending.length >= 9) {
        const length = pending.readUInt32BE(5);
        if (pending.length < length + 9) break;
        const type = pending[0], id = pending.readUInt32BE(1);
        const payload = pending.subarray(9, 9 + length);
        pending = pending.subarray(9 + length);
        if (type === 0) {
          process.stdin.pause();
          decoder.write(payload, error => { if (error) fail(error); else acknowledge(id); process.stdin.resume(); });
        } else if (type === 1) {
          ended = true;
          decoder.end(() => acknowledge(id));
        } else if (type === 2) {
          decoder.flush(payload.readUInt32BE(0), () => acknowledge(id));
        } else if (type === 3) {
          try { decoder.reset(); acknowledge(id); } catch (error) { fail(error); }
        }
      }
    });
    process.stdin.on('end', () => {
      if (!ended || pending.length) decoder.destroy();
    });
  }
}

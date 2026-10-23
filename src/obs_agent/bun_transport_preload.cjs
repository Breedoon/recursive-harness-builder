// CACHE-PREFIX WARNING: launch-env-only adapter; gate these bytes, worker and fixed Node together.
// INVARIANT: attach before any application capture; neither prompts nor tool schemas change.
// Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
'use strict';
if (process.versions.bun && !globalThis[Symbol.for('obs.bun.transport')]) {
  const fs = require('node:fs');
  const crypto = require('node:crypto');
  const cp = require('node:child_process');
  const {Duplex, Readable, pipeline} = require('node:stream');
  const zlib = require('node:zlib');
  const node = process.env.OBS_BUN_DECODER_NODE;
  const expected = process.env.OBS_BUN_DECODER_SHA256;
  const actual = node && crypto.createHash('sha256').update(fs.readFileSync(node)).digest('hex');
  if (!actual || actual !== expected) throw new Error('OBS fixed HTTP decoder identity mismatch');
  const worker = require('node:path').join(__dirname, 'bun_decoder_worker.cjs');
  const workerEnv = {PATH:'/usr/bin:/bin', LANG:'C.UTF-8'};
  const children = new Set();
  process.once('exit', () => { for (const child of children) child.kill(); });
  const restoreError = value => {
    const Constructor = {Error,TypeError,RangeError,SyntaxError}[value.name] || Error;
    return Object.assign(new Constructor(value.message), value);
  };
  const frame = (type, id, data = Buffer.alloc(0)) => {
    const header = Buffer.alloc(9);
    header[0] = type; header.writeUInt32BE(id, 1); header.writeUInt32BE(data.length, 5);
    return Buffer.concat([header, data]);
  };
  function inputBytes(value) {
    if (typeof value === 'string' || Buffer.isBuffer(value)) return Buffer.from(value);
    if (value instanceof ArrayBuffer || (typeof SharedArrayBuffer !== 'undefined' && value instanceof SharedArrayBuffer)) return Buffer.from(value);
    if (ArrayBuffer.isView(value)) return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    const error = new TypeError('The "buffer" argument must be a string, Buffer, TypedArray, DataView, or ArrayBuffer');
    error.code = 'ERR_INVALID_ARG_TYPE'; throw error;
  }
  // INVARIANT: Brotli bytes cross only into the fixed Node child, including zlib's
  // factories/constructors and captured aliases; stdout/stdin backpressure is bounded.
  // Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
  const flushOperation = Symbol('Brotli flush');
  class FixedBrotliDecompress extends Duplex {
    constructor(options = {}) {
      super({...options, decodeStrings:true, autoDestroy:true});
      this.bytesWritten = 0;
      this._sequence = 0;
      this._acks = new Map();
      this._finalCallback = null;
      this._decoderError = null;
      this._child = cp.spawn(node, [worker, 'stream', JSON.stringify(options)], {env:workerEnv, stdio:['pipe','pipe','pipe']});
      children.add(this._child);
      let diagnostic = '';
      this._child.stderr.setEncoding('utf8');
      this._child.stderr.on('data', text => {
        diagnostic += text;
        let end;
        while ((end = diagnostic.indexOf('\n')) >= 0) {
          const line = diagnostic.slice(0, end); diagnostic = diagnostic.slice(end + 1);
          try {
            const record = JSON.parse(line);
            if (record.error) { this._decoderError = restoreError(record.error); this.destroy(this._decoderError); }
            else if (record.ack) { const callback = this._acks.get(record.ack); this._acks.delete(record.ack); callback?.(); }
          } catch (error) { this.destroy(error); }
        }
      });
      this._child.stdout.on('data', data => { if (!this.push(data)) this._child.stdout.pause(); });
      this._child.stdout.on('end', () => this.push(null));
      this._child.stdin.on('error', error => { if (!this.destroyed) this.destroy(this._decoderError || error); });
      this._child.on('error', error => this.destroy(error));
      this._child.on('close', (code, signal) => {
        children.delete(this._child);
        if (!this.destroyed && (code !== 0 || signal)) this.destroy(this._decoderError || new Error(`OBS HTTP decoder exited ${code ?? signal}`));
        const callback = this._finalCallback; this._finalCallback = null;
        callback?.(this.destroyed ? this._decoderError : undefined);
      });
    }
    _read() { this._child.stdout.resume(); }
    _send(type, data, callback) {
      const id = ++this._sequence;
      this._acks.set(id, callback);
      this._child.stdin.write(frame(type, id, data));
    }
    _write(chunk, encoding, callback) {
      if (chunk[flushOperation] !== undefined) {
        const data = Buffer.alloc(4); data.writeUInt32BE(chunk[flushOperation]);
        this._send(2, data, callback);
      } else {
        this.bytesWritten += chunk.length;
        this._send(0, chunk, callback);
      }
    }
    _final(callback) {
      this._finalCallback = callback;
      this._child.stdin.end(frame(1, ++this._sequence));
    }
    _destroy(error, callback) {
      this._decoderError ||= error;
      this._child.stdin.destroy(); this._child.stdout.destroy();
      if (this._child.exitCode === null && this._child.signalCode === null) this._child.kill();
      for (const cb of this._acks.values()) cb(error || Object.assign(new Error('HTTP decoder closed'), {code:'ERR_STREAM_DESTROYED'}));
      this._acks.clear();
      if (this._child.exitCode === null && this._child.signalCode === null) this._child.once('close', () => callback(error));
      else callback(error);
    }
    flush(kind, callback) {
      if (typeof kind === 'function') { callback = kind; kind = undefined; }
      if (this.writableFinished) { if (callback) queueMicrotask(callback); return; }
      const chunk = Buffer.alloc(0);
      chunk[flushOperation] = kind ?? zlib.constants.BROTLI_OPERATION_FLUSH;
      this.write(chunk, callback);
    }
    reset() { this.bytesWritten = 0; this._send(3, Buffer.alloc(0), () => {}); }
    close(callback) { if (callback) this.once('close', callback); this.destroy(); }
  }
  function BrotliDecompress(options) { return new FixedBrotliDecompress(options); }
  BrotliDecompress.prototype = FixedBrotliDecompress.prototype;
  Object.setPrototypeOf(BrotliDecompress, FixedBrotliDecompress);
  const createBrotliDecompress = options => new FixedBrotliDecompress(options);
  function brotliDecompress(buffer, options, callback) {
    if (typeof options === 'function') { callback = options; options = {}; }
    if (typeof callback !== 'function') throw Object.assign(new TypeError('The "callback" argument must be a function'), {code:'ERR_INVALID_ARG_TYPE'});
    const input = inputBytes(buffer), stream = createBrotliDecompress(options);
    const chunks = []; let size = 0, called = false;
    const done = (error, result) => { if (!called) { called = true; callback(error, result); } };
    stream.on('data', chunk => {
      size += chunk.length;
      if (options?.maxOutputLength !== undefined && size > options.maxOutputLength) stream.destroy(Object.assign(new RangeError('Cannot create a Buffer larger than maxOutputLength'), {code:'ERR_BUFFER_TOO_LARGE'}));
      else chunks.push(chunk);
    });
    stream.once('error', error => done(error));
    stream.once('end', () => { const result = Buffer.concat(chunks); done(null, options?.info ? {buffer:result, engine:stream} : result); });
    stream.end(input);
  }
  function brotliDecompressSync(buffer, options = {}) {
    const input = inputBytes(buffer);
    const result = cp.spawnSync(node, [worker, 'sync', JSON.stringify(options)], {input, env:workerEnv, maxBuffer:require('node:buffer').constants.MAX_LENGTH});
    if (result.error) throw result.error;
    if (result.status !== 0) {
      let error;
      try { error = restoreError(JSON.parse(result.stderr.toString())); } catch (_) { error = new Error(`OBS HTTP decoder exited ${result.status ?? result.signal}`); }
      throw error;
    }
    return options.info ? {buffer:result.stdout, engine:{bytesWritten:input.length}} : result.stdout;
  }
  Object.assign(zlib, {BrotliDecompress, createBrotliDecompress, brotliDecompress, brotliDecompressSync});
  require('node:module').syncBuiltinESMExports();

  function decoders(headers) {
    const encoding = headers.get('content-encoding');
    if (!encoding) return [];
    const factories = encoding.split(',').map(x => x.trim().toLowerCase()).reverse().map(name => {
      if (name === 'br') return createBrotliDecompress;
      if (name === 'gzip' || name === 'x-gzip') return zlib.createGunzip;
      if (name === 'deflate') return zlib.createInflate;
      if (name === 'identity') return null;
      return undefined;
    });
    // Unknown encodings remain raw, as native fetch does; never decode only half a chain.
    return factories.includes(undefined) ? [] : factories.filter(Boolean);
  }
  function decodedStream(source, factories, signal) {
    const streams = [source, ...factories.map(factory => factory())];
    const output = streams.at(-1);
    const abort = () => { for (const stream of streams) stream.destroy(signal.reason || new DOMException('The operation was aborted.', 'AbortError')); };
    if (streams.length > 1) pipeline(...streams, () => {}); // propagate errors/cancel to every input/child.
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, {once:true});
    }
    output.once('close', () => signal?.removeEventListener('abort', abort));
    return output;
  }
  function decodedHeaders(source, HeadersClass) {
    const headers = new HeadersClass(source);
    for (const name of ['content-encoding','content-length','transfer-encoding','trailer']) headers.delete(name);
    return headers;
  }
  const nativeBody = Object.getOwnPropertyDescriptor(Response.prototype, 'body').get;
  function metadata(response, original, nodeBody = false) {
    if (nodeBody && nativeBody.call(response)) {
      // Bun's builtin _ReadableFromWeb loses rejected reads on abort. Keep
      // acquisition lazy (clone/text must still work) and handle reader failures.
      let reader, reading = false;
      const body = new Readable({
        read() {
          if (reading) return;
          reading = true;
          try { reader ||= nativeBody.call(response).getReader(); }
          catch (error) { reading = false; this.destroy(error); return; }
          reader.read().then(({done,value}) => {
            reading = false;
            if (done) this.push(null); else this.push(Buffer.from(value));
          }, error => { reading = false; this.destroy(error); });
        },
        destroy(error, callback) {
          let cancelled;
          try { cancelled = reader ? reader.cancel(error) : nativeBody.call(response).cancel(error); }
          catch (_) { callback(error); return; }
          Promise.resolve(cancelled).then(() => callback(error), () => callback(error));
        },
      });
      Object.defineProperty(response, 'body', {value:body, configurable:true});
    }
    for (const name of ['url','redirected','type']) Object.defineProperty(response, name, {value:original[name], configurable:true});
    const clone = response.clone.bind(response);
    Object.defineProperty(response, 'clone', {value:() => metadata(clone(), original, nodeBody), configurable:true});
    return response;
  }
  const nativeFetch = globalThis.fetch;
  // INVARIANT: no response body reaches native fetch's auto-decoder, even when the
  // server ignores Accept-Encoding. Redirect/TLS/auth/request/cancel policy stays native.
  // Projects/Personal Projects/OBS Platform/CLI Runtime/OBS Touchpoints/Bundled Bun HTTP Decoding/Overview.md
  globalThis.fetch = async function fixedDecoderFetch(input, init) {
    const response = await nativeFetch(input, {...init, decompress:false});
    const factories = decoders(response.headers);
    if (!response.body || !factories.length) return response;
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    const body = Readable.toWeb(decodedStream(Readable.fromWeb(response.body), factories, signal));
    return metadata(new Response(body, {status:response.status, statusText:response.statusText, headers:decodedHeaders(response.headers, Headers)}), response);
  };
  // Bun's builtin node-fetch has its own immutable Bun.fetch alias; replace its
  // module entry BEFORE Gaxios captures it, retaining Node-readable Response APIs.
  const originalModule = require('node-fetch');
  const nativeNodeFetch = originalModule.default || originalModule;
  const fixedNodeFetch = async function(input, init) {
    const signal = init?.signal ?? input?.signal;
    const controller = new AbortController();
    const abortHeaders = () => controller.abort(signal.reason);
    if (signal?.aborted) abortHeaders();
    else signal?.addEventListener('abort', abortHeaders, {once:true});
    let response;
    try { response = await nativeNodeFetch(input, {...init, signal:controller.signal, decompress:false}); }
    finally { signal?.removeEventListener('abort', abortHeaders); }
    const factories = decoders(response.headers);
    const rawBody = nativeBody.call(response);
    if (!rawBody) return response;
    const source = Readable.fromWeb(rawBody);
    return metadata(new originalModule.Response(decodedStream(source, factories, signal), {
      status:response.status, statusText:response.statusText,
      headers:factories.length ? decodedHeaders(response.headers, originalModule.Headers) : response.headers,
      url:response.url, size:response.size, counter:response.counter, highWaterMark:response.highWaterMark,
    }), response, true);
  };
  for (const name of Object.getOwnPropertyNames(originalModule)) {
    if (!['length','name','prototype','caller','arguments','default'].includes(name)) Object.defineProperty(fixedNodeFetch, name, Object.getOwnPropertyDescriptor(originalModule, name));
  }
  Object.defineProperty(fixedNodeFetch, 'default', {value:fixedNodeFetch, configurable:true});
  const moduleKey = require.resolve('node-fetch');
  require.cache[moduleKey] = {id:moduleKey, filename:moduleKey, loaded:true, exports:fixedNodeFetch};
  const NativeDecompressionStream = globalThis.DecompressionStream;
  globalThis.DecompressionStream = class extends NativeDecompressionStream {
    constructor(format) {
      if (format !== 'brotli') { super(format); return; }
      // The native Brotli constructor is deliberately never instantiated.
      const pair = Duplex.toWeb(createBrotliDecompress());
      return Object.create(new.target.prototype, {readable:{value:pair.readable}, writable:{value:pair.writable}});
    }
  };
  globalThis[Symbol.for('obs.bun.transport')] = {decoderSha256:actual};
}

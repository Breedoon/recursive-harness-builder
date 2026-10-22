// Benign test-only routing/observation. No decoder replacement or mitigation.
const fs = require('node:fs');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const https = require('node:https');
const http = require('node:http');
const file = process.env.BUN_PROBE_TRACE;
const port = process.env.BUN_PROBE_PORT;
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const event = (kind, data = {}) => fs.appendFileSync(file, JSON.stringify({kind, pid:process.pid, ...data})+'\n');
const originalFetch = globalThis.fetch;
function route(input) {
  const u = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
  if (u.hostname === '127.0.0.1') return {target:u.href, origin:u.pathname, kind:'fixture'};
  const kind = u.hostname === 'mcp.exa.ai' ? 'mcp' : u.pathname === '/api/web/domain_info' ? 'direct' : u.hostname === 'fixture.obs.test' ? 'webfetch' : 'bootstrap';
  return {target:`https://127.0.0.1:${port}/${kind}`, origin:`${u.hostname}${u.pathname}`, kind};
}
event('preload', {bun:process.versions.bun, node:process.versions.node, argv_flags:process.argv.filter(x=>x.startsWith('--')), native_fetch_source:Function.prototype.toString.call(originalFetch), zlib_exports:Object.keys(zlib).filter(x=>/Brotli|brotli/.test(x))});
globalThis.fetch = async function observedNativeFetch(input, init) {
  const r = route(input);
  const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
  headers.set('accept-encoding','identity');
  headers.set('x-bun-probe-path',r.kind);
  const mapped = input instanceof Request ? new Request(r.target,input) : r.target;
  event('fetch_request',{origin:r.origin, path:r.kind, method:init?.method || (input instanceof Request ? input.method:'GET'), decompress:init?.decompress ?? 'default'});
  const response = await originalFetch(mapped,{...init,headers});
  event('fetch_response',{origin:r.origin,path:r.kind,status:response.status,encoding:response.headers.get('content-encoding'),content_type:response.headers.get('content-type')});
  if (r.kind !== 'bootstrap') response.clone().arrayBuffer().then(x=>event('fetch_body',{origin:r.origin,path:r.kind,bytes:x.byteLength,sha256:sha(Buffer.from(x))})).catch(e=>event('fetch_observer_error',{path:r.kind,error:e.name}));
  return response;
};
for (const name of ['createBrotliDecompress','brotliDecompress','brotliDecompressSync','BrotliDecompress']) {
  const original = zlib[name];
  if (typeof original !== 'function') continue;
  const descriptor = Object.getOwnPropertyDescriptor(zlib,name);
  event('zlib_descriptor',{name,writable:descriptor.writable,configurable:descriptor.configurable});
  if (name !== 'createBrotliDecompress') continue;
  zlib[name] = function observedOriginalBrotli(...args) {
    event('original_zlib_decoder_create',{name,stack:new Error().stack.split('\n').slice(1,8)});
    const stream = Reflect.apply(original,this,args);
    const input=[]; const output=[];
    const write=stream.write; const end=stream.end;
    stream.write=function(chunk,...rest){if(chunk)input.push(Buffer.from(chunk));return Reflect.apply(write,this,[chunk,...rest]);};
    stream.end=function(chunk,...rest){if(chunk && typeof chunk!=='function')input.push(Buffer.from(chunk));return Reflect.apply(end,this,[chunk,...rest]);};
    const emit=stream.emit;
    stream.emit=function(type,...args){
      if(type==='data')output.push(Buffer.from(args[0]));
      if(type==='end')event('original_zlib_decoder_end',{input_bytes:Buffer.concat(input).length,input_sha256:sha(Buffer.concat(input)),output_bytes:Buffer.concat(output).length,output_sha256:sha(Buffer.concat(output))});
      return Reflect.apply(emit,this,[type,...args]);
    };
    return stream;
  };
  event('zlib_observer_installed',{name,installed:zlib[name]!==original});
}
try { require('node:module').syncBuiltinESMExports(); event('esm_sync',{ok:true}); } catch(e) { event('esm_sync',{ok:false,error:e.name}); }
for (const [module, protocol] of [[https,'https:'],[http,'http:']]) {
  const request=module.request;
  module.request=function observedNativeRequest(input, options, callback){
    let o;
    if(typeof input==='string' || input instanceof URL){o={...require('node:url').urlToHttpOptions(new URL(input)),...(typeof options==='object'?options:{})};}
    else o={...input};
    const cb=typeof options==='function'?options:callback;
    const originHost=String(o.hostname || o.host || '').replace(/:\d+$/,'');
    const path=String(o.path || '/');
    if(originHost==='127.0.0.1' && Number(o.port)===Number(port))return Reflect.apply(request,this,[o,cb]);
    const kind=originHost==='mcp.exa.ai'?'mcp':path.startsWith('/api/web/domain_info')?'direct':originHost==='fixture.obs.test'?'webfetch':'bootstrap';
    event('node_http_request',{module:protocol,origin:originHost+path.split('?')[0],path:kind,method:o.method || 'GET'});
    // Redirect the real application's HTTPS request to the isolated TLS fixture.
    // Original node:http/https and original zlib still perform all network/decode work.
    const mapped={...o,protocol,hostname:'127.0.0.1',host:'127.0.0.1',servername:'fixture.obs.test',port,path:`/${kind}`,headers:{...o.headers,'Accept-Encoding':'identity','x-bun-probe-path':kind}};
    return Reflect.apply(request,this,[mapped,cb]);
  };
}
if (!process.argv.includes('--version')) {
  Promise.resolve().then(async()=>{
    const url=`https://127.0.0.1:${port}/control`;
    for(const decompress of [false,true]){
      const r=await originalFetch(url,{headers:{'accept-encoding':'identity'},decompress});
      const bytes=Buffer.from(await r.arrayBuffer());
      event('native_fetch_positive_control',{decompress,encoding:r.headers.get('content-encoding'),bytes:bytes.length,sha256:sha(bytes)});
    }
  }).catch(e=>event('native_control_error',{error:e.name,message:e.message}));
}

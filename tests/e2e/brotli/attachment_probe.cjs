// Supporting attachment/runtime feasibility only; real CLI path proof is separate.
if(process.argv.includes('--help')){console.log('Benign attachment and valid-format preservation probe; no work in help mode');process.exit(0);}
const fixedNode=process.env.BUN_PROBE_FIXED_NODE || process.env.OBS_BUN_DECODER_NODE;
const http=require('node:http'), zlib=require('node:zlib'), cp=require('node:child_process');
const originalFetch=globalThis.fetch;
const plain=Buffer.from('BUN_ATTACHMENT_VALID_BODY\n');
const br=zlib.brotliCompressSync(plain);
const result={bun:process.versions.bun,node:process.versions.node,cases:[]};
const server=http.createServer((req,res)=>{
  if(req.url==='/redirect'){res.writeHead(302,{'Location':'/body','content-encoding':'br'});res.end(br);return;}
  if(req.url==='/raw-deflate'){const raw=zlib.deflateRawSync(plain);res.writeHead(200,{'content-encoding':'deflate','content-length':raw.length});res.write(raw.subarray(0,1));setTimeout(()=>res.end(raw.subarray(1)),80);return;}
  const status=req.url==='/error'?418:200;
  res.writeHead(status,{'content-type':req.url==='/sse'?'text/event-stream':'text/plain','content-encoding':'br','content-length':br.length});res.end(br);
});
const record=(name,value)=>result.cases.push({name,...value});
server.listen(0,'127.0.0.1',async()=>{
  const url=`http://127.0.0.1:${server.address().port}`;
  try{
    for(const path of ['/body','/redirect','/error','/sse']){
      const r=await originalFetch(url+path,{decompress:false,headers:{'accept-encoding':'identity'}});
      const raw=Buffer.from(await r.arrayBuffer());
      const decoded=cp.spawnSync(fixedNode,['-e',"process.stdin.pipe(require('node:zlib').createBrotliDecompress()).pipe(process.stdout)"],{input:raw,env:{PATH:'/usr/bin:/bin'}});
      const h=new Headers(r.headers);h.delete('content-encoding');h.delete('content-length');
      const rebuilt=new Response(decoded.stdout,{status:r.status,statusText:r.statusText,headers:h});
      for(const key of ['url','redirected','type'])Object.defineProperty(rebuilt,key,{value:r[key],configurable:true});
      record(path,{raw_equal:raw.equals(br),node_decode_exit:decoded.status,external_decode_equal:decoded.stdout.equals(plain),status:r.status,url_preserved:rebuilt.url===r.url,redirect_preserved:rebuilt.redirected===r.redirected,type_preserved:rebuilt.type===r.type,no_stale_encoding:!rebuilt.headers.has('content-encoding'),no_stale_length:!rebuilt.headers.has('content-length')});
    }
    const controller=new AbortController();controller.abort();
    try{await originalFetch(url+'/body',{decompress:false,signal:controller.signal});record('abort',{honored:false});}catch(e){record('abort',{honored:e.name==='AbortError',error:e.name});}
    let count=0;globalThis.fetch=(...args)=>{count++;return originalFetch(...args);};
    let nf;
    try{
      nf=require('node-fetch');const impl=nf.default||nf;
      const text=await(await impl(url+'/body')).text();
      const descriptor=Object.getOwnPropertyDescriptor(nf,'default');
      record('node-fetch',{uses_global_fetch:count>0,decoded:text===plain.toString(),module_type:typeof nf,es_module:!!nf.__esModule,default_descriptor:descriptor?{writable:descriptor.writable,configurable:descriptor.configurable}:null,source:Function.prototype.toString.call(impl).slice(0,400)});
      for(const options of [{decompress:false},{compress:false},{compress:false,decompress:false}]){
        const r=await impl(url+'/body',options);const raw=Buffer.from(await r.arrayBuffer());
        record('node-fetch-raw',{options,raw_equal:raw.equals(br),decoded:raw.equals(plain),body_type:r.body?.constructor?.name});
      }
    }catch(e){record('node-fetch',{error:e.message});}
    globalThis.fetch=originalFetch;
    record('Bun.fetch',{type:typeof Bun.fetch,same_original:Bun.fetch===originalFetch,descriptor:Object.getOwnPropertyDescriptor(Bun,'fetch')?{writable:Object.getOwnPropertyDescriptor(Bun,'fetch').writable,configurable:Object.getOwnPropertyDescriptor(Bun,'fetch').configurable}:null});
    try{
      const d=new DecompressionStream('brotli');const writer=d.writable.getWriter();const read=new Response(d.readable).arrayBuffer();await writer.write(br);await writer.close();record('DecompressionStream-brotli',{supported:true,decoded:Buffer.from(await read).equals(plain),global_descriptor:{writable:Object.getOwnPropertyDescriptor(globalThis,'DecompressionStream').writable,configurable:Object.getOwnPropertyDescriptor(globalThis,'DecompressionStream').configurable}});
    }catch(e){record('DecompressionStream-brotli',{supported:false,error:e.message});}
    const compatibility=[];
    for(const [name,fetcher,path,options,expected] of [
      ['native raw-deflate',Bun.fetch,'/raw-deflate',{},plain],
      ['adapted raw-deflate',originalFetch,'/raw-deflate',{},plain],
      ['node-fetch raw-deflate',nf.default||nf,'/raw-deflate',{},plain],
      ['adapted explicit no-decode',originalFetch,'/body',{decompress:false},br],
      ['node-fetch explicit no-decode',nf.default||nf,'/body',{decompress:false},br]]){
      const response=await fetcher(url+path,options);const bytes=Buffer.from(await response.arrayBuffer());
      compatibility.push({name,pass:bytes.equals(expected)&&(options.decompress!==false || response.headers.get('content-encoding')==='br')});
    }
    const info=zlib.brotliDecompressSync(br,{info:true});
    compatibility.push({name:'sync info engine contract',pass:info.buffer.equals(plain)&&info.engine instanceof zlib.BrotliDecompress&&info.engine.constructor===zlib.BrotliDecompress});
    result.compatibility=compatibility;result.pass=compatibility.every(row=>row.pass);
    if(!result.pass)throw new Error('Valid-format/API preservation regression');
    record('fixed-decoder-identity',{executable:fixedNode,versions:JSON.parse(cp.spawnSync(fixedNode,['-p','JSON.stringify(process.versions)'],{env:{PATH:'/usr/bin:/bin'},encoding:'utf8'}).stdout)});
  }catch(e){result.error={name:e.name,message:e.message};}
  server.close(()=>{console.log(JSON.stringify(result));process.exit(result.error?1:0);});
});
setTimeout(()=>{result.timeout=true;console.log(JSON.stringify(result));process.exit(2);},15000).unref();

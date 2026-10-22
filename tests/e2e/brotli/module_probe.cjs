// Test-only loader attachment probe. No network and no production mutation.
const result={bun:process.versions.bun,cases:[]};
const original=require('node-fetch');
const sentinel=async()=>new Response('MODULE_SENTINEL');
const key=require.resolve('node-fetch');
const saved=require.cache[key];
try{
  require.cache[key]={id:key,filename:key,loaded:true,exports:sentinel};
  const next=require('node-fetch');
  result.cases.push({name:'require-cache',key,replaced:next===sentinel,export_type:typeof next});
  if(saved)require.cache[key]=saved;else delete require.cache[key];
}catch(e){result.cases.push({name:'require-cache',error:e.message});}
try{
  const Module=require('node:module'), load=Module._load;
  Module._load=function(name,...args){if(name==='node-fetch')return sentinel;return load.call(this,name,...args);};
  result.cases.push({name:'Module._load',replaced:require('node-fetch')===sentinel});
  Module._load=load;
}catch(e){result.cases.push({name:'Module._load',error:e.message});}
try{
  const {mock}=require('bun:test');
  mock.module('node-fetch',()=>({default:sentinel}));
  const next=require('node-fetch');
  result.cases.push({name:'mock.module',replaced:(next.default||next)===sentinel,export_type:typeof next});
}catch(e){result.cases.push({name:'mock.module',error:e.message});}
result.cases.push({name:'zlib-alias',same:require('zlib')===require('node:zlib')});
console.log(JSON.stringify(result));

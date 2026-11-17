const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function setup() {
  const elements = new Map();
  let created = 0;
  const el = (s) => {
    if (!elements.has(s)) elements.set(s, { hidden:false, children:[], value:'', dataset:{}, style:{}, textContent:'', innerHTML:'', disabled:false,
      listeners:{}, classList:{ toggle(){},add(){},remove(){},contains(){return true;} }, addEventListener(name,fn){this.listeners[name]=fn;}, removeAttribute(k){delete this[k];}, append(...nodes){this.children.push(...nodes);}, prepend(...nodes){this.children.unshift(...nodes);}, querySelector(selector){return el(s+' '+selector);}, querySelectorAll(){return [];} });
    return elements.get(s);
  };
  const sources = [];
  class ES { constructor(url){this.url=url; sources.push(this);} close(){this.closed=true;} }
  const context = vm.createContext({ document:{querySelector:el,querySelectorAll:()=>[],createElement:(tag)=>Object.assign(el('created-'+(++created)),{tagName:tag.toUpperCase()}),body:el('body'),addEventListener(){}},
    EventSource:ES, fetch:async()=>({ok:true,json:async()=>[]}), FormData:class{constructor(){this.entries=[];} append(k,v){this.entries.push([k,v]);} get(k){return this.entries.find(x=>x[0]===k)?.[1]??null;} getAll(k){return this.entries.filter(x=>x[0]===k).map(x=>x[1]);}},setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){},
    location:{hash:'',pathname:'/',search:''},history:{replaceState(_a,_b,url){context.location.hash = url.includes('#') ? url.slice(url.indexOf('#')) : '';}},
    window:{addEventListener(){}},alert(){},confirm:()=>true,console,Date,URLSearchParams,encodeURIComponent });
  let source=fs.readFileSync('frontend/app.js','utf8').split('/* ---------- 启动 ---------- */')[0];
  vm.runInContext(source,context);
  return {run:(s)=>vm.runInContext(s,context),el,sources,context};
}
test('new workflow stages distinguish answer review from OCR location',()=>{
  const s=setup();
  const stages=JSON.parse(s.run('JSON.stringify(STEPS.map(x=>x[0]))'));
  assert.ok(stages.includes('answer_review'));
  assert.ok(stages.includes('locate'));
  assert.ok(stages.indexOf('answer_review')<stages.indexOf('grade'));
  assert.ok(stages.indexOf('ocr')<stages.indexOf('parse'));
});
test('hook activity cannot downgrade a completed stage',()=>{
  const s=setup();
  s.run('stageState={grade:{status:"done",t:10}};applyJobEvent({id:9,stage:"grade",status:"info",kind:"hook",t:11,message:"main: get_state"})');
  assert.equal(s.run('stageState.grade.status'),'done');
});
test('manual positioning switches to the student page and original image',()=>{
  const s=setup();
  s.run('cur={id:"sub_test",page_count:2,questions:[{id:"7",page:2,status:"correct"}]};beginManualPosition("7")');
  assert.equal(s.run('page'),2);
  assert.equal(s.run('kind'),'raw');
  assert.match(s.el('#positionHint').textContent,/7/);
  assert.equal(s.el('#manualPositionTools').hidden,false);
  s.run('cancelManualPosition()');
  assert.equal(s.el('#manualPositionTools').hidden,true);
});
test('preparation draft sends mode without student archive fields',async()=>{
  const s=setup(), uploads=[];
  s.context.fetch=async(url,opt={})=>{
    if(opt.method==='POST') { uploads.push(opt.body); throw new Error('keep draft'); }
    return {ok:true,json:async()=>[]};
  };
  s.run('uploadMode="prepare";stageFiles([{name:"paper.png",size:10}],"question_paper")');
  s.el('#uStudent').value='stale-student';s.el('#uClass').value='stale-class';
  await s.run('uploadFromHome()');
  assert.equal(uploads[0].get('mode'),'prepare');
  assert.equal(uploads[0].get('student_id'),null);
  assert.equal(uploads[0].get('class_id'),null);
});
test('selected saved paper and preparation mode survive draft switching',()=>{
  const s=setup();
  s.run('uploadMode="prepare";selectedPaperId="paper_saved";stageFiles([{name:"one.png",size:10}]);saveUploadDraft();uploadMode="grade";selectedPaperId="";restoreUploadDraft(activeDraftId)');
  assert.equal(s.run('uploadMode'),'prepare');
  assert.equal(s.run('selectedPaperId'),'paper_saved');
  assert.equal(s.el('#startUpload').textContent,'开始建库');
});
test('paper detail defaults to usable answers with an explicit edit button',async()=>{
  const s=setup();
  const paper={id:'paper_test',title:'Quiz',questions:[{id:'1',question:'12+8='}],answers:[{id:'1',answer:'20',teacher_answer:'21',review_status:'review',review_reason:'冲突'}]};
  s.context.fetch=async(url)=>({ok:true,json:async()=>url==='/api/papers'?[]:paper});
  await s.run('openPaper("paper_test")');
  assert.match(s.el('#paperDetail').innerHTML,/默认直接使用/);
  assert.match(s.el('#paperDetail').innerHTML,/21/);
  assert.match(s.el('#paperDetail').innerHTML,/编辑答案/);
  assert.match(s.el('#paperDetail').innerHTML,/readonly/);
  assert.doesNotMatch(s.el('#paperDetail').innerHTML,/data-confirm|待人工确认|使用前需审核/);
});
test('library edit can be cancelled without saving and only saves on explicit click',async()=>{
  const s=setup(), writes=[];
  const paper={id:'paper_test',title:'Quiz',questions:[{id:'1',question:'1+1='}],answers:[{id:'1',answer:'2',knowledge:'加法'}]};
  const inputs=[{dataset:{id:'1',f:'answer'},value:'2',readOnly:true},{dataset:{id:'1',f:'knowledge'},value:'加法',readOnly:true}];
  s.el('#paperDetail').querySelectorAll=()=>inputs;
  s.context.fetch=async(url,options={})=>{
    if(options.method==='PUT') { writes.push(JSON.parse(options.body)); return {ok:true,json:async()=>paper}; }
    return {ok:true,json:async()=>url==='/api/papers'?[]:paper};
  };
  await s.run('openPaper("paper_test")');
  s.el('#editAnswers').onclick();
  assert.equal(inputs[0].readOnly,false);
  assert.equal(s.el('#saveAns').hidden,false);
  inputs[0].value='3'; s.el('#cancelAnswers').onclick();
  assert.equal(inputs[0].value,'2'); assert.equal(inputs[0].readOnly,true);
  assert.equal(writes.length,0);
  s.el('#editAnswers').onclick(); inputs[0].value='4';
  await s.el('#saveAns').onclick();
  assert.equal(writes[0].answers[0].answer,'4');
  assert.match(s.el('#ansMsg').textContent,/已保存/);
});
test('library edit keeps changes after a save failure',async()=>{
  const s=setup();
  const paper={id:'paper_test',title:'Quiz',questions:[{id:'1',question:'1+1='}],answers:[{id:'1',answer:'2'}]};
  const input={dataset:{id:'1',f:'answer'},value:'2',readOnly:true};
  s.el('#paperDetail').querySelectorAll=()=>[input];
  s.context.fetch=async(url,options={})=>{
    if(options.method==='PUT') throw new Error('网络错误');
    return {ok:true,json:async()=>url==='/api/papers'?[]:paper};
  };
  await s.run('openPaper("paper_test")');
  s.el('#editAnswers').onclick(); input.value='3';
  await s.el('#saveAns').onclick();
  assert.equal(input.value,'3'); assert.equal(input.readOnly,false);
  assert.equal(s.el('#saveAns').disabled,false);
  assert.match(s.el('#ansMsg').textContent,/保存失败/);
});
test('running job raw preview uses active job ID before result exists',()=>{
  const s=setup();s.run('cur = null; activeJob = {id:"running-a",page_count:2}; page=1; kind="raw"; drawPage()');
  assert.match(s.el('#pageImg').src,/running-a\/pages\/1\?kind=raw/);
  assert.equal(s.el('#showAnn').disabled,true);
});
test('leaving workbench ignores late messages from a closed subscription',async()=>{
  const s=setup();
  await s.run('watch("first", "first.pdf")');
  const old=s.sources[0];assert.ok(old);
  s.run('show_("settings")');
  const before=s.el('#paperWait').textContent;
  old.onmessage({data:JSON.stringify({id:4,stage:'error',status:'error',message:'late'})});
  assert.equal(s.el('#paperWait').textContent,before);
  assert.equal(old.closed,true);
});
test('stream output preserves completed status and deduplicates replay IDs',()=>{
  const s=setup();
  s.run('stageState = {}; seenEvents = new Set(); applyJobEvent({id:1,stage:"parse",status:"done",kind:"status",message:"完成"}); applyJobEvent({id:2,stage:"parse",status:"output",kind:"output",text:"abc"}); applyJobEvent({id:2,stage:"parse",status:"output",kind:"output",text:"abc"})');
  assert.equal(s.run('stageState.parse.status'),'done');
  assert.equal(s.run('stageState.parse.outputs.get("").text'),'abc');
});
test('thinking activity appears once with ellipsis even through historical replay',()=>{
  const s=setup();
  s.run('applyJobEvent({id:1,stage:"process",status:"start",kind:"status",message:"开始"});applyJobEvent({id:2,stage:"process",kind:"activity",message:"模型正在思考"});applyJobEvent({id:3,stage:"process",kind:"activity",message:"模型正在思考...",activity_type:"thinking"});applyJobEvent({id:4,stage:"process",kind:"activity",message:"正在读取原图"});applyJobEvent({id:5,stage:"process",kind:"activity",message:"模型正在思考"})');
  assert.equal(s.run('stageState.process.logs.filter(x=>x.startsWith("模型正在思考")).length'),1);
  assert.equal(s.run('stageState.process.logs[1]'),'模型正在思考...');
  assert.equal(s.run('stageState.process.status'),'start');
  assert.equal(s.run('stageState.process.logs[2]'),'正在读取原图');
});
test('grading route survives reload and safely decodes submission IDs',()=>{
  const s=setup();assert.equal(s.run('gradingRoute("#grading/test-123")'),'test-123');
  assert.equal(s.run('gradingRoute("#settings")'),null);
  assert.equal(s.run('gradingRoute("#grading/%ZZ")'),null);
});
test('delayed status response cannot open an abandoned grading route',async()=>{
  const s=setup();let resolve;
  s.context.fetch=()=>new Promise((r)=>{resolve=r;});
  const opening=s.run('watch("slow", "slow.pdf")');
  s.run('show_("settings")');
  resolve({ok:true,json:async()=>({id:'slow',filename:'late.pdf',status:'running',page_count:1})});
  await opening;
  assert.equal(s.sources.length,0);assert.notEqual(s.el('#dTitle').textContent,'late.pdf');
});
test('old replay status cannot downgrade a completed snapshot',()=>{
  const s=setup();s.run('stageState={parse:{status:"done",t:20,message:"完成"}};applyJobEvent({id:1,stage:"parse",status:"start",t:10,message:"开始"})');
  assert.equal(s.run('stageState.parse.status'),'done');assert.equal(s.run('stageState.parse.logs[0]'),'开始');
});
test('leaving page before upload completes does not force navigation back',async()=>{
  const s=setup();let resolve;
  s.context.fetch=()=>new Promise((r)=>{resolve=r;});
  const uploading=s.run('uploadFromHome([{name:"one.pdf"}])');
  s.run('show_("settings")');
  resolve({ok:true,json:async()=>({id:'uploaded',cached:false})});
  await uploading;
  assert.equal(s.context.location.hash,'');
});
test('stream stage start and finish keep elapsed time without snapshot refresh',()=>{
  const s=setup();s.run('stageState={};applyJobEvent({id:1,stage:"parse",status:"start",t:100,message:"开始"});applyJobEvent({id:2,stage:"parse",status:"done",t:105,message:"完成"})');
  assert.equal(s.run('stageState.parse.elapsed_s'),5);
});
test('failed job marks its active stage as failed while retaining its logs',()=>{
  const s=setup();s.run('stageState={parse:{status:"start",logs:["已经读取第 1 页"]}};showJobFailure("调用失败")');
  assert.equal(s.run('stageState.parse.status'),'error');
  assert.equal(s.run('stageState.parse.logs[0]'),'已经读取第 1 页');
});
test('available model metrics expose first output, queue time and token usage',()=>{
  const s=setup();s.run('renderMetrics({elapsed_s:8,queue_s:2,model:"m",model_calls:[{stage:"parse",model:"m",elapsed_s:6,queue_s:1,first_output_s:0.8,usage:{input_tokens:120,output_tokens:40}}]})');
  const html=s.el('#jobMetrics').innerHTML;
  assert.match(html,/首字 0.8s/);assert.match(html,/输入 120/);assert.match(html,/输出 40/);assert.match(html,/排队 1.0s/);
});
test('late assignment response cannot replace a newly opened result',async()=>{
  const s=setup();let resolve;
  s.context.fetch=()=>new Promise((r)=>{resolve=r;});
  s.run('cur={id:"old"}');
  const assigning=s.el('#dStudent').onchange({target:{value:'student'}});
  s.run('stopWatching();cur={id:"new"}');
  resolve({ok:true,json:async()=>({id:'old',student_id:'student'})});
  await assigning;assert.equal(s.run('cur.id'),'new');
});
function taskHarness() {
  const s=setup(), deferred=[];
  s.context.fetch=async(url,opt={})=>{
    if (opt.method==='POST' || opt.method==='DELETE') return new Promise((resolve,reject)=>deferred.push({resolve,reject,url}));
    const value=url.startsWith('/api/tasks/') ? {id:url.split('/').at(-1),title:url.split('/').at(-1),class_name:'班级',class_id:'class',rows:[],unassigned:[]} : [];
    return {ok:true,json:async()=>value};
  };
  return {...s,deferred};
}
for (const fail of [false,true]) test(`late task upload ${fail?'failure':'success'} cannot overwrite another task notice`,async()=>{
  const s=taskHarness();await s.run('openTask("A")');
  await s.el('#tFile').onchange({target:{files:[{name:'a.pdf'}]}});
  const uploading=s.run('uploadFromHome()');
  await s.run('openTask("B")');s.el('#tErr').textContent='B 的提示';
  if(fail) s.deferred[0].reject(new Error('A 上传失败')); else s.deferred[0].resolve({ok:true,json:async()=>({id:'uploaded'})});
  await uploading;assert.equal(s.el('#tErr').textContent,'B 的提示');
});
test('late task assignment cannot reopen its original task',async()=>{
  const s=taskHarness(), assign={value:'student',dataset:{assign:'submission'}};
  s.el('#taskDetail').querySelectorAll=(sel)=>sel==='[data-assign]'?[assign]:[];
  await s.run('openTask("A")');const assigning=assign.onchange();
  await s.run('openTask("B")');const generation=s.run('viewGeneration');
  s.deferred[0].resolve({ok:true,json:async()=>({})});await assigning;
  assert.equal(s.run('viewGeneration'),generation);assert.match(s.el('#taskDetail').innerHTML,/<h1>B<\/h1>/);
});
test('late task deletion cannot navigate away from another task',async()=>{
  const s=taskHarness();await s.run('openTask("A")');const deleting=s.el('#tDel').onclick();
  await s.run('openTask("B")');const generation=s.run('viewGeneration');
  s.deferred[0].resolve({ok:true,json:async()=>({})});await deleting;
  assert.equal(s.run('viewGeneration'),generation);assert.match(s.el('#taskDetail').innerHTML,/<h1>B<\/h1>/);
});
test('slow task-home roster response cannot hide a newer task detail',async()=>{
  const s=taskHarness(), originalFetch=s.context.fetch;let resolve;
  s.context.fetch=(url,opt)=>url==='/api/classes' ? new Promise((r)=>{resolve=r;}) : originalFetch(url,opt);
  const showing=s.run('show("tasks")');await s.run('openTask("B")');
  resolve({ok:true,json:async()=>[]});await showing;
  assert.equal(s.el('#taskDetail').hidden,false);assert.equal(s.el('#taskHome').hidden,true);
});
test('late home upload failure cannot replace a newer home notice',async()=>{
  const s=setup();let reject;
  s.context.fetch=()=>new Promise((_r,j)=>{reject=j;});
  const uploading=s.run('uploadFromHome([{name:"A.pdf"}])');
  s.run('show_("settings");show_("work")');s.el('#uploadErr').textContent='B 的提示';
  reject(new Error('A failed'));await uploading;
  assert.equal(s.el('#uploadErr').textContent,'B 的提示');
});
test('late retry failure cannot replace another grading job notice',async()=>{
  const s=setup();let reject;
  s.context.fetch=()=>new Promise((_r,j)=>{reject=j;});
  s.run('activeJob={id:"A",filename:"A.pdf"}');const retrying=s.el('#retryJob').onclick();
  s.run('stopWatching();activeJob={id:"B"}');s.el('#jobNotice').textContent='B 的提示';
  reject(new Error('A retry failed'));await retrying;
  assert.equal(s.el('#jobNotice').textContent,'B 的提示');
});
test('stop button cancels active job once and offers retry',async()=>{
  const s=setup(), calls=[];let resolve;
  s.context.fetch=(url)=>{calls.push(url);return url.endsWith('/stop')?new Promise(r=>resolve=r):Promise.resolve({ok:true,json:async()=>[]});};
  s.run('activeJob={id:"stop-me",status:"running"};stageState={process:{status:"start",started_at:100,logs:["正在读图"]}};es=new EventSource("stream");syncStopButton()');
  assert.equal(s.el('#stopJob').hidden,false);
  const stopping=s.el('#stopJob').onclick();await s.el('#stopJob').onclick();
  assert.equal(s.el('#stopJob').disabled,true);
  assert.equal(calls.filter(x=>x.endsWith('/stop')).length,1);
  resolve({ok:true,json:async()=>({id:'stop-me',status:'interrupted',done:true,error:'任务已手动停止，可重试',finished_at:105})});
  await stopping;
  assert.equal(s.run('activeJob.status'),'interrupted');
  assert.equal(s.el('#stopJob').hidden,true);assert.equal(s.el('#retryJob').hidden,false);
  assert.equal(s.run('stageState.process.status'),'error');assert.equal(s.sources[0].closed,true);
});
test('late stop response cannot replace a different active job',async()=>{
  const s=setup();let resolve;
  s.context.fetch=url=>url.endsWith('/stop')?new Promise(r=>resolve=r):Promise.resolve({ok:true,json:async()=>[]});
  s.run('activeJob={id:"A",status:"running"}');const stopping=s.el('#stopJob').onclick();
  s.run('stopWatching();activeJob={id:"B",status:"running"};syncStopButton()');s.el('#jobNotice').textContent='B 的提示';
  resolve({ok:true,json:async()=>({id:'A',status:'interrupted',error:'已停止'})});await stopping;
  assert.equal(s.run('activeJob.id'),'B');assert.equal(s.el('#jobNotice').textContent,'B 的提示');
  assert.equal(s.el('#stopJob').hidden,false);assert.equal(s.el('#stopJob').disabled,false);
});
test('replaying stopped job history does not revive stop button',async()=>{
  const s=setup();s.context.fetch=async()=>({ok:true,json:async()=>({id:'stopped',filename:'work',status:'interrupted',done:true,error:'任务已手动停止',page_count:0,last_event_id:2})});
  await s.run('watch("stopped")');
  await s.sources[0].onmessage({data:JSON.stringify({id:1,stage:'process',status:'start',kind:'status',message:'开始'})});
  assert.equal(s.run('activeJob.status'),'interrupted');assert.equal(s.el('#stopJob').hidden,true);
});
test('late task creation does not reopen tasks or clear newer title input',async()=>{
  const s=taskHarness();s.el('#tClass').value='class';s.el('#tTitle').value='A';
  const creating=s.el('#taskForm').onsubmit({preventDefault(){}});
  s.run('show_("settings")');s.el('#tTitle').value='B';const generation=s.run('viewGeneration');
  s.deferred[0].resolve({ok:true,json:async()=>({id:'A'})});await creating;
  assert.equal(s.run('viewGeneration'),generation);assert.equal(s.el('#tTitle').value,'B');
});
for (const stage of ['parse','grade','review']) test(`${stage} output keeps interleaved model calls separate through replay`,()=>{
  const s=setup();
  const events=[
    {id:1,call_id:'__proto__',label:'第 1 页',text:'A1'},
    {id:2,call_id:'B',label:'第 2 页',text:'B1'},
    {id:3,call_id:'__proto__',label:'第 1 页',text:'A2'},
    {id:4,call_id:'B',label:'第 2 页',text:'B2'},
  ].map(e=>({...e,stage,kind:'output',status:'output'}));
  const apply=()=>s.run(`${JSON.stringify(events)}.forEach(applyJobEvent)`);
  const texts=()=>s.run(`JSON.stringify(Array.from(stageState.${stage}.outputs.values(), x=>[x.label,x.text]))`);
  apply();
  assert.equal(s.run(`stageState.${stage}.outputs?.size`),2);
  assert.equal(texts(),JSON.stringify([['第 1 页','A1A2'],['第 2 页','B1B2']]));
  apply();assert.equal(texts(),JSON.stringify([['第 1 页','A1A2'],['第 2 页','B1B2']]));
  s.run('stageState={};seenEvents=new Set()');apply();
  assert.equal(texts(),JSON.stringify([['第 1 页','A1A2'],['第 2 页','B1B2']]));
});
test('raw output panes retain expansion and literal text across chunks',()=>{
  const s=setup();s.el('#steps').children.push({});
  const node=s.el('#steps [data-stage="parse"]');
  s.run('applyJobEvent({id:1,stage:"parse",kind:"output",call_id:"A",label:"<b>页 1</b>",text:"<script>A1</script>"});renderSteps(stageState)');
  const raw=node.querySelector('.raw-output');
  assert.equal(raw.hidden,false);assert.equal(raw.children.length,1);
  const pane=raw.children[0];pane.open=true;raw.open=true;
  s.run('applyJobEvent({id:2,stage:"parse",kind:"output",call_id:"B",label:"页 2",text:"B1"});applyJobEvent({id:3,stage:"parse",kind:"output",call_id:"A",text:"A2"});renderSteps(stageState)');
  assert.equal(raw.children.length,2);assert.equal(raw.children[0],pane);
  assert.equal(pane.open,true);assert.equal(raw.open,true);
  assert.equal(pane.children[0].textContent,'<b>页 1</b>');
  assert.equal(pane.children[1].textContent,'<script>A1</script>A2');
  assert.equal(pane.children[1].innerHTML,'');
});
test('file selection, drop and answer material only stage files without requests',async()=>{
  const s=setup(), calls=[];
  s.context.fetch=async(...args)=>{calls.push(args);return {ok:true,json:async()=>({})};};
  s.el('#file').onchange({target:{files:[{name:'work.png',size:10}],value:'x'}});
  s.el('#drop').listeners.drop({preventDefault(){},dataTransfer:{files:[{name:'paper.pdf',size:10}]}});
  await s.el('#keyFile').onchange({target:{files:[{name:'key.pdf',size:10}],value:'x'}});
  assert.equal(calls.length,0);
  assert.equal(s.run('stagedFiles.length'),3);
  assert.equal(s.run('stagedFiles[2].role'),'answer_key');
  assert.match(s.el('#materialList').innerHTML,/work.png/);
  assert.equal(s.el('#startUpload').disabled,false);
});
test('start sends every material once with roles and selected student, while retaining later additions',async()=>{
  const s=setup(), uploads=[];
  s.context.fetch=async(url,opt={})=>{
    if(opt.method==='POST') return new Promise((resolve,reject)=>uploads.push({resolve,reject,body:opt.body}));
    return {ok:true,json:async()=>[]};
  };
  s.run('settingsCache={ocr_available:true};syncOcrSelectors();stageFiles([{name:"paper.pdf",size:10},{name:"work.png",size:20}]);setMaterialRole(stagedFiles[0].id,"question_paper")');
  s.el('#uStudent').value='student';s.el('#answerKey').value='1. A';
  const uploading=s.el('#startUpload').onclick();
  await s.el('#startUpload').onclick();
  assert.equal(uploads.length,1);assert.equal(uploads[0].body.getAll('files').length,2);
  assert.equal(uploads[0].body.get('file'),null);
  assert.equal(uploads[0].body.get('file_roles'),'["question_paper","auto"]');
  assert.equal(uploads[0].body.get('student_id'),'student');assert.equal(uploads[0].body.get('answer_key'),'1. A');
  assert.equal(uploads[0].body.get('ocr_mode'),'vision');
  s.run('stageFiles([{name:"next.png",size:5}]);show_("settings")');
  s.el('#answerKey').value='new answer';
  uploads[0].resolve({ok:true,json:async()=>({id:'bundle',cached:false})});await uploading;
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'next.png');
  assert.equal(s.el('#answerKey').value,'new answer');
});
test('failed start preserves materials, roles and answer text for retry',async()=>{
  const s=setup();s.context.fetch=async()=>{throw new Error('network down');};
  s.run('stageFiles([{name:"key.pdf",size:5}],"answer_key")');s.el('#answerKey').value='答案';
  await s.run('uploadFromHome()');
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].role'),'answer_key');
  assert.equal(s.el('#answerKey').value,'答案');assert.equal(s.el('#startUpload').disabled,false);
  assert.match(s.el('#uploadErr').textContent,/network down/);
});
test('materials can be removed and invalid file sets are rejected before uploading',()=>{
  const s=setup();s.run('stageFiles([{name:"one.png",size:10},{name:"two.png",size:10}]);removeMaterial(stagedFiles[0].id)');
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'two.png');
  s.run('stageFiles([{name:"large.pdf",size:41*1024*1024}])');assert.equal(s.run('stagedFiles.length'),1);
  assert.match(s.el('#uploadErr').textContent,/40/);
  s.run('stageFiles(Array.from({length:20},()=>({name:"x.png",size:1})))');assert.equal(s.run('stagedFiles.length'),1);
  assert.match(s.el('#uploadErr').textContent,/20/);
  s.run('stageFiles(Array.from({length:6},()=>({name:"x.pdf",size:40*1024*1024})))');assert.equal(s.run('stagedFiles.length'),1);
  assert.match(s.el('#uploadErr').textContent,/200/);
});
test('new uploads always use vision without OCR',()=>{
  const s=setup();s.run('settingsCache={ocr_available:true};syncOcrSelectors()');
  assert.equal(s.run('homeOcrMode'),'vision');
  s.run('settingsCache={ocr_available:false};syncOcrSelectors()');
  assert.equal(s.run('defaultOcrMode()'),'vision');
});
test('task file selection moves to staged home with task context and no upload',async()=>{
  const s=taskHarness();await s.run('openTask("A")');
  for (const selector of ['#uClass','#uTask']) {
    const node=s.el(selector);let value='';
    Object.defineProperty(node,'value',{get(){return value;},set(next){value=node.innerHTML.includes(`value="${next}"`)?next:'';}});
  }
  await s.el('#tFile').onchange({target:{files:[{name:'one.pdf',size:10},{name:'two.png',size:10}],value:'x'}});
  assert.equal(s.deferred.length,0);assert.equal(s.run('stagedFiles.length'),2);
  assert.equal(s.el('#uTask').value,'A');assert.equal(s.el('#uClass').value,'class');assert.equal(s.el('#home').hidden,false);
  const uploading=s.run('uploadFromHome()');assert.equal(s.deferred.length,1);
  s.deferred[0].reject(new Error('retry'));await uploading;
  assert.equal(s.run('stagedFiles.length'),2);
});
test('result opens first student page and tabs identify each material source',async()=>{
  const s=setup();
  const result={id:'bundle',filename:'bundle',page_count:3,graded_pages:[3],materials:[{page:1,filename:'paper.pdf',source_page:1,role:'question_paper'},{page:2,filename:'key.pdf',source_page:1,role:'answer_key'},{page:3,filename:'work.png',source_page:1,role:'student_work'}],questions:[],summary:{correct:0,wrong:0,partial:0,review:0}};
  s.context.fetch=async()=>({ok:true,json:async()=>result});
  s.run('activeJob={id:"bundle"};allStudents=[{id:"student"}]');
  await s.run('openResult("bundle",false,undefined,viewGeneration)');
  assert.equal(s.run('page'),3);assert.match(s.el('#pageTabs').innerHTML,/试卷.*paper.pdf/);assert.match(s.el('#pageTabs').innerHTML,/标准答案.*key.pdf/);assert.match(s.el('#pageTabs').innerHTML,/学生作答.*work.png/);
});
test('cached single upload keeps a result link and cache hint on its retained row',async()=>{
  const s=setup();
  s.context.fetch=async(url,opt={})=>({ok:true,json:async()=>opt.method==='POST'?{id:'cached',cached:true}:url.endsWith('/status')?{id:'cached',status:'done',last_event_id:1}:[]});
  await s.run('uploadFromHome([{name:"cached.pdf"}])');
  const row=s.el('#queue').children[0];assert.ok(row);
  assert.match(row.querySelector('small').textContent,/同一材料/);
  assert.equal(s.run('activeJob.fileCached'),true);
  await s.run('show("work")');row.onclick();
  assert.equal(s.context.location.hash,'#grading/cached');
  assert.equal(s.run('activeJob.fileCached'),true);
});

test('OCR coordinate mode displays a program check without model effort badge',()=>{
  const s=setup();s.el('#steps').children.push({});
  s.run('activeJob={position_method:"ocr_blocks"};renderSteps({review:{status:"start"}})');
  const node=s.el('#steps [data-stage="review"]');
  assert.equal(node.querySelector('b').textContent,'OCR 位置检查');
  assert.equal(node.querySelector('.effort').hidden,true);
  s.run('renderMetrics({position_method:"ocr_blocks",timings:{review:.1}})');
  assert.match(s.el('#jobMetrics').innerHTML,/OCR 位置检查/);
});

test('vision and historical OCR results retain actual visual review labels',()=>{
  const s=setup();s.el('#steps').children.push({});
  s.run('activeJob={ocr_mode:"deepseek_ocr"};renderSteps({review:{status:"done"}})');
  const node=s.el('#steps [data-stage="review"]');
  assert.equal(node.querySelector('b').textContent,'复核 Agent');
  assert.equal(node.querySelector('.effort').hidden,false);
});

test('OCR queue entries label the position stage as a program check',()=>{
  const s=setup();
  const html=s.run('jobRows([{id:"x",filename:"x.png",status:"running",stage:"review",position_method:"ocr_blocks"}])');
  assert.match(html,/OCR 位置检查/);assert.doesNotMatch(html,/复核 Agent/);
});

test('task material selection keeps the prior draft separate and restores all of its fields',async()=>{
  const s=taskHarness();
  s.run('settingsCache={ocr_available:true};syncOcrSelectors();classes=[{id:"class",name:"班级"}];allTasks=[{id:"A",class_id:"class",title:"A"}];allStudents=[{id:"student-A",class_id:"class",name:"学生A"}];fillUploadPickers();stageFiles([{name:"A.pdf",size:10}],"student_work")');
  s.el('#uClass').value='class';s.run('fillTaskStudent()');s.el('#uTask').value='A';s.el('#uStudent').value='student-A';
  s.el('#answerKey').value='A标准答案';
  await s.run('openTask("B")');s.el('#tFile').onchange({target:{files:[{name:'B.pdf',size:10}],value:'x'}});
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'B.pdf');
  assert.equal(s.el('#answerKey').value,'');assert.equal(s.el('#uTask').value,'B');
  assert.match(s.el('#uploadDrafts').innerHTML,/A/);
  const previous=s.run('[...uploadDrafts].find(([,draft])=>draft.task_id==="A")[0]');
  s.run(`switchUploadDraft(${previous})`);
  assert.equal(s.run('stagedFiles[0].file.name'),'A.pdf');assert.equal(s.run('stagedFiles[0].role'),'student_work');
  assert.equal(s.el('#answerKey').value,'A标准答案');assert.equal(s.el('#uClass').value,'class');
  assert.equal(s.el('#uTask').value,'A');assert.equal(s.el('#uStudent').value,'student-A');assert.equal(s.run('homeOcrMode'),'vision');
  await s.run('openTask("B")');s.el('#tFile').onchange({target:{files:[{name:'B-2.png',size:10}],value:'x'}});
  assert.equal(s.run('stagedFiles.length'),2);assert.equal(s.run('stagedFiles[0].file.name'),'B.pdf');assert.equal(s.el('#answerKey').value,'');
});

test('rejected task materials leave the active draft and archive context untouched',async()=>{
  const s=taskHarness();s.run('stageFiles([{name:"A.pdf",size:10}])');
  s.el('#answerKey').value='A标准答案';
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:Array.from({length:21},()=>({name:'B.pdf',size:10})),value:'x'}});
  assert.equal(s.el('#uTask').value,'');assert.equal(s.el('#uClass').value,'');
  assert.equal(s.el('#answerKey').value,'A标准答案');assert.equal(s.run('stagedFiles.length'),1);
  assert.equal(s.run('stagedFiles[0].file.name'),'A.pdf');assert.match(s.el('#tErr').textContent,/20/);
  assert.match(s.el('#taskDetail').innerHTML,/<h1>B<\/h1>/);
});

for(const fail of [false,true]) test(`upload ${fail?'failure':'success'} updates its own saved draft after switching tasks`,async()=>{
  const s=taskHarness();s.run('stageFiles([{name:"A.pdf",size:10}])');s.el('#answerKey').value='A答案';
  const uploading=s.run('uploadFromHome()');
  await s.run('openTask("B")');s.el('#tFile').onchange({target:{files:[{name:'B.pdf',size:10}],value:'x'}});
  s.el('#answerKey').value='B答案';
  if(fail) s.deferred[0].reject(new Error('A失败'));else s.deferred[0].resolve({ok:true,json:async()=>({id:'A',cached:false})});
  await uploading;
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'B.pdf');
  assert.equal(s.el('#answerKey').value,'B答案');assert.equal(s.context.location.hash,'');
  const previous=s.run('[...uploadDrafts].find(([,draft])=>!draft.task_id)[0]');s.run(`switchUploadDraft(${previous})`);
  assert.equal(s.run('stagedFiles.length'),fail?1:0);assert.equal(s.el('#answerKey').value,fail?'A答案':'');
});

test('task import validates the saved destination draft limit without changing the current draft',async()=>{
  const s=taskHarness();s.run('stageFiles([{name:"A.pdf",size:10}])');s.el('#answerKey').value='A答案';
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:Array.from({length:20},(_,i)=>({name:`B-${i}.pdf`,size:10})),value:'x'}});
  assert.equal(s.run('stagedFiles.length'),20);
  const previous=s.run('[...uploadDrafts].find(([,draft])=>!draft.task_id)[0]');
  s.el('#uploadDrafts').onclick({target:{dataset:{uploadDraft:String(previous)}}});
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:[{name:'B-extra.pdf',size:10}],value:'x'}});
  assert.match(s.el('#tErr').textContent,/20/);assert.equal(s.el('#uTask').value,'');
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'A.pdf');assert.equal(s.el('#answerKey').value,'A答案');
  assert.equal(s.run('[...uploadDrafts.values()].find(draft=>draft.task_id==="B").files.length'),20);
});

test('task uploads normalize historical OCR drafts to vision',async()=>{
  const s=taskHarness();s.run('settingsCache={ocr_available:true};syncOcrSelectors()');
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:[{name:'one.png',size:10}],value:'x'}});
  assert.equal(s.run('homeOcrMode'),'vision');
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:[{name:'two.png',size:10}],value:'x'}});
  assert.equal(s.run('stagedFiles.length'),2);assert.equal(s.run('homeOcrMode'),'vision');
  assert.equal(s.run('uploadDrafts.get(activeDraftId).ocr_mode'),'vision');
});

test('repeated stage starts do not reset time and later attempts accumulate',()=>{
  const s=setup();
  s.run('stageState={};applyJobEvent({id:1,stage:"parse",status:"start",t:100});applyJobEvent({id:2,stage:"parse",status:"start",t:110});applyJobEvent({id:3,stage:"parse",status:"done",t:120})');
  assert.equal(s.run('stageState.parse.elapsed_s'),20);
  s.run('applyJobEvent({id:4,stage:"parse",status:"start",t:150});applyJobEvent({id:5,stage:"parse",status:"done",t:155})');
  assert.equal(s.run('stageState.parse.elapsed_s'),25);
});
test('running steps add current processing time to previously completed time',()=>{
  const s=setup();
  s.run('stageState={parse:{status:"start",started_at:100,elapsed_s:20}};renderSteps(stageState)');
  assert.notEqual(s.el('#steps [data-stage="parse"] .stage-time').textContent,'20.0s');
});

test('single-agent jobs show one reasoning step and hide legacy model stages',()=>{
  const s=setup();
  s.run('activeJob={execution_mode:"single_agent",workflow_version:2,mode:"grade"};renderSteps({process:{status:"start",started_at:100}})');
  assert.equal(s.el('#steps [data-stage="process"]').hidden,false);
  for(const key of ['parse','solve','answer_review','grade','review']) assert.equal(s.el(`#steps [data-stage="${key}"]`).hidden,true);
});

function reviewFixture(s) {
  const questions=[{id:'1',page:3,status:'review',draw:false,bbox_ok:true,bbox_px:[30,40,60,70],student_answer:'2',answer_state:'answered',question:'1+1=',standard_answer:'2',reasons:[]},
    {id:'2',page:4,status:'review',draw:false,bbox_ok:true,bbox_px:[30,40,60,70],student_answer:'4',answer_state:'answered',question:'2+2=',standard_answer:'4',reasons:[]}];
  const job={id:'bundle-review',page_count:4,materials:[{page:1,role:'question_paper',filename:'paper.pdf',source_page:1},
    {page:3,role:'student_work',filename:'work-a.png',source_page:1},{page:4,role:'student_work',filename:'work-b.png',source_page:1}],
    questions,summary:{correct:0,wrong:0,partial:0,review:2,accuracy:0}};
  s.run(`cur=${JSON.stringify(job)};page=1;kind="raw"`);
  return job;
}
test('reference alignment warning remains visible on completed grading results',()=>{
  const s=setup();reviewFixture(s);
  s.run('cur.warnings=["老师答案题号 99 未对齐，其他题继续批改"];drawResult()');
  assert.match(s.el('#jobNotice').textContent,/参考答案提醒.*99.*其他题继续批改/);
  assert.match(s.el('#qlist').innerHTML,/data-q="1"/);
});
test('prepared paper displays escaped reference warnings',async()=>{
  const s=setup();
  s.context.fetch=async()=>({ok:true,json:async()=>({id:'paper',title:'Quiz',questions:[],answers:[]})});
  await s.run('openPaper("paper",["老师答案 <img> 未对齐"])');
  assert.match(s.el('#paperDetail').innerHTML,/参考答案提醒.*&lt;img&gt;/);
  assert.doesNotMatch(s.el('#paperDetail').innerHTML,/<img>/);
});
test('manual correct switches from another file to the question annotated page',async()=>{
  const s=setup(), job=reviewFixture(s);
  job.questions[0]={...job.questions[0],status:'correct',draw:true,overridden:true};
  s.context.fetch=async()=>({ok:true,json:async()=>job});
  const button={dataset:{q:'1',s:'correct'}};
  s.context.document.querySelectorAll=selector=>selector==='#qlist button[data-q]'?[button]:[];
  s.run('drawResult()');await button.onclick();
  assert.equal(s.run('page'),3);
  assert.equal(s.run('kind'),'annotated');
  assert.match(s.el('#pageImg').src,/pages\/3\?kind=annotated/);
});
test('manual review on the raw student page opens the annotated view',async()=>{
  const s=setup(), job=reviewFixture(s);
  s.run('page=4;kind="raw"');job.questions[1]={...job.questions[1],status:'wrong',draw:true};
  s.context.fetch=async()=>({ok:true,json:async()=>job});
  await s.run('applyManualOverride("2","wrong")');
  assert.equal(s.run('page'),4);assert.equal(s.run('kind'),'annotated');
});
test('rapid manual reviews wait for prior updates and retain both decisions',async()=>{
  const s=setup(), job=reviewFixture(s), calls=[];
  let release;
  s.context.fetch=async(url,opt)=>{
    const data=JSON.parse(opt.body);calls.push(data.qid);
    if(calls.length===1) await new Promise(resolve=>release=resolve);
    job.questions=job.questions.map(q=>q.id===data.qid?{...q,status:data.status,draw:true}:q);
    return {ok:true,json:async()=>structuredClone(job)};
  };
  const first=s.run('applyManualOverride("1","correct")');
  const second=s.run('applyManualOverride("2","wrong")');
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,['1']);release();await Promise.all([first,second]);
  assert.equal(s.run('cur.questions[0].status'),'correct');
  assert.equal(s.run('cur.questions[1].status'),'wrong');
  assert.equal(s.run('page'),4);
});
test('marked-but-undrawn answers keep positioning with compact page metadata',()=>{
  const s=setup();reviewFixture(s);
  s.run('cur.questions[0].status="correct";cur.questions[0].position_note="答案旁没有可用的标注空间";drawResult()');
  assert.match(s.el('#qlist').innerHTML,/data-position="1"/);
  assert.match(s.el('#qlist').innerHTML,/<div class="q-verdict"><span>人工改判<\/span>/);
  assert.match(s.el('#qlist').innerHTML,/class="q-position" data-position="1">框选答案位置<\/button>/);
  assert.match(s.el('#qlist').innerHTML,/<div class="q-meta"><span title="work-a.png 第 1 页">第 3 页<\/span>/);
});

test('hierarchical question labels display without changing manual review IDs',()=>{
  const s=setup();reviewFixture(s);
  s.run('cur.questions[0].question_label="1.1";cur.questions[1].question_label="2.1";drawResult();beginManualPosition("1")');
  assert.match(s.el('#qlist').innerHTML,/<span class="q-id">1\.1<\/span>/);
  assert.match(s.el('#qlist').innerHTML,/<span class="q-id">2\.1<\/span>/);
  assert.match(s.el('#qlist').innerHTML,/data-q="1"/);
  assert.match(s.el('#positionHint').textContent,/第 1\.1 题/);
});

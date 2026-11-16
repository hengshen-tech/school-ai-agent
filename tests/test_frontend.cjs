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
test('paper detail offers explicit confirmation and shows conflicting answers',async()=>{
  const s=setup();
  const paper={id:'paper_test',title:'Quiz',questions:[{id:'1',question:'12+8='}],answers:[{id:'1',answer:'20',teacher_answer:'21',review_status:'review',review_reason:'冲突'}]};
  s.context.fetch=async(url)=>({ok:true,json:async()=>url==='/api/papers'?[]:paper});
  await s.run('openPaper("paper_test")');
  assert.match(s.el('#paperDetail').innerHTML,/待人工确认/);
  assert.match(s.el('#paperDetail').innerHTML,/21/);
  assert.match(s.el('#paperDetail').innerHTML,/data-confirm/);
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
  assert.equal(uploads[0].body.get('ocr_mode'),'deepseek_ocr');
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
test('new uploads always use OCR and never fall back to vision',()=>{
  const s=setup();s.run('settingsCache={ocr_available:true};syncOcrSelectors()');assert.equal(s.el('#uOcr').value,'deepseek_ocr');
  s.el('#uOcr').value='vision';s.el('#uOcr').onchange({target:s.el('#uOcr')});
  s.run('syncOcrSelectors()');assert.equal(s.el('#uOcr').value,'deepseek_ocr');
  s.run('settingsCache={ocr_available:false};syncOcrSelectors()');assert.equal(s.el('#uOcr').value,'deepseek_ocr');
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
  s.el('#answerKey').value='A标准答案';s.el('#uOcr').value='vision';s.el('#uOcr').onchange({target:s.el('#uOcr')});
  await s.run('openTask("B")');s.el('#tFile').onchange({target:{files:[{name:'B.pdf',size:10}],value:'x'}});
  assert.equal(s.run('stagedFiles.length'),1);assert.equal(s.run('stagedFiles[0].file.name'),'B.pdf');
  assert.equal(s.el('#answerKey').value,'');assert.equal(s.el('#uTask').value,'B');
  assert.match(s.el('#uploadDrafts').innerHTML,/A/);
  const previous=s.run('[...uploadDrafts].find(([,draft])=>draft.task_id==="A")[0]');
  s.run(`switchUploadDraft(${previous})`);
  assert.equal(s.run('stagedFiles[0].file.name'),'A.pdf');assert.equal(s.run('stagedFiles[0].role'),'student_work');
  assert.equal(s.el('#answerKey').value,'A标准答案');assert.equal(s.el('#uClass').value,'class');
  assert.equal(s.el('#uTask').value,'A');assert.equal(s.el('#uStudent').value,'student-A');assert.equal(s.el('#uOcr').value,'deepseek_ocr');
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

test('task uploads normalize historical vision drafts to OCR',async()=>{
  const s=taskHarness();s.run('settingsCache={ocr_available:true};syncOcrSelectors()');
  await s.run('openTask("B")');
  s.el('#tFile').onchange({target:{files:[{name:'one.png',size:10}],value:'x'}});
  assert.equal(s.el('#uOcr').value,'deepseek_ocr');
  await s.run('openTask("B")');s.el('#tOcr').value='vision';s.el('#tOcr').onchange({target:s.el('#tOcr')});
  s.el('#tFile').onchange({target:{files:[{name:'two.png',size:10}],value:'x'}});
  assert.equal(s.run('stagedFiles.length'),2);assert.equal(s.el('#uOcr').value,'deepseek_ocr');
  assert.equal(s.run('uploadDrafts.get(activeDraftId).ocr_mode'),'deepseek_ocr');
});

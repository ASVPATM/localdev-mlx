"use strict";
const $ = id => document.getElementById(id);
let latest = {}, drafts = [], loaded = false, storageKey = "", revision = 0;
let generation = 0, savedGeneration = 0, saving = null, saveTimer = null, conflict = false;
let activeCapture = null, pendingExport = null, stopped = false, refreshing = false, clearingCaptures = false;
let tabOwner=crypto.randomUUID();
const captureSurfaces=new Map();
try{tabOwner=sessionStorage.getItem('project-flow-tab')||tabOwner;sessionStorage.setItem('project-flow-tab',tabOwner);}catch(_){}
const makeIssue = () => ({id:crypto.randomUUID(),prompt:"",expected:"",priority:"normal",type:"behavior",scope:"shared",selection_ids:[]});
const clone = value => JSON.parse(JSON.stringify(value));
function fail(error) { $("error").textContent = error.message || String(error); }
function node(tag, text, className) {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
async function api(action, body) {
  const response=await fetch(`/api/${action}`,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-Flow-Token":latest.control_token||""},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||"Request failed");return data;
}
function persist() {
  if(!storageKey)return;
  try{
    const value=JSON.stringify({owner:tabOwner,revision,dirty:generation!==savedGeneration,issues:drafts});
    sessionStorage.setItem(storageKey,value); // A second tab cannot overwrite this tab's recovery copy.
    if(generation!==savedGeneration)localStorage.setItem(storageKey,value);
    else if(JSON.parse(localStorage.getItem(storageKey)||'null')?.owner===tabOwner)localStorage.removeItem(storageKey);
  }
  catch(_){$("save-state").textContent="Browser draft storage unavailable; keep this tab open until saved";}
}
function changed(){generation++;persist();$("save-state").textContent="Saving…";clearTimeout(saveTimer);saveTimer=setTimeout(()=>flush().catch(fail),450);renderStatus();}
async function flush(){
  if(conflict)throw new Error("Another tab saved changes. Your local draft is retained; reload to review the conflict.");
  if(saving){await saving;if(generation!==savedGeneration)return flush();return;}
  if(generation===savedGeneration)return;
  const sent=generation, values=clone(drafts), expected=revision;
  saving=(async()=>{try{
    const result=await api("issues",{issues:values,revision:expected});revision=result.issues_revision;savedGeneration=sent;
    persist();$("save-state").textContent=generation===savedGeneration?"Saved locally":"Saving…";
  }catch(error){if(/another tab|unknown capture|capture was removed/.test(error.message))conflict=true;$("save-state").textContent="Not saved · draft retained in this browser";throw error;}finally{saving=null;}})();
  await saving;if(generation!==savedGeneration)return flush();
}
function issueById(id){return drafts.find(row=>row.id===id);}
function surfaceLabel(value){return [...new Set(['page','dialog','section','element'].map(key=>typeof value?.[key]==='string'?value[key].trim().slice(0,80):'').filter(Boolean))].join(' · ');}
function captureTitle(capture){return capture.label||surfaceLabel(capture.surface||captureSurfaces.get(capture.id))||(capture.text||'').trim().replace(/\s+/g,' ').slice(0,120)||capture.selector||'Manual evidence';}
function captureLocation(capture){return [capture.path,capture.selector].filter(Boolean).join(' · ');}
async function loadCaptureSurfaces(){
  // Older running controllers persist new surface metadata only in capture.json.
  // Read it once per capture, without rewriting historical evidence or resetting a preview.
  const pending=(latest.selections||[]).filter(capture=>!capture.surface&&!capture.label&&!captureSurfaces.has(capture.id)&&capture.artifacts?.capture);
  pending.forEach(capture=>captureSurfaces.set(capture.id,null));
  for(let index=0;index<pending.length;index+=4)await Promise.all(pending.slice(index,index+4).map(async capture=>{
    try{const response=await fetch(assetURL(capture.id,'capture'));if(response.ok)captureSurfaces.set(capture.id,(await response.json()).surface||null);}catch(_){}
  }));
  if(pending.length)renderIssues();
}
function saveButton(text, action, label){const el=node("button",text,"quiet");el.type="button";if(label)el.setAttribute("aria-label",label);el.onclick=async()=>{try{await action();}catch(error){fail(error);}};return el;}
function editField(issueId,key,el){el.addEventListener("input",()=>{issueById(issueId)[key]=el.value;changed();});}
function createIssue(issue){
  const card=node("article",undefined,"issue");card.dataset.issueId=issue.id;
  const heading=node("div",undefined,"issue-heading"), title=node("h3"), actions=node("div",undefined,"actions");
  heading.append(title,actions);card.append(heading);
  for(const [label,delta] of [["↑",-1],["↓",1]])actions.append(saveButton(label,()=>{const index=drafts.findIndex(r=>r.id===issue.id),next=index+delta;if(next>=0&&next<drafts.length){[drafts[index],drafts[next]]=[drafts[next],drafts[index]];changed();renderIssues();}},delta<0?"Move change up":"Move change down"));
  actions.append(saveButton("Remove",()=>{if(!confirm("Remove this change description? Captures remain available."))return;drafts=drafts.filter(row=>row.id!==issue.id);if(!drafts.length)drafts=[makeIssue()];changed();renderIssues();},"Remove change"));
  const input=node("textarea");input.rows=3;input.maxLength=12000;input.dataset.field="prompt";input.placeholder="Describe a change, unexpected result, or question.";editField(issue.id,"prompt",input);card.append(input);
  const details=node("details",undefined,"options");details.append(node("summary","Optional details"));
  const expectedLabel=node("label","Expected result"),expected=node("input");expected.dataset.field="expected";expected.maxLength=4000;expected.placeholder="Optional—leave blank if your description explains it.";editField(issue.id,"expected",expected);expectedLabel.append(expected);details.append(expectedLabel);
  const options=node("div",undefined,"toolbar");
  for(const [key,caption,values] of [["type","Type",["behavior","question","visual","feature","bug","data","native"]],["priority","Priority",["normal","high","blocking","low"]],["scope","Scope",["shared","selected","unsure"]]]){
    const label=node("label",caption),select=node("select");select.dataset.field=key;
    const choices=values.includes(issue[key])?values:[...values,issue[key]];
    choices.filter(Boolean).forEach(value=>{const option=node("option",key==='scope'?{shared:'Check shared behavior',selected:'Selected record only',unsure:'Determine scope'}[value]||value:value);option.value=value;select.append(option);});
    editField(issue.id,key,select);label.append(select);options.append(label);
  }
  details.append(options);card.append(details,node("p","App parts for this change", "parts-heading"),node("div",undefined,"group-parts"));
  return card;
}
function renderIssues(){
  $("draft-conflict").hidden=!conflict;
  const container=$("issues"), ids=drafts.map(row=>row.id).join("|");
  const existing=new Map([...container.children].map(el=>[el.dataset.issueId,el]));
  if(container.dataset.order!==ids){container.replaceChildren(...drafts.map(row=>existing.get(row.id)||createIssue(row)));container.dataset.order=ids;}
  const selections=latest.selections||[];
  drafts.forEach((issue,index)=>{
    const card=[...container.children].find(el=>el.dataset.issueId===issue.id);
    card.querySelector("h3").textContent=`Change ${index+1}`;
    for(const key of ["prompt","expected","priority","type","scope"]){const field=card.querySelector(`[data-field="${key}"]`);field.setAttribute("aria-label",`Change ${index+1} ${key==="prompt"?"description":key}`);if(document.activeElement!==field&&field.value!==issue[key])field.value=issue[key]||(key==='scope'?'shared':'');}
    const parts=card.querySelector(".group-parts"),version=JSON.stringify([selections.map(s=>[s.id,captureTitle(s),s.path,s.selector,s.summary]),issue.selection_ids,index]);
    if(parts.dataset.version===version)return;parts.dataset.version=version;
    if(!selections.length){parts.replaceChildren(node("p","No parts captured yet. Open the test app and select an element, or attach evidence below.","no-parts"));return;}
    parts.replaceChildren(...selections.map((capture,captureIndex)=>{
      const wrapper=node("div",undefined,"part-choice"),label=node("label"),check=node("input"),copy=node("span",undefined,"part-copy");
      check.type="checkbox";check.checked=issue.selection_ids.includes(capture.id);wrapper.classList.toggle("selected",check.checked);
      const title=captureTitle(capture);check.setAttribute("aria-label",`Use ${title} for Change ${index+1}`);
      check.onchange=()=>{const current=issueById(issue.id);current.selection_ids=check.checked?[...new Set([...current.selection_ids,capture.id])]:current.selection_ids.filter(id=>id!==capture.id);changed();renderIssues();};
      copy.append(node("span",`${captureIndex+1}. ${title}`));const summary=capture.summary||{};
      if(captureLocation(capture))copy.append(node('small',captureLocation(capture),'part-location'));
      copy.append(node("small",`${summary.has_screenshot?"Screenshot · ":""}${summary.errors||0} errors · ${summary.failed_requests||0} failed requests`));
      label.append(check,copy);wrapper.append(label,saveButton("Evidence",()=>showEvidence(capture.id),`Evidence for ${title}`));return wrapper;
    }));
  });
}
function assetURL(id,name){return `/api/artifact?id=${encodeURIComponent(id)}&name=${encodeURIComponent(name)}`;}
async function showEvidence(id){
  const capture=(latest.selections||[]).find(row=>row.id===id);if(!capture)return;
  activeCapture=id;$("evidence-title").textContent=captureTitle(capture);
  $("evidence-location").textContent=captureLocation(capture);
  $("evidence-summary").textContent=`${capture.summary?.actions||0} actions · ${capture.summary?.errors||0} errors · ${capture.summary?.failed_requests||0} failed requests${capture.warning?' · '+capture.warning:''}`;
  const artifacts=capture.artifacts||{},first=["annotated","viewport","element",...Object.keys(artifacts)].find(key=>artifacts[key]?.endsWith(".png"));
  $("evidence-image").hidden=!first;if(first)$("evidence-image").src=assetURL(id,first);
  $("evidence-files").replaceChildren(...Object.keys(artifacts).map(name=>{const link=node("a",name.replaceAll("_"," "));link.href=assetURL(id,name);link.target="_blank";link.rel="noopener";return link;}));
  $("locate").disabled=!capture.selector||latest.browser_status!=="recording";$("replay").disabled=$("locate").disabled;
  $("evidence-facts").textContent=JSON.stringify({source_hints:capture.source_hints,comparison:capture.comparison},null,2);
  if(!$("evidence-dialog").open)$("evidence-dialog").showModal();
  if(artifacts.capture){const response=await fetch(assetURL(id,"capture"));if(response.ok&&activeCapture===id){const facts=await response.json();$("evidence-facts").textContent=JSON.stringify({source_hints:capture.source_hints,...facts},null,2).slice(0,24000);}}
}
function hydrate(next){
  storageKey=`project-flow-composer-v3:${next.project}${next.mode==='personal'?':personal':''}`;revision=next.issues_revision||0;drafts=clone(next.issues||[]);
  try{
    const stored=JSON.parse(sessionStorage.getItem(storageKey)||localStorage.getItem(storageKey)||"null");
    if(stored?.dirty&&Array.isArray(stored.issues)){
      if(stored.revision===revision){drafts=stored.issues;generation=1;}
      else{conflict=true;drafts=stored.issues;generation=1;fail(new Error("Another tab has newer saved changes. This browser’s draft is retained. Export it as a draft backup before choosing which version to keep."));}
    }
    const legacyKey=`project-flow-draft:${next.project}`,legacy=JSON.parse(localStorage.getItem(legacyKey)||"null");
    if(next.mode!=='personal'&&!stored&&legacy?.prompt){drafts.push({...makeIssue(),prompt:legacy.prompt,expected:legacy.expected||""});generation=1;}
  }catch(_){fail(new Error("Stored draft could not be read; server-saved changes were loaded"));}
  if(!drafts.length)drafts=[makeIssue()];loaded=true;persist();
  const info=next.inspection||{};
  for(const scenario of next.scenarios||["default"]){const option=node("option",scenario);option.value=scenario;$("scenario").append(option);}
  if(info.scenario)$("scenario").value=info.scenario;
  $("seed").value=info.seed??42;$("viewport").value=info.width===720?"narrow":"wide";$("capture-narrow").checked=next.capture_narrow===true;
}
function renderStatus(){
  const captureCount=(latest.selections||[]).length;
  $("capture-count").textContent=`${captureCount} app part${captureCount===1?'':'s'}`;
  const exportBusy=latest.export_job&&!['ready','failed'].includes(latest.export_job.stage);
  $("clear-captures").disabled=!captureCount||clearingCaptures||conflict||exportBusy||!latest.can_clear_selections;
  $("clear-changes").disabled=clearingCaptures||conflict||exportBusy||!latest.can_clear_issues||!drafts.some(row=>row.prompt.trim()||row.expected||row.selection_ids.length);
  $("clear-captures").title=!latest.can_clear_selections?"Restart Flow to enable clearing all app parts":exportBusy?"Wait for the current export to finish":"Clear the working capture list; descriptions and exported handoffs stay";
  const model=latest.model_status||{},labels={not_configured:"Not configured",not_loaded:"Not loaded",ready:"Ready",busy:"Busy",unavailable:"Unavailable"};
  $("project-name").textContent=latest.project?.split(/[\\/]/).filter(Boolean).pop()||"";$("project").textContent=latest.project||"";$("project").title=`Copy directory: ${latest.project||""}`;
  $("model-status").textContent=`Local helper${model.model?' · '+model.model:''}\n${model.provider?model.provider+' · ':''}${labels[model.state]||"Checking"}${model.host&&model.host!=="Standalone"?' · '+model.host:''}`;
  $("preview-status").textContent=`Preview: ${latest.inspector_status} · Browser: ${latest.browser_status}${latest.evidence_warning?' · '+latest.evidence_warning:''}${latest.preview_kind==='command'&&latest.preview_exit_code!==null&&latest.preview_exit_code!==undefined?' · Command exit: '+latest.preview_exit_code:''}`;
  $("preview-setup").textContent=latest.preview_setup||"";
  const personal=latest.mode==='personal';
  $("flow-mode").textContent=personal?'Personal-data Flow · offline copy':'Sample-data Flow';
  document.title=`${personal?'Personal-data':'Sample-data'} Flow · ${latest.project?.split('/').pop()||''}`;
  document.querySelector('[data-action="open-test-app"]').textContent=personal?'Open personal-data app':'Open test app';
  document.querySelector('[data-action="inspect"]').textContent=personal?'Refresh code & data snapshot':'Refresh to latest code';
  $("evidence-privacy").textContent=personal?'Personal content may appear in screenshots and exports. Form fields remain masked; review evidence before sharing.':'Uploads must contain only non-sensitive test data. Screenshot uploads are not automatically masked.';
  $("stale").textContent=latest.preview_source_error?`Source could not be verified: ${latest.preview_source_error}`:latest.has_preview&&latest.preview_stale?"Source has changed. Open test app keeps your current test state; Refresh to latest code starts a fresh preview.":"";
  $("scenario-field").hidden=(latest.scenarios||[]).length<=1;$("seed-field").hidden=!latest.supports_seed;
  for(const id of ["viewport-field","narrow-field","narrow-hint"])$(id).hidden=latest.preview_kind!=="web";
  $("source-field").hidden=latest.hosted||personal||!latest.run_id;$("editing-tools").hidden=latest.hosted||personal;
  $("organize-hint").textContent=model.state==='ready'?"The connected local helper can suggest ordering. Original descriptions and evidence stay unchanged.":model.state==='not_loaded'&&model.can_load?"LocalDev may prepare the configured helper during export under its saved permissions.":"No ready local helper. Ordinary export works immediately; no model is required.";
  for(const key of ["status","message","plan","workspace","evidence","diff"])$(key).textContent=latest[key]||"";
  $("risk").textContent=latest.high_risk?.length?`High-risk paths: ${latest.high_risk.join(', ')}`:"";
  $("checks").replaceChildren(...(latest.checks||[]).map(check=>{const details=node("details");details.append(node("summary",`${check.passed?'PASS':'FAIL'} · ${check.command.join(' ')}`),node("pre",check.output));return details;}));
  const states={begin:["idle","accepted","rejected","cancelled","error"],"approve-plan":["planned"],check:["editing","review","checks_failed","error","interrupted"],accept:["review"]};
  for(const [action,allowed]of Object.entries(states))document.querySelector(`[data-action="${action}"]`).disabled=!allowed.includes(latest.status);
  for(const action of ["open-test-app","inspect"])document.querySelector(`[data-action="${action}"]`).disabled=latest.inspector_status==='preparing'||latest.preview_configured===false;
}
async function refresh(){
  if(refreshing||stopped||clearingCaptures)return;refreshing=true;
  try{
    const next=await api("state");
    if(clearingCaptures||(loaded&&(next.issues_revision||0)<revision))return;
    latest=next;if(!loaded)hydrate(latest);
    else if(!saving&&generation===savedGeneration&&(latest.issues_revision||0)!==revision){revision=latest.issues_revision||0;drafts=clone(latest.issues||[]);if(!drafts.length)drafts=[makeIssue()];persist();}
    renderStatus();renderIssues();void loadCaptureSurfaces();await renderExport();
  }finally{refreshing=false;}
}
function previewOptions(){return {source:$("source").value,scenario:$("scenario").value,seed:Number($("seed").value),viewport:$("viewport").value,capture_narrow:$("capture-narrow").checked};}
document.addEventListener("click",async event=>{
  const button=event.target.closest("[data-action]");if(!button)return;const action=button.dataset.action;
  try{
    const previewAction=action==='inspect'||action==='open-test-app';
    if(previewAction&&latest.preview_needs_confirmation&&!confirm(latest.mode==='personal'?'Create an offline copy of your personal app data? The live database will not be attached, credentials/sync are disabled, and changes never write back. Screenshots and exports may contain personal content. Stop/Refresh discards preview data edits; captures and handoffs stay.':`Run the detected ${latest.adapter_name} in a disposable source copy? It uses separate test data. Only run trusted project code; a source copy is not an OS sandbox.`))return;
    if(action==='inspect'&&latest.has_preview&&!confirm(latest.mode==='personal'?"Discard preview data edits and take a new snapshot of current code and personal data? Captures, descriptions and handoffs stay.":"Refresh from current source and reset the temporary test app? Captures and change descriptions remain saved."))return;
    if(action==='shutdown'&&!confirm("Stop this Flow server and its owned preview? Saved changes and captures stay."))return;
    if(action==='accept'&&!confirm("Apply the reviewed and tested diff to the original project? Recoverable originals are retained."))return;
    await flush();button.disabled=true;$("error").textContent="";
    let body={};if(previewAction)body={...previewOptions(),confirmed:true};if(action==='begin')body={request:$("request").value};
    if(action==='accept')body={confirmed:true,risk_confirmed:!latest.high_risk?.length||confirm(`Approve high-risk paths: ${latest.high_risk.join(', ')}`)};
    await api(action,body);if(action==='shutdown'){stopped=true;$("preview-status").textContent="Server stopped. Saved evidence is retained.";return;}await refresh();
  }catch(error){fail(error);}finally{button.disabled=false;}
});
$("add-issue").onclick=()=>{drafts.push(makeIssue());changed();renderIssues();$("issues").lastElementChild.querySelector("textarea").focus();};
$("capture-narrow").onchange=async()=>{try{await api("capture-options",{narrow:$("capture-narrow").checked});}catch(error){fail(error);}};
$("project").onclick=async()=>{try{await navigator.clipboard.writeText(latest.project);$("save-state").textContent="Project directory copied";}catch(_){fail(new Error(`Project directory: ${latest.project}`));}};
$("add-anchor").onclick=async()=>{try{await flush();const path=$("anchor-path").value.trim();await api("selection",{text:path||"Manual evidence",anchor:path?{path,line:Number($("anchor-line").value)}:null});await refresh();await showEvidence(latest.selections.at(-1).id);}catch(error){fail(error);}};
$("close-evidence").onclick=()=>$("evidence-dialog").close();
$("attach").onclick=()=>$("attachment").click();
$("attachment").onchange=async event=>{
  const input=event.currentTarget,file=input.files[0],id=activeCapture;
  try{if(!file)return;if(file.size>4*1024*1024)throw new Error("Attachment exceeds 4 MB");if(!confirm(latest.mode==='personal'?"This attachment may contain personal content and will be included in exports. Check that it has no credentials. Continue?":"Confirm this screenshot/log contains only synthetic, non-sensitive data. Uploaded screenshots are not automatically masked."))return;
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
    await api("attach",{id,filename:file.name,data});await refresh();await showEvidence(id);
  }catch(error){fail(error);}finally{input.value="";}
};
$("locate").onclick=async()=>{try{await api("locate",{id:activeCapture});}catch(error){fail(error);}};
$("replay").onclick=async()=>{if(!confirm("Replay can repeat writes in the synthetic preview. It does not prove a fix is correct. Continue?"))return;try{const result=await api("replay",{id:activeCapture,confirmed:true});$("evidence-summary").textContent=`Replay: ${result.state} · ${result.steps} steps`;}catch(error){fail(error);}};
async function clearAppParts(){
  if(clearingCaptures)return;
  clearingCaptures=true;
  $("issues").inert=true;$("add-issue").disabled=true;renderStatus();
  try{
    await flush();
    const current=await api("state");
    if((current.issues_revision||0)!==revision)throw new Error("Changes were saved in another tab. Nothing was cleared; review and try again.");
    const ids=(current.selections||[]).map(row=>row.id);
    if(!ids.length)return;
    if(!confirm(`Clear all ${ids.length} app parts from the working list and detach them from every change?\n\nYour descriptions, exported handoffs, and saved evidence files stay. A local recovery copy will be saved. Export first if you still need these attachments in a handoff.`))return;
    const result=await api("clear-selections",{ids,revision,confirmed:true});
    latest={...latest,...result};revision=result.issues_revision;
    drafts=clone(result.issues||[]);if(!drafts.length)drafts=[makeIssue()];
    savedGeneration=generation;persist();captureSurfaces.clear();activeCapture=null;
    $("evidence-dialog").close();$("error").textContent="";
    $("capture-state").textContent=`Cleared ${ids.length} app parts. Descriptions and handoffs kept; a local recovery copy was saved.`;
    renderIssues();
  }catch(error){fail(error);}
  finally{
    clearingCaptures=false;$("issues").inert=false;$("add-issue").disabled=false;
    renderStatus();await refresh().catch(fail);
  }
}
$("clear-captures").onclick=clearAppParts;
async function clearChanges(){
  if(clearingCaptures)return;
  clearingCaptures=true;clearTimeout(saveTimer);
  $("issues").inert=true;$("add-issue").disabled=true;renderStatus();
  try{
    await flush();
    const current=await api('state');
    if((current.issues_revision||0)!==revision)throw new Error('Changes were saved in another tab. Nothing was cleared; review and try again.');
    const ids=(current.issues||[]).map(row=>row.id);
    if(!ids.length)return;
    if(!confirm('Clear all change descriptions and their capture assignments? App parts and exported handoffs stay. A private recovery copy will be saved.'))return;
    const result=await api('clear-issues',{ids,revision,confirmed:true});
    latest={...latest,...result};revision=result.issues_revision;drafts=[makeIssue()];savedGeneration=generation;persist();
    $("error").textContent='';$("capture-state").textContent='Cleared all changes. App parts and handoffs kept; a local recovery copy was saved.';renderIssues();
  }catch(error){fail(error);}
  finally{clearingCaptures=false;$("issues").inert=false;$("add-issue").disabled=false;renderStatus();await refresh().catch(fail);}
}
$("clear-changes").onclick=clearChanges;
$("remove-capture").onclick=async()=>{if(!confirm("Remove this capture from changes? Its saved files remain recoverable."))return;try{await flush();const result=await api("remove-selection",{id:activeCapture});revision=result.issues_revision;drafts=clone(result.issues||[]);if(!drafts.length)drafts=[makeIssue()];persist();$("evidence-dialog").close();await refresh();}catch(error){fail(error);}};
$("backup-draft").onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({project:latest.project,revision,issues:drafts},null,2)],{type:'application/json'})),link=node('a');link.href=url;link.download='project-flow-browser-draft.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$("use-server-draft").onclick=async()=>{if(!confirm("Load the server’s changes in this tab? Download your browser draft first if you need both versions."))return;try{const next=await api('state');revision=next.issues_revision;drafts=clone(next.issues||[]);if(!drafts.length)drafts=[makeIssue()];generation=savedGeneration=0;conflict=false;persist();$("error").textContent='';await refresh();}catch(error){fail(error);}};
$("use-browser-draft").onclick=async()=>{if(!confirm("Replace the server’s saved change descriptions and assignments with this browser’s version? Other captures remain available."))return;try{const next=await api('state');revision=next.issues_revision;conflict=false;changed();await flush();$("error").textContent='';await refresh();}catch(error){fail(error);}};
async function startExport(format){
  try{await flush();if(!drafts.some(row=>row.prompt.trim()))throw new Error("Describe at least one change before exporting");
    const index=drafts.findIndex(row=>!row.prompt.trim()&&(row.selection_ids.length||row.expected.trim()));if(index>=0)throw new Error(`Change ${index+1} needs a description before exporting`);
    $("error").textContent="";const job=await api("export-start",{format,level:$("level").value,organize:$("organize").checked});pendingExport={id:job.id,format,downloaded:false};await refresh();
  }catch(error){fail(error);}
}
async function downloadJob(job){
  const response=await fetch('/api/export-download',{method:'POST',headers:{'Content-Type':'application/json','X-Flow-Token':latest.control_token},body:JSON.stringify({id:job.id,format:job.format})});
  if(!response.ok)throw new Error((await response.json()).error);
  const url=URL.createObjectURL(await response.blob()),link=node('a');link.href=url;link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||`handoff.${job.format}`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function renderExport(){
  const job=latest.export_job;$("export-progress").hidden=!job;if(!job)return;
  const busy=!['ready','failed'].includes(job.stage);for(const id of ['export-handoff','export-bundle'])$(id).disabled=busy;
  $("export-status").textContent=job.message+(job.organization?.reason?' · '+job.organization.reason:'')+(job.stage==='ready'&&job.issues_revision!==revision?' · Newer edits are not in this handoff; export again to include them.':'');
  document.querySelectorAll('[data-stage]').forEach(el=>el.classList.toggle('current',el.dataset.stage===job.stage));
  $("skip-organization").hidden=job.stage!=='organizing';$("download-again").hidden=job.stage!=='ready';
  $("session-link").textContent=job.session_link?.message||(job.stage==='ready'&&latest.hosted?'Linking to the LocalDev session…':'');
  $("retry-session-link").hidden=job.stage!=='ready'||!job.session_link?.retryable;
  if(job.stage==='ready'&&pendingExport?.id===job.id&&!pendingExport.downloaded){pendingExport.downloaded=true;try{await downloadJob(job);}catch(error){fail(error);}}
}
$("export-handoff").onclick=()=>startExport('md');$("export-bundle").onclick=()=>startExport('zip');
$("skip-organization").onclick=async()=>{try{await api('export-skip',{id:latest.export_job.id});await refresh();}catch(error){fail(error);}};
$("download-again").onclick=()=>downloadJob(latest.export_job).catch(fail);
$("retry-session-link").onclick=async()=>{try{await api('export-link',{id:latest.export_job.id});await refresh();}catch(error){fail(error);}};
window.addEventListener('beforeunload',event=>{persist();if(generation!==savedGeneration){event.preventDefault();event.returnValue='';}});
async function poll(){try{await refresh();}catch(error){if(!loaded)fail(error);}if(!stopped)setTimeout(poll,latest.export_job&&!['ready','failed'].includes(latest.export_job.stage)?400:1800);}
poll();

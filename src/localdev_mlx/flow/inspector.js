"use strict";
(() => {
  if (window.__projectFlow) return;
  const token = "__CAPTURE_TOKEN__", storageKey = "__project_flow_actions", revision = "__PREVIEW_REVISION__";
  const safeKeys = __SAFE_QUERY_KEYS__;
  const personal = "__FLOW_MODE__" === "personal";
  const privateSelector = 'input,textarea,select,[contenteditable],[data-flow-secret]' + (personal ? '' : ',[data-flow-private]');
  const pageID = crypto.randomUUID();
  function safePath() {const params=new URLSearchParams();for(const [key,value] of new URLSearchParams(location.search))if(safeKeys.includes(key)&&!/(token|password|secret|key|cookie)/i.test(key))params.set(key,value.slice(0,120));return location.pathname+(params.size?'?'+params:'');}
  let selecting = false, selected = null, hovered = null, busy = false, corner = 0;
  let actions = [];
  let startPath=safePath();
  try {if(sessionStorage.getItem('__project_flow_revision')!==revision){sessionStorage.removeItem(storageKey);sessionStorage.setItem('__project_flow_revision',revision);sessionStorage.setItem('__project_flow_start',startPath);}actions = JSON.parse(sessionStorage.getItem(storageKey) || "[]").slice(-100);startPath=sessionStorage.getItem('__project_flow_start') || startPath;} catch (_) {}
  const privateElement = el => el?.closest(privateSelector);
  const visibleText = el => {
    const excluded = node => privateElement(node) || node.closest('script,style,[hidden],[aria-hidden="true"],[data-project-flow-overlay]') || getComputedStyle(node).display==='none' || getComputedStyle(node).visibility==='hidden';
    if (!el || excluded(el)) return "";
    const walker=document.createTreeWalker(el,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:node=>node.nodeType===Node.TEXT_NODE?NodeFilter.FILTER_ACCEPT:excluded(node)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_SKIP});
    let text='', current;
    while(text.length<600&&(current=walker.nextNode()))text+=' '+current.textContent.trim();
    return text.trim().replace(/\s+/g," ").slice(0,600);
  };
  const shortText = value => {const text=(value || '').trim().replace(/\s+/g,' ');return text.length>80?text.slice(0,79)+'…':text;};
  const isVisible = el => !!el && !el.closest('[hidden],[aria-hidden="true"],[data-flow-secret],[data-project-flow-overlay]' + (personal?'':',[data-flow-private]')) && el.getClientRects().length>0 && getComputedStyle(el).visibility!=='hidden';
  function accessibleLabel(el) {
    if (!el || privateElement(el)) return '';
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>document.getElementById(id)).filter(node=>isVisible(node)&&!privateElement(node)).map(visibleText).join(' ');
    return shortText(labelled || el.getAttribute('aria-label') || el.getAttribute('title') || (el instanceof HTMLImageElement ? el.alt : ''));
  }
  function headingLabel(root) {
    const heading = [...(root?.querySelectorAll('h1,h2,h3,h4,[role="heading"]') || [])].find(isVisible);
    return shortText(visibleText(heading));
  }
  function surface(el) {
    const dialog=el?.closest('dialog[open],[role="dialog"],[role="alertdialog"]');
    const region=el?.closest('section,article,nav,aside,[role="region"],[role="tabpanel"]');
    const scope=dialog || el?.closest('main,[role="main"]') || document.body;
    const tab=[...scope.querySelectorAll('[role="tab"][aria-selected="true"]')].find(isVisible);
    // Only visible labels/headings, never input values or hidden/private panel text.
    const pageRoot=document.querySelector('main,[role="main"]');
    const element=privateElement(el)?'Form control':accessibleLabel(el) || headingLabel(el) || shortText(visibleText(el)) || shortText(el?.tagName?.toLowerCase());
    return {
      page: headingLabel(pageRoot) || shortText(document.title),
      dialog: accessibleLabel(dialog) || headingLabel(dialog),
      section: shortText(visibleText(tab)) || accessibleLabel(region) || headingLabel(region),
      element,
    };
  }
  function selector(el) {
    if (!el || el === document.body) return "body";
    const unique = value => { try { return document.querySelectorAll(value).length === 1; } catch (_) { return false; } };
    if (el.id && unique(`#${CSS.escape(el.id)}`)) return `#${CSS.escape(el.id)}`;
    for (const key of ["data-flow-id", "data-testid", "data-test", "data-action", "aria-label"]) {
      const value = el.getAttribute(key), candidate = value && `[${key}="${CSS.escape(value)}"]`;
      if (candidate && unique(candidate)) return candidate;
    }
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 12) {
      const peers = el.parentElement ? [...el.parentElement.children].filter(p => p.tagName === el.tagName) : [];
      parts.unshift(el.tagName.toLowerCase() + (peers.length > 1 ? `:nth-of-type(${peers.indexOf(el) + 1})` : ""));
      if (unique(parts.join(" > "))) break;
      el = el.parentElement;
    }
    return parts.join(" > ");
  }
  function snapshot(el) {
    const r = el?.getBoundingClientRect(), style = el ? getComputedStyle(el) : null;
    const parents=[]; let ancestor=el?.parentElement;
    for(let i=0;ancestor&&i<4;i++,ancestor=ancestor.parentElement){const box=ancestor.getBoundingClientRect(),s=getComputedStyle(ancestor);parents.push({selector:selector(ancestor),geometry:{x:box.x,y:box.y,width:box.width,height:box.height,scroll_width:ancestor.scrollWidth,client_width:ancestor.clientWidth},computed:{display:s.display,position:s.position,overflow_x:s.overflowX,overflow_y:s.overflowY,gap:s.gap,align_items:s.alignItems,grid_columns:s.gridTemplateColumns}});}
    const center=r?document.elementFromPoint(r.x+r.width/2,r.y+r.height/2):null;
    const controls = [...document.querySelectorAll('button,a,input,select,textarea,[role="button"]')].filter(node => !node.closest('[data-project-flow-overlay]'));
    return {actions, parents, surface:surface(el), start_path:startPath, path: safePath(), page_id:pageID, environment: {language:document.documentElement.lang, user_agent:navigator.userAgent, width:innerWidth, height:innerHeight, scale:devicePixelRatio},
      geometry: r ? {x:r.x,y:r.y,width:r.width,height:r.height,scroll_width:el.scrollWidth,client_width:el.clientWidth} : {},
      computed: style ? {display:style.display,position:style.position,overflow:style.overflow,z_index:style.zIndex,color:style.color,background:style.backgroundColor,font_size:style.fontSize,line_height:style.lineHeight} : {},
      audit: {found:!!el, unique:el ? document.querySelectorAll(selector(el)).length === 1 : false, clipped:r ? r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight : false, accessible_name:!!(el?.getAttribute('aria-label') || visibleText(el)), controls:controls.length, stable_controls:controls.filter(node => node.id || node.hasAttribute('data-testid') || node.hasAttribute('data-flow-id') || node.hasAttribute('aria-label')).length},
      state:{open_dialogs:[...document.querySelectorAll('dialog[open],[role="dialog"]')].slice(0,8).map(selector), focused:selector(document.activeElement), ready_state:document.readyState,
        target:{tag:el?.tagName,role:el?.getAttribute('role'),disabled:!!el?.disabled,busy:el?.getAttribute('aria-busy'),expanded:el?.getAttribute('aria-expanded'),hidden:!!el?.hidden,
          center_hit:center?selector(center):null,center_covered:!!(center&&el&&!el.contains(center)&&!center.closest('[data-project-flow-overlay]')),
          image:el instanceof HTMLImageElement?{complete:el.complete,natural_width:el.naturalWidth,natural_height:el.naturalHeight}:null}}};
  }
  window.__projectFlow = {snapshot, selector};
  const overlay = document.createElement("aside");
  overlay.dataset.projectFlowOverlay = "true";
  overlay.setAttribute("aria-label", "Project Flow inspector");
  if(personal){const badge=document.createElement('span');badge.textContent='Personal copy · offline';badge.title='No live-data writeback. Background jobs, sync and models are disabled.';overlay.append(badge);}
  Object.assign(overlay.style, {position:"fixed",right:"10px",bottom:"10px",zIndex:"2147483647",display:"flex",gap:"5px",alignItems:"center",maxWidth:"calc(100vw - 20px)",padding:"6px",border:"1px solid #536c60",borderRadius:"12px",background:"#15231f",color:"#f1faf5",font:"12px system-ui",boxShadow:"0 4px 20px #0005"});
  function button(label, handler) {
    const node = document.createElement("button"); node.type="button"; node.textContent=label;
    Object.assign(node.style,{font:"inherit",padding:"5px 8px",color:"inherit",background:"#26483b",border:"none",borderRadius:"7px",cursor:"pointer"});
    node.addEventListener("click",handler); overlay.append(node); return node;
  }
  const selectionStyle = document.createElement("style");
  selectionStyle.dataset.projectFlowOverlay = "true";
  selectionStyle.textContent = '[data-project-flow-selecting], [data-project-flow-selecting] * { cursor: crosshair !important; } [data-project-flow-target] { outline: 2px solid #24cd09 !important; outline-offset: 2px !important; }';
  function highlight(el) {
    hovered?.removeAttribute('data-project-flow-target');
    hovered=el;
    hovered?.setAttribute('data-project-flow-target','');
  }
  function setSelecting(active) {
    selecting = !!active && !busy;
    highlight(null);
    syncOverlay();
    toggle.textContent = selecting ? "Selecting…" : "Select";
    toggle.setAttribute("aria-pressed", String(selecting));
    document.documentElement.toggleAttribute("data-project-flow-selecting", selecting);
    if (selecting) {
      overlay.hidden=false;
      [toggle,pageCapture,parent,move,status].forEach(node=>node.hidden=false);
      fold.textContent='−';
      if (!selectionStyle.isConnected) (document.head || document.documentElement).append(selectionStyle);
      status.textContent = "Click an element · Esc cancels";
    } else if (!busy) status.textContent = "";
  }
  const toggle = button("Select", () => setSelecting(!selecting));
  toggle.setAttribute("aria-pressed", "false");
  toggle.setAttribute("aria-keyshortcuts", "Alt+Shift+I");
  toggle.title = "Select an element · Option/Alt + Shift + I · Esc cancels";
  const pageCapture = button("Page", () => capture(document.body));
  const parent = button("Parent", () => selected?.parentElement && capture(selected.parentElement));
  const move = button("Move", () => { corner=(corner+1)%4; overlay.style.left=corner===1||corner===2 ? "10px":"auto"; overlay.style.right=corner===1||corner===2 ? "auto":"10px"; overlay.style.top=corner>=2 ? "10px":"auto"; overlay.style.bottom=corner>=2 ? "auto":"10px"; });
  const fold = button("−", e => { const folded = !toggle.hidden; [toggle,pageCapture,parent,move,status].forEach(n=>n.hidden=folded); e.currentTarget.textContent=folded?"Flow":"−"; });
  const status = document.createElement("span"); status.setAttribute("role","status"); status.style.maxWidth="160px"; overlay.append(status);
  // showModal() makes everything outside the dialog inert, even a maximum-z-index
  // toolbar. Keep the controls and live feedback inside the active modal instead.
  let modalStack=[];
  function syncOverlay() {
    const dialogs=[...document.querySelectorAll('dialog[open],[role="dialog"][aria-modal="true"],[role="alertdialog"][aria-modal="true"]')].filter(isVisible);
    modalStack=modalStack.filter(node=>dialogs.includes(node));
    for(const dialog of dialogs)if(!modalStack.includes(dialog))modalStack.push(dialog);
    const focused=document.activeElement?.closest('dialog[open],[aria-modal="true"]');
    if(focused&&modalStack.includes(focused)&&focused!==modalStack.at(-1))modalStack=[...modalStack.filter(node=>node!==focused),focused];
    const mount=modalStack.at(-1) || document.body;
    if(overlay.parentElement!==mount)mount.append(overlay);
  }
  new MutationObserver(syncOverlay).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['open','aria-modal','role','hidden']});
  syncOverlay();
  async function capture(el) {
    if (busy || !el) return;
    busy = true; setSelecting(false); selected=el; status.textContent="Capturing…";
    try {
      const result = await fetch("/__project_flow_capture", {method:"POST",headers:{"Content-Type":"application/json","X-Flow-Capture":token},body:JSON.stringify({selector:selector(el),text:visibleText(el),path:safePath(),page_id:pageID,capture:snapshot(el)})});
      const data=await result.json(); if(!result.ok) throw new Error(data.error || "Capture failed");
      status.textContent=data.summary?.has_screenshot ? "Saved + screenshots" : "Saved · no screenshot";
    } catch(error) {status.textContent=error.message;} finally {busy=false;}
  }
  window.addEventListener("pointermove", e => {
    if(selecting&&e.target instanceof Element)highlight(e.target.closest('[data-project-flow-overlay]')?null:e.target);
  },true);
  // A selection must not fire app pointer handlers or change a focused form first.
  for(const type of ['pointerdown','mousedown','pointerup','mouseup'])window.addEventListener(type,e=>{
    if(selecting&&e.button===0&&e.target instanceof Element&&!e.target.closest('[data-project-flow-overlay]')){e.preventDefault();e.stopImmediatePropagation();}
  },true);
  window.addEventListener("click", e => {
    if (!(e.target instanceof Element) || e.target.closest('[data-project-flow-overlay]')) return;
    if (selecting || e.altKey) {e.preventDefault();e.stopImmediatePropagation();void capture(e.target.closest('svg')?.parentElement || e.target);return;}
    record("click", e.target);
  },true);
  function record(type, el) {
    if (el.closest('[data-project-flow-overlay]')) return;
    actions.push({id:crypto.randomUUID(),ts_ms:Date.now(),at:new Date().toISOString(),type,selector:selector(el),requires_input:type!=="click",path:safePath()});
    actions=actions.slice(-100); try{sessionStorage.setItem(storageKey,JSON.stringify(actions));}catch(_){}
  }
  document.addEventListener("change", e=> {if(e.target instanceof Element)record("change",e.target);},true);
  // Window capture works inside app dialogs and after SPA updates remove the toolbar.
  // Physical KeyI also handles macOS Option producing a non-ASCII event.key.
  window.addEventListener("keydown", e => {
    const shortcut = e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey &&
      (e.code === "KeyI" || (!e.isComposing && !e.code && e.key?.toLowerCase() === "i"));
    // On macOS Option-I can be a dead/composing key, particularly in form fields.
    // Recognize the explicit physical shortcut before discarding composition events.
    if (shortcut) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (!e.repeat) setSelecting(true);
    } else if (e.key === "Escape" && selecting) {
      e.preventDefault(); e.stopImmediatePropagation();
      setSelecting(false);
    }
  }, true);
})();

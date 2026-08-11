const COMMAND_HISTORY_KEY='multiagent-provider-command-history';
const CHAT_OVERRIDES_KEY='multiagent-chat-overrides';
const THEME_KEY='multiagent-theme';
const VERSION_CONSOLIDATED_KEY='multiagent-version-consolidated';
const INTERNAL_CONTINUATION_PROMPT='Process the queued team messages now. Follow each command, use reports as context, and send a concise report to the requesting agent when the work is complete.';
function loadCommandHistory(){
  try{
    const value=JSON.parse(localStorage.getItem(COMMAND_HISTORY_KEY)||'[]');
    if(!Array.isArray(value))return[];
    return value.map(item=>typeof item==='string'?{
      provider: '*', name: item
    }: {
      provider: item.provider||'*', name: item.name||''
    }).filter(item=>item.name.startsWith('/'))
  }
  catch{
    return[]
  }
}
function loadChatOverrides(){
  try{
    const value=JSON.parse(localStorage.getItem(CHAT_OVERRIDES_KEY)||'{}');
    return value&&typeof value==='object'&&!Array.isArray(value)?value:{}
  }
  catch{
    return{}
  }
}
function chatOverrideKey(projectId, role){
  return `${projectId}:${role}`
}
function chatOverride(projectId, role){
  const value=state.chatOverrides[chatOverrideKey(projectId, role)];
  return value&&typeof value==='object'?value:{model:'', effort:''}
}
function saveChatOverride(projectId, role, model, effort){
  const key=chatOverrideKey(projectId, role);
  if(!model&&!effort)delete state.chatOverrides[key];
  else state.chatOverrides[key]={model, effort};
  localStorage.setItem(CHAT_OVERRIDES_KEY, JSON.stringify(state.chatOverrides));
}
function preferredTheme(){
  const saved=localStorage.getItem(THEME_KEY);
  return saved==='dark'||saved==='light'?saved:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')
}
function applyTheme(theme){
  const dark=theme==='dark';
  document.documentElement.dataset.theme=dark?'dark':'light';
  localStorage.setItem(THEME_KEY,dark?'dark':'light');
  const button=$('#theme-toggle');
  if(button){
    const description=button.querySelector('small');
    if(description)description.textContent=dark?'Switch to light mode.':'Switch to dark mode.';
    else button.textContent=dark?'Light mode':'Dark mode';
    button.setAttribute('aria-label',dark?'Switch to light mode':'Switch to dark mode');
    button.setAttribute('aria-pressed',String(dark))
  }
}
const state={
  agents: [],
  providers: [],
  projects: [],
  skills: [],
  activeSkillId: null,
  skillSearch: '',
  skillSort: 'name',
  toolsets: [],
  activeToolsetSlug: null,
  toolsetSearch: '',
  gitStatus: null,
  gitOverview: null,
  selectedVersionBranch: '',
  selectedVersionBranches: [],
  selectedVersionCommit: '',
  selectedVersionCommitDetail: null,
  selectedVersionDiffPath: '',
  selectedVersionDiff: '',
  versionCommitDiffLoading: false,
  versionCommitDetailLoading: false,
  versionCommitDetailRequest: 0,
  versionCommitDiffRequest: 0,
  versionControlLoadPromise: null,
  versionCommitActionBusy: false,
  versionCommitActionStatus: '',
  versionCommitTargetBranch: '',
  versionGraphConsolidated: false,
  versionConsolidationBusy: false,
  versionConsolidationStatus: '',
  versionGraphView: null,
  versionGraphFull: null,
  versionGraphPanMoved: false,
  versionAgentSearch: '',
  pendingGitAgent: null,
  pendingGitEnableAgent: null,
  marketplace: [],
  project: null,
  layout: [],
  edges: [],
  templates: [],
  workflowTemplates: [],
  actionPermissions: null,
  workflowMemories: [],
  activeWorkflowMemoryId: 0,
  contextAgent: null,
  active: 'orchestrator',
  context: [],
  messages: {},
  pendingAttachments: {},
  replyTo: {},
  activities: {},
  runs: {},
  busy: new Set(),
  runtimeModels: [],
  chatModels: [],
  providerCommands: [],
  commandHistory: loadCommandHistory(),
  historySearch: '',
  commandIndex: 0,
  drawingLink: null,
  relationshipMode: 'move',
  runPollPromise: null,
  runWatchers: {},
  historyLoads: {},
  contextUsage: {},
  contextUsageFetchedAt: {},
  contextUsageLoads: {},
  chatOverrides: loadChatOverrides(),
  runtimeModelRequest: 0,
  chatControlsRequest: 0,
  agentSearch: ''
};
const $=s=>document.querySelector(s);
let messagesScrollFrame=0,
messagesScrollTimer=0,
observedMessagesBox=null,
messagesMutationObserver=null,
messagesResizeObserver=null,
messagesPinnedToLatest=true,
messagesRendering=false;
const MESSAGES_BOTTOM_THRESHOLD=8;
function messagesIsNearLatest(box=$('#messages')){
  if(!box)return true;
  return box.scrollHeight-box.clientHeight-box.scrollTop<=MESSAGES_BOTTOM_THRESHOLD
}
function cancelScheduledMessagesScroll(){
  if(messagesScrollFrame){
    cancelAnimationFrame(messagesScrollFrame);
    messagesScrollFrame=0
  }
  if(messagesScrollTimer){
    clearTimeout(messagesScrollTimer);
    messagesScrollTimer=0
  }
}
function stopFollowingMessages(box=$('#messages')){
  if(messagesRendering)return;
  messagesPinnedToLatest=false;
  cancelScheduledMessagesScroll();
  updateLatestMessagesButton()
}
function updateLatestMessagesButton(){
  const box=$('#messages'), button=$('#jump-to-latest');
  if(!box||!button)return;
  const show=!messagesPinnedToLatest&&box.scrollHeight>box.clientHeight+4;
  button.classList.toggle('hidden',!show);
  button.setAttribute('aria-hidden',String(!show))
}
function scrollMessagesToLatest(force=false){
  const box=$('#messages');
  if(!box)return;
  if(!force&&!messagesPinnedToLatest){
    cancelScheduledMessagesScroll();
    updateLatestMessagesButton();
    return
  }
  const apply=()=>{
    if(!box.isConnected)return;
    if(!force&&!messagesPinnedToLatest){
      updateLatestMessagesButton();
      return
    }
    // Use the largest valid offset rather than relying on the browser to
    // clamp scrollHeight. This remains reliable when the pane is flex-sized
    // or was hidden while the selected agent's history was rendered.
    messagesPinnedToLatest=true;
    box.scrollTop=Math.max(0, box.scrollHeight-box.clientHeight);
    updateLatestMessagesButton()
  };
  cancelScheduledMessagesScroll();
  apply();
  messagesScrollFrame=requestAnimationFrame(()=>{
    messagesScrollFrame=0;
    apply();
    messagesScrollFrame=requestAnimationFrame(()=>{
      messagesScrollFrame=0;
      apply()
    })
  });
  // Give late layout changes (for example a long formatted response) one
  // final opportunity to update the scroll height.
  messagesScrollTimer=setTimeout(()=>{
    messagesScrollTimer=0;
    apply()
  }, 150)
}
function observeMessagesForAutoScroll(){
  const box=$('#messages');
  if(!box)return;
  if(observedMessagesBox!==box){
    messagesMutationObserver?.disconnect();
    messagesResizeObserver?.disconnect();
    observedMessagesBox=box;
    if(typeof MutationObserver!=='undefined'){
      messagesMutationObserver=new MutationObserver(()=>{
        observeMessagesForAutoScroll();
        scrollMessagesToLatest()
      });
      messagesMutationObserver.observe(box, {childList:true, subtree:true, characterData:true})
    }
    box.addEventListener('scroll',()=>{
      // Re-rendering a transcript can temporarily clamp scrollTop while
      // innerHTML is replaced. That is not user intent and must not turn a
      // reader who is above the latest message back into a live-follow chat.
      if(messagesRendering)return;
      if(messagesIsNearLatest(box)){
        messagesPinnedToLatest=true;
        updateLatestMessagesButton()
      }else stopFollowingMessages(box)
    }, {passive:true});
    box.addEventListener('wheel',event=>{
      // Lock on the user's upward intent before the browser emits its scroll
      // event, so a polling/render pass cannot win the race and pull down.
      if(event.deltaY<0)stopFollowingMessages(box)
    }, {passive:true});
  }
  // A formatter, font, or image can change a message's height without
  // changing the DOM. Observe the message wrappers so those late layout
  // changes also keep the active transcript pinned to its newest message.
  if(typeof ResizeObserver!=='undefined'){
    messagesResizeObserver??=new ResizeObserver(()=>scrollMessagesToLatest());
    messagesResizeObserver.disconnect();
    messagesResizeObserver.observe(box);
    Array.from(box.children).forEach(child=>messagesResizeObserver.observe(child))
  }
  updateLatestMessagesButton()
}
const commands=[
{
  name: '/gh',
  args: '',
  description: 'Show the current repository, branch, changes, commits, and remotes'
},
{
  name: '/app help',
  args: '',
  description: 'Show app-only commands (provider slash commands remain untouched)'
},
{
  name: '/app agent',
  args: '<role>',
  description: 'Switch to another team member'
},
{
  name: '/app clear',
  args: '',
  description: 'Clear this agent’s saved conversation'
},
{
  name: '/app context',
  args: '',
  description: 'Create a shared context card'
},
{
  name: '/app model',
  args: '<model>',
  description: 'Select an app-side model override for this message'
},
{
  name: '/app effort',
  args: '<level>',
  description: 'Select an app-side reasoning effort override'
},
{
  name: '/app skill',
  args: '<skill> [JSON]',
  description: 'Run an assigned reusable skill with a JSON inputs object'
},
{
  name: '/app project',
  args: '',
  description: 'Return to the project and team map'
},
{
  name: '/app team',
  args: '',
  description: 'Open the agent hierarchy'
},
];
function errorText(detail, fallback){
  if(typeof detail==='string')return detail;
  if(detail)return JSON.stringify(detail, null, 2);
  return fallback
}
async function api(path, options={}){
  const r=await fetch(path, {
    headers: {
      'Content-Type': 'application/json'
    }, ...options
  });
  if(!r.ok){
    let d;
    try{
      d=await r.json()
    } catch{}
    const error=new Error(errorText(d?.detail, `Request failed (${r.status})`));
    error.status=r.status;
    throw error
  }
  return r.status===204?null: r.json()
}
async function apiNdjson(path, options={}, onEvent=()=>{}){
  const r=await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/x-ndjson'
    }, ...options
  });
  if(!r.ok){
    let d;
    try{d=await r.json()}
    catch{}
    throw new Error(errorText(d?.detail, `Request failed (${r.status})`))
  }
  if(!r.body?.getReader)throw new Error('This browser cannot show marketplace download progress.');
  const reader=r.body.getReader(), decoder=new TextDecoder();
  let buffer='', completed=null;
  const consume=line=>{
    if(!line.trim())return;
    let event;
    try{event=JSON.parse(line)}
    catch{throw new Error('Marketplace returned an invalid progress event.');}
    onEvent(event);
    if(event.event==='error')throw new Error(event.detail||'Marketplace download failed.');
    if(event.event==='complete')completed=event.skill;
  };
  while(true){
    const {value,done}=await reader.read();
    buffer+=decoder.decode(value||new Uint8Array(), {stream:!done});
    const lines=buffer.split('\n');
    buffer=lines.pop()||'';
    lines.forEach(consume);
    if(done)break
  }
  if(buffer.trim())consume(buffer);
  if(!completed)throw new Error('Marketplace download ended without an installed skill.');
  return completed
}
async function upload(path, form){
  const r=await fetch(path, {
    method: 'POST', body: form
  });
  if(!r.ok){
    let d;
    try{
      d=await r.json()
    }
    catch{}throw new Error(d?.detail||`Upload failed (${r.status})`)
  }
  return r.json()
}
const activeAgent=()=>state.agents.find(x=>x.id===state.active);
function providerName(id){
  return state.providers.find(p=>p.id===id)?.name||id
}
function runtimeLabel(r){
  return `${providerName(r.provider)}${r.model?` · ${r.model}`:''}${r.reasoning_effort?` · ${r.reasoning_effort}`:''}`
}
function showDashboard(){
  $('#workspace').classList.add('hidden');
  $('#version-control').classList.add('hidden');
  $('#dashboard').classList.remove('hidden');
  document.querySelectorAll('.settings-tabs a').forEach(item=>item.classList.toggle('active',item.getAttribute('href')==='#dashboard'));
  renderFlowchart()
}
function showWorkspace(role=state.active){
  $('#dashboard').classList.add('hidden');
  $('#version-control').classList.add('hidden');
  $('#workspace').classList.remove('hidden');
  document.querySelectorAll('.settings-tabs a').forEach(item=>item.classList.toggle('active',item.getAttribute('href')==='#workspace'));
  selectAgent(role);
  // The workspace may have been hidden while the transcript was first
  // rendered. Scroll again after making it visible so its dimensions exist.
  requestAnimationFrame(scrollMessagesToLatest)
}
async function showVersionControl(){
  const opening=$('#version-control').classList.contains('hidden');
  $('#dashboard').classList.add('hidden');
  $('#workspace').classList.add('hidden');
  $('#version-control').classList.remove('hidden');
  document.querySelectorAll('.settings-tabs a').forEach(item=>item.classList.toggle('active',item.getAttribute('href')==='#version-control'));
  if(opening)state.versionGraphView=null;
  await loadVersionControl()
}
document.querySelectorAll('.settings-tabs a').forEach(tab=>tab.addEventListener('click', event=>{
  event.preventDefault();
  const target=tab.getAttribute('href');
  if(target==='#dashboard')showDashboard();
  else if(target==='#workspace'||target==='#context-list'){
    showWorkspace();
    if(target==='#context-list')setTimeout(()=>$('#context-list')?.scrollIntoView({behavior:'smooth',block:'start'}),0)
  }else if(target==='#version-control')showVersionControl().catch(err=>alert(err.message));
  document.querySelectorAll('.settings-tabs a').forEach(item=>item.classList.toggle('active',item===tab));
}));
function renderProjects(){
  const nav=$('#projects');
  nav.innerHTML='';
  state.projects.forEach(project=>{
    const b=document.createElement('button');
    b.className=`project-button ${state.project?.id===project.id?'active':''}`;
    const strong=document.createElement('strong'), small=document.createElement('small');
    strong.textContent=project.name;
    small.textContent=project.root_path||project.description||'Agent workspace';
    b.append(strong, small);
    b.onclick=()=>selectProject(project.id);
    nav.append(b)
  })
}
function skillById(id){
  return state.skills.find(skill=>Number(skill.id)===Number(id))||null
}
function skillSchemaText(value){
  return JSON.stringify(value||{}, null, 2)
}
function ensureSkillSecretEditor(){
  if($('#skill-required-secrets'))return;
  const body=$('#skill-body'), bodyLabel=body?.closest('label');
  if(!bodyLabel)return;
  const label=document.createElement('label');
  label.id='skill-secret-definition-label';
  label.append(document.createTextNode('Required API keys / secrets'));
  const help=document.createElement('small');
  help.textContent='Declare names only as a JSON array. Values are stored locally and are never added to SKILL.md, prompts, or chat.';
  const textarea=document.createElement('textarea');
  textarea.id='skill-required-secrets';
  textarea.rows=4;
  textarea.spellcheck=false;
  textarea.placeholder='[{"name":"WEATHER_API_KEY","label":"Weather API key","required":true}]';
  textarea.className='skill-secret-json';
  const status=document.createElement('div');
  status.id='skill-secret-status';
  status.className='skill-secret-status';
  label.append(help, textarea, status);
  bodyLabel.parentElement.insertBefore(label, bodyLabel);
}
function renderSkillSecretStatus(skill, statuses=null){
  ensureSkillSecretEditor();
  const box=$('#skill-secret-status');
  if(!box)return;
  box.innerHTML='';
  if(!skill||!skill.id){
    box.textContent='Save the skill first, then configure any declared keys here.';
    return
  }
  const refs=statuses||skill.secret_status||skill.required_secrets||[];
  if(!refs.length){
    box.textContent='No skill-specific secrets declared.';
    return
  }
  refs.forEach(ref=>{
    const row=document.createElement('div'), info=document.createElement('div'), title=document.createElement('strong'), description=document.createElement('small'), stateLabel=document.createElement('span'), input=document.createElement('input'), actions=document.createElement('div'), save=document.createElement('button'), clear=document.createElement('button');
    row.className='skill-secret-row';
    info.className='skill-secret-info';
    title.textContent=ref.label||ref.name;
    description.textContent=`${ref.name}${ref.description?` · ${ref.description}`:''}`;
    stateLabel.className=`skill-secret-state ${ref.configured?'configured':'missing'}`;
    stateLabel.textContent=ref.configured?'Configured':'Missing';
    input.type='password';
    input.autocomplete='new-password';
    input.className='skill-secret-input';
    input.placeholder=ref.configured?'Replace local value':'Paste value (never displayed)';
    save.type='button'; save.className='secondary compact'; save.textContent='Save';
    clear.type='button'; clear.className='danger compact'; clear.textContent='Clear';
    save.onclick=async()=>{
      const value=input.value.trim();
      if(!value)return alert('Enter a value before saving this secret.');
      save.disabled=true;
      try{
        await api(`/api/skills/${skill.id}/secrets/${encodeURIComponent(ref.name)}`, {method:'PUT', body:JSON.stringify({credential:value})});
        input.value='';
        await loadSkillSecretStatus(skill)
      }
      catch(err){alert(err.message)}
      finally{save.disabled=false}
    };
    clear.onclick=async()=>{
      if(!ref.configured||!confirm(`Clear the locally stored value for ${ref.name}?`))return;
      clear.disabled=true;
      try{
        await api(`/api/skills/${skill.id}/secrets/${encodeURIComponent(ref.name)}`, {method:'DELETE'});
        await loadSkillSecretStatus(skill)
      }
      catch(err){alert(err.message)}
      finally{clear.disabled=false}
    };
    info.append(title, description); actions.append(stateLabel, save, clear); row.append(info, input, actions); box.append(row)
  });
}
async function loadSkillSecretStatus(skill){
  if(!skill?.id)return;
  try{
    const data=await api(`/api/skills/${skill.id}/secrets`);
    skill.secret_status=data.secrets||[];
    if(Number(state.activeSkillId)===Number(skill.id))renderSkillSecretStatus(skill, skill.secret_status)
  }
  catch(err){
    if(Number(state.activeSkillId)===Number(skill.id)){
      const box=$('#skill-secret-status');
      if(box)box.textContent=`Could not load secret status: ${err.message}`;
    }
  }
}
function renderSkillsList(){
  const list=$('#skills-list');
  if(!list)return;
  const count=$('#skills-count');
  const query=state.skillSearch.trim().toLowerCase();
  const filtered=state.skills.filter(skill=>{
    const text=`${skill.name} ${skill.summary} ${skill.slug} ${skill.author||''}`.toLowerCase();
    return !query||text.includes(query)
  });
  filtered.sort((a,b)=>{
    if(state.skillSort==='recent')return String(b.updated_at||'').localeCompare(String(a.updated_at||''));
    if(state.skillSort==='source')return String(a.source||'').localeCompare(String(b.source||''))||String(a.name).localeCompare(String(b.name));
    return String(a.name||'').localeCompare(String(b.name||''))
  });
  if(count)count.textContent=filtered.length===state.skills.length?state.skills.length:`${filtered.length}/${state.skills.length}`;
  list.innerHTML='';
  if(!filtered.length){
    list.innerHTML=`<div class="skills-empty">${state.skills.length?'No installed skills match this search.':'No ACP skills yet. Create one or open the marketplace.'}</div>`;
    return
  }
  filtered.forEach(skill=>{
    const button=document.createElement('button');
    button.type='button';
    button.className=`skill-list-item ${Number(skill.id)===Number(state.activeSkillId)?'active':''}`;
    const name=document.createElement('strong'), summary=document.createElement('span'), assigned=document.createElement('small');
    name.textContent=skill.name;
    summary.textContent=skill.summary;
    const missingSecrets=(skill.secret_status||[]).filter(item=>item.required!==false&&!item.configured).length;
    assigned.textContent=`${skill.version||'1.0.0'} / ${skill.source||'local'} / ${skill.assigned_roles?.length||0} assigned agent${skill.assigned_roles?.length===1?'':'s'}${missingSecrets?` / ${missingSecrets} missing key${missingSecrets===1?'':'s'}`:''}`;
    button.append(name, summary, assigned);
    button.onclick=()=>selectSkill(skill.id);
    list.append(button)
  })
}
function renderSkillAssignments(skill){
  const checks=$('#skill-agent-checks');
  if(!checks)return;
  checks.innerHTML='';
  state.agents.forEach(agent=>{
    const label=document.createElement('label'), input=document.createElement('input');
    input.type='checkbox';
    input.value=agent.id;
    input.checked=Boolean(skill?.assigned_roles?.includes(agent.id));
    label.append(input, document.createTextNode(` ${agent.name}`));
    checks.append(label);
  });
}
function fillSkillEditor(skill=null){
  ensureSkillSecretEditor();
  $('#skill-id').value=skill?.id||'';
  $('#skill-name').value=skill?.name||'';
  $('#skill-slug').value=skill?.slug||'';
  $('#skill-version').value=skill?.version||'1.0.0';
  $('#skill-summary').value=skill?.summary||'';
  $('#skill-compatibility').value=skill?.compatibility||'';
  $('#skill-license').value=skill?.license||'';
  $('#skill-allowed-tools').value=skill?.allowed_tools||skill?.manifest?.['allowed-tools']||'';
  $('#skill-body').value=skill?.body||'';
  $('#skill-required-secrets').value=skillSchemaText(skill?.required_secrets||[]);
  $('#skill-delete').disabled=!skill;
  renderSkillSecretStatus(skill, skill?.secret_status||null);
  renderSkillAssignments(skill)
}
async function selectSkill(id){
  state.activeSkillId=id?Number(id):null;
  const listed=skillById(state.activeSkillId);
  fillSkillEditor(listed);
  renderSkillsList()
  if(!state.activeSkillId||!state.project)return;
  try{
    const detail=await api(`/api/skills/${state.activeSkillId}?project_id=${state.project.id}`);
    if(Number(detail.id)!==Number(state.activeSkillId))return;
    state.skills=state.skills.map(skill=>Number(skill.id)===Number(detail.id)?detail:skill);
    fillSkillEditor(detail);
    renderSkillsList()
    await loadSkillSecretStatus(detail)
  }
  catch(err){
    console.warn('Could not load the full ACP skill package', err)
  }
}
async function loadSkills(){
  if(!state.project){state.skills=[];return}
  state.skills=await api(`/api/skills?project_id=${state.project.id}`);
  renderSkillsList();
  renderActiveSkillSummary()
}
function renderActiveSkillSummary(){
  const output=$('#agent-skills'), agent=activeAgent();
  if(!output)return;
  const assigned=agent?state.skills.filter(skill=>skill.assigned_roles?.includes(agent.id)):[];
  output.textContent=assigned.length?`Skills: ${assigned.map(skill=>skill.name).join(' · ')}`:'No assigned skills';
  output.title=assigned.length?assigned.map(skill=>`${skill.name}: ${skill.summary}`).join('\n'):'Assign reusable skills from the Skills menu.'
}
function openSkillsDialog(skillId=state.activeSkillId){
  $('#skill-library-search').value=state.skillSearch;
  $('#skill-library-sort').value=state.skillSort;
  renderSkillsList();
  selectSkill(skillId||state.skills[0]?.id||null);
  $('#skills-dialog').showModal()
}
function toolsetBySlug(slug){
  return state.toolsets.find(item=>item.slug===slug)||null
}
function renderToolsetsList(){
  const list=$('#toolsets-list');
  if(!list)return;
  const query=state.toolsetSearch.trim().toLowerCase();
  const filtered=state.toolsets.filter(item=>`${item.name} ${item.slug} ${item.description}`.toLowerCase().includes(query));
  $('#toolsets-count').textContent=filtered.length===state.toolsets.length?state.toolsets.length:`${filtered.length}/${state.toolsets.length}`;
  list.innerHTML='';
  if(!filtered.length){
    list.innerHTML=`<div class="skills-empty">${state.toolsets.length?'No toolsets match this search.':'No toolsets yet. Create one to expose local CLI capabilities.'}</div>`;
    return
  }
  filtered.forEach(item=>{
    const button=document.createElement('button'), name=document.createElement('strong'), description=document.createElement('span'), meta=document.createElement('small');
    button.type='button';
    button.className=`skill-list-item ${item.slug===state.activeToolsetSlug?'active':''}`;
    name.textContent=item.name;
    description.textContent=item.description;
    meta.textContent=`${item.tools?.length||0} tool${item.tools?.length===1?'':'s'} / ${item.assigned_roles?.length||0} assigned agent${item.assigned_roles?.length===1?'':'s'}`;
    button.append(name, description, meta);
    button.onclick=()=>selectToolset(item.slug);
    list.append(button)
  })
}
function renderToolsetAssignments(toolset){
  const checks=$('#toolset-agent-checks');
  checks.innerHTML='';
  state.agents.forEach(agent=>{
    const label=document.createElement('label'), input=document.createElement('input');
    input.type='checkbox';
    input.value=agent.id;
    input.checked=Boolean(toolset?.assigned_roles?.includes(agent.id));
    label.append(input, document.createTextNode(` ${agent.name}`));
    checks.append(label)
  })
}
function addToolDefinition(tool={}){
  const card=document.createElement('section');
  card.className='tool-definition-card';
  card.innerHTML='<div class="tool-card-head"><strong>Tool definition</strong><button type="button" class="remove-tool-definition">Remove</button></div><div class="skill-form-grid"><label>Tool name<input class="tool-name" required maxlength="80" placeholder="search-web"></label><label>Executable filename<input class="tool-filename" required maxlength="500" placeholder="search.py"></label></div><label>Description<textarea class="tool-description" rows="2" required maxlength="2000" placeholder="Searches the public web for a query."></textarea></label><div class="skill-form-grid"><label>Inputs<textarea class="tool-inputs" rows="2" maxlength="4000" placeholder="1. Query string; 2. Maximum result count."></textarea></label><label>Outputs<textarea class="tool-outputs" rows="2" maxlength="4000" placeholder="JSON array of matching pages."></textarea></label></div><div class="skill-form-grid"><label>Chat output format<select class="tool-output-format"><option value="text">Text</option><option value="markdown">Markdown</option><option value="json">JSON</option><option value="code">Code block</option></select></label><label>Environment variable names<input class="tool-env-vars" maxlength="2000" placeholder="SEARCH_API_KEY, HTTP_PROXY"></label></div><label>Result template<small>Supported markers: {stdout}, {stderr}, {exit_code}, {toolset}, and {tool}.</small><textarea class="tool-result-template" rows="2" maxlength="20000">{stdout}</textarea></label><label>Executable source<textarea class="tool-source" rows="10" spellcheck="false" maxlength="500000"></textarea></label>';
  card.querySelector('.tool-name').value=tool.name||'';
  card.querySelector('.tool-filename').value=tool.filename||'';
  card.querySelector('.tool-description').value=tool.description||'';
  card.querySelector('.tool-inputs').value=tool.inputs||'No arguments.';
  card.querySelector('.tool-outputs').value=tool.outputs||'Text output.';
  card.querySelector('.tool-output-format').value=tool.output_format||'text';
  card.querySelector('.tool-env-vars').value=(tool.env_vars||[]).join(', ');
  card.querySelector('.tool-result-template').value=tool.result_template||'{stdout}';
  card.querySelector('.tool-source').value=tool.source||'';
  card.querySelector('.remove-tool-definition').onclick=()=>card.remove();
  $('#tool-definitions').append(card)
}
function fillToolsetEditor(toolset=null){
  const persisted=Boolean(toolset?.slug&&Array.isArray(toolset?.assigned_roles));
  state.activeToolsetSlug=persisted?toolset.slug:null;
  $('#toolset-existing-slug').value=persisted?toolset.slug:'';
  $('#toolset-name').value=toolset?.name||'';
  $('#toolset-slug').value=toolset?.slug||'';
  $('#toolset-slug').disabled=persisted;
  $('#toolset-description').value=toolset?.description||'';
  $('#toolset-details').value=toolset?.details||'';
  $('#toolset-delete').disabled=!persisted;
  $('#tool-definitions').innerHTML='';
  const tools=toolset?.tools?.length?toolset.tools:[{
    name:'', filename:'tool.py', description:'', inputs:'No arguments.', outputs:'Text output.',
    output_format:'text', result_template:'{stdout}', env_vars:[],
    source:'import sys\n\nprint(" ".join(sys.argv[1:]))\n'
  }];
  tools.forEach(addToolDefinition);
  renderToolsetAssignments(toolset);
  renderToolsetsList()
}
async function selectToolset(slug){
  if(!slug){fillToolsetEditor(null);return}
  state.activeToolsetSlug=slug;
  const listed=toolsetBySlug(slug);
  if(listed)fillToolsetEditor(listed);
  try{
    const detail=await api(`/api/toolsets/${encodeURIComponent(slug)}?project_id=${state.project.id}`);
    if(state.activeToolsetSlug!==slug)return;
    state.toolsets=state.toolsets.map(item=>item.slug===slug?detail:item);
    fillToolsetEditor(detail)
  }
  catch(err){alert(err.message)}
}
async function loadToolsets(){
  if(!state.project){state.toolsets=[];return}
  state.toolsets=await api(`/api/toolsets?project_id=${state.project.id}`);
  renderToolsetsList()
}
async function openToolsDialog(){
  await loadToolsets();
  $('#toolset-search').value=state.toolsetSearch;
  renderToolsetsList();
  await selectToolset(state.activeToolsetSlug&&toolsetBySlug(state.activeToolsetSlug)?state.activeToolsetSlug:state.toolsets[0]?.slug||null);
  $('#tools-dialog').showModal()
}
async function loadGitStatus(){
  if(!state.project)return null;
  state.gitStatus=await api(`/api/projects/${state.project.id}/git`);
  return state.gitStatus
}
function openGitSetup(status, pendingAgent){
  state.pendingGitAgent=pendingAgent||null;
  const noRepository=!status?.is_repository;
  const statusBox=$('#git-setup-status');
  statusBox.textContent=noRepository
    ? 'This project folder is not a Git repository. Choose its main branch and explicitly confirm initialization.'
    : `Repository: ${status.repository}\nCurrent branch: ${status.current_branch||'(detached HEAD)'}. Git-enabled agents work on role-named branches and merge into the selected main branch.`;
  statusBox.classList.remove('git-setup-error');
  $('#git-branch-input').value=status?.main_branch||status?.branch||((status?.current_branch&&status.current_branch!=='(detached HEAD)')?status.current_branch:'main');
  $('#git-remote-name-input').value='gh';
  $('#git-remote-url-input').value='';
  $('#git-initialize-label').classList.toggle('hidden', !noRepository);
  $('#git-initialize-input').checked=noRepository;
  $('#git-setup-dialog').showModal()
}
async function refreshAgentsAfterGitChange(activeRole=state.active){
  state.agents=await api(`/api/agents?project_id=${state.project.id}`);
  state.active=activeRole;
  renderAgents();
  renderFlowchart();
  if(activeRole)selectAgent(activeRole)
}
async function saveExistingAgentGitEnabled(agent, enabled){
  await api(`/api/projects/${state.project.id}/git/agents/${encodeURIComponent(agent.id)}`, {
    method:'PUT', body:JSON.stringify({enabled})
  });
  await refreshAgentsAfterGitChange(agent.id)
}
async function toggleExistingAgentGit(agent){
  if(agent.git_enabled){
    if(!confirm(`Disable the shared Git workflow for ${agent.name}? Future runs will no longer be auto-committed.`))return;
    await saveExistingAgentGitEnabled(agent, false);
    return
  }
  const status=await loadGitStatus();
  if(status?.configured){
    await saveExistingAgentGitEnabled(agent, true);
    return
  }
  state.pendingGitEnableAgent=agent;
  openGitSetup(status, null)
}
async function createAgent(payload){
  const created=await api('/api/agents', {method:'POST', body:JSON.stringify(payload)});
  $('#agent-dialog').close();
  state.agents=await api(`/api/agents?project_id=${state.project.id}`);
  state.layout=await api(`/api/projects/${state.project.id}/layout`);
  renderAgents();
  renderFlowchart();
  selectAgent(created.role)
}
async function prepareGitAgent(payload){
  const status=await loadGitStatus();
  if(status?.configured){
    await createAgent(payload);
    return
  }
  openGitSetup(status, payload)
}
function gitCommitCard(commit){
  const card=document.createElement('article'), head=document.createElement('div'), title=document.createElement('div'), hash=document.createElement('strong'), meta=document.createElement('span'), actions=document.createElement('div'), files=document.createElement('div');
  card.className='git-commit-card'; head.className='git-commit-head'; actions.className='git-commit-actions'; files.className='git-file-list';
  hash.textContent=commit.commit_hash.slice(0,12);
  meta.textContent=`${commit.message} / ${commit.agent_branch||commit.role} → ${commit.main_branch||state.gitStatus?.main_branch||state.gitStatus?.branch||'main'} / ${commit.state}${commit.pushed?' / pushed':''}`;
  title.append(hash, meta); head.append(title, actions);
  const invoke=async(path, method, body=null)=>{
    await api(`/api/projects/${state.project.id}/git/commits/${commit.commit_hash}/${path}`, {method, body:body?JSON.stringify(body):undefined});
    await openGitChanges()
  };
  const revert=document.createElement('button'); revert.type='button'; revert.className='secondary compact'; revert.textContent='Revert';
  revert.onclick=async()=>{
    if(!confirm(`Create a new commit that reverts ${commit.commit_hash.slice(0,12)}?`))return;
    try{await invoke('revert','POST')}catch(err){alert(err.message)}
  };
  const rollback=document.createElement('button'); rollback.type='button'; rollback.className='danger compact'; rollback.textContent='Rollback HEAD';
  rollback.onclick=async()=>{
    if(!confirm(`Hard-reset the main branch to remove ${commit.commit_hash.slice(0,12)}? This discards that merged agent change from the current branch.`))return;
    try{await invoke('rollback','POST')}catch(err){alert(err.message)}
  };
  const push=document.createElement('button'); push.type='button'; push.className='secondary compact'; push.textContent='Push';
  push.onclick=async()=>{
    const remote=prompt('Remote to push to', state.gitStatus?.configuration?.remote||'origin');
    if(remote===null)return;
    try{await invoke('push','POST',{remote})}catch(err){alert(err.message)}
  };
  if(commit.state==='committed'){actions.append(revert, rollback, push)}
  (commit.files||[]).forEach(file=>{
    const row=document.createElement('div'), status=document.createElement('span'), path=document.createElement('code'), stats=document.createElement('span'), fileActions=document.createElement('div'), diff=document.createElement('button'), pycharm=document.createElement('button'), vscode=document.createElement('button');
    row.className='git-file-row'; status.className='git-file-status'; path.textContent=file.path; stats.className='git-file-stats'; fileActions.className='git-file-actions';
    status.textContent=file.status; stats.textContent=`+${file.additions??'?'} / -${file.deletions??'?'}`;
    diff.type='button'; diff.className='secondary compact'; diff.textContent='View diff';
    diff.onclick=async()=>{
      const out=await api(`/api/projects/${state.project.id}/git/commits/${commit.commit_hash}/diff?path=${encodeURIComponent(file.path)}`);
      let preview=row.querySelector('.git-diff-preview');
      if(!preview){preview=document.createElement('pre');preview.className='git-diff-preview';row.append(preview)}
      preview.textContent=out.diff||'(No textual diff available.)'
    };
    pycharm.type='button'; pycharm.className='secondary compact'; pycharm.textContent='PyCharm';
    pycharm.onclick=()=>invoke('open-diff','POST',{path:file.path,editor:'pycharm'}).catch(err=>alert(err.message));
    vscode.type='button'; vscode.className='secondary compact'; vscode.textContent='VS Code';
    vscode.onclick=()=>invoke('open-diff','POST',{path:file.path,editor:'vscode'}).catch(err=>alert(err.message));
    fileActions.append(diff, pycharm, vscode); row.append(status,path,stats,fileActions); files.append(row)
  });
  card.append(head, files);
  return card
}
function renderGitChanges(status){
  const list=$('#git-changes-list'), agent=activeAgent();
  $('#git-changes-title').textContent=agent?`${agent.name} Git changes`:'Git changes';
  if(!status?.configured){
    $('#git-changes-status').textContent=status?.is_repository
      ? 'No Git workflow is configured. Enable Git for an agent to choose the main branch.'
      : 'This project folder is not a Git repository.';
    list.innerHTML='<div class="git-empty">No shared Git workflow is active.</div>';
    return
  }
  $('#git-changes-status').textContent=`${status.repository} / main branch ${status.main_branch||status.branch} / ${status.clean?'clean':'uncommitted changes'}${status.identity_configured?'':' / Git identity missing'}`;
  const commits=(status.commits||[]).filter(commit=>!agent||commit.role===agent.id);
  list.innerHTML='';
  if(!commits.length){list.innerHTML='<div class="git-empty">This agent has not created a tracked commit yet.</div>';return}
  commits.forEach(commit=>list.append(gitCommitCard(commit)))
}
async function openGitChanges(){
  const status=await loadGitStatus();
  renderGitChanges(status);
  if(!$('#git-changes-dialog').open)$('#git-changes-dialog').showModal()
}
async function loadVersionControl(){
  if(!state.project)return null;
  if(state.versionControlLoadPromise)return state.versionControlLoadPromise;
  const projectId=state.project.id;
  const request=api(`/api/projects/${projectId}/git/overview`).then(overview=>{
    if(state.project?.id===projectId){
      state.gitOverview=overview;
      renderVersionControl(overview)
    }
    return overview
  });
  state.versionControlLoadPromise=request;
  try{return await request}
  finally{if(state.versionControlLoadPromise===request)state.versionControlLoadPromise=null}
}
async function refreshVersionControlIfVisible(){
  if(!state.project||document.hidden||$('#version-control').classList.contains('hidden')||state.versionCommitActionBusy||state.versionConsolidationBusy)return;
  try{await loadVersionControl()}
  catch(err){console.warn('Could not refresh version control',err)}
}
function selectVersionBranch(name, additive=false){
  const selected=[...(state.selectedVersionBranches||[])];
  if(additive&&selected.length){
    const index=selected.indexOf(name);
    if(index>=0&&selected.length>1)selected.splice(index,1);
    else if(index<0)selected.push(name)
  }else selected.splice(0,selected.length,name);
  state.selectedVersionBranches=selected.length?selected:[name];
  state.selectedVersionBranch=state.selectedVersionBranches[state.selectedVersionBranches.length-1]||name;
  renderVersionControl(state.gitOverview)
}
function selectVersionCommit(hash){
  state.selectedVersionCommit=hash;
  state.selectedVersionCommitDetail=null;
  state.selectedVersionDiffPath='';
  state.selectedVersionDiff='';
  state.versionCommitDiffLoading=false;
  state.versionCommitActionStatus='';
  renderVersionCommitGraph(state.gitOverview);
  requestAnimationFrame(()=>$('#version-commit-inspector')?.scrollIntoView({behavior:'smooth',block:'start',inline:'nearest'}));
  loadVersionCommitDetails(hash).catch(err=>{
    state.versionCommitDiffLoading=false;
    state.versionCommitActionStatus=`Could not load commit details: ${err.message}`;
    renderVersionCommitInspector()
  })
}
function renderVersionDiffPreview(diff){
  const host=$('#version-commit-diff');
  if(!host)return;
  host.innerHTML='';
  if(!diff){
    host.textContent='No textual diff is available for this file (it may be binary or unchanged in the selected parent).';
    return
  }
  String(diff).split(/\r?\n/).forEach(line=>{
    const row=document.createElement('span');
    row.className=`version-diff-line${line.startsWith('@@')?' hunk':line.startsWith('+++')||line.startsWith('---')||line.startsWith('diff ')||line.startsWith('index ')?' header':line.startsWith('+')?' addition':line.startsWith('-')?' removal':''}`;
    row.textContent=line||' ';
    host.append(row)
  })
}
function renderVersionCommitInspector(){
  const detail=state.selectedVersionCommitDetail, hash=state.selectedVersionCommit, title=$('#version-selected-commit-title'), meta=$('#version-selected-commit-meta'), target=$('#version-commit-target-branch'), rebase=$('#version-rebase-commit'), merge=$('#version-merge-commit'), mergeAll=$('#version-merge-all-branches'), revert=$('#version-revert-commit'), files=$('#version-commit-files'), fileCount=$('#version-commit-file-count'), diffTitle=$('#version-diff-title'), diffMeta=$('#version-diff-meta');
  if(!title||!target)return;
  const valid=Boolean(state.gitOverview?.is_repository&&detail&&detail.hash===hash), branches=state.gitOverview?.branches||[], branchNames=branches.map(item=>item.name), fallback=state.gitOverview?.current_branch||state.gitOverview?.main_branch||branchNames[0]||'';
  target.innerHTML='';
  branchNames.forEach(name=>{
    const option=document.createElement('option'); option.value=name; option.textContent=name; target.append(option)
  });
  if(!branchNames.includes(state.versionCommitTargetBranch))state.versionCommitTargetBranch=fallback;
  target.value=state.versionCommitTargetBranch;
  target.disabled=!valid||state.versionCommitActionBusy||!branchNames.length;
  rebase.disabled=!valid||state.versionCommitActionBusy||!target.value;
  merge.disabled=!valid||state.versionCommitActionBusy||!target.value;
  mergeAll.disabled=!valid||state.versionCommitActionBusy||!branchNames.length;
  revert.disabled=!valid||state.versionCommitActionBusy;
  $('#version-commit-action-status').textContent=state.versionCommitActionStatus||'';
  if(!valid){
    title.textContent='Select a commit';
    meta.textContent='Click an individual commit in the graph to inspect it.';
    files.innerHTML='<div class="version-empty">No commit selected.</div>';
    fileCount.textContent=''; diffTitle.textContent='Commit diff'; diffMeta.textContent=''; renderVersionDiffPreview('Choose a commit to inspect its diff.');
    return
  }
  title.textContent=`${detail.short_hash} · ${detail.subject||'Commit'}`;
  meta.textContent=`${detail.author||'Unknown author'} · ${detail.date||'Unknown date'} · ${detail.hash.slice(0,12)}${detail.decorations?` · ${detail.decorations}`:''}`;
  const changedFiles=detail.files||[];
  fileCount.textContent=`${changedFiles.length} file${changedFiles.length===1?'':'s'}`;
  files.innerHTML='';
  if(!changedFiles.length)files.innerHTML='<div class="version-empty">This commit has no file changes.</div>';
  changedFiles.forEach(file=>{
    const button=document.createElement('button'), name=document.createElement('strong'), stats=document.createElement('small');
    button.type='button'; button.className=`version-commit-file${state.selectedVersionDiffPath===file.path?' active':''}`;
    name.textContent=file.path; stats.textContent=`${file.status||'M'} · +${file.additions??'?'} / -${file.deletions??'?'}`;
    button.append(name,stats); button.onclick=()=>loadVersionCommitDiff(detail.hash,file.path).catch(err=>{state.versionCommitDiffLoading=false;state.versionCommitActionStatus=`Could not load diff: ${err.message}`;renderVersionCommitInspector()}); files.append(button)
  });
  const selectedFile=changedFiles.find(file=>file.path===state.selectedVersionDiffPath)||changedFiles[0];
  if(!selectedFile){diffTitle.textContent='Commit diff';diffMeta.textContent='';renderVersionDiffPreview('This commit has no file changes.');return}
  diffTitle.textContent=`Diff · ${selectedFile.path}`;
  diffMeta.textContent=`${selectedFile.status||'M'} · +${selectedFile.additions??'?'} / -${selectedFile.deletions??'?'}`;
  renderVersionDiffPreview(state.versionCommitDiffLoading?'Loading diff…':state.selectedVersionDiff)
}
async function loadVersionCommitDetails(hash){
  if(!state.project||!hash)return;
  const request=++state.versionCommitDetailRequest;
  state.versionCommitDetailLoading=true;
  state.selectedVersionCommitDetail=null;
  state.selectedVersionDiffPath=''; state.selectedVersionDiff=''; state.versionCommitDiffLoading=false;
  renderVersionCommitInspector();
  try{
    const detail=await api(`/api/projects/${state.project.id}/git/commits/${encodeURIComponent(hash)}`);
    if(request!==state.versionCommitDetailRequest||state.selectedVersionCommit!==hash)return;
    state.selectedVersionCommitDetail=detail;
    renderVersionCommitInspector();
    const firstFile=detail.files?.[0];
    if(firstFile)await loadVersionCommitDiff(detail.hash,firstFile.path)
  }
  finally{if(request===state.versionCommitDetailRequest)state.versionCommitDetailLoading=false}
}
async function loadVersionCommitDiff(hash,path){
  if(!state.project||!hash||!path)return;
  const request=++state.versionCommitDiffRequest;
  state.selectedVersionDiffPath=path; state.selectedVersionDiff=''; state.versionCommitDiffLoading=true;
  renderVersionCommitInspector();
  const result=await api(`/api/projects/${state.project.id}/git/commits/${encodeURIComponent(hash)}/diff?path=${encodeURIComponent(path)}`);
  if(request!==state.versionCommitDiffRequest||state.selectedVersionCommit!==hash)return;
  state.selectedVersionDiff=result.diff||''; state.versionCommitDiffLoading=false; renderVersionCommitInspector()
}
async function runVersionCommitAction(action){
  const hash=state.selectedVersionCommit, target=$('#version-commit-target-branch')?.value||'';
  if(!hash||state.versionCommitActionBusy)return;
  if((action==='rebase'||action==='merge')&&!target)return alert('Choose a target branch first.');
  const shortHash=hash.slice(0,12);
  const messages={
    rebase:`Rebase '${target}' onto commit ${shortHash}? This rewrites the target branch history. The working tree must be clean.`,
    merge:`Merge commit ${shortHash} into '${target}'? This creates a merge commit on the target branch.`,
    'merge-all':`Merge commit ${shortHash} into every local branch that does not already contain it? This may create merge commits on multiple branches.`,
    revert:`Create a new revert commit for ${shortHash} on the configured main branch?`
  };
  if(!confirm(messages[action]))return;
  state.versionCommitActionBusy=true;
  state.versionCommitActionStatus=action==='rebase'?`Rebasing '${target}'…`:action==='merge'?`Merging into '${target}'…`:action==='merge-all'?`Merging into all missing branches…`:'Creating revert commit on the configured main branch…';
  renderVersionCommitInspector();
  try{
    const body=action==='revert'||action==='merge-all'?undefined:JSON.stringify({branch:target});
    const result=await api(`/api/projects/${state.project.id}/git/commits/${encodeURIComponent(hash)}/${action}`,{method:'POST',...(body?{body}:{})});
    state.versionCommitActionStatus=action==='rebase'
      ? `Rebased '${result.rebased}' onto ${shortHash}.`
      : action==='merge'
        ? `Merged ${shortHash} into '${result.target_branch}'.`
        : action==='merge-all'
          ? formatMergeAllStatus(shortHash,result)
          : `Created revert commit ${String(result.revert_commit||'').slice(0,12)} on '${result.main_branch}'.`;
    await loadVersionControl()
  }
  catch(err){state.versionCommitActionStatus=`${action[0].toUpperCase()+action.slice(1)} failed: ${err.message}`}
  finally{state.versionCommitActionBusy=false;renderVersionCommitInspector()}
}
function formatMergeAllStatus(shortHash,result){
  const merged=result.merged||[], skipped=result.skipped||[], failed=result.failed||[];
  let status=merged.length?`Merged ${shortHash} into ${merged.join(', ')}.`:'No branches needed a merge.';
  if(skipped.length)status+=` Already present in ${skipped.join(', ')}.`;
  if(failed.length)status+=` Failed on ${failed.map(item=>`${item.branch}: ${item.error}`).join(' · ')}.`;
  return status
}
async function runVersionConsolidation(){
  if(!state.project||state.versionConsolidationBusy)return;
  const main=state.gitOverview?.main_branch||'';
  if(!main)return alert('Configure a main branch before consolidating branches.');
  if(!confirm(`Consolidate all local branches into '${main}'? Their histories will be merged into the main branch. Existing branch references will be retained for recovery.`))return;
  state.versionConsolidationBusy=true;
  state.versionConsolidationStatus=`Consolidating branches into '${main}'…`;
  renderVersionControl(state.gitOverview);
  try{
    const result=await api(`/api/projects/${state.project.id}/git/consolidate`,{method:'POST'});
    state.versionGraphConsolidated=Boolean(result.consolidated);
    const storageKey=`${VERSION_CONSOLIDATED_KEY}:${state.project.id}`;
    if(state.versionGraphConsolidated)localStorage.setItem(storageKey,'true');
    else localStorage.removeItem(storageKey);
    state.versionConsolidationStatus=formatConsolidationStatus(result);
    await loadVersionControl()
  }
  catch(err){state.versionGraphConsolidated=false;localStorage.removeItem(`${VERSION_CONSOLIDATED_KEY}:${state.project.id}`);state.versionConsolidationStatus=`Consolidation failed: ${err.message}`}
  finally{state.versionConsolidationBusy=false;renderVersionControl(state.gitOverview)}
}
function formatConsolidationStatus(result){
  const merged=result.merged||[], skipped=result.skipped||[], failed=result.failed||[];
  let status=merged.length?`Consolidated ${merged.join(', ')} into '${result.main_branch}'.`:`'${result.main_branch}' already contained every branch.`;
  if(skipped.length)status+=` Already contained: ${skipped.join(', ')}.`;
  if(failed.length)status+=` Failed on ${failed.map(item=>`${item.branch}: ${item.error}`).join(' · ')}.`;
  if(!failed.length)status+=' Branch references were retained.';
  return status
}
function branchCommitHistory(head, commitByHash){
  const history=new Set(), pending=[head];
  while(pending.length){
    const hash=pending.pop();
    if(!hash||history.has(hash)||!commitByHash.has(hash))continue;
    history.add(hash);
    (commitByHash.get(hash).parents||[]).forEach(parent=>pending.push(parent))
  }
  return history
}
const VERSION_BRANCH_COLORS=['#4d8df7','#c45b48','#9b62c2','#c39424','#269b91','#d14d78','#7d8f2e','#8a5b3c','#5d6fd0','#b75c99','#4c8a62'];
function versionBranchColor(item,index,branchItems=[]){
  if(item.main)return '#1d684b';
  const nonMainIndex=branchItems.filter(branch=>!branch.main).findIndex(branch=>branch.name===item.name);
  return VERSION_BRANCH_COLORS[(nonMainIndex>=0?nonMainIndex:index)%VERSION_BRANCH_COLORS.length]
}
function normalizeVersionGraphView(view, full){
  const minWidth=Math.min(full.width,Math.max(480,full.initialWidth*.35)), minHeight=Math.min(full.height,Math.max(240,full.height*.45));
  const width=Math.min(full.width,Math.max(minWidth,Number(view.width)||full.initialWidth)), height=Math.min(full.height,Math.max(minHeight,Number(view.height)||full.initialHeight));
  return {
    x:Math.max(0,Math.min(full.width-width,Number(view.x)||0)),
    y:Math.max(0,Math.min(full.height-height,Number(view.y)||0)),
    width,height
  }
}
function setVersionGraphView(view){
  const svg=$('#version-commit-graph svg'), full=state.versionGraphFull;
  if(!svg||!full)return;
  const next=normalizeVersionGraphView(view,full);
  state.versionGraphView=next;
  svg.setAttribute('viewBox',`${next.x} ${next.y} ${next.width} ${next.height}`);
  const label=$('#version-graph-zoom-label');
  if(label){
    const initial=full.initialCommitCount<=5&&Math.abs(next.width-full.initialWidth)<1;
    label.textContent=initial?`${full.initialCommitCount} commits`:`${Math.round((full.width/next.width)*100)}%`
  }
}
function versionGraphMetrics(svg){
  const rect=svg.getBoundingClientRect(), view=state.versionGraphView, scale=Math.min(rect.width/Math.max(1,view.width),rect.height/Math.max(1,view.height)), renderedWidth=view.width*scale, renderedHeight=view.height*scale;
  return {rect,scale,offsetX:(rect.width-renderedWidth)/2,offsetY:(rect.height-renderedHeight)/2,renderedWidth,renderedHeight}
}
function zoomVersionGraph(multiplier, clientX=null, clientY=null){
  const svg=$('#version-commit-graph svg'), full=state.versionGraphFull, current=state.versionGraphView;
  if(!svg||!full||!current)return;
  const metrics=versionGraphMetrics(svg), focusX=clientX===null?0.5:Math.max(0,Math.min(1,(clientX-metrics.rect.left-metrics.offsetX)/Math.max(1,metrics.renderedWidth))), focusY=clientY===null?0.5:Math.max(0,Math.min(1,(clientY-metrics.rect.top-metrics.offsetY)/Math.max(1,metrics.renderedHeight))), width=current.width*multiplier, height=current.height*multiplier;
  setVersionGraphView({x:current.x+(current.width-width)*focusX,y:current.y+(current.height-height)*focusY,width,height})
}
function resetVersionGraphView(){
  if(state.versionGraphFull)setVersionGraphView(state.versionGraphFull.initial)
}
function bindVersionGraphInteractions(){
  const host=$('#version-commit-graph');
  if(!host)return;
  host.onwheel=event=>{
    event.preventDefault();
    const delta=event.deltaMode===1?event.deltaY*16:event.deltaY, normalized=Math.max(-1.5,Math.min(1.5,delta/120));
    zoomVersionGraph(Math.exp(normalized*.08),event.clientX,event.clientY)
  };
  host.onpointerdown=event=>{
    if(event.button!==0||!state.versionGraphView)return;
    if(event.target?.closest?.('.version-commit-node,.version-commit-branch-label')){
      state.versionGraphPanMoved=false;
      return
    }
    host._versionGraphPointer={clientX:event.clientX,clientY:event.clientY,view:{...state.versionGraphView}};
    state.versionGraphPanMoved=false;
    host.classList.add('panning');
    host.setPointerCapture?.(event.pointerId)
  };
  host.onpointermove=event=>{
    const pointer=host._versionGraphPointer;
    if(!pointer)return;
    const svg=host.querySelector('svg'), metrics=svg?versionGraphMetrics(svg):null, scale=metrics?.scale||1, dx=(event.clientX-pointer.clientX)/scale, dy=(event.clientY-pointer.clientY)/scale;
    if(Math.abs(event.clientX-pointer.clientX)>3||Math.abs(event.clientY-pointer.clientY)>3)state.versionGraphPanMoved=true;
    setVersionGraphView({x:pointer.view.x-dx,y:pointer.view.y-dy,width:pointer.view.width,height:pointer.view.height})
  };
  const endPointer=event=>{
    if(host._versionGraphPointer){host.releasePointerCapture?.(event.pointerId);host._versionGraphPointer=null}
    host.classList.remove('panning')
  };
  host.onpointerup=endPointer; host.onpointercancel=endPointer; host.oncontextmenu=event=>event.preventDefault()
}
function renderVersionCommitGraph(data){
  const host=$('#version-commit-graph'), detail=$('#version-commit-detail'), commits=data?.commits||[], allBranchItems=data?.branches||[], branchItems=state.versionGraphConsolidated?allBranchItems.filter(item=>item.main):allBranchItems, topologyBranchItems=allBranchItems;
  host.innerHTML='';
  if(!commits.length){
    state.versionGraphView=null;
    state.versionGraphFull=null;
    host.innerHTML='<div class="version-empty">No commits are available yet.</div>';
    detail.textContent='Create a commit to populate the graph.';
    return
  }
  if(!state.selectedVersionCommit||!commits.some(item=>item.hash===state.selectedVersionCommit))state.selectedVersionCommit=commits[0].hash;
  const chronological=[...commits].reverse(), known=new Set(commits.map(item=>item.hash)), commitByHash=new Map(commits.map(item=>[item.hash,item])), commitOrder=new Map(chronological.map((item,index)=>[item.hash,index])), laneByHash=new Map(), coordinates=new Map(), generationMemo=new Map();
  const findCommit=reference=>reference?commits.find(item=>item.hash===reference||item.short_hash===reference||item.hash.startsWith(reference)):null;
  const generationOf=hash=>{
    if(generationMemo.has(hash))return generationMemo.get(hash);
    const commit=commitByHash.get(hash);
    if(!commit){generationMemo.set(hash,0);return 0}
    const generation=1+Math.max(-1,...commit.parents.filter(parent=>known.has(parent)).map(generationOf));
    generationMemo.set(hash,generation);
    return generation
  };
  const layoutOrder=[...commits].sort((left,right)=>generationOf(left.hash)-generationOf(right.hash)||(commitOrder.get(left.hash)||0)-(commitOrder.get(right.hash)||0));
  const branchHistories=new Map(topologyBranchItems.map(item=>[item.name,branchCommitHistory(findCommit(item.head)?.hash,commitByHash)]));
  const mainItem=topologyBranchItems.find(item=>item.main||item.name===data?.main_branch)||topologyBranchItems.find(item=>item.current)||topologyBranchItems[0];
  const mainHead=findCommit(mainItem?.head)||commits[0];
  const trunkHashes=[], trunkSeen=new Set();
  let trunkCommit=mainHead;
  while(trunkCommit&&!trunkSeen.has(trunkCommit.hash)){
    trunkSeen.add(trunkCommit.hash);
    trunkHashes.push(trunkCommit.hash);
    trunkCommit=findCommit(trunkCommit.parents?.[0]);
  }
  const trunkSet=new Set(trunkHashes);
  const branchLaneByName=new Map(topologyBranchItems.filter(item=>!item.main).map((item,index)=>{
    const level=Math.floor(index/2)+1;
    return [item.name,index%2===0?-level:level]
  }));
  trunkHashes.forEach(hash=>laneByHash.set(hash,0));
  topologyBranchItems.filter(item=>!item.main).forEach(item=>{
    const lane=branchLaneByName.get(item.name)||0;
    (branchHistories.get(item.name)||new Set()).forEach(hash=>{
      if(!trunkSet.has(hash)&&!laneByHash.has(hash))laneByHash.set(hash,lane)
    })
  });
  layoutOrder.forEach(commit=>{
    if(!laneByHash.has(commit.hash))laneByHash.set(commit.hash,0)
  });
  const laneSpacing=58, laneValues=[0,...branchLaneByName.values(),...laneByHash.values()], maxAbove=Math.max(0,...laneValues.map(value=>value<0?-value:0)), maxBelow=Math.max(0,...laneValues.map(value=>value>0?value:0)), topPadding=64, bottomPadding=64, trunkY=topPadding+maxAbove*laneSpacing;
  layoutOrder.forEach((commit,index)=>{
    const lane=laneByHash.get(commit.hash)||0;
    coordinates.set(commit.hash,{x:54+index*96,y:trunkY+lane*laneSpacing,lane});
  });
  const selectedNames=new Set((state.selectedVersionBranches?.length?state.selectedVersionBranches:[state.selectedVersionBranch]).filter(Boolean));
  const selectedHashes=new Set();
  selectedNames.forEach(name=>(branchHistories.get(name)||new Set()).forEach(hash=>selectedHashes.add(hash)));
  const branchColorByName=new Map(topologyBranchItems.map((item,index)=>[item.name,versionBranchColor(item,index,topologyBranchItems)]));
  const worktreeByBranch=new Map((data.worktrees||[]).filter(item=>item.branch).map(item=>[item.branch,item]));
  const laneColorIndex=lane=>((lane%6)+6)%6;
  const timelineWidth=Math.max(760,chronological.length*96+80), labelX=timelineWidth+28, labelWidth=250, width=labelX+labelWidth+34;
  const height=Math.max(300,topPadding+(maxAbove+maxBelow)*laneSpacing+bottomPadding), svgNs='http://www.w3.org/2000/svg';
  const initialCommitCount=Math.min(5,commits.length), latestPoints=commits.slice(0,initialCommitCount).map(commit=>coordinates.get(commit.hash)).filter(Boolean), initialX=Math.max(0,(Math.min(...latestPoints.map(point=>point.x))||0)-72), initialWidth=Math.min(width,Math.max(520,width-initialX)), previousFull=state.versionGraphFull, preserveView=Boolean(state.versionGraphView&&previousFull?.width===width&&previousFull?.height===height), full={width,height,initialWidth,initialHeight:height,initialCommitCount,initial:{x:initialX,y:0,width:initialWidth,height}};
  state.versionGraphFull=full;
  if(!preserveView)state.versionGraphView=full.initial;
  const svg=document.createElementNS(svgNs,'svg'); svg.classList.add('version-commit-svg'); svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('preserveAspectRatio','xMidYMid meet'); svg.setAttribute('role','img'); svg.setAttribute('aria-label','Interactive repository commit graph with branch and worktree labels');
  layoutOrder.forEach(commit=>{
    const child=coordinates.get(commit.hash);
    commit.parents.filter(parent=>coordinates.has(parent)).forEach(parent=>{
      const source=coordinates.get(parent), path=document.createElementNS(svgNs,'path'), selected=selectedHashes.has(parent)&&selectedHashes.has(commit.hash), selectedBranchName=[...selectedNames].find(name=>branchHistories.get(name)?.has(parent)&&branchHistories.get(name)?.has(commit.hash));
      path.setAttribute('class',`version-commit-edge lane-${laneColorIndex(child.lane)}${selected?' selected':''}`);
      if(selectedBranchName)path.style.stroke=branchColorByName.get(selectedBranchName);
      const midpoint=source.x+(child.x-source.x)*.5;
      path.setAttribute('d',`M ${source.x} ${source.y} C ${midpoint} ${source.y}, ${midpoint} ${child.y}, ${child.x} ${child.y}`);
      svg.append(path)
    })
  });
  layoutOrder.forEach(commit=>{
    const point=coordinates.get(commit.hash), selectedBranchName=[...selectedNames].find(name=>branchHistories.get(name)?.has(commit.hash)), selectedBranch=selectedHashes.has(commit.hash), group=document.createElementNS(svgNs,'g'), hit=document.createElementNS(svgNs,'circle'), outer=document.createElementNS(svgNs,'circle'), title=document.createElementNS(svgNs,'title');
    group.setAttribute('class',`version-commit-node${selectedBranch?' selected-branch':''}`); group.setAttribute('tabindex','0'); group.setAttribute('role','button'); group.setAttribute('aria-label',`${commit.short_hash}: ${commit.subject||'commit'}`);
    hit.setAttribute('cx',String(point.x)); hit.setAttribute('cy',String(point.y)); hit.setAttribute('r','19'); hit.setAttribute('class','version-commit-hit');
    outer.setAttribute('cx',String(point.x)); outer.setAttribute('cy',String(point.y)); outer.setAttribute('r','9'); outer.setAttribute('class',`version-commit-outer lane-${laneColorIndex(point.lane)}${commit.agent_commit?' agent':''}`);
    if(selectedBranchName){outer.style.stroke=branchColorByName.get(selectedBranchName);outer.style.strokeWidth='4'}
    title.textContent=`${commit.short_hash} ${commit.subject||''}`; group.append(hit,outer,title);
    if(commit.hash===state.selectedVersionCommit){
      const inner=document.createElementNS(svgNs,'circle'); inner.setAttribute('cx',String(point.x)); inner.setAttribute('cy',String(point.y)); inner.setAttribute('r','3.5'); inner.setAttribute('class','version-commit-selected-dot'); group.append(inner)
    }
    group.onclick=()=>{if(state.versionGraphPanMoved){state.versionGraphPanMoved=false;return}selectVersionCommit(commit.hash)}; group.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectVersionCommit(commit.hash)}}; svg.append(group)
  });
  branchItems.forEach((item,index)=>{
    const head=findCommit(item.head), point=head&&coordinates.get(head.hash), labelLane=item.main?0:(branchLaneByName.get(item.name)||0), rowY=trunkY+labelLane*laneSpacing, selected=selectedNames.has(item.name), color=versionBranchColor(item,index,topologyBranchItems), worktree=worktreeByBranch.get(item.name), group=document.createElementNS(svgNs,'g'), connector=document.createElementNS(svgNs,'path'), rect=document.createElementNS(svgNs,'rect'), name=document.createElementNS(svgNs,'text'), meta=document.createElementNS(svgNs,'text'), title=document.createElementNS(svgNs,'title');
    connector.setAttribute('class',`version-commit-branch-edge${selected?' selected':''}`); connector.style.stroke=color; connector.style.strokeWidth=selected?'4':'2'; connector.setAttribute('d',`M ${point?.x||timelineWidth-18} ${point?.y||rowY} C ${timelineWidth-12} ${point?.y||rowY}, ${labelX-28} ${rowY}, ${labelX} ${rowY}`); svg.append(connector);
    group.setAttribute('class',`version-commit-branch-label${item.main?' main':''}${item.current?' current':''}${selected?' selected':''}`); group.setAttribute('tabindex','0'); group.setAttribute('role','button'); group.setAttribute('aria-label',`Select branch ${item.name}`);
    rect.setAttribute('x',String(labelX)); rect.setAttribute('y',String(rowY-19)); rect.setAttribute('width',String(labelWidth)); rect.setAttribute('height','38'); rect.setAttribute('rx','9'); rect.style.stroke=color; rect.style.strokeWidth=selected?'3':'1.5'; if(item.main)rect.style.fill=color;
    name.setAttribute('x',String(labelX+12)); name.setAttribute('y',String(rowY-2)); name.setAttribute('class','version-commit-branch-name'); name.textContent=item.name;
    const worktreeLabel=worktree?(worktree.primary?'⌂ project folder':`⌂ ${(worktree.path||'worktree').split(/[\\/]/).pop()}`):item.current?'checked out':item.main?'main branch':'local branch';
    meta.setAttribute('x',String(labelX+12)); meta.setAttribute('y',String(rowY+13)); meta.setAttribute('class','version-commit-branch-meta'); meta.textContent=worktreeLabel;
    title.textContent=`${item.name}${worktree?.path?` · ${worktree.path}`:''}`; group.append(rect,name,meta,title); group.onclick=event=>{if(state.versionGraphPanMoved){state.versionGraphPanMoved=false;return}selectVersionBranch(item.name,event.metaKey||event.ctrlKey||event.shiftKey)}; group.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectVersionBranch(item.name,event.metaKey||event.ctrlKey||event.shiftKey)}}; svg.append(group)
  });
  host.append(svg);
  setVersionGraphView(state.versionGraphView);
  bindVersionGraphInteractions();
  const selected=commits.find(item=>item.hash===state.selectedVersionCommit);
  if(selected){
    const parentText=selected.parents.length?selected.parents.map(parent=>parent.slice(0,12)).join(', '):'none (root commit)', branchText=selectedNames.size?` · highlighted: ${[...selectedNames].join(', ')}`:'';
    detail.textContent=`${selected.short_hash} · parents: ${parentText}${selected.decorations?` · ${selected.decorations}`:''}${branchText}${data.commits_truncated?' · showing the latest 1,000 commits.':''}`
  }
}
function renderBranchTopology(data, branchItems){
  const graph=document.createElement('div'); graph.className='branch-topology-graph';
  if(!branchItems.length){graph.innerHTML='<div class="version-empty">No branches are available.</div>';return graph}
  const svgNs='http://www.w3.org/2000/svg';
  const svgElement=(name,attributes={})=>{
    const element=document.createElementNS(svgNs,name);
    Object.entries(attributes).forEach(([key,value])=>element.setAttribute(key,String(value)));
    return element
  };
  const commits=data.commits||[], commitByHash=new Map(commits.map(commit=>[commit.hash,commit]));
  const findCommit=reference=>commits.find(commit=>commit.hash===reference||commit.short_hash===reference||commit.hash.startsWith(reference||''));
  const firstParentPath=start=>{
    const path=[], seen=new Set(); let current=start;
    while(current&&!seen.has(current.hash)){
      seen.add(current.hash); path.push(current.hash);
      current=commitByHash.get(current.parents?.[0]);
    }
    return path
  };
  const mainItem=branchItems.find(item=>item.name===data.main_branch)||branchItems.find(item=>item.main)||branchItems[0];
  const mainHead=findCommit(mainItem?.head)||commits[0];
  const mainPath=firstParentPath(mainHead), mainSet=new Set(mainPath), chronological=[...mainPath].reverse();
  const mainLabelWidth=Math.max(78,(mainItem?.name||'main').length*7+24), mainStartX=18+mainLabelWidth+20;
  const mainX=new Map(chronological.map((hash,index)=>[hash,mainStartX+index*72]));
  const sideBranches=branchItems.filter(item=>item.name!==mainItem?.name);
  const topCount=Math.ceil(sideBranches.length/2), bottomCount=Math.floor(sideBranches.length/2), mainY=50+topCount*50;
  const branchRows=sideBranches.map((item,index)=>{
    const head=findCommit(item.head), path=firstParentPath(head), commonIndex=path.findIndex(hash=>mainSet.has(hash));
    const commonHash=commonIndex>=0?path[commonIndex]:mainPath[mainPath.length-1];
    const baseX=mainX.get(commonHash)||mainStartX, uniqueCount=commonIndex>=0?commonIndex:path.length;
    const labelWidth=Math.max(78,item.name.length*7+24), targetX=baseX+Math.max(112,uniqueCount*52+labelWidth+20);
    const side=index%2===0?-1:1, lane=Math.floor(index/2)+1;
    return {item,baseX,targetX,labelWidth,uniqueCount,side,lane,y:mainY+side*lane*50}
  });
  const mainEnd=Math.max(mainStartX+(chronological.length-1)*72,mainStartX), maxX=Math.max(mainEnd+130,...branchRows.map(row=>row.targetX+row.labelWidth+20),540);
  const height=Math.max(140,mainY+bottomCount*50+62), svg=svgElement('svg',{class:'branch-topology-svg',viewBox:`0 0 ${maxX} ${height}`,role:'img','aria-label':'Repository branch topology'});
  const trunk=svgElement('path',{class:'branch-topology-trunk',d:`M ${mainStartX} ${mainY} H ${mainEnd}`}); svg.append(trunk);
  chronological.forEach(hash=>{
    const point=svgElement('circle',{class:'branch-topology-commit',cx:mainX.get(hash),cy:mainY,r:5});
    const commit=commitByHash.get(hash);
    if(commit){const title=svgElement('title'); title.textContent=`${commit.short_hash} ${commit.subject||''}`; point.append(title)}
    svg.append(point)
  });
  const addBranchLabel=(row)=>{
    const {item,targetX,labelWidth,y}=row, selected=item.name===state.selectedVersionBranch;
    const group=svgElement('g',{class:`branch-topology-label-group${item.main?' main':''}${item.current?' current':''}${selected?' selected':''}`,tabindex:'0',role:'button','aria-label':`Select branch ${item.name}`});
    const rect=svgElement('rect',{class:'branch-topology-label',x:targetX,y:y-16,width:labelWidth,height:32,rx:8});
    const text=svgElement('text',{class:'branch-topology-label-text',x:targetX+10,y:y+4}); text.textContent=item.name;
    group.append(rect,text); group.addEventListener('click',()=>selectVersionBranch(item.name)); group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectVersionBranch(item.name)}});
    svg.append(group)
  };
  const mainLabel={item:mainItem,targetX:12,labelWidth:mainLabelWidth,y:mainY,uniqueCount:0};
  addBranchLabel(mainLabel);
  branchRows.forEach(row=>{
    const {baseX,targetX,y,side,uniqueCount}=row;
    const edge=svgElement('path',{class:`branch-topology-edge lane-${(side<0?0:1)+Math.floor((row.lane-1)/2)*2}`,d:`M ${baseX} ${mainY} C ${baseX+30} ${mainY}, ${baseX+30} ${y}, ${baseX+62} ${y} H ${targetX}`});
    edge.setAttribute('stroke-width',String(Math.min(5,2.2+uniqueCount*.35))); svg.insertBefore(edge,svg.firstChild);
    const commitCount=Math.min(uniqueCount,6);
    for(let commitIndex=0;commitIndex<commitCount;commitIndex++){
      const x=baseX+((targetX-baseX)*(commitIndex+1))/(commitCount+1);
      svg.append(svgElement('circle',{class:`branch-topology-commit lane-${(side<0?0:1)+Math.floor((row.lane-1)/2)*2}`,cx:x,cy:y,r:4}))
    }
    addBranchLabel(row)
  });
  graph.append(svg); return graph
}
function renderVersionAgentList(data){
  const agents=$('#version-agent-list');
  if(!agents)return;
  const query=String(state.versionAgentSearch||'').trim().toLowerCase();
  const items=(data?.agents||[]).filter(item=>`${item.name} ${item.role} ${item.branch}`.toLowerCase().includes(query));
  agents.innerHTML='';
  if(!items.length){
    agents.innerHTML='<div class="version-empty">No matching agents.</div>';
    return
  }
  items.forEach(item=>{
    const card=document.createElement('article'); card.className='version-agent-card';
    const copy=document.createElement('div'), title=document.createElement('strong'), meta=document.createElement('small'), actions=document.createElement('div');
    title.textContent=item.name; meta.textContent=`${item.enabled?'Git enabled':'Git disabled'} / ${item.branch}${item.branch_exists?item.merged_into_main?' / merged into main':' / unmerged':''}`;
    copy.append(title,meta); actions.className='version-agent-actions';
    const toggle=document.createElement('button'); toggle.type='button'; toggle.className='secondary compact'; toggle.textContent=item.enabled?'Disable Git':'Enable Git';
    toggle.onclick=async()=>{const agent=state.agents.find(value=>value.id===item.role);if(!agent)return;try{await toggleExistingAgentGit(agent);await loadVersionControl()}catch(err){alert(err.message)}};
    const changes=document.createElement('button'); changes.type='button'; changes.className='secondary compact'; changes.textContent='Changes'; changes.disabled=!item.enabled;
    changes.onclick=async()=>{state.active=item.role;selectAgent(item.role);try{await openGitChanges()}catch(err){alert(err.message)}};
    actions.append(toggle,changes); card.append(copy,actions); agents.append(card)
  })
}
function renderVersionControl(data){
  const status=$('#version-control-status'), graph=$('#version-commit-graph'), branches=$('#version-branch-list'), source=$('#version-branch-source'), consolidateButton=$('#version-consolidate-branches');
  if(!data?.is_repository){
    status.textContent='This project folder is not a Git repository. Configure Git to initialize it or add a remote.';
    graph.innerHTML='<div class="version-empty">No repository graph is available.</div>';
    if(consolidateButton)consolidateButton.disabled=true;
    branches.innerHTML=''; source.innerHTML=''; renderVersionCommitInspector(); renderVersionAgentList(data); return
  }
  const current=data.current_branch||'(detached HEAD)', main=data.main_branch||'';
  const baseStatus=current===main
    ? `On main branch “${main}” · ${data.clean?'working tree clean':'uncommitted changes'}`
    : `On branch “${current}”${main?` · main branch “${main}”`:''} · ${data.clean?'working tree clean':'uncommitted changes'}`;
  status.textContent=state.versionConsolidationStatus?`${baseStatus} · ${state.versionConsolidationStatus}`:baseStatus;
  if(consolidateButton)consolidateButton.disabled=state.versionConsolidationBusy||!main;
  const branchItems=data.branches||[];
  if(state.versionGraphConsolidated&&branchItems.some(item=>!item.main&&!item.merged_into_main)){
    state.versionGraphConsolidated=false;
    if(state.project)localStorage.removeItem(`${VERSION_CONSOLIDATED_KEY}:${state.project.id}`)
  }
  const validSelected=(state.selectedVersionBranches||[]).filter(name=>branchItems.some(item=>item.name===name));
  if(!validSelected.length){
    const fallback=data.current_branch||data.main_branch||branchItems[0]?.name||'';
    if(fallback)validSelected.push(fallback)
  }
  state.selectedVersionBranches=validSelected;
  state.selectedVersionBranch=validSelected[validSelected.length-1]||'';
  renderVersionCommitGraph(data);
  renderVersionCommitInspector();
  if(state.selectedVersionCommit&&!state.versionCommitDetailLoading&&state.selectedVersionCommitDetail?.hash!==state.selectedVersionCommit){
    loadVersionCommitDetails(state.selectedVersionCommit).catch(err=>{
      state.versionCommitDiffLoading=false;
      state.versionCommitActionStatus=`Could not load commit details: ${err.message}`;
      renderVersionCommitInspector()
    })
  }
  branches.innerHTML=''; source.innerHTML='';
  const selectedNames=new Set(state.selectedVersionBranches), worktreeByBranch=new Map((data.worktrees||[]).filter(item=>item.branch).map(item=>[item.branch,item]));
  branchItems.forEach((item,index)=>{
    const option=document.createElement('option'); option.value=item.name; option.textContent=item.main?`${item.name} (main)`:item.name; option.selected=item.name===(data.main_branch||data.current_branch); source.append(option);
    const color=versionBranchColor(item,index,branchItems), row=document.createElement('article'); row.className=`version-branch-row${selectedNames.has(item.name)?' selected':''}`; row.style.setProperty('--version-branch-color',color);
    const info=document.createElement('button'); info.type='button'; info.className='version-branch-info'; info.setAttribute('aria-pressed',String(selectedNames.has(item.name))); info.title='Select this branch. Hold Command or Control to highlight multiple branches.';
    const name=document.createElement('strong'), meta=document.createElement('small'); name.textContent=item.name;
    const worktree=worktreeByBranch.get(item.name), worktreeText=worktree?`worktree: ${worktree.primary?'project folder':worktree.path}`:'';
    meta.textContent=[item.main?'main branch': '',item.current?'checked out':'',item.upstream?`tracks ${item.upstream}`:'local only',item.head||'no commits',worktreeText].filter(Boolean).join(' / ');
    info.append(name,meta); info.onclick=event=>selectVersionBranch(item.name,event.metaKey||event.ctrlKey||event.shiftKey);
    const actions=document.createElement('div'); actions.className='version-branch-actions';
    const checkout=document.createElement('button'); checkout.type='button'; checkout.className='secondary compact'; checkout.textContent=item.current?'Checked out':'Checkout'; checkout.disabled=item.current;
    checkout.onclick=async()=>{try{await api(`/api/projects/${state.project.id}/git/branches/${encodeURIComponent(item.name)}/checkout`,{method:'POST'});await loadVersionControl()}catch(err){alert(err.message)}};
    const agentBranch=(data.agents||[]).some(agent=>agent.enabled&&agent.branch===item.name);
    const remove=document.createElement('button'); remove.type='button'; remove.className='danger compact'; remove.textContent='Delete'; remove.disabled=item.main||item.current||agentBranch;
    remove.onclick=async()=>{if(!confirm(`Delete branch '${item.name}'?`))return;try{await api(`/api/projects/${state.project.id}/git/branches/${encodeURIComponent(item.name)}`,{method:'DELETE'});await loadVersionControl()}catch(err){alert(err.message)}};
    actions.append(checkout,remove); row.append(info,actions); branches.append(row)
  });
  const selected=branchItems.filter(item=>selectedNames.has(item.name));
  $('#version-branch-detail').textContent=selected.length?`${selected.map(item=>item.name).join(', ')} highlighted · ${selected.length===1?(selected[0].main?'configured main branch':selected[0].current?'checked out':'local branch'):'multiple branches selected'}`:'Select a branch to inspect it.';
  renderVersionAgentList(data)
}
async function openVersionControl(){
  return showVersionControl()
}
function renderMarketplace(){
  const list=$('#marketplace-results');
  if(!list)return;
  list.innerHTML='';
  if(!state.marketplace.length){
    list.innerHTML='<div class="skills-empty">Search the marketplace to discover portable SKILL.md packages.</div>';
    return
  }
  state.marketplace.forEach(item=>{
    const card=document.createElement('article');
    card.className='marketplace-card';
    const title=document.createElement('div'), name=document.createElement('strong'), meta=document.createElement('small'), description=document.createElement('p'), actions=document.createElement('div'), install=document.createElement('button'), progressWrap=document.createElement('div'), progress=document.createElement('progress'), progressLabel=document.createElement('span');
    name.textContent=item.name||item.id||'Unnamed skill';
    meta.textContent=`${item.type||'marketplace'} · ${item.author||'community'} · ★ ${item.stars||0}`;
    description.textContent=item.description||'No description provided.';
    install.type='button'; install.className='secondary compact'; install.textContent=item.installed?'Installed':item.installing?'Downloading…':'Install';
    install.disabled=!item.github_url||Boolean(item.installed||item.installing);
    progressWrap.className='marketplace-progress';
    progress.max=100; progress.value=item.installed?100:0; progress.className='marketplace-progress-bar';
    progressLabel.className='marketplace-progress-label';
    progressLabel.textContent=item.installed?'Installed':'';
    progressWrap.hidden=!item.installed&&!item.installing;
    progressWrap.append(progress, progressLabel);
    install.onclick=()=>installMarketplaceSkill(item, install, progress, progressLabel, progressWrap);
    title.append(name, meta); actions.append(install);
    card.append(title, description, progressWrap, actions);
    list.append(card)
  })
}
async function searchMarketplace(){
  const query=$('#marketplace-query').value.trim(), status=$('#marketplace-status'), button=$('#marketplace-search-button');
  if(!query)return alert('Enter a marketplace search query first.');
  button.classList.add('loading'); status.textContent='Searching the ACP skill catalog…';
  try{
    const params=new URLSearchParams({q:query, category:$('#marketplace-category').value, sort:$('#marketplace-sort').value});
    const data=await api(`/api/skills/marketplace/search?${params}`);
    state.marketplace=data.skills||[];
    status.textContent=`${state.marketplace.length} result${state.marketplace.length===1?'':'s'} returned`;
    renderMarketplace()
  }
  catch(err){state.marketplace=[]; status.textContent=err.message; renderMarketplace()}
  finally{button.classList.remove('loading')}
}
function updateMarketplaceProgress(progress, label, phase, received=0, total=0){
  const phaseNames={download:'Downloading', extract:'Extracting', install:'Installing', complete:'Installed'};
  const title=phaseNames[phase]||'Working';
  if(total>0){
    progress.max=total;
    progress.value=Math.min(received,total);
    label.textContent=`${title} ${Math.round((received/total)*100)}%`;
  }
  else{
    progress.removeAttribute('value');
    label.textContent=`${title}…`;
  }
}
async function installMarketplaceSkill(item, button, progress, progressLabel, progressWrap){
  if(!item.github_url)return;
  if(!confirm(`Install “${item.name}” from ${item.author||'the marketplace'}?\n\nReview the downloaded SKILL.md and scripts before assigning it to an agent.`))return;
  item.installing=true;
  button.disabled=true;
  button.classList.add('loading');
  button.textContent='Downloading…';
  progressWrap.hidden=false;
  updateMarketplaceProgress(progress, progressLabel, 'download');
  let installed;
  try{
    installed=await apiNdjson('/api/skills/marketplace/install-stream', {method:'POST', body:JSON.stringify({source_url:item.github_url, marketplace_id:item.id, expected_name:item.name})}, event=>{
      if(event.event==='progress')updateMarketplaceProgress(progress, progressLabel, event.phase, event.received, event.total);
    });
  }
  catch(err){
    item.installing=false;
    button.classList.remove('loading');
    button.disabled=!item.github_url;
    button.textContent='Install';
    progressWrap.hidden=true;
    progressLabel.textContent='';
    alert(err.message)
    return
  }
  item.installing=false;
  item.installed=true;
  button.classList.remove('loading');
  button.textContent='Installed';
  button.disabled=true;
  progressWrap.hidden=false;
  progress.max=1;
  progress.value=1;
  progressLabel.textContent='Installed';
  try{
    await loadSkills();
    await selectSkill(installed.id);
  }
  catch(err){
    console.warn('Skill installed but the local library could not refresh', err);
  }
}
function skillResultText(result){
  const output=result.output===undefined||result.output===null?'':typeof result.output==='string'?result.output:JSON.stringify(result.output, null, 2);
  const parts=[`[${result.skill||'skill'} · v${result.version||'1.0.0'} · ${result.platform||'current OS'} · ${result.output_format||'text'}]`];
  if(output)parts.push(output);
  if(result.stdout&&result.stdout.trim()&&result.stdout.trim()!==output.trim())parts.push(`[stdout]\n${result.stdout.trim()}`);
  if(result.stderr)parts.push(`[stderr]\n${result.stderr.trim()}`);
  if(!parts.length)parts.push('(skill produced no output)');
  return parts.join('\n\n')
}
function skillResultMessageText(result){
  const text=skillResultText(result), format=result.output_format||'text';
  if(result.output===undefined||result.output===null)return text;
  const rendered=typeof result.output==='string'?result.output:JSON.stringify(result.output, null, 2);
  if(format==='diff')return text.replace(rendered, `\n\`\`\`diff\n${rendered}\n\`\`\``);
  if(format==='json')return text.replace(rendered, `\n\`\`\`json\n${rendered}\n\`\`\``);
  if(format==='code')return text.replace(rendered, `\n\`\`\`\n${rendered}\n\`\`\``);
  return text
}
function showSkillResult(result, skill){
  showCodeTerminal({...result, language:result.language||skill?.language||'skill', stdout:skillResultText(result), stderr:''}, skill?.name||'skill')
}
async function runAssignedSkill(skillId, role, inputs, confirmRun=true){
  const skill=skillById(skillId);
  if(!skill||!role)return null;
  if(confirmRun&&!confirm(`Run “${skill.name}” for ${state.agents.find(agent=>agent.id===role)?.name||role}?\n\nThe skill script can read or modify files in the project folder.`))return null;
  try{
    const result=await api(`/api/skills/${skill.id}/run`, {method:'POST', body:JSON.stringify({project_id:state.project.id, role, inputs})});
    showSkillResult(result, skill);
    return result
  }
  catch(err){
    showSkillResult({ok:false, language:skill.language, cwd:state.project.root_path||'project folder', exit_code:'error', stderr:err.message}, skill);
    return null
  }
}
async function selectProject(id){
  const project=state.projects.find(p=>p.id===id);
  if(!project)return;
  state.project=project;
  state.selectedVersionCommitDetail=null;
  state.selectedVersionDiffPath='';
  state.selectedVersionDiff='';
  state.versionCommitDiffLoading=false;
  state.versionCommitDetailLoading=false;
  state.versionCommitActionBusy=false;
  state.versionCommitActionStatus='';
  state.versionCommitTargetBranch='';
  state.versionGraphConsolidated=localStorage.getItem(`${VERSION_CONSOLIDATED_KEY}:${id}`)==='true';
  state.versionConsolidationBusy=false;
  state.versionConsolidationStatus='';
  state.versionCommitDetailRequest++;
  state.versionCommitDiffRequest++;
  state.versionControlLoadPromise=null;
  state.versionGraphView=null;
  state.versionGraphFull=null;
  state.messages={};
  state.pendingAttachments={};
  state.replyTo={};
  state.activities={};
  state.runs={};
  state.busy.clear();
  localStorage.setItem('multiagent-project', String(id));
  state.agents=await api(`/api/agents?project_id=${id}`);
  state.active=state.agents.some(agent=>agent.id===state.active)?state.active: state.agents[0]?.id||'';
  renderProjects();
  $('#project-name').textContent=state.project.name;
  $('#project-description').textContent=state.project.description||state.project.root_path||'Draw any number of command and reporting relationships between agents.';
  [state.layout,
  state.edges,
  state.skills,
  state.toolsets]=await Promise.all([api(`/api/projects/${id}/layout`), api(`/api/projects/${id}/edges`), api(`/api/skills?project_id=${id}`), api(`/api/toolsets?project_id=${id}`)]);
  renderAgents();
  if(state.active){
    selectAgent(state.active);
    await loadHistory(state.active);
    recoverActiveRun(state.active)
  }
  else{
    selectAgent('')
  }
  await loadContext();
  renderFlowchart();
  api(`/health?project_id=${id}`).then(health=>{
    if(state.project?.id===id)$('#status').textContent=`${health.agents} agents · MCP online`
  }).catch(()=>{})
}
async function loadWorkflowTemplates(){
  if(!state.project)return;
  state.workflowTemplates=await api(`/api/projects/${state.project.id}/workflow-templates`)
}
function fillAgentSelect(select, value=''){
  select.innerHTML='';
  state.agents.forEach(agent=>{
    const option=document.createElement('option');
    option.value=agent.id; option.textContent=agent.name;
    option.selected=agent.id===value;
    select.append(option)
  })
}
function renderWorkflowDialog(){
  const source=$('#relationship-source'), target=$('#relationship-target'), list=$('#relationship-editor-list');
  $('#relationship-enforcement').checked=Boolean(state.project?.enforce_relationships);
  const sourceValue=source.value, targetValue=target.value;
  fillAgentSelect(source,sourceValue);
  fillAgentSelect(target,targetValue);
  list.innerHTML='';
  if(!state.edges.length)list.innerHTML='<p class="workflow-empty">No relationships yet. Add one above or drag a map handle to another agent.</p>';
  state.edges.forEach((edge,index)=>{
    const row=document.createElement('div'); row.className='relationship-editor-row';
    const from=document.createElement('select'), kind=document.createElement('select'), to=document.createElement('select');
    fillAgentSelect(from,edge.source_role); fillAgentSelect(to,edge.target_role);
    kind.innerHTML='<option value="command">Commands</option><option value="report">Reports to</option>'; kind.value=edge.relationship;
    const update=()=>{
      const next={source_role:from.value,target_role:to.value,relationship:kind.value};
      if(next.source_role===next.target_role){alert('An agent cannot have a relationship with itself.'); renderWorkflowDialog(); return}
      if(state.edges.some((item,itemIndex)=>itemIndex!==index&&item.source_role===next.source_role&&item.target_role===next.target_role&&item.relationship===next.relationship)){
        alert('That relationship already exists.'); renderWorkflowDialog(); return
      }
      state.edges[index]=next; saveEdges(); renderWorkflowDialog(); renderFlowchart()
    };
    from.onchange=update; kind.onchange=update; to.onchange=update;
    const remove=document.createElement('button'); remove.type='button'; remove.className='danger compact'; remove.textContent='Remove';
    remove.onclick=()=>{state.edges.splice(index,1); saveEdges(); renderWorkflowDialog(); renderFlowchart()};
    row.append(from,kind,to,remove); list.append(row)
  });
  const templateSelect=$('#workflow-template-select'), selectedId=Number(templateSelect.value)||state.workflowTemplates[0]?.id||0;
  templateSelect.innerHTML='<option value="">Choose a saved workflow</option>';
  state.workflowTemplates.forEach(template=>{
    const option=document.createElement('option'); option.value=String(template.id); option.textContent=template.name; option.selected=template.id===selectedId;
    templateSelect.append(option)
  });
  const selected=state.workflowTemplates.find(template=>template.id===Number(templateSelect.value));
  $('#workflow-template-detail').textContent=selected?`${selected.layout.length} agent positions · ${selected.edges.length} relationships`:'Save the current map to reuse it in this or another workspace.';
  const agents=$('#workflow-agent-list'); agents.innerHTML='';
  state.agents.forEach(agent=>{
    const row=document.createElement('div'); row.className='workflow-agent-row';
    const text=document.createElement('div'), name=document.createElement('strong'), brief=document.createElement('small');
    name.textContent=agent.name; brief.textContent=agent.brief; text.append(name,brief);
    const remove=document.createElement('button'); remove.type='button'; remove.className='danger compact'; remove.textContent='Remove agent';
    remove.onclick=async()=>{try{await deleteAgentFromMenu(agent); renderWorkflowDialog()}catch(err){alert(err.message)}};
    row.append(text,remove); agents.append(row)
  })
}
async function openWorkflowDialog(){
  if(!state.project)return;
  await loadWorkflowTemplates();
  renderWorkflowDialog();
  $('#workflow-dialog').showModal()
}
async function loadActionPermissions(){
  if(!state.project)return;
  state.actionPermissions=await api(`/api/projects/${state.project.id}/action-permissions`)
}
function updateAgentPermissions(role, permissions){
  const agent=state.agents.find(item=>item.id===role);
  if(agent)agent.permissions=permissions;
  if(state.actionPermissions){
    const index=state.actionPermissions.agents.findIndex(item=>item.role===role);
    if(index>=0)state.actionPermissions.agents[index]=permissions
  }
}
function renderExternalAccessButton(){
  const button=$('#external-access-button'), agent=activeAgent();
  if(!button)return;
  const globalAccess=Boolean(state.project?.allow_full_system_access);
  const individualAccess=Boolean(agent?.permissions?.allow_full_system_access);
  button.hidden=!agent||agent.runtime?.provider!=='codex';
  button.disabled=globalAccess;
  button.textContent=globalAccess?'External access: global':individualAccess?'External access: on':'External access';
  button.classList.toggle('external-access-enabled', individualAccess||globalAccess);
  button.title=globalAccess
    ? 'All agents already have unrestricted system access.'
    : individualAccess
      ? `Remove ${agent.name}'s access outside the project folder.`
      : `Allow only ${agent.name} to work outside the project folder.`;
  const scope=$('#scope-badge');
  if(scope){
    const external=globalAccess||individualAccess;
    scope.textContent=external?'External access':'Project scope';
    scope.classList.toggle('external',external)
  }
}
function renderContextMeter(){
  const meter=$('#context-meter'), agent=activeAgent(), usage=state.contextUsage[state.active];
  if(!meter)return;
  meter.hidden=!agent;
  if(!agent||!usage){
    $('#context-remaining').textContent='—';
    $('#context-progress').style.width='0%';
    $('#context-detail').textContent='Estimating…';
    return
  }
  const remaining=Math.max(0,Math.min(100,Number(usage.remaining_percent)||0));
  $('#context-remaining').textContent=`${remaining}%`;
  $('#context-progress').style.width=`${remaining}%`;
  const countPrefix=usage.is_estimate?'~':'';
  const sourceLabel=usage.is_estimate?'estimated':'provider reported';
  $('#context-detail').textContent=`${countPrefix}${formatTokenCount(usage.used_tokens ?? usage.estimated_tokens)} used of ${formatTokenCount(usage.context_window_tokens)} tokens · ${sourceLabel}`;
  meter.classList.toggle('warning',remaining<=25&&remaining>10);
  meter.classList.toggle('critical',remaining<=10)
}
function formatTokenCount(value){
  const tokens=Math.max(0,Number(value)||0);
  return tokens>=1000?`${Math.round(tokens/100)/10}k`:String(tokens)
}
async function refreshContextUsage(role=state.active, force=false){
  const projectId=state.project?.id, agent=state.agents.find(item=>item.id===role);
  if(!projectId||!agent)return;
  const now=Date.now();
  if(!force&&now-(state.contextUsageFetchedAt[role]||0)<2000)return state.contextUsage[role];
  if(state.contextUsageLoads[role])return state.contextUsageLoads[role];
  const request=(async()=>{
    try{
      const usage=await api(`/api/agents/${role}/context-usage?project_id=${projectId}`);
      if(state.project?.id!==projectId)return null;
      state.contextUsage[role]=usage;
      state.contextUsageFetchedAt[role]=Date.now();
      if(state.active===role)renderContextMeter();
      return usage
    }
    catch(err){
      console.warn('Could not refresh context estimate',err);
      return null
    }
    finally{
      if(state.contextUsageLoads[role]===request)delete state.contextUsageLoads[role]
    }
  })();
  state.contextUsageLoads[role]=request;
  return request
}
function renderPermissionsDialog(){
  const policy=state.actionPermissions||{auto_approve_agent_actions:false,allow_full_system_access:false,agents:[]};
  const autonomous=Boolean(policy.auto_approve_agent_actions);
  const fullSystemAccess=Boolean(policy.allow_full_system_access);
  $('#auto-approve-agent-actions').checked=autonomous;
  $('#allow-full-system-access').checked=fullSystemAccess;
  const list=$('#agent-permissions-list'); list.innerHTML='';
  state.agents.forEach(agent=>{
    const permissions=policy.agents.find(item=>item.role===agent.id)||agent.permissions||{};
    const row=document.createElement('article'); row.className='agent-permission-row';
    const details=document.createElement('div'), name=document.createElement('strong'), summary=document.createElement('small');
    name.textContent=agent.name;
    const effectiveFullSystemAccess=Boolean(permissions.full_system_access);
    summary.textContent=fullSystemAccess?'Unrestricted system access is enabled for every agent.':effectiveFullSystemAccess?'This agent has unrestricted system access.':autonomous?'Workspace autonomy is overriding individual grants.':`${permissions.allow_commands?'Commands allowed':'Commands blocked'} · ${permissions.allow_file_edits?'File edits allowed':'File edits blocked'}`;
    details.append(name,summary);
    const controls=document.createElement('div'); controls.className='agent-permission-controls';
    const addToggle=(labelText,key)=>{
      const label=document.createElement('label'), input=document.createElement('input'), text=document.createTextNode(labelText);
      input.type='checkbox'; input.checked=Boolean(permissions[key]); input.disabled=autonomous||effectiveFullSystemAccess;
      label.append(input,text); controls.append(label);
      input.onchange=async()=>{
        const next={allow_commands:key==='allow_commands'?input.checked:Boolean(permissions.allow_commands),allow_file_edits:key==='allow_file_edits'?input.checked:Boolean(permissions.allow_file_edits),allow_full_system_access:Boolean(permissions.allow_full_system_access)};
        try{
          const saved=await api(`/api/projects/${state.project.id}/agents/${agent.id}/action-permissions`, {method:'PUT',body:JSON.stringify(next)});
          updateAgentPermissions(agent.id,saved); renderPermissionsDialog(); renderAgents()
        }
        catch(err){input.checked=!input.checked;alert(err.message)}
      }
    };
    addToggle('Commands','allow_commands'); addToggle('Edit files','allow_file_edits');
    row.append(details,controls); list.append(row)
  })
}
async function openPermissionsDialog(){
  await loadActionPermissions();
  renderPermissionsDialog();
  $('#permissions-dialog').showModal()
}
async function loadWorkflowMemories(){
  if(!state.project)return;
  const data=await api(`/api/projects/${state.project.id}/workflow-memories`);
  state.workflowMemories=data.memories||[];
  state.activeWorkflowMemoryId=Number(data.active_memory_id)||0
}
function fillWorkflowMemory(memory=null){
  $('#workflow-memory-id').value=memory?.id||'';
  $('#workflow-memory-name').value=memory?.name||'';
  $('#workflow-memory-content').value=memory?.content||'';
  $('#delete-workflow-memory').style.visibility=memory?'visible':'hidden'
}
function renderWorkflowMemories(){
  const select=$('#workflow-memory-select'), editingId=Number($('#workflow-memory-id').value)||0;
  select.innerHTML='<option value="">No active workflow memory</option>';
  state.workflowMemories.forEach(memory=>{
    const option=document.createElement('option'); option.value=String(memory.id); option.textContent=memory.name;
    option.selected=memory.id===state.activeWorkflowMemoryId; select.append(option)
  });
  const editing=state.workflowMemories.find(memory=>memory.id===editingId);
  if(editing)fillWorkflowMemory(editing);
  else if(!editingId)fillWorkflowMemory(state.workflowMemories.find(memory=>memory.id===state.activeWorkflowMemoryId)||null)
}
async function openWorkflowMemories(){
  await loadWorkflowMemories();
  $('#workflow-memory-id').value='';
  renderWorkflowMemories();
  $('#memory-dialog').showModal()
}
function relationshipModeDescription(mode){
  return {
     move:'Move agents to arrange the map. Choose a relationship above to drag directly between agents.',
     command:'Commands selected: drag from the commanding agent to the agent it commands.',
     report:'Reports selected: drag from the reporting agent to the agent it reports to.',
     supervisor:'Supervisor -> Employee selected: start dragging on the supervisor, then release on the employee. This creates command and report links in the expected directions.',
     bidirectional:'Interconnected selected: drag from either agent to the other. Both agents will command and report to each other; the canvas shows both lines with arrows in both directions.',
  }[mode]||''
}
function renderRelationshipToolbar(){
  document.querySelectorAll('.relationship-toolbar [data-relationship-mode]').forEach(button=>{
    const active=button.dataset.relationshipMode===state.relationshipMode;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
    if(button.dataset.relationshipMode==='supervisor')button.textContent='Supervisor -> Employee';
    if(button.dataset.relationshipMode==='bidirectional')button.textContent='Interconnected (both ways)'
  });
  const chart=$('#flowchart'), help=$('#flow-help');
  if(chart)chart.dataset.relationshipMode=state.relationshipMode;
  if(help)help.textContent=relationshipModeDescription(state.relationshipMode);
  let toolbarNote=$('#relationship-toolbar-note');
  if(!toolbarNote){
    const toolbar=document.querySelector('.relationship-toolbar');
    if(toolbar){
      toolbarNote=document.createElement('small');
      toolbarNote.id='relationship-toolbar-note';
      toolbar.append(toolbarNote)
    }
  }
  if(toolbarNote)toolbarNote.textContent=relationshipModeDescription(state.relationshipMode)
}
function setRelationshipMode(mode){
  state.relationshipMode=mode;
  renderRelationshipToolbar()
}
function clearLinkDrawing(redraw=true){
  const active=state.drawingLink;
  if(active?.origin){
    active.origin.classList.remove('active');
    active.origin.onpointermove=null;
    active.origin.onpointerup=null;
    active.origin.onpointercancel=null;
    active.origin.onlostpointercapture=null
  }
  if(active?.sourceNode)active.sourceNode.classList.remove('link-source');
  if(active?.onWindowBlur)window.removeEventListener('blur',active.onWindowBlur);
  document.querySelectorAll('.flow-node.link-source').forEach(node=>node.classList.remove('link-source'));
  document.querySelectorAll('.link-handle.active').forEach(handle=>handle.classList.remove('active'));
  const wasDrawing=Boolean(active);
  state.drawingLink=null;
  if(redraw&&wasDrawing){
    renderRelationshipToolbar();
    drawLines()
  }
}
function normalizeGraphState(){
  const roles=new Set(state.agents.map(agent=>agent.id));
  state.layout=state.layout.filter(item=>item&&roles.has(item.role));
  const seenEdges=new Set();
  const nextEdges=state.edges.filter(edge=>{
    if(!edge||edge.source_role===edge.target_role||!roles.has(edge.source_role)||!roles.has(edge.target_role)||!['command','report'].includes(edge.relationship))return false;
    const key=`${edge.source_role}::${edge.target_role}::${edge.relationship}`;
    if(seenEdges.has(key))return false;
    seenEdges.add(key);
    return true
  });
  const edgesChanged=nextEdges.length!==state.edges.length;
  state.edges=nextEdges;
  if(edgesChanged)saveEdges()
}
function renderFlowchart(){
  if(!state.project)return;
  clearLinkDrawing(false);
  normalizeGraphState();
  renderRelationshipToolbar();
  const host=$('#flow-nodes');
  host.innerHTML='';
  state.layout.forEach(item=>{
    const agent=state.agents.find(a=>a.id===item.role);
    if(!agent)return;
    const node=document.createElement('article');
    node.className=`flow-node ${item.role==='orchestrator'?'orchestrator':''}`;
    node.dataset.role=item.role;
    node.style.left=`${item.x}px`;
    node.style.top=`${item.y}px`;
    const head=document.createElement('div');
    head.className='flow-node-head';
    head.innerHTML=`<span class="mini-avatar">${agent.name[0]}</span><div class="flow-node-title"><h3></h3><span class="runtime-badge"></span></div>`;
    head.querySelector('h3').textContent=agent.name;
    const badge=head.querySelector('.runtime-badge'), label=runtimeLabel(agent.runtime);
    badge.textContent=label;
    badge.title=label;
    badge.dataset.provider=agent.runtime.provider;
    const removeAgent=document.createElement('button');
    removeAgent.type='button';
    removeAgent.className='flow-node-remove';
    removeAgent.textContent='Remove';
    removeAgent.title=`Remove ${agent.name} from the team`;
    removeAgent.onclick=async event=>{
      event.stopPropagation();
      try{await deleteAgentFromMenu(agent)}catch(err){alert(err.message)}
    };
    head.append(removeAgent);
    const brief=document.createElement('p');
    brief.textContent=agent.brief;
    const controls=document.createElement('div');
    controls.className='relationship-controls';
    for(const kind of ['command', 'report']){
      const row=document.createElement('div');
      row.className=`relationship-control ${kind}`;
      row.innerHTML=`<span class="link-handle ${kind}" title="Drag to create a ${kind} relationship"></span><strong>${kind==='command'?'Commands':'Reports to'}</strong>`;
      enableLinkDrawing(row.querySelector('.link-handle'), item, kind);
      controls.append(row)
    }
    const links=document.createElement('div');
     links.className='relationship-list';
     const renderedInterconnected=new Set();
    state.edges.filter(edge=>edge.source_role===item.role).forEach(edge=>{
      const target=state.agents.find(a=>a.id===edge.target_role);
      const chip=document.createElement('span');
       const interconnected=isInterconnectedPair(edge.source_role,edge.target_role), supervisorPair=supervisorEmployeePairForEdge(edge);
       if(interconnected){
         const pairKey=relationshipPairKey(edge.source_role,edge.target_role);
         if(renderedInterconnected.has(pairKey))return;
         renderedInterconnected.add(pairKey);
         chip.className='relationship-chip interconnected';
       }else if(supervisorPair)chip.className='relationship-chip supervisor';
       else chip.className=`relationship-chip ${edge.relationship}`;
      chip.textContent=`${edge.relationship==='command'?'→':'⇢'} ${target?.name||edge.target_role}`;
       if(interconnected)chip.textContent=`INTERCONNECTED: ${target?.name||edge.target_role} (command + report both ways)`;
       else if(supervisorPair)chip.textContent=edge.relationship==='command'
         ? `SUPERVISOR -> EMPLOYEE: ${target?.name||edge.target_role}`
         : `EMPLOYEE REPORTS TO: ${target?.name||edge.target_role}`;
       if(!interconnected&&!supervisorPair)chip.textContent=`${edge.relationship==='command'?'COMMANDS':'REPORTS TO'}: ${target?.name||edge.target_role}`;
       const remove=document.createElement('button');
      remove.type='button';
      remove.textContent='×';
      remove.title='Remove relationship';
       remove.onclick=e=>{
         e.stopPropagation();
         if(interconnected){
           const roles=new Set([edge.source_role,edge.target_role]);
           state.edges=state.edges.filter(candidate=>!(roles.has(candidate.source_role)&&roles.has(candidate.target_role)&&['command','report'].includes(candidate.relationship)));
         }else if(supervisorPair){
           state.edges=state.edges.filter(candidate=>!(candidate.source_role===supervisorPair.supervisor&&candidate.target_role===supervisorPair.employee&&candidate.relationship==='command')&&!(candidate.source_role===supervisorPair.employee&&candidate.target_role===supervisorPair.supervisor&&candidate.relationship==='report'));
         }else state.edges=state.edges.filter(candidate=>candidate!==edge);
         saveEdges();
         renderFlowchart()
       };
      chip.append(remove);
      links.append(chip)
    });
    node.append(head, brief, controls, links);
    node.ondblclick=()=>showWorkspace(item.role);
    node.oncontextmenu=e=>openAgentContextMenu(e, agent);
    enableDrag(node, item);
    host.append(node)
  });
  requestAnimationFrame(drawLines)
}
function enableDrag(node, item){
  node.onpointerdown=e=>{
    if(e.target.closest('button, .relationship-control, .relationship-list'))return;
    if(state.relationshipMode!=='move'){
      startLinkDrawing(node,item,state.relationshipMode,e);
      return
    }
    e.preventDefault();
    node.setPointerCapture(e.pointerId);
    node.classList.add('dragging');
    const startX=e.clientX,
    startY=e.clientY,
    originX=item.x,
    originY=item.y;
    node.onpointermove=move=>{
      item.x=Math.max(0, originX+move.clientX-startX);
      item.y=Math.max(0, originY+move.clientY-startY);
      node.style.left=`${item.x}px`;
      node.style.top=`${item.y}px`;
      drawLines()
    };
    const finish=()=>{
      node.classList.remove('dragging');
      node.onpointermove=null;
      node.onpointerup=null;
      node.onpointercancel=null;
      node.onlostpointercapture=null;
      saveLayout()
    };
    node.onpointerup=finish;
    node.onpointercancel=finish;
    node.onlostpointercapture=finish
  }
}
function addRelationship(sourceRole,targetRole,relationship){
  const edges=relationship==='supervisor'?[{
    source_role:sourceRole,target_role:targetRole,relationship:'command'
  },{
    source_role:targetRole,target_role:sourceRole,relationship:'report'
  }]:relationship==='bidirectional'?[{
    source_role:sourceRole,target_role:targetRole,relationship:'command'
  },{
    source_role:targetRole,target_role:sourceRole,relationship:'command'
  },{
    source_role:sourceRole,target_role:targetRole,relationship:'report'
  },{
    source_role:targetRole,target_role:sourceRole,relationship:'report'
  }]:[{
    source_role:sourceRole,target_role:targetRole,relationship
  }];
  let changed=false;
  edges.forEach(edge=>{
    if(!state.edges.some(item=>item.source_role===edge.source_role&&item.target_role===edge.target_role&&item.relationship===edge.relationship)){
      state.edges.push(edge); changed=true
    }
  });
  if(changed)saveEdges();
  return changed
}
function startLinkDrawing(origin, item, relationship, event){
  event.preventDefault();
  event.stopPropagation();
  clearLinkDrawing(false);
  origin.setPointerCapture(event.pointerId);
  const chart=$('#flowchart'), rect=chart.getBoundingClientRect();
  const sourceNode=origin.closest('.flow-node');
  const originName=state.agents.find(agent=>agent.id===item.role)?.name||item.role, help=$('#flow-help');
  if(help)help.textContent=relationship==='supervisor'
    ? `Supervisor selected: ${originName}. Release on the employee.`
    : relationship==='bidirectional'
      ? `First selected: ${originName}. Release on either agent to connect both directions.`
      : relationship==='command'
        ? `Commanding agent selected: ${originName}. Release on the agent it commands.`
        : `Reporting agent selected: ${originName}. Release on the agent it reports to.`;
  state.drawingLink={
    role:item.role,
    relationship,
    x:event.clientX-rect.left+chart.scrollLeft,
    y:event.clientY-rect.top+chart.scrollTop,
    origin,
    sourceNode,
  };
  origin.classList.add('active');
  sourceNode?.classList.add('link-source');
  drawLines();
  function finish(up, createLink){
    if(!state.drawingLink)return;
    const target=up?document.elementFromPoint(up.clientX,up.clientY)?.closest('.flow-node'):null;
    const shouldLink=Boolean(createLink&&target&&target.dataset.role!==item.role);
    clearLinkDrawing(false);
    if(shouldLink){
      addRelationship(item.role,target.dataset.role,relationship);
      renderFlowchart();
      return
    }
    renderRelationshipToolbar();
    drawLines()
  }
  const onWindowBlur=()=>finish(null,false);
  state.drawingLink.onWindowBlur=onWindowBlur;
  window.addEventListener('blur',onWindowBlur);
  origin.onpointermove=move=>{
    if(!state.drawingLink)return;
    state.drawingLink.x=move.clientX-rect.left+chart.scrollLeft;
    state.drawingLink.y=move.clientY-rect.top+chart.scrollTop;
    drawLines()
  };
  origin.onpointerup=up=>finish(up,true);
  origin.onpointercancel=()=>finish(null,false);
  origin.onlostpointercapture=()=>finish(null,false)
}
function enableLinkDrawing(handle, item, relationship){
  handle.onpointerdown=e=>startLinkDrawing(handle,item,relationship,e)
}
function relationshipPairKey(sourceRole,targetRole){
  return [sourceRole,targetRole].sort().join('::')
}
function isInterconnectedPair(sourceRole,targetRole){
  const has=(source,target,relationship)=>state.edges.some(edge=>edge.source_role===source&&edge.target_role===target&&edge.relationship===relationship);
  return has(sourceRole,targetRole,'command')&&has(targetRole,sourceRole,'command')&&has(sourceRole,targetRole,'report')&&has(targetRole,sourceRole,'report')
}
function isSupervisorEmployeePair(supervisorRole,employeeRole){
  const has=(source,target,relationship)=>state.edges.some(edge=>edge.source_role===source&&edge.target_role===target&&edge.relationship===relationship);
  return !isInterconnectedPair(supervisorRole,employeeRole)&&has(supervisorRole,employeeRole,'command')&&has(employeeRole,supervisorRole,'report')
}
function supervisorEmployeePairForEdge(edge){
  if(edge.relationship==='command'&&isSupervisorEmployeePair(edge.source_role,edge.target_role))return{supervisor:edge.source_role,employee:edge.target_role};
  if(edge.relationship==='report'&&isSupervisorEmployeePair(edge.target_role,edge.source_role))return{supervisor:edge.target_role,employee:edge.source_role};
  return null
}
function nodeBox(role){
  const node=document.querySelector(`.flow-node[data-role="${role}"]`);
  if(!node)return null;
  return{
    x: node.offsetLeft,
    y: node.offsetTop,
    w: node.offsetWidth,
    h: node.offsetHeight
  }
}
function edgePoints(from, to){
  const ax=from.x+from.w/2,
  ay=from.y+from.h/2,
  bx=to.x+to.w/2,
  by=to.y+to.h/2,
  dx=bx-ax,
  dy=by-ay;
  const scaleA=1/Math.max(Math.abs(dx)/(from.w/2), Math.abs(dy)/(from.h/2), .001);
  const scaleB=1/Math.max(Math.abs(dx)/(to.w/2), Math.abs(dy)/(to.h/2), .001);
  return{
    x1: ax+dx*scaleA,
    y1: ay+dy*scaleA,
    x2: bx-dx*scaleB,
    y2: by-dy*scaleB
  }
}
function appendArrow(svg, x1, y1, x2, y2, relationship, preview=false){
  const path=document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', `connection-line ${relationship}${preview?' preview':''}`);
  path.setAttribute('marker-end', `url(#${relationship}-arrow)`);
  const curve=Math.min(90, Math.hypot(x2-x1, y2-y1)/3),
  horizontal=Math.abs(x2-x1)>Math.abs(y2-y1);
  path.setAttribute('d', horizontal?`M ${x1} ${y1} C ${x1+(x2>x1?curve:-curve)} ${y1}, ${x2-(x2>x1?curve:-curve)} ${y2}, ${x2} ${y2}`: `M ${x1} ${y1} C ${x1} ${y1+(y2>y1?curve:-curve)}, ${x2} ${y2-(y2>y1?curve:-curve)}, ${x2} ${y2}`);
  svg.append(path)
}
function appendOffsetArrow(svg, from, to, relationship, offset=0, preview=false){
  const p=edgePoints(from,to), dx=p.x2-p.x1, dy=p.y2-p.y1, length=Math.max(1,Math.hypot(dx,dy));
  const nx=-dy/length*offset, ny=dx/length*offset;
  const x1=p.x1+nx, y1=p.y1+ny, x2=p.x2+nx, y2=p.y2+ny;
  const curve=Math.min(90,Math.hypot(x2-x1,y2-y1)/3), horizontal=Math.abs(x2-x1)>Math.abs(y2-y1);
  const path=document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', `connection-line ${relationship}${preview?' preview':''}`);
  path.setAttribute('marker-end', `url(#${relationship}-arrow)`);
  path.setAttribute('d', horizontal
    ? `M ${x1} ${y1} C ${x1+(x2>x1?curve:-curve)} ${y1}, ${x2-(x2>x1?curve:-curve)} ${y2}, ${x2} ${y2}`
    : `M ${x1} ${y1} C ${x1} ${y1+(y2>y1?curve:-curve)}, ${x2} ${y2-(y2>y1?curve:-curve)}, ${x2} ${y2}`);
  svg.append(path)
}
function appendInterconnectedLine(svg, from, to, relationship, offset=0, preview=false){
  const p=edgePoints(from,to), dx=p.x2-p.x1, dy=p.y2-p.y1, length=Math.max(1,Math.hypot(dx,dy));
  const nx=-dy/length*offset, ny=dx/length*offset;
  const cx=(p.x1+p.x2)/2+nx, cy=(p.y1+p.y2)/2+ny;
  const path=document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', `connection-line interconnected ${relationship}${preview?' preview':''}`);
  path.setAttribute('marker-start', `url(#${relationship}-arrow)`);
  path.setAttribute('marker-end', `url(#${relationship}-arrow)`);
  path.setAttribute('d', `M ${p.x1+nx} ${p.y1+ny} Q ${cx} ${cy} ${p.x2+nx} ${p.y2+ny}`);
  svg.append(path)
}
function drawLines(){
  const svg=$('#flow-lines');
  if(!svg)return;
  const chart=$('#flowchart');
  svg.setAttribute('width', Math.max(chart.clientWidth, chart.scrollWidth));
  svg.setAttribute('height', Math.max(chart.clientHeight, chart.scrollHeight));
  svg.innerHTML='<defs><marker id="report-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path class="connection-arrow report" d="M 1 1 L 11 6 L 1 11 z"/></marker><marker id="command-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path class="connection-arrow command" d="M 1 1 L 11 6 L 1 11 z"/></marker></defs>';
  const drawnSupervisor=new Set(), drawnInterconnected=new Set();
  state.edges.forEach(edge=>{
    const from=nodeBox(edge.source_role), to=nodeBox(edge.target_role);
    if(from&&to){
      const supervisorPair=supervisorEmployeePairForEdge(edge);
      if(supervisorPair){
        const pairKey=relationshipPairKey(supervisorPair.supervisor,supervisorPair.employee);
        if(drawnSupervisor.has(pairKey))return;
        drawnSupervisor.add(pairKey);
        const supervisorBox=nodeBox(supervisorPair.supervisor), employeeBox=nodeBox(supervisorPair.employee);
        appendOffsetArrow(svg,supervisorBox,employeeBox,'command',-5);
        appendOffsetArrow(svg,employeeBox,supervisorBox,'report',-5);
      }else if(isInterconnectedPair(edge.source_role,edge.target_role)){
        const pairKey=relationshipPairKey(edge.source_role,edge.target_role);
        if(drawnInterconnected.has(pairKey))return;
        drawnInterconnected.add(pairKey);
        appendInterconnectedLine(svg,from,to,'command',-5);
        appendInterconnectedLine(svg,from,to,'report',5);
      }else{
        const p=edgePoints(from, to);
        appendArrow(svg, p.x1, p.y1, p.x2, p.y2, edge.relationship)
      }
    }
  });
  if(state.drawingLink){
    const from=nodeBox(state.drawingLink.role);
    if(from){
      const pointer={
        x: state.drawingLink.x,
        y: state.drawingLink.y,
        w: 1,
        h: 1
      };
      if(state.drawingLink.relationship==='supervisor'){
        appendOffsetArrow(svg,from,pointer,'command',-5,true);
        appendOffsetArrow(svg,pointer,from,'report',-5,true);
      }else if(state.drawingLink.relationship==='bidirectional'){
        appendInterconnectedLine(svg,from,pointer,'command',-5,true);
        appendInterconnectedLine(svg,from,pointer,'report',5,true);
      }else{
        const p=edgePoints(from, pointer);
        appendArrow(svg, p.x1, p.y1, state.drawingLink.x, state.drawingLink.y, state.drawingLink.relationship, true)
      }
    }
  }
}
let layoutTimer;
function saveLayout(){
  clearTimeout(layoutTimer);
  drawLines();
  layoutTimer=setTimeout(()=>api(`/api/projects/${state.project.id}/layout`, {
    method: 'PUT', body: JSON.stringify({
      items: state.layout
    })
  }).catch(err=>alert(err.message)), 250)
}
let edgeTimer;
function saveEdges(){
  clearTimeout(edgeTimer);
  drawLines();
  edgeTimer=setTimeout(()=>api(`/api/projects/${state.project.id}/edges`, {
    method: 'PUT', body: JSON.stringify({
      edges: state.edges
    })
  }).catch(err=>alert(err.message)), 150)
}
function renderAgents(){
  const nav=$('#agents');
  nav.innerHTML='';
  const query=state.agentSearch.trim().toLowerCase();
  const visible=state.agents.filter(a=>!query||`${a.name} ${a.brief} ${a.id}`.toLowerCase().includes(query));
  if(!visible.length){nav.innerHTML='<p class="agent-search-empty">No matching agents.</p>'}
  visible.forEach(a=>{
    const b=document.createElement('button');
    b.className=`agent-button ${a.id===state.active?'active':''}`;
    b.innerHTML=`<span class="mini-avatar">${a.name[0]}</span><div><strong>${a.name}</strong><small></small></div>`;
    b.querySelector('small').textContent=`${runtimeLabel(a.runtime)}${a.git_enabled?' / shared Git':''}`;
    b.onclick=()=>selectAgent(a.id);
    nav.append(b)
  });
  $('#role-checks').innerHTML=state.agents.map(a=>`<label><input type="checkbox" value="${a.id}"> ${a.name}</label>`).join('')
}
function selectAgent(id){
  state.active=id;
  messagesPinnedToLatest=true;
  updateLatestMessagesButton();
  state.providerCommands=[];
  const a=activeAgent();
  if(!a){
    $('#agent-name').textContent='No agents yet';
    $('#agent-brief').textContent='Add a team member to this workspace to start a conversation.';
    $('#avatar').textContent='+';
    $('#runtime-badge').textContent='';
    renderExternalAccessButton();
    renderContextMeter();
    renderAgents();
    renderActiveSkillSummary();
    renderMessages();
    renderComposer();
    return
  }
  $('#agent-name').textContent=a.name;
  $('#agent-brief').textContent=a.brief;
  $('#avatar').textContent=a.name[0];
  $('#runtime-badge').textContent=runtimeLabel(a.runtime);
  $('#active-model-label').textContent=a.runtime?.model||'Agent default';
  renderExternalAccessButton();
  renderContextMeter();
  renderActiveSkillSummary();
  renderAgents();
  renderMessages();
  renderComposer();
  loadChatControls();
  if(state.project)loadHistory(id).then(()=>{
    if(state.active===id)scrollMessagesToLatest();
    return recoverActiveRun(id)
  })
}
function normalizeMessage(m, role=state.active){
  const kind=m.speaker==='user'?'user': m.speaker==='error'?'error': m.speaker==='native'?'native': m.speaker==='app'?'app': 'agent';
  const sourceRole=m.source_role||'';
  const messageKind=m.message_kind||'';
  const compiledParts=messageKind==='command'?compiledCommandParts(m.content):null;
  const sourceAgent=sourceRole?state.agents.find(agent=>agent.id===sourceRole):null;
  const permissionRequest=m.permission_request||permissionRequestFromText(m.content);
  return{
    id: m.id,
    kind,
    text: permissionRequest?String(m.content).replace(/\s*<permission_request\s+scope="(?:workspace|external)">[\s\S]*?<\/permission_request>\s*/i,'').trim()||'I need your approval before continuing.':m.content,
    internal: m.speaker==='user'&&m.content===INTERNAL_CONTINUATION_PROMPT,
    provider: m.provider,
    model: m.model,
    created_at: m.created_at,
    reply_to_id: m.reply_to_id,
    attachments: m.attachments||[],
    source_role: sourceRole,
    message_kind: messageKind,
    compiled: Boolean(compiledParts?.length),
    compiled_parts: compiledParts||[],
    permission_request: permissionRequest,
    delivery_status: m.delivery_status||'',
    by: sourceAgent?.name||(
      kind==='agent'?state.agents.find(a=>a.id===role)?.name:
      kind==='native'?'Codex': kind==='error'?'Provider error': 'Workspace'
    )
  }
}
function permissionRequestFromText(value){
  const match=String(value||'').match(/<permission_request\s+scope="(workspace|external)">\s*([\s\S]*?)\s*<\/permission_request>/i);
  if(!match)return null;
  const body=match[2].trim(),
  reason=body.match(/<reason>\s*([\s\S]*?)\s*<\/reason>/i),
  commandBody=body.match(/<commands>\s*([\s\S]*?)\s*<\/commands>/i),
  commands=(commandBody?.[1]||'').split(/\r?\n/).filter(line=>line.trim().startsWith('- ')).map(line=>line.trim().slice(2)).filter(Boolean);
  return {scope:match[1].toLowerCase(),reason:reason?.[1].trim()||body,commands,status:'pending'}
}
function compiledCommandParts(value){
  const text=String(value??'').trim();
  const envelopes=[
    ['COMPILED COMMAND REQUEST:','END COMPILED COMMAND REQUEST'],
    ['<compiled_command_request>','</compiled_command_request>']
  ];
  const envelope=envelopes.find(([prefix,suffix])=>text.startsWith(prefix)&&text.endsWith(suffix));
  if(!envelope)return null;
  const [prefix,suffix]=envelope, body=text.slice(prefix.length,-suffix.length).trim();
  const parts=[], current=[];
  body.split(/\r?\n/).forEach(line=>{
    if(line.startsWith('- ')){
      if(current.length)parts.push(current.join('\n').trim());
      current.length=0;
      current.push(line.slice(2));
    }else if(current.length){
      current.push(line)
    }
  });
  if(current.length)parts.push(current.join('\n').trim());
  return parts.filter(Boolean).length?parts.filter(Boolean):body?[body]:null
}
async function loadHistory(role){
  if(!state.project)return false;
  const projectId=state.project.id;
  const activeLoad=state.historyLoads[role];
  if(activeLoad)return activeLoad;
  const hadVisibleHistory=Array.isArray(state.messages[role])&&state.messages[role].length>0;
  let request;
  request=(async()=>{
    try{
      const out=await api(`/api/agents/${role}/history?project_id=${projectId}`);
      if(state.project?.id!==projectId)return false;
      const messages=out.messages.map(m=>normalizeMessage(m, role));
      // A history request can have started before a provider run finished.
      // Keep any run-level error rendered locally until a later history
      // response contains its persisted transcript row; otherwise polling can
      // erase the diagnostic immediately after it appears.
      const localErrors=(state.messages[role]||[]).filter(message=>message.localOnly&&message.kind==='error');
      const persistedErrors=new Set(messages.filter(message=>message.kind==='error').map(message=>message.text));
      localErrors.forEach(message=>{
        if(!persistedErrors.has(message.text))messages.push(message)
      });
      state.messages[role]=messages;
      if(state.active===role){
        renderMessages();
        scrollMessagesToLatest()
      }
      refreshContextUsage(role);
      return true
    }
    catch(err){
      console.warn('Could not refresh chat history', err);
      // A polling refresh can fail transiently while an already-rendered
      // transcript remains valid. Do not turn that into a misleading chat bubble.
      if(state.active===role&&!hadVisibleHistory)localMessage(`Could not load history: ${err.message}`);
      return false
    }
    finally{
      if(state.historyLoads[role]===request)delete state.historyLoads[role]
    }
  })();
  state.historyLoads[role]=request;
  return request
}
function formatTime(value){
  if(!value)return'Now';
  const raw=value.includes('T')?value: value.replace(' ', 'T')+'Z';
  const date=new Date(raw);
  return Number.isNaN(date.valueOf())?'': date.toLocaleTimeString([], {
    hour: 'numeric', minute: '2-digit'
  })
}
function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g, character=>({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[character]))
}
const ANSI_COLOR_CLASSES={
  30:'ansi-black',31:'ansi-red',32:'ansi-green',33:'ansi-yellow',34:'ansi-blue',35:'ansi-magenta',36:'ansi-cyan',37:'ansi-white',
  90:'ansi-bright-black',91:'ansi-bright-red',92:'ansi-bright-green',93:'ansi-bright-yellow',94:'ansi-bright-blue',95:'ansi-bright-magenta',96:'ansi-bright-cyan',97:'ansi-bright-white'
};
function linkifyTerminalText(value){
  const source=String(value??''), pattern=/https?:\/\/[^\s<]+/gi;
  let html='', index=0, match;
  while((match=pattern.exec(source))){
    html+=escapeHtml(source.slice(index, match.index));
    let url=match[0], trailing='';
    while(/[.,!?;:)]$/.test(url)){
      trailing=url.slice(-1)+trailing;
      url=url.slice(0,-1)
    }
    const href=markdownUrl(url);
    html+=href
      ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>${escapeHtml(trailing)}`
      : escapeHtml(match[0]);
    index=match.index+match[0].length
  }
  return html+escapeHtml(source.slice(index))
}
function renderAnsiTerminal(value){
  const source=String(value??''), pattern=/\x1b\[([0-?]*)(?:[ -/]*)([@-~])/g;
  let html='', index=0, match, bold=false, dim=false, foreground='';
  const renderSegment=text=>{
    text=text.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'').replace(/\x1b/g,'');
    if(!text)return'';
    const classes=[];
    if(bold)classes.push('ansi-bold');
    if(dim)classes.push('ansi-dim');
    if(foreground)classes.push(foreground);
    const content=linkifyTerminalText(text);
    return classes.length?`<span class="${classes.join(' ')}">${content}</span>`:content
  };
  const applySgr=codes=>{
    if(!codes.length)codes=[0];
    codes.forEach(code=>{
      if(code===0){bold=false;dim=false;foreground=''}
      else if(code===1)bold=true;
      else if(code===2)dim=true;
      else if(code===22){bold=false;dim=false}
      else if(code===39)foreground='';
      else if(ANSI_COLOR_CLASSES[code])foreground=ANSI_COLOR_CLASSES[code]
    })
  };
  while((match=pattern.exec(source))){
    html+=renderSegment(source.slice(index, match.index));
    if(match[2]==='m'){
      const codes=(match[1]||'0').split(';').map(Number).filter(Number.isFinite);
      applySgr(codes)
    }
    index=match.index+match[0].length
  }
  return html+renderSegment(source.slice(index))
}
const CODE_LANGUAGE_ALIASES={
  js: 'javascript',
  jsx: 'jsx',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'tsx',
  py: 'python',
  rb: 'ruby',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  node: 'javascript',
  python3: 'python',
  yml: 'yaml',
  md: 'markdown',
  mkdown: 'markdown',
  txt: 'plaintext',
  text: 'plaintext',
  plain: 'plaintext',
  golang: 'go',
  rs: 'rust',
  cs: 'csharp',
  'c++': 'cpp',
  hpp: 'cpp',
  hxx: 'cpp',
  htm: 'html',
  svg: 'xml',
  xhtml: 'xml'
};
const CODE_KEYWORDS={
  generic: new Set('as async await break case catch class const continue debugger default delete do else export extends finally for from function if implements import in instanceof interface let new of package private protected public return static super switch throw try typeof undefined var void while with yield'.split(' ')),
  javascript: new Set('as async await break case catch class const continue debugger default delete do else export extends finally for from function get if import in instanceof let new of return set static super switch throw try typeof var void while with yield'.split(' ')),
  typescript: new Set('abstract as async await boolean break case catch class const continue debugger declare default delete do else enum export extends finally for from function get if implements import in infer interface is keyof let module namespace never new null number object of private protected public readonly return set static string super switch throw type typeof undefined unique unknown var void while with yield'.split(' ')),
  tsx: new Set('abstract as async await boolean break case catch class const continue debugger declare default delete do else enum export extends finally for from function get if implements import in infer interface is keyof let module namespace never new null number object of private protected public readonly return set static string super switch throw type typeof undefined unique unknown var void while with yield'.split(' ')),
  python: new Set('and as assert async await break case class continue def del elif else except finally for from global if import in is lambda match nonlocal not or pass raise return try while with yield'.split(' ')),
  ruby: new Set('alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield'.split(' ')),
  shell: new Set('case do done elif else esac fi for function if in select then time until while coproc'.split(' ')),
  yaml: new Set('true false null yes no on off'.split(' ')),
  json: new Set('true false null'.split(' ')),
  sql: new Set('add all alter and any as asc begin between by case check column commit constraint create cross delete desc distinct drop else end exists from full group having in index inner insert into is join left like limit not null on or order outer primary references right rollback select set table then top truncate union unique update values view when where with'.split(' ')),
  java: new Set('abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while'.split(' ')),
  csharp: new Set('abstract as async await base bool break byte case catch char checked class const continue decimal default delegate do double else enum event explicit extern false finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new null object operator out override params private protected public readonly ref return sbyte sealed short sizeof stackalloc static string struct switch this throw true try typeof uint ulong unchecked unsafe ushort using virtual void volatile while'.split(' ')),
  cpp: new Set('alignas alignof and asm auto bool break case catch char class const constexpr continue decltype default delete do double else enum explicit export extern false float for friend if inline int long mutable namespace new noexcept not nullptr operator private protected public register reinterpret_cast requires return short signed sizeof static static_assert struct switch template this throw true try typedef typeid typename union unsigned using virtual void volatile wchar_t while'.split(' ')),
  go: new Set('break default func interface select case defer go map struct chan else goto package switch const fallthrough if range type continue for import return var'.split(' ')),
  rust: new Set('as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while'.split(' ')),
  css: new Set('and as by from import media not or supports'.split(' ')),
  markdown: new Set('true false null'.split(' '))
};
const CODE_BOOLEAN_WORDS=new Set(['true','false','True','False','TRUE','FALSE','yes','no','on','off']);
const CODE_NULL_WORDS=new Set(['null','None','nil','undefined','NaN']);
const CODE_HASH_COMMENT_LANGUAGES=new Set(['python','ruby','shell','yaml','perl','r','toml','ini']);
const CODE_MARKUP_LANGUAGES=new Set(['html','xml','jsx','tsx']);
const CODE_LANGUAGE_LABELS={
  javascript: 'JavaScript', typescript: 'TypeScript', tsx: 'TSX', jsx: 'JSX',
  python: 'Python', ruby: 'Ruby', shell: 'Shell', yaml: 'YAML', json: 'JSON',
  sql: 'SQL', html: 'HTML', xml: 'XML', css: 'CSS', markdown: 'Markdown', diff: 'Git diff',
  java: 'Java', csharp: 'C#', cpp: 'C/C++', go: 'Go', rust: 'Rust', plaintext: 'Plain text'
};
const CODE_RUNNABLE_LANGUAGES=new Set(['python','javascript','shell','ruby']);
function normalizeCodeLanguage(value){
  const raw=String(value??'').trim().toLowerCase().replace(/^language-/, '');
  return CODE_LANGUAGE_ALIASES[raw]||raw||'plaintext'
}
function inferCodeLanguage(source){
  const code=String(source??'').trim();
  if(/^<!doctype\s+html|^<html[\s>]/i.test(code))return'html';
  if(/^(?:\{|\[)[\s\S]*(?:\}|\])$/.test(code)&&/"[^"\n]+"\s*:/.test(code))return'json';
  if(/^(?:from\s+\w+\s+import|import\s+\w+|def\s+\w+\s*\(|class\s+\w+\s*[:(]|#!.*\bpython)/m.test(code))return'python';
  if(/^(?:#!.*\b(?:ba|z)?sh\b|function\s+\w+|(?:const|let|var)\s+\w+\s*=|console\.log\s*\()/m.test(code))return'javascript';
  if(/^(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\b/im.test(code))return'sql';
  if(/^\s*[-\w]+:\s+[^\n]+(?:\n\s*[-\w]+:)/.test(code))return'yaml';
  return'plaintext'
}
function codeToken(kind, value){
  return `<span class="code-token ${kind}">${escapeHtml(value)}</span>`
}
function highlightDiff(value){
  return String(value??'').split('\n').map(line=>{
    const kind=line.startsWith('+')&&!line.startsWith('+++')?'diff-add':line.startsWith('-')&&!line.startsWith('---')?'diff-remove':line.startsWith('@@')?'diff-hunk':line.startsWith('diff ')||line.startsWith('index ')?'diff-header':'';
    return kind?codeToken(kind, line):escapeHtml(line)
  }).join('\n')
}
function consumeQuoted(source, start){
  const quote=source[start], triple=source.startsWith(quote.repeat(3), start), delimiter=triple?quote.repeat(3):quote;
  let index=start+delimiter.length;
  while(index<source.length){
    if(source[index]==='\\'){
      index+=Math.min(2, source.length-index);
      continue
    }
    if(source.startsWith(delimiter, index))return{end:index+delimiter.length, terminated:true};
    index++
  }
  return{end:source.length, terminated:false}
}
function codeCommentAt(source, index, language){
  const pair=source.slice(index, index+2);
  if(pair==='//'&&['javascript','typescript','jsx','tsx','java','csharp','cpp','go','rust','css','php','ruby','shell'].includes(language))return'//';
  if(pair==='/*'&&['javascript','typescript','jsx','tsx','java','csharp','cpp','go','rust','css','php','swift','kotlin'].includes(language))return'/*';
  if(pair==='--'&&['sql','lua','haskell'].includes(language))return'--';
  if(source[index]==='#'&&CODE_HASH_COMMENT_LANGUAGES.has(language))return'#';
  if(source.startsWith('<!--', index)&&CODE_MARKUP_LANGUAGES.has(language))return'<!--';
  return''
}
function findMarkupEnd(source, start){
  let quote='';
  for(let index=start+1;index<source.length;index++){
    const character=source[index];
    if(quote){
      if(character==='\\')index++;
      else if(character===quote)quote='';
      continue
    }
    if(character==='"'||character==="'")quote=character;
    else if(character==='>')return index+1
  }
  return-1
}
function highlightMarkupTag(value){
  const match=value.match(/^(<\s*\/?\s*)([A-Za-z][\w:.-]*)([\s\S]*?)(\/?\s*>)$/);
  if(!match)return escapeHtml(value);
  let html=codeToken('punctuation', match[1])+codeToken('tag', match[2]);
  const attributes=match[3];
  let index=0;
  while(index<attributes.length){
    const rest=attributes.slice(index), whitespace=rest.match(/^\s+/);
    if(whitespace){html+=escapeHtml(whitespace[0]);index+=whitespace[0].length;continue}
    const name=rest.match(/^[A-Za-z_:][\w:.-]*/);
    if(name){html+=codeToken('attribute', name[0]);index+=name[0].length;continue}
    if(attributes[index]==='"'||attributes[index]==="'"){
      const quoted=consumeQuoted(attributes, index);
      html+=codeToken('string', attributes.slice(index, quoted.end));
      index=quoted.end;
      continue
    }
    const operator=rest.match(/^(?:=|\/)/);
    if(operator){html+=codeToken('operator', operator[0]);index+=operator[0].length;continue}
    html+=escapeHtml(attributes[index++])
  }
  return html+codeToken('punctuation', match[4])
}
function highlightCode(value, rawLanguage=''){
  const source=String(value??''), language=normalizeCodeLanguage(rawLanguage)||inferCodeLanguage(source);
  if(language==='diff')return highlightDiff(source);
  if(language==='plaintext')return escapeHtml(source);
  const keywords=CODE_KEYWORDS[language]||CODE_KEYWORDS.generic;
  let html='', index=0, previousWord='';
  while(index<source.length){
    const comment=codeCommentAt(source, index, language);
    if(comment){
      if(comment==='/*'){
        const end=source.indexOf('*/', index+2), finish=end<0?source.length:end+2;
        html+=codeToken('comment', source.slice(index, finish));
        index=finish
      }
      else if(comment==='<!--'){
        const end=source.indexOf('-->', index+4), finish=end<0?source.length:end+3;
        html+=codeToken('comment', source.slice(index, finish));
        index=finish
      }
      else{
        const end=source.indexOf('\n', index+comment.length), finish=end<0?source.length:end;
        html+=codeToken('comment', source.slice(index, finish));
        index=finish
      }
      continue
    }
    if(CODE_MARKUP_LANGUAGES.has(language)&&source[index]==='<'){
      const candidate=source.slice(index).match(/^<\s*\/?\s*[A-Za-z][\w:.-]*/);
      if(candidate){
        const end=findMarkupEnd(source, index);
        if(end>index){html+=highlightMarkupTag(source.slice(index, end));index=end;continue}
      }
    }
    if(source[index]==='"'||source[index]==="'"||source[index]==='`'){
      const quoted=consumeQuoted(source, index);
      html+=codeToken('string', source.slice(index, quoted.end));
      index=quoted.end;
      continue
    }
    const number=source.slice(index).match(/^(?:0[xob][\da-f]+|(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?n?)/i);
    if(number){html+=codeToken('number', number[0]);index+=number[0].length;previousWord='';continue}
    const word=source.slice(index).match(/^[A-Za-z_$][\w$]*/);
    if(word){
      const value=word[0], lower=value.toLowerCase(), following=source.slice(index+value.length).match(/^\s*(.)/);
      let kind='';
      if(keywords.has(value)||keywords.has(lower))kind='keyword';
      else if(CODE_BOOLEAN_WORDS.has(value)||CODE_BOOLEAN_WORDS.has(lower))kind='boolean';
      else if(CODE_NULL_WORDS.has(value)||CODE_NULL_WORDS.has(lower))kind='constant';
      else if(previousWord&&['class','interface','enum','struct','trait','type'].includes(previousWord.toLowerCase()))kind='class-name';
      else if(previousWord&&['def','function','func','fn'].includes(previousWord.toLowerCase()))kind='function';
      else if(following?.[1]==='(')kind='function';
      else if(source[index-1]==='.'||source[index-1]=== '@')kind='property';
      else if(language==='css'&&following?.[1]===':')kind='property';
      html+=kind?codeToken(kind, value):escapeHtml(value);
      previousWord=value;
      index+=value.length;
      continue
    }
    const operator=source.slice(index).match(/^(?:===|!==|==|!=|=>|<=|>=|\+\+|--|&&|\|\||\?\?|\?\.|\*\*|<<|>>|\+=|-=|\*=|\/=|%=|->|::|:=|[=+\-*\/%<>!&|^~?:])/);
    if(operator){html+=codeToken('operator', operator[0]);index+=operator[0].length;previousWord='';continue}
    html+=escapeHtml(source[index++])
  }
  return html
}
function codeLineNumber(source, index){
  return source.slice(0, index).split('\n').length
}
function lintCode(value, rawLanguage=''){
  const source=String(value??''), language=normalizeCodeLanguage(rawLanguage);
  if(language==='plaintext'||language==='diff'||!source.trim())return[];
  const issues=[], stack=[], pairs={')':'(',']':'[','}':'{'};
  let index=0;
  while(index<source.length){
    const comment=codeCommentAt(source, index, language);
    if(comment){
      if(comment==='/*'){
        const end=source.indexOf('*/', index+2);
        if(end<0)issues.push(`Unterminated comment on line ${codeLineNumber(source,index)}`), index=source.length;
        else index=end+2
      }
      else if(comment==='<!--'){
        const end=source.indexOf('-->', index+4);
        if(end<0)issues.push(`Unterminated comment on line ${codeLineNumber(source,index)}`), index=source.length;
        else index=end+3
      }
      else{
        const end=source.indexOf('\n', index+comment.length);
        index=end<0?source.length:end
      }
      continue
    }
    if(source[index]==='"'||source[index]==="'"||source[index]==='`'){
      const quoted=consumeQuoted(source,index);
      if(!quoted.terminated)issues.push(`Unterminated string on line ${codeLineNumber(source,index)}`);
      index=quoted.end;
      continue
    }
    if('([{'.includes(source[index]))stack.push({character:source[index], line:codeLineNumber(source,index)});
    else if(')]}'.includes(source[index])){
      const expected=pairs[source[index]], opening=stack.pop();
      if(!opening||opening.character!==expected){
        issues.push(`Unexpected ${source[index]} on line ${codeLineNumber(source,index)}`);
        if(opening)stack.push(opening)
      }
    }
    index++
  }
  stack.slice(-3).reverse().forEach(item=>issues.push(`Unclosed ${item.character} from line ${item.line}`));
  if(language==='json'&&!issues.length){
    try{JSON.parse(source)}
    catch(error){issues.push(`Invalid JSON: ${String(error.message||error).replace(/^Unexpected token /, '')}`)}
  }
  return issues.slice(0,3)
}
function renderCodeBlock(value, rawLanguage=''){
  const source=String(value??''), requested=String(rawLanguage??'').trim(), language=normalizeCodeLanguage(requested||inferCodeLanguage(source));
  const label=CODE_LANGUAGE_LABELS[language]||((requested||language||'Plain text').replace(/[-_]+/g, ' ').replace(/\b\w/g, character=>character.toUpperCase()));
  const issues=lintCode(source, language), lint=language==='plaintext'?'':issues.length?`<span class="code-lint warning" title="${escapeHtml(issues.join('\n'))}">⚠ ${issues.length} issue${issues.length===1?'':'s'}</span>`:'<span class="code-lint clean" title="Basic syntax check passed">✓</span>';
  const className=language==='plaintext'?'':` class="language-${escapeHtml(language)}"`;
  const run=CODE_RUNNABLE_LANGUAGES.has(language)?'<button type="button" class="code-action code-run" data-code-action="run">▶ Run</button>':'';
  return `<div class="markdown-code-block" data-language="${escapeHtml(language)}"><div class="code-toolbar"><span class="code-language">${escapeHtml(label)}</span><span class="code-toolbar-actions"><span class="code-lint-wrap">${lint}</span><button type="button" class="code-action code-copy" data-code-action="copy">Copy</button>${run}</span></div><pre><code${className}>${highlightCode(source, language)}</code></pre></div>`
}
function markdownUrl(value){
  const raw=String(value??'').trim().replace(/[\u0000-\u001f\u007f]/g, '');
  if(!raw)return'';
  try{
    const base=typeof document==='undefined'?'http://localhost/':document.baseURI;
    const url=new URL(raw, base);
    if(!['http:', 'https:', 'mailto:'].includes(url.protocol))return'';
    return url.href
  }
  catch{
    return''
  }
}
function markdownTableCells(line){
  let value=line.trim();
  if(value.startsWith('|'))value=value.slice(1);
  if(value.endsWith('|')&&!value.endsWith('\\|'))value=value.slice(0,-1);
  const cells=[], current=[];
  let escaped=false;
  for(const character of value){
    if(character==='|'&&!escaped){
      cells.push(current.join('').trim());
      current.length=0;
      continue
    }
    if(character==='\\'&&!escaped){
      escaped=true;
      continue
    }
    current.push(character);
    escaped=false
  }
  cells.push(current.join('').trim());
  return cells
}
function markdownTableSeparator(line){
  const cells=markdownTableCells(line);
  return cells.length>0&&cells.every(cell=>/^:?-{3,}:?$/.test(cell))
}
function markdownInline(value){
  let source=String(value??'').replace(/\r\n?/g, '\n');
  const tokens=[];
  const protect=html=>{
    const token=`\u0000md${tokens.length}\u0000`;
    tokens.push(html);
    return token
  };
  source=source.replace(/\\([\\`*_[\]{}()#+.!>~-])/g, '$1');
  source=source.replace(/(`+)([\s\S]*?)\1/g, (_, ticks, code)=>{
    let content=code.replace(/\n/g, ' ');
    if(content.length>1&&content.startsWith(' ')&&content.endsWith(' ')&&content.trim())content=content.slice(1,-1);
    return protect(`<code>${escapeHtml(content)}</code>`)
  });
  source=source.replace(/!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+["']([^"']*)["'])?\s*\)/g, (match, alt, rawUrl, title)=>{
    const href=markdownUrl(rawUrl.replace(/^<|>$/g, ''));
    if(!href)return match;
    const titleAttribute=title?` title="${escapeHtml(title)}"`:'';
    return protect(`<img src="${escapeHtml(href)}" alt="${escapeHtml(alt)}"${titleAttribute} loading="lazy">`)
  });
  source=source.replace(/\[([^\]\n]+)\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+["']([^"']*)["'])?\s*\)/g, (match, label, rawUrl, title)=>{
    const href=markdownUrl(rawUrl.replace(/^<|>$/g, ''));
    if(!href)return match;
    const titleAttribute=title?` title="${escapeHtml(title)}"`:'';
    return protect(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"${titleAttribute}>${markdownInline(label)}</a>`)
  });
  source=source.replace(/<((?:https?:\/\/|mailto:)[^>\s]+)>/gi, (_, rawUrl)=>{
    const href=markdownUrl(rawUrl);
    return href?protect(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(rawUrl)}</a>`):escapeHtml(rawUrl)
  });
  source=source.replace(/https?:\/\/[^\s<]+/gi, raw=>{
    let url=raw,
    trailing='';
    while(/[.,!?;:)]$/.test(url)){
      trailing=url.slice(-1)+trailing;
      url=url.slice(0,-1)
    }
    const href=markdownUrl(url);
    return href?protect(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a>`)+trailing:raw
  });
  let html=escapeHtml(source);
  html=html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  html=html.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  html=html.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  html=html.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  html=html.replace(/(^|[^\w_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
  html=html.replace(/\n/g, '<br>');
  return html.replace(/\u0000md(\d+)\u0000/g, (_, index)=>tokens[Number(index)]||'')
}
function renderMarkdown(value){
  const lines=String(value??'').replace(/\r\n?/g, '\n').split('\n');
  const blocks=[];
  const blank=line=>/^\s*$/.test(line);
  const listPattern=/^\s{0,3}([-+*]|\d+[.)])\s+(.*)$/;
  const blockStart=(line, next='')=>{
    if(/^\s{0,3}(?:`{3,}|~{3,})/.test(line))return true;
    if(/^\s{0,3}#{1,6}\s+/.test(line))return true;
    if(/^\s{0,3}> ?/.test(line))return true;
    if(listPattern.test(line))return true;
    if(/^\s{0,3}((?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line))return true;
    return line.includes('|')&&markdownTableSeparator(next)
  };
  let index=0;
  while(index<lines.length){
    if(blank(lines[index])){
      index++;
      continue
    }
    const line=lines[index];
    const fence=line.match(/^\s{0,3}(`{3,}|~{3,})\s*([^\s]*)\s*$/);
    if(fence){
      const marker=fence[1][0], size=fence[1].length, code=[];
      index++;
      while(index<lines.length&&!new RegExp(`^\\s{0,3}${marker}{${size},}\\s*$`).test(lines[index]))code.push(lines[index++]);
      if(index<lines.length)index++;
      const language=fence[2].replace(/[^a-z0-9_+-]/gi, '');
      blocks.push(renderCodeBlock(code.join('\n'), language));
      continue
    }
    if(/^ {4}/.test(line)){
      const code=[];
      while(index<lines.length&&( /^ {4}/.test(lines[index])||blank(lines[index]))){
        code.push(lines[index].startsWith('    ')?lines[index].slice(4):'');
        index++
      }
      while(code.length&&code[code.length-1]==='')code.pop();
      blocks.push(renderCodeBlock(code.join('\n')));
      continue
    }
    const heading=line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if(heading){
      const level=heading[1].length;
      blocks.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
      index++;
      continue
    }
    if(/^\s{0,3}((?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line)){
      blocks.push('<hr>');
      index++;
      continue
    }
    if(/^\s{0,3}> ?/.test(line)){
      const quoted=[];
      while(index<lines.length){
        const match=lines[index].match(/^\s{0,3}> ?(.*)$/);
        if(!match)break;
        quoted.push(match[1]);
        index++
      }
      blocks.push(`<blockquote>${renderMarkdown(quoted.join('\n'))}</blockquote>`);
      continue
    }
    if(line.includes('|')&&index+1<lines.length&&markdownTableSeparator(lines[index+1])){
      const headers=markdownTableCells(line), separators=markdownTableCells(lines[index+1]);
      index+=2;
      const rows=[];
      while(index<lines.length&&!blank(lines[index])&&lines[index].includes('|')){
        rows.push(markdownTableCells(lines[index++]));
      }
      const alignments=separators.map(cell=>cell.startsWith(':')&&cell.endsWith(':')?'center':cell.startsWith(':')?'left':cell.endsWith(':')?'right':'');
      const alignAttribute=position=>alignments[position]?` style="text-align:${alignments[position]}"`:'';
      blocks.push(`<div class="markdown-table-wrap"><table><thead><tr>${headers.map((cell, position)=>`<th${alignAttribute(position)}>${markdownInline(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${headers.map((_, position)=>`<td${alignAttribute(position)}>${markdownInline(row[position]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue
    }
    const firstList=line.match(listPattern);
    if(firstList){
      const ordered=/^\d/.test(firstList[1]), items=[];
      while(index<lines.length){
        const match=lines[index].match(listPattern);
        if(!match||/^\d/.test(match[1])!==ordered)break;
        let item=match[2];
        index++;
        while(index<lines.length&&/^ {2,}\S/.test(lines[index])&&!listPattern.test(lines[index]))item+=`\n${lines[index].trim()}`, index++;
        const task=item.match(/^\[([ xX])\]\s+(.*)$/);
        const checkbox=task?`<input type="checkbox" disabled${task[1].toLowerCase()==='x'?' checked':''}> `:'';
        items.push(`<li>${checkbox}${markdownInline(task?task[2]:item)}</li>`)
      }
      blocks.push(`<${ordered?'ol':'ul'}>${items.join('')}</${ordered?'ol':'ul'}>`);
      continue
    }
    const paragraph=[];
    while(index<lines.length&&!blank(lines[index])){
      if(paragraph.length&&blockStart(lines[index], lines[index+1]))break;
      paragraph.push(lines[index++])
    }
    blocks.push(`<p>${markdownInline(paragraph.join('\n'))}</p>`)
  }
  return blocks.join('')
}
function startReply(message){
  state.replyTo[state.active]=message;
  renderComposer();
  $('#message').focus()
}
async function copyText(value){
  if(navigator.clipboard?.writeText){
    await navigator.clipboard.writeText(value);
    return
  }
  const area=document.createElement('textarea');
  area.value=value;
  area.setAttribute('readonly', '');
  area.style.position='fixed';
  area.style.opacity='0';
  document.body.append(area);
  area.select();
  if(!document.execCommand('copy'))throw new Error('Clipboard access was denied');
  area.remove()
}
function codeBlockDetails(button){
  const block=button.closest('.markdown-code-block'), code=block?.querySelector('pre code');
  return block&&code?{
    block,
    code: code.textContent||'',
    language: block.dataset.language||'plaintext'
  }:null
}
function showCodeTerminal(result, language=''){
  const dialog=$('#code-terminal-dialog'), status=$('#code-terminal-status'), meta=$('#code-terminal-meta'), output=$('#code-terminal-output');
  if(!dialog||!status||!meta||!output)return;
  const success=Boolean(result.ok), timedOut=Boolean(result.timed_out), exitCode=result.exit_code===null||result.exit_code===undefined?'—':result.exit_code;
  status.textContent=timedOut?'Timed out':success?'Completed':`Exited ${exitCode}`;
  status.className=`code-terminal-status ${success?'success':'failure'}`;
  meta.textContent=`${(language||result.language||'code').toUpperCase()} · ${result.cwd||'project folder'}`;
  const sections=[];
  if(result.stdout)sections.push(result.stdout);
  if(result.stderr)sections.push(`${result.stdout?'\n\n':''}[stderr]\n${result.stderr}`);
  output.textContent=sections.join('')||'(process produced no output)';
  if(!dialog.open)dialog.showModal()
}
async function runCodeBlock(button){
  const details=codeBlockDetails(button);
  if(!details||!state.project||!CODE_RUNNABLE_LANGUAGES.has(details.language))return;
  const location=state.project.root_path||'the app workspace';
  if(!confirm(`Run this ${details.language} block in ${location}?\n\nCode can read or modify files in that project folder.`))return;
  const original=button.textContent;
  button.disabled=true;
  button.textContent='Running…';
  try{
    const result=await api('/api/code/execute', {
      method: 'POST', body: JSON.stringify({
        code: details.code,
        language: details.language,
        project_id: state.project.id
      })
    });
    showCodeTerminal(result, details.language)
  }
  catch(err){
    showCodeTerminal({ok:false, language:details.language, cwd:location, exit_code:'error', stderr:err.message}, details.language)
  }
  finally{
    button.disabled=false;
    button.textContent=original
  }
}
function wireCodeActions(container){
  container.querySelectorAll('[data-code-action]').forEach(button=>{
    button.onclick=async()=>{
      const details=codeBlockDetails(button);
      if(!details)return;
      if(button.dataset.codeAction==='run'){
        await runCodeBlock(button);
        return
      }
      try{
        await copyText(details.code);
        const original=button.textContent;
        button.textContent='Copied';
        setTimeout(()=>button.textContent=original, 1200)
      }
      catch(err){
        button.textContent='Copy failed';
        setTimeout(()=>button.textContent='Copy', 1500)
      }
    }
  })
}
function renderMessages(){
  const box=$('#messages'),
  allItems=(state.messages[state.active]||[]).filter(m=>!m.internal),
  query=state.historySearch.trim().toLowerCase(),
  items=query?allItems.filter(m=>`${m.text||''} ${m.by||''}`.toLowerCase().includes(query)):allItems;
  const preserveScrollPosition=!messagesPinnedToLatest,
  previousScrollTop=box.scrollTop;
  messagesRendering=true;
  observeMessagesForAutoScroll();
  box.innerHTML=items.length?'': `<div class="empty"><strong>${query?'No matching messages':'Start a conversation'}</strong><span>${query?'Try a different search term.':'Type / to browse commands. This role keeps its own transcript.'}</span></div>`;
  const count=$('#history-count');
  if(count)count.textContent=query?`${items.length} of ${allItems.length} messages`:allItems.length?`${allItems.length} messages`:'';
  items.forEach(m=>{
    const wrap=document.createElement('article');
    const compiled=Boolean(m.compiled&&m.compiled_parts?.length);
    wrap.className=`message-wrap ${m.kind}${m.message_kind?` inter-agent ${m.message_kind}`:''}${compiled?' compiled-command':''}`;
    const bubble=document.createElement('div');
    bubble.className=`message ${m.kind}${m.message_kind?` ${m.message_kind}`:''}${compiled?' compiled-command':''}`;
    if(m.message_kind&&m.source_role){
      const route=document.createElement('div');
      route.className='message-route';
      route.textContent=`${compiled?'Compiled command':m.message_kind==='command'?'Command':'Report'} from ${m.by}`;
      bubble.append(route)
    }
    const replyId=Number(m.reply_to_id), replied=Number.isInteger(replyId)&&replyId>0?items.find(x=>Number(x.id)===replyId): null;
    if(replied){
      const quote=document.createElement('div');
      quote.className='quoted-message';
      quote.textContent=`${replied.kind==='user'?'You':replied.by||activeAgent()?.name||'Team'}: ${replied.text}`;
      bubble.append(quote)
    }
    if(compiled){
      const label=document.createElement('div');
      label.className='compiled-command-label';
      label.textContent=`${m.compiled_parts.length} command${m.compiled_parts.length===1?'':'s'} combined`;
      bubble.append(label);
      const list=document.createElement('div');
      list.className='compiled-command-list';
      m.compiled_parts.forEach((part,index)=>{
        const item=document.createElement('div');
        item.className='compiled-command-item';
        const number=document.createElement('span');
        number.className='compiled-command-number';
        number.textContent=String(index+1).padStart(2,'0');
        const text=document.createElement('div');
        text.className='message-markdown';
        text.innerHTML=renderMarkdown(part);
        wireCodeActions(text);
        item.append(number,text);
        list.append(item)
      });
      bubble.append(list)
    }else{
      const text=document.createElement('div');
      text.className='message-markdown';
      text.innerHTML=renderMarkdown(m.text);
      wireCodeActions(text);
      bubble.append(text)
    }
    if(m.attachments?.length){
      const files=document.createElement('div');
      files.className='message-attachments';
      m.attachments.forEach(file=>{
        const link=document.createElement('a');
        link.className='attachment-chip';
        link.href=`/api/attachments/${file.id}`;
        link.target='_blank';
        link.rel='noopener';
        link.innerHTML='<b>↗</b><span></span>';
        link.querySelector('span').textContent=file.name;
        files.append(link)
      });
      bubble.append(files)
    }
    if(m.kind==='agent'&&m.permission_request){
      const request=m.permission_request;
      const requestCard=document.createElement('section');
      requestCard.className=`permission-request ${request.status||'pending'}`;
      const heading=document.createElement('strong');
      heading.textContent=request.scope==='external'?'External access requested':'Workspace action requested';
      const reason=document.createElement('p');
      reason.textContent=request.reason;
      requestCard.append(heading,reason);
      const commands=document.createElement('div');
      commands.className='permission-request-commands';
      const commandLabel=document.createElement('small');
      commandLabel.textContent=request.commands?.length?'Proposed commands':'No commands were supplied; this request cannot be approved.';
      commands.append(commandLabel);
      if(request.commands?.length){
        const list=document.createElement('ol');
        request.commands.forEach(command=>{
          const item=document.createElement('li'), code=document.createElement('code');
          code.textContent=command; item.append(code); list.append(item)
        });
        commands.append(list)
      }
      requestCard.append(commands);
      if(request.status==='pending'){
        const controls=document.createElement('div');
        controls.className='permission-request-controls';
        const approve=document.createElement('button'), deny=document.createElement('button');
        approve.type=deny.type='button'; approve.textContent='Approve once'; approve.disabled=!request.commands?.length; deny.textContent='Deny'; deny.className='danger';
        approve.onclick=()=>respondToPermissionRequest(m, true);
        deny.onclick=()=>respondToPermissionRequest(m, false);
        controls.append(approve,deny); requestCard.append(controls)
      }else{
        const status=document.createElement('small');
        status.textContent=request.status==='approved'?'Approved for one continuation':'Denied';
        requestCard.append(status)
      }
      bubble.append(requestCard)
    }
    const meta=document.createElement('div');
    meta.className='message-meta';
    meta.textContent=`${m.kind==='user'?'You':m.by||activeAgent()?.name||'Team'} · ${formatTime(m.created_at)}${m.runtime?` · ${m.runtime}`:''}`;
    bubble.append(meta);
    const actions=document.createElement('div');
    actions.className='message-actions';
    if(m.id){
      const reply=document.createElement('button');
      reply.type='button';
      reply.textContent='↩ Reply';
      reply.onclick=()=>startReply(m);
      actions.append(reply)
    }
    const copy=document.createElement('button');
    copy.type='button';
    copy.textContent='Copy';
    copy.onclick=async()=>{
      await navigator.clipboard.writeText(compiled?m.compiled_parts.join('\n\n'):m.text);
      copy.textContent='Copied';
      setTimeout(()=>copy.textContent='Copy', 1000)
    };
    actions.append(copy);
    wrap.append(bubble, actions);
    box.append(wrap)
  });
  if(preserveScrollPosition){
    box.scrollTop=Math.min(previousScrollTop,Math.max(0,box.scrollHeight-box.clientHeight));
    messagesPinnedToLatest=false
  }else messagesPinnedToLatest=true;
  messagesRendering=false;
  // Every agent has its own transcript. Keep pinned chats at the newest
  // message, but preserve the user's position when they are reading above it.
  observeMessagesForAutoScroll();
  scrollMessagesToLatest()
}
async function respondToPermissionRequest(message, approved){
  const request=message.permission_request, role=state.active, projectId=state.project?.id;
  if(!request||request.status!=='pending'||!projectId)return;
  try{
    const out=await api(`/api/agents/${role}/permission-response?project_id=${projectId}`, {
      method:'POST',body:JSON.stringify({message_id:message.id,approved})
    });
    request.status=approved?'approved':'denied';
    renderMessages();
    state.busy.add(role);
    setChatActivity(role, 'running', approved?`${activeAgent()?.name||role} is continuing with the approved access…`:`${activeAgent()?.name||role} is responding to the denied request…`);
    renderComposer();
    await waitForChatRun(out.run.id, role, projectId)
  }
  catch(err){alert(err.message)}
}
function setChatActivity(role, type, text){
  state.activities[role]={
    type,
    text
  };
  if(state.active===role)renderComposer()
}
const wait=milliseconds=>new Promise(resolve=>setTimeout(resolve, milliseconds));
function waitForChatRun(runId, role, projectId){
  if(state.runWatchers[role])return state.runWatchers[role];
  const watcher=(async()=>{
    let currentRunId=runId;
    state.runs[role]=currentRunId;
    console.info(`[chat-run] watching ${role} run ${currentRunId}`);
    while(true){
      const run=await api(`/api/chat-runs/${currentRunId}`);
      if(['completed', 'error'].includes(run.status)){
        console.info(`[chat-run] ${role} run ${currentRunId} ${run.status}`);
        await loadHistory(role);
        if(run.status==='error'){
          const text=String(run.error||run.result?.response||'');
          const messages=state.messages[role]??=[];
          if(text&&!messages.some(message=>message.kind==='error'&&message.text===text))messages.push({
            id: `run-error-${currentRunId}`,
            localOnly: true,
            kind: 'error', text, by: 'Workspace', created_at: run.updated_at, attachments: []
          })
        }
        let next=null;
        try{
          const active=await api(`/api/agents/${role}/active-run?project_id=${projectId}`);
          if(active.run&&active.run.id!==currentRunId)next=active.run
        }
        catch{}
        if(next){
          currentRunId=next.id;
          state.runs[role]=currentRunId;
          setChatActivity(role, 'running', `${state.agents.find(a=>a.id===role)?.name||role} has another queued prompt…`);
          continue
        }
        delete state.runs[role];
        state.busy.delete(role);
        setChatActivity(role, run.status==='error'?'error': 'complete', run.status==='error'?'The provider returned an error. Its full response is shown in the chat.': 'Response received and saved in this chat.');
        if(state.active===role){
          renderMessages();
          renderComposer()
        }
        return run
      }
      const name=state.agents.find(a=>a.id===role)?.name||role;
      if(run.status==='queued'){
        setChatActivity(role, 'queued', `${name} is busy; this prompt is queued for its next turn.`)
      }
      else{
        setChatActivity(role, 'running', `${name} is working… New prompts will be queued.`)
      }
      await wait(750)
    }
  })();
  let tracked;
  tracked=watcher.finally(()=>{
    if(state.runWatchers[role]===tracked)delete state.runWatchers[role]
  });
  state.runWatchers[role]=tracked;
  return tracked
}
async function recoverActiveRun(role){
  if(!state.project||state.runs[role])return;
  const projectId=state.project.id;
  state.runs[role]='checking';
  try{
    const out=await api(`/api/agents/${role}/active-run?project_id=${projectId}`);
    if(!out.run||state.project?.id!==projectId){
      delete state.runs[role];
      state.busy.delete(role);
      if(state.active===role)renderComposer();
      return
    }
    console.info(`[chat-run] found active ${role} run ${out.run.id}`);
    state.busy.add(role);
    setChatActivity(role, 'running', `${state.agents.find(a=>a.id===role)?.name||role} is still working…`);
    renderComposer();
    await waitForChatRun(out.run.id, role, projectId)
  }
  catch(err){
    state.busy.delete(role);
    delete state.runs[role];
    setChatActivity(role, 'error', `Could not follow the agent run: ${err.message}`);
    renderComposer()
  }
}
async function monitorAgentRuns(){
  if(!state.project||!state.agents.length||state.runPollPromise)return;
  const projectId=state.project.id;
  state.runPollPromise=Promise.all(state.agents.map(agent=>{
    if(state.project?.id!==projectId||state.runs[agent.id])return null;
    return recoverActiveRun(agent.id)
  })).finally(()=>{
    state.runPollPromise=null
  });
  await state.runPollPromise
}
function renderComposer(){
  const role=state.active,
  agent=activeAgent(),
  reply=state.replyTo[role],
  pending=state.pendingAttachments[role]||[],
  busy=state.busy.has(role),
  preview=$('#reply-preview'),
  tray=$('#attachment-tray'),
  activity=$('#chat-activity'),
  activityState=state.activities[role];
  preview.classList.toggle('hidden', !reply);
  if(reply){
    $('#reply-author').textContent=reply.kind==='user'?'You': activeAgent().name;
    $('#reply-text').textContent=reply.text
  }
  tray.innerHTML='';
  pending.forEach(file=>{
    const chip=document.createElement('span');
    chip.className='pending-attachment';
    const name=document.createElement('span');
    name.textContent=file.name;
    const remove=document.createElement('button');
    remove.type='button';
    remove.textContent='×';
    remove.onclick=async()=>{
      try{
        await api(`/api/attachments/${file.id}`, {
          method: 'DELETE'
        })
      }
      catch{}state.pendingAttachments[role]=pending.filter(x=>x.id!==file.id);
      renderComposer()
    };
    chip.append(name, remove);
    tray.append(chip)
  });
  tray.classList.toggle('hidden', !pending.length);
  activity.className=`chat-activity ${activityState?.type||''}${activityState?'':' hidden'}`;
  const activityLabel=activity.querySelector('.chat-activity-label');
  if(activityLabel)activityLabel.textContent=activityState?.text||'';
  else activity.textContent=activityState?.text||'';
  activity.dataset.state=activityState?.type||'';
  // An active run blocks provider execution, not composing. Messages sent
  // while busy are persisted as queued runs and submitted when this agent is
  // free, so the send control must remain usable.
  $('#message').disabled=!agent;
  $('#chat-model').disabled=!agent;
  $('#chat-effort').disabled=!agent;
  $('#attach-button').disabled=!agent||$('#attach-button').dataset.enabled!=='true';
  $('#send-button').disabled=!agent||!$('#message').value.trim();
  $('#send-button').innerHTML=busy?'Queue <span>↗</span>': 'Send <span>↗</span>'
  const model=$('#chat-model')?.selectedOptions?.[0]?.textContent||agent?.runtime?.model||'Agent default';
  const modelLabel=$('#active-model-label');
  if(modelLabel)modelLabel.textContent=model;
}
function openAgentContextMenu(event, agent){
  event.preventDefault();
  event.stopPropagation();
  state.contextAgent=agent;
  const menu=$('#agent-context-menu');
  menu.querySelector('[data-agent-action="git"]').textContent=agent.git_enabled
    ? '⌁ Disable shared Git workflow'
    : '⌁ Enable shared Git workflow';
  menu.style.left=`${Math.min(event.clientX,window.innerWidth-245)}px`;
  menu.style.top=`${Math.min(event.clientY,window.innerHeight-150)}px`;
  menu.classList.remove('hidden')
}
function closeAgentContextMenu(){
  $('#agent-context-menu').classList.add('hidden');
  state.contextAgent=null
}
async function deleteAgentFromMenu(agent){
  if(!confirm(`Remove ${agent.name} from the team?\n\nThis permanently removes its runtime settings, conversations, and all command/reporting relationships. Saved templates are not affected.`))return;
  await api(`/api/agents/${agent.id}?project_id=${state.project.id}`, {
    method: 'DELETE'
  });
  state.agents=await api(`/api/agents?project_id=${state.project.id}`);
  state.layout=await api(`/api/projects/${state.project.id}/layout`);
  state.edges=await api(`/api/projects/${state.project.id}/edges`);
  await loadSkills();
  if(state.active===agent.id)state.active=state.agents[0]?.id||'';
  renderAgents();
  if(state.active)selectAgent(state.active);
  else selectAgent('');
  renderFlowchart();
  await loadTemplates()
}
async function loadTemplates(){
  state.templates=await api('/api/agent-templates');
  const box=$('#agent-template-select');
  box.innerHTML='<option value="">No template</option>'+state.templates.map(t=>`<option value="${t.id}">${t.name}</option>`).join('')
}
function renderContext(){
  const box=$('#context-list');
  $('#context-count').textContent=state.context.length;
  const summary=$('#context-scope-summary');
  if(summary){
    const scoped=state.context.filter(item=>Array.isArray(item.roles)&&item.roles.includes(state.active));
    summary.textContent=state.active?`${scoped.length} item${scoped.length===1?'':'s'} scoped to this agent · ${state.context.length-scoped.length} shared`:'';
  }
  box.innerHTML=state.context.length?'': '<p style="color:var(--muted);font-size:12px">No shared context yet.</p>';
  state.context.forEach(x=>{
    const d=document.createElement('article');
    d.className='context-card';
    const roles=x.roles?.length?x.roles: ['All agents'];
    const scope=roles.includes(state.active)?'Available to this agent':(x.roles?.length?'Other agents':'Shared with all agents');
    d.innerHTML=`<h3></h3><p></p><div class="context-scope-label">${scope}</div><div class="tags">${roles.map(r=>`<span class="tag">${r}</span>`).join('')}</div>`;
    d.querySelector('h3').textContent=x.title;
    d.querySelector('p').textContent=x.content;
    d.onclick=()=>openContext(x);
    box.append(d)
  })
}
function openContext(item=null){
  $('#context-id').value=item?.id||'';
  $('#context-title-input').value=item?.title||'';
  $('#context-content').value=item?.content||'';
  document.querySelectorAll('#role-checks input').forEach(c=>c.checked=item?.roles.includes(c.value)||false);
  $('#delete-context').style.visibility=item?'visible': 'hidden';
  $('#context-dialog').showModal()
}
function allCommands(){
  const provider=activeAgent()?.runtime?.provider||'*',
  history=state.commandHistory.filter(item=>item.provider==='*'||item.provider===provider).map(item=>({
    name: item.name, args: '', description: 'Previously used provider command'
  })),
  seen=new Set();
  return [...state.providerCommands,
  ...commands,
  ...history].filter(command=>{
    const key=command.name.toLowerCase();
    if(seen.has(key))return false;
    seen.add(key);
    return true
  })
}
function commandMatches(value){
  const query=value.toLowerCase(),
  trimmed=query.trimEnd();
  return allCommands().filter(command=>{
    const name=command.name.toLowerCase();
    return name.startsWith(trimmed)||trimmed.startsWith(`${name} `)
  })
}
function renderCommands(){
  const input=$('#message'),
  menu=$('#command-menu');
  if(!input.value.startsWith('/')||input.value.includes('\n')){
    menu.classList.add('hidden');
    return
  }
  const matches=commandMatches(input.value);
  if(!matches.length){
    menu.classList.add('hidden');
    return
  }
  state.commandIndex=Math.min(state.commandIndex, matches.length-1);
  menu.innerHTML='';
  matches.forEach((c, i)=>{
    const b=document.createElement('button');
    b.type='button';
    b.className=`command-option ${i===state.commandIndex?'active':''}`;
    b.innerHTML=`<code>${c.name} ${c.args}</code><span>${c.description}</span>`;
    b.onmousedown=e=>{
      e.preventDefault();
      chooseCommand(c)
    };
    menu.append(b)
  });
  menu.classList.remove('hidden')
}
function chooseCommand(command){
  const input=$('#message'),
  typed=input.value.trim(),
  name=command.name;
  const suffix=typed.toLowerCase().startsWith(name.toLowerCase())?typed.slice(name.length).trimStart(): '';
  input.value=`${name}${command.args?' ':''}${suffix}`;
  input.focus();
  $('#command-menu').classList.add('hidden')
}
function recordCommand(text){
  const trimmed=text.trim(),
  provider=activeAgent()?.runtime?.provider||'*',
  name=trimmed.split(/\s+/)[0];
  if(!name.startsWith('/')||name==='/gh'||name==='/app')return;
  state.commandHistory=[{
    provider,
    name
  },
  ...state.commandHistory.filter(item=>!(item.provider===provider&&item.name===name))].slice(0, 100);
  try{
    localStorage.setItem(COMMAND_HISTORY_KEY, JSON.stringify(state.commandHistory))
  }
  catch{}
}
function localMessage(text, kind='app', by='Workspace'){
  const role=state.active,
  projectId=state.project?.id;
  (state.messages[role]??=[]).push({
    kind, text, by, created_at: new Date().toISOString(), attachments: []
  });
  renderMessages();
  if(kind==='app'&&projectId)api(`/api/agents/${role}/app-message`, {
    method: 'POST', body: JSON.stringify({
      content: text, project_id: projectId
    })
  }).catch(()=>{})
}
async function executeAppCommand(subcommand, arg){
  if(subcommand==='help'){
    localMessage(commands.map(c=>`${c.name} ${c.args} — ${c.description}`).join('\n'));
    return true
  }
  if(subcommand==='project'||subcommand==='team'){
    showDashboard();
    return true
  }
  if(subcommand==='context'){
    openContext();
    return true
  }
  if(subcommand==='clear'){
    if(confirm(`Clear ${activeAgent().name}'s saved transcript?`)){
      await clearHistory()
    }
    return true
  }
  if(subcommand==='agent'){
    const query=arg.toLowerCase();
    const agent=state.agents.find(a=>a.id===query||a.name.toLowerCase()===query);
    if(agent)selectAgent(agent.id);
    else localMessage(`Unknown agent “${arg}”. Try: ${state.agents.map(a=>a.id).join(', ')}`);
    return true
  }
  if(subcommand==='model'){
    const option=[...$('#chat-model').options].find(o=>o.value===arg||o.text.toLowerCase().includes(arg.toLowerCase()));
    if(option){
      $('#chat-model').value=option.value;
      $('#chat-model').onchange();
      localMessage(`Chat model override set to ${option.text}.`)
    }
    else localMessage('Model not found in this provider’s available model list.');
    return true
  }
  if(subcommand==='effort'){
    const option=[...$('#chat-effort').options].find(o=>o.value===arg);
    if(option){
      $('#chat-effort').value=option.value;
      localMessage(`Chat effort override set to ${option.text}.`)
    }
    else localMessage('Unsupported effort for the selected model.');
    return true
  }
  if(subcommand==='skill'){
    const parts=arg.trim().split(/\s+/), slug=parts.shift()||'';
    if(!slug){
      openSkillsDialog();
      return true
    }
    const skill=state.skills.find(item=>String(item.id)===slug||item.slug===slug||item.name.toLowerCase()===slug.toLowerCase());
    if(!skill||!skill.assigned_roles?.includes(state.active)){
      localMessage(`Skill “${slug}” is not assigned to ${activeAgent()?.name||'this agent'}.`, 'error');
      return true
    }
    let inputs={};
    const raw=parts.join(' ').trim();
    if(raw){
      try{inputs=JSON.parse(raw)}
      catch{localMessage('Skill inputs must be one valid JSON object.', 'error');return true}
    }
    const result=await runAssignedSkill(skill.id, state.active, inputs, false);
    if(result)localMessage(`Skill: ${skill.name}\n\n${skillResultMessageText(result)}`, result.ok?'app':'error', 'Skill');
    return true
  }
  return false
}
async function executeCommand(text){
  const [command,
  ...parts]=text.trim().split(/\s+/),
  arg=parts.join(' ');
  if(command==='/gh'){
    try{
      const out=await api(`/api/projects/${state.project.id}/github`);
      localMessage(out.report, 'app')
    }
    catch(err){
      localMessage(`GitHub report error: ${err.message}`)
    }
    return true
  }
  if(command==='/app'){
    return executeAppCommand(parts.shift()||'help', parts.join(' '))
  }
  if(!command.startsWith('/'))return false;
  recordCommand(text);
  const provider=activeAgent()?.runtime?.provider;
  if(provider!=='codex')return false;
  try{
    const out=await api(`/api/native-command/${state.active}`, {
      method: 'POST', body: JSON.stringify({
        command: text, project_id: state.project.id
      })
    });
    localMessage(out.response, 'native', 'Codex')
  }
  catch(err){
    localMessage(`Native provider command error: ${err.message}`, 'error', 'Workspace')
  }
  return true
}
$('#message').oninput=()=>{
  state.commandIndex=0;
  renderCommands();
  renderComposer()
};
$('#message').onkeydown=e=>{
  const menu=$('#command-menu'),
  visible=!menu.classList.contains('hidden'),
  matches=visible?commandMatches(e.currentTarget.value): [];
  if(visible&&(e.key==='ArrowDown'||e.key==='ArrowUp')){
    e.preventDefault();
    state.commandIndex=(state.commandIndex+(e.key==='ArrowDown'?1: -1)+matches.length)%matches.length;
    renderCommands();
    return
  }
  if(visible&&e.key==='Tab'){
    e.preventDefault();
    chooseCommand(matches[state.commandIndex]);
    return
  }
  if(e.key==='Escape'&&visible){
    menu.classList.add('hidden');
    return
  }
  if(e.key==='Enter'&&!e.shiftKey){
    e.preventDefault();
    if(visible&&matches.length){
      const selected=matches[state.commandIndex];
      chooseCommand(selected);
      if(selected.args)return
    }
    $('#chat-form').requestSubmit()
  }
};
$('#cancel-reply').onclick=()=>{
  delete state.replyTo[state.active];
  renderComposer()
};
$('#attach-button').onclick=()=>$('#attachment-input').click();
$('#attachment-input').onchange=async e=>{
  const role=state.active,
  files=[...e.target.files],
  current=state.pendingAttachments[role]||[];
  e.target.value='';
  if(current.length+files.length>6)return alert('You can attach up to 6 files per message.');
  $('#attach-button').disabled=true;
  try{
    for(const file of files){
      const form=new FormData();
      form.append('attachment', file);
      const item=await upload(`/api/agents/${role}/attachments?project_id=${state.project.id}`, form);
      (state.pendingAttachments[role]??=[]).push(item)
    }
  }
  catch(err){
    alert(err.message)
  }
  finally{
    renderComposer()
  }
};
async function openRuntime(){
  const a=activeAgent(),
  r=a.runtime;
  $('#runtime-title').textContent=`Configure ${a.name}`;
  $('#restart-codex-session').style.display=r.provider==='codex'?'inline-block':'none';
  $('#provider-select').innerHTML=state.providers.map(p=>`<option value="${p.id}">${p.name}${p.available?'':' — not configured'}</option>`).join('');
  $('#provider-select').value=r.provider;
  $('#base-url-input').value=r.base_url;
  $('#key-env-input').value=r.api_key_env;
  $('#context-window-input').value=Number(r.context_window_tokens)||128000;
  $('#context-compaction-input').value=String(Number(r.context_compaction_threshold)||0);
  $('#context-window-input').nextElementSibling.textContent='Fallback window when the provider does not report one; Codex uses its runtime-reported effective window when available.';
  $('#context-compaction-input').nextElementSibling.textContent='Codex uses reported token counts before native /compact; other providers use reported usage when available and saved summaries otherwise.';
  updateRuntimeFields();
  $('#runtime-dialog').showModal();
  await loadModels(r.model, r.reasoning_effort)
}
function updateRuntimeFields(){
  const id=$('#provider-select').value,
  p=state.providers.find(x=>x.id===id);
  $('#provider-help').textContent=p?.description||'';
  $('#base-url-field').style.display=id==='compatible'?'flex': 'none';
  $('#base-url-input').required=id==='compatible';
  $('#key-env-field').style.display=['google',
  'compatible'].includes(id)?'flex': 'none';
  if(!$('#key-env-input').value)$('#key-env-input').value=p?.api_key_env||''
}
function modelLabel(m){
  return `${m.displayName||m.id}${m.hidden?' (hidden)':''}${m.upgrade?` → ${m.upgrade}`:''}`
}
function fillEfforts(box, models, modelId, selected='', defaultLabel='Model default'){
  const model=models.find(m=>m.id===modelId),
  efforts=model?.supportedReasoningEfforts||[];
  box.innerHTML=`<option value="">${defaultLabel}${model?.defaultReasoningEffort?` (${model.defaultReasoningEffort})`:''}</option>`+efforts.map(e=>`<option value="${e.reasoningEffort}" title="${e.description}">${e.reasoningEffort}</option>`).join('');
  if(selected&&efforts.some(e=>e.reasoningEffort===selected))box.value=selected;
  box.disabled=!efforts.length
}
async function loadModels(selected='', effort=''){
  const request=++state.runtimeModelRequest,
  box=$('#model-input'),
  provider=$('#provider-select').value;
  box.innerHTML='<option value="">Loading models…</option>';
  box.disabled=true;
  try{
    const q=new URLSearchParams({
      base_url: $('#base-url-input').value, api_key_env: $('#key-env-input').value
    });
    const out=await api(`/api/providers/${provider}/models?${q}`);
    if(request!==state.runtimeModelRequest)return;
    state.runtimeModels=out.models;
    const models=[...out.models];
    if(selected&&!models.some(m=>m.id===selected))models.unshift({id:selected, displayName:`${selected} (saved)`, hidden:true});
    box.innerHTML=models.map(m=>`<option value="${m.id}">${modelLabel(m)}</option>`).join('')||'<option value="">No models returned</option>';
    if(selected)box.value=selected;
    fillEfforts($('#effort-input'), out.models, box.value, effort)
  }
  catch(err){
    if(request!==state.runtimeModelRequest)return;
    box.innerHTML=`<option value="">${err.message}</option>`;
    $('#effort-input').innerHTML='<option value="">Unavailable</option>'
  }
  finally{
    box.disabled=false
  }
}
async function loadChatControls(){
  const a=activeAgent(),
  projectId=state.project?.id,
  request=++state.chatControlsRequest,
  override=chatOverride(projectId, a.id),
  modelBox=$('#chat-model'),
  effortBox=$('#chat-effort'),
  attach=$('#attach-button');
  modelBox.innerHTML='<option value="">Loading…</option>';
  attach.dataset.enabled='false';
  state.providerCommands=[];
  api(`/api/providers/${a.runtime.provider}/commands`).then(out=>{
    if(a.id!==state.active)return;
    state.providerCommands=out.commands||[];
    renderCommands()
  }).catch(()=>{});
  try{
    const q=new URLSearchParams({
      base_url: a.runtime.base_url, api_key_env: a.runtime.api_key_env
    });
    const [out,
    capabilities]=await Promise.all([api(`/api/providers/${a.runtime.provider}/models?${q}`), api(`/api/agents/${a.id}/attachment-capabilities?project_id=${state.project.id}`)]);
    if(request!==state.chatControlsRequest||a.id!==state.active||state.project?.id!==projectId)return;
    state.chatModels=out.models;
    const models=[...out.models];
    if(override.model&&!models.some(m=>m.id===override.model))models.unshift({id:override.model, displayName:`${override.model} (saved)`, hidden:true});
    modelBox.innerHTML=`<option value="">Agent default (${a.runtime.model||'provider default'})</option>`+models.map(m=>`<option value="${m.id}">${modelLabel(m)}</option>`).join('');
    if(override.model)modelBox.value=override.model;
    fillEfforts(effortBox, out.models, modelBox.value||a.runtime.model, override.effort, 'Agent default');
    attach.dataset.enabled=String(capabilities.enabled);
    attach.title=capabilities.enabled?`Attach files · ${capabilities.note}`: 'Attachments unavailable for this runtime';
    $('#attachment-input').accept=capabilities.accept.join(',')
  }
  catch{
    if(request!==state.chatControlsRequest||a.id!==state.active||state.project?.id!==projectId)return;
    modelBox.innerHTML='<option value="">Agent default</option>';
    effortBox.innerHTML='<option value="">Agent default</option>';
    attach.title='Attachments unavailable'
  }
  finally{
    renderComposer()
  }
}
$('#chat-form').onsubmit=async e=>{
  e.preventDefault();
  const form=e.currentTarget;
  const input=$('#message'),
  text=input.value.trim(),
  role=state.active,
  projectId=state.project.id;
  if(!text)return;
  if(await executeCommand(text)){
    input.value='';
    renderCommands();
    renderComposer();
    return
  }
  console.log("Message received")
  const reply=state.replyTo[role],
  attachments=[...(state.pendingAttachments[role]||[])],
  optimistic={
    kind: 'user',
    text,
    reply_to_id: reply?.id??null,
    attachments,
    created_at: new Date().toISOString()
  };
  (state.messages[role]??=[]).push(optimistic);
  // Detach this turn's uploads before awaiting the API. A second prompt can
  // now be composed while the first is running and must get its own files.
  state.pendingAttachments[role]=[];
  input.value='';
  delete state.replyTo[role];
  const wasBusy=state.busy.has(role);
  state.busy.add(role);
  setChatActivity(role, 'running', wasBusy?`${activeAgent().name} is busy; your prompt is queued for its next turn.`:`${activeAgent().name} is working… The result will appear in this chat.`);
  renderCommands();
  renderMessages();
  renderComposer();
  let submitted=false;
  try{
    console.log("About to post")
    const out=await api(`/api/chat/${role}`, {
      method: 'POST', body: JSON.stringify({
        message: text, model: $('#chat-model').value, reasoning_effort: $('#chat-effort').value, project_id: projectId, reply_to_id: reply?.id||null, attachment_ids: attachments.map(x=>x.id)
      })
    });
    submitted=true;
    await waitForChatRun(out.run.id, role, projectId);
    await loadContext()
    console.log("Posted successfully")
  }
  catch(err){
    if(!submitted){
      const current=state.pendingAttachments[role]||[], ids=new Set(current.map(item=>item.id));
      state.pendingAttachments[role]=[
        ...attachments.filter(item=>!ids.has(item.id)),
        ...current,
      ];
    }
    state.messages[role]=state.messages[role].filter(item=>item!==optimistic);
    state.replyTo[role]=reply;
    if(state.active===role)input.value=text;
    state.messages[role].push({
      kind: 'error', text: `Call failed before a provider result was saved.\n${err.message}`, by: 'Workspace', created_at: new Date().toISOString(), attachments: []
    });
    setChatActivity(role, 'error', 'The call failed. Diagnostic details are shown in the chat.')
  }
  finally{
    if(!state.runs[role]&&!state.runWatchers[role])state.busy.delete(role);
    if(state.active===role){
      renderMessages();
      renderComposer()
    }
  }
};
$('#context-form').onsubmit=async e=>{
  e.preventDefault();
  const id=$('#context-id').value,
  payload={
    title: $('#context-title-input').value,
    content: $('#context-content').value,
    roles: [...document.querySelectorAll('#role-checks input:checked')].map(x=>x.value),
    project_id: state.project.id
  };
  await api(id?`/api/context/${id}`: '/api/context', {
    method: id?'PUT': 'POST', body: JSON.stringify(payload)
  });
  $('#context-dialog').close();
  await loadContext()
};
$('#runtime-form').onsubmit=async e=>{
  e.preventDefault();
  const role=state.active,
  payload={
    provider: $('#provider-select').value,
    model: $('#model-input').value,
    base_url: $('#base-url-input').value,
    api_key_env: $('#key-env-input').value,
    reasoning_effort: $('#effort-input').value,
    context_window_tokens: Number($('#context-window-input').value),
    context_compaction_threshold: Number($('#context-compaction-input').value),
    project_id: state.project.id
  };
  await api(`/api/agents/${role}/runtime`, {
    method: 'PUT', body: JSON.stringify(payload)
  });
  state.agents=await api(`/api/agents?project_id=${state.project.id}`);
  state.active=role;
  $('#runtime-dialog').close();
  selectAgent(role);
  renderFlowchart()
};
$('#agent-form').onsubmit=async e=>{
  e.preventDefault();
  const payload={
    name: $('#agent-name-input').value,
    role: $('#agent-role-input').value,
    brief: $('#agent-brief-input').value,
    instructions: $('#agent-instructions-input').value,
    project_id: state.project.id,
    git_enabled: $('#agent-git-enabled').checked
  };
  try{
    if(payload.git_enabled)await prepareGitAgent(payload);
    else await createAgent(payload)
  }
  catch(err){alert(err.message)}
};
$('#generate-agent').onclick=async e=>{
  const button=e.currentTarget;
  const prompt=$('#agent-prompt').value.trim();
  if(!prompt)return alert('Describe the role you want first.');
  button.classList.add('loading');
  try{
    const draft=await api('/api/agents/generate', {
      method: 'POST', body: JSON.stringify({
        prompt
      })
    });
    $('#agent-name-input').value=draft.name;
    $('#agent-role-input').value=draft.role;
    $('#agent-brief-input').value=draft.brief;
    $('#agent-instructions-input').value=draft.instructions
  }
  catch(err){
    alert(err.message)
  }
  finally{
    button.classList.remove('loading')
  }
};
async function clearHistory(){
  await api(`/api/agents/${state.active}/history?project_id=${state.project.id}`, {
    method: 'DELETE'
  });
  state.messages[state.active]=[];
  state.pendingAttachments[state.active]=[];
  delete state.replyTo[state.active];
  renderMessages();
  renderComposer()
}
$('#clear-history').onclick=async()=>{
  if(confirm(`Clear ${activeAgent().name}'s saved transcript?`)){
    await clearHistory();
    $('#runtime-dialog').close()
  }
};
$('#delete-context').onclick=async()=>{
  const id=$('#context-id').value;
  if(id&&confirm('Delete this shared context item?')){
    await api(`/api/context/${id}?project_id=${state.project.id}`, {
      method: 'DELETE'
    });
    $('#context-dialog').close();
    await loadContext()
  }
};
function openAgentDialog(){
  $('#agent-form').reset();
  state.pendingGitAgent=null;
  $('#agent-dialog').showModal()
}
function selectedSkillRoles(){
  return [...document.querySelectorAll('#skill-agent-checks input:checked')].map(input=>input.value)
}
function selectedToolsetRoles(){
  return [...document.querySelectorAll('#toolset-agent-checks input:checked')].map(input=>input.value)
}
$('#new-context').onclick=()=>openContext();
$('#new-agent').onclick=openAgentDialog;
$('#new-chat').onclick=async()=>{
  if(!activeAgent()||!state.messages[state.active]?.length)return $('#message').focus();
  if(confirm(`Start a new chat with ${activeAgent().name}? This clears the saved transcript.`))await clearHistory()
};
$('#agent-search').oninput=e=>{state.agentSearch=e.currentTarget.value;renderAgents()};
$('#history-search').oninput=e=>{state.historySearch=e.currentTarget.value;renderMessages()};
$('#jump-to-latest').onclick=()=>scrollMessagesToLatest(true);
$('#add-agent-dashboard').onclick=openAgentDialog;
$('#close-agent').onclick=()=>$('#agent-dialog').close();
$('#workflow-button').onclick=()=>openWorkflowDialog().catch(err=>alert(err.message));
$('#close-workflow').onclick=()=>$('#workflow-dialog').close();
function filterSettingsMenu(value=''){
  const query=String(value).trim().toLowerCase(), sections=[...document.querySelectorAll('#settings-menu-panel [data-settings-section]')];
  let visible=0;
  sections.forEach(section=>{
    const matches=!query||section.dataset.settingsSearch.includes(query);
    section.classList.toggle('hidden',!matches);
    if(matches)visible++
  });
  $('#settings-menu-empty')?.classList.toggle('hidden',visible>0)
}
function setSettingsMenuOpen(open){
  const panel=$('#settings-menu-panel'), trigger=$('#settings-button');
  if(!panel||!trigger)return;
  panel.classList.toggle('hidden',!open);
  trigger.setAttribute('aria-expanded',String(open));
  if(open){
    filterSettingsMenu($('#settings-search')?.value||'');
    requestAnimationFrame(()=>$('#settings-search')?.focus())
  }else{
    const search=$('#settings-search');
    if(search){search.value='';filterSettingsMenu('')}
  }
}
$('#settings-button').onclick=()=>{
  const panel=$('#settings-menu-panel');
  setSettingsMenuOpen(panel?.classList.contains('hidden'))
};
$('#settings-search').oninput=event=>filterSettingsMenu(event.currentTarget.value);
$('#permissions-button').onclick=()=>{
  setSettingsMenuOpen(false);
  openPermissionsDialog().catch(err=>alert(err.message))
};
$('#close-permissions').onclick=()=>$('#permissions-dialog').close();
$('#memory-button').onclick=()=>openWorkflowMemories().catch(err=>alert(err.message));
$('#close-memory').onclick=()=>$('#memory-dialog').close();
$('#workflow-memory-select').onchange=event=>{
  const memory=state.workflowMemories.find(item=>item.id===Number(event.currentTarget.value));
  fillWorkflowMemory(memory||null)
};
$('#restart-codex-session').onclick=async()=>{
  const agent=activeAgent();
  if(!agent||agent.runtime.provider!=='codex')return;
  if(!confirm(`Restart ${agent.name}'s Codex session? The saved Codex thread will be removed, but this app's conversation history stays visible.`))return;
  try{
    await api(`/api/agents/${agent.id}/session/restart?project_id=${state.project.id}`, {method:'POST'});
    alert('Codex session restarted. The next message starts a fresh session with the current permissions.')
  }
  catch(err){
    if(err.status===404){
      alert('The running app server is an older version and does not have session restart yet. Stop and start the app once, then try again.');
      return
    }
    alert(err.message)
  }
};
$('#new-workflow-memory').onclick=()=>fillWorkflowMemory();
$('#activate-workflow-memory').onclick=async()=>{
  const memoryId=Number($('#workflow-memory-select').value)||0;
  try{
    const data=await api(`/api/projects/${state.project.id}/active-workflow-memory`, {method:'PUT',body:JSON.stringify({memory_id:memoryId})});
    state.activeWorkflowMemoryId=Number(data.active_memory_id)||0;
    state.project.active_workflow_memory_id=state.activeWorkflowMemoryId;
    renderWorkflowMemories()
  }
  catch(err){alert(err.message)}
};
$('#workflow-memory-form').onsubmit=async event=>{
  event.preventDefault();
  const id=$('#workflow-memory-id').value,
  payload={name:$('#workflow-memory-name').value,content:$('#workflow-memory-content').value};
  try{
    const saved=await api(id?`/api/projects/${state.project.id}/workflow-memories/${id}`:`/api/projects/${state.project.id}/workflow-memories`, {method:id?'PUT':'POST',body:JSON.stringify(payload)});
    await loadWorkflowMemories(); fillWorkflowMemory(saved); renderWorkflowMemories()
  }
  catch(err){alert(err.message)}
};
$('#delete-workflow-memory').onclick=async()=>{
  const id=$('#workflow-memory-id').value, memory=state.workflowMemories.find(item=>item.id===Number(id));
  if(!id||!confirm(`Delete workflow memory “${memory?.name||''}”?`))return;
  try{await api(`/api/projects/${state.project.id}/workflow-memories/${id}`,{method:'DELETE'});await loadWorkflowMemories();fillWorkflowMemory();renderWorkflowMemories()}
  catch(err){alert(err.message)}
};
$('#auto-approve-agent-actions').onchange=async event=>{
  const enabled=event.currentTarget.checked;
  if(enabled&&!confirm('Allow every agent to run commands and edit files inside this project without permission prompts? This does not grant access outside the project folder.')){
    event.currentTarget.checked=false;
    return
  }
  try{
    const project=await api(`/api/projects/${state.project.id}/action-policy`, {
      method:'PUT',body:JSON.stringify({auto_approve_agent_actions:enabled,allow_full_system_access:Boolean(state.actionPermissions?.allow_full_system_access)})
    });
    state.project=project;
    state.projects=state.projects.map(item=>item.id===project.id?project:item);
    if(state.actionPermissions){
      state.actionPermissions.auto_approve_agent_actions=enabled;
      state.actionPermissions.allow_full_system_access=Boolean(project.allow_full_system_access)
    }
    renderPermissionsDialog(); renderAgents()
  }
  catch(err){event.currentTarget.checked=!enabled;alert(err.message)}
};
$('#allow-full-system-access').onchange=async event=>{
  const enabled=event.currentTarget.checked;
  if(enabled&&!confirm('Give every agent unrestricted access to this computer? They will be able to read, write, and run commands outside this project folder without confirmation.')){
    event.currentTarget.checked=false;
    return
  }
  try{
    const project=await api(`/api/projects/${state.project.id}/action-policy`, {
      method:'PUT',body:JSON.stringify({auto_approve_agent_actions:Boolean(state.actionPermissions?.auto_approve_agent_actions),allow_full_system_access:enabled})
    });
    state.project=project;
    state.projects=state.projects.map(item=>item.id===project.id?project:item);
    if(state.actionPermissions)state.actionPermissions.allow_full_system_access=enabled;
    renderPermissionsDialog(); renderAgents()
  }
  catch(err){event.currentTarget.checked=!enabled;alert(err.message)}
};
$('#external-access-button').onclick=async()=>{
  const agent=activeAgent();
  if(!agent||agent.runtime?.provider!=='codex'||state.project?.allow_full_system_access)return;
  const enabled=!Boolean(agent.permissions?.allow_full_system_access);
  const action=enabled?'Give':'Remove';
  const detail=enabled
    ? `${agent.name} will be able to read, write, and run commands outside this project folder without confirmation.`
    : `${agent.name} will return to its project-only access level.`;
  if(!confirm(`${action} external system access for ${agent.name}?\n\n${detail}`))return;
  try{
    const saved=await api(`/api/projects/${state.project.id}/agents/${agent.id}/action-permissions`, {
      method:'PUT',body:JSON.stringify({
        allow_commands:Boolean(agent.permissions?.allow_commands),
        allow_file_edits:Boolean(agent.permissions?.allow_file_edits),
        allow_full_system_access:enabled
      })
    });
    updateAgentPermissions(agent.id,saved);
    renderExternalAccessButton();
    renderPermissionsDialog();
    alert(enabled?`${agent.name} now has external system access.`:`${agent.name}'s external system access was removed.`)
  }
  catch(err){alert(err.message)}
};
$('#theme-toggle').onclick=()=>{
  applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');
  setSettingsMenuOpen(false)
};
document.addEventListener('pointerdown',event=>{
  const menu=$('#settings-menu');
  if(menu&&!menu.contains(event.target))setSettingsMenuOpen(false)
});
document.addEventListener('keydown', event=>{
  if(event.key==='Escape'&&!$('#settings-menu-panel')?.classList.contains('hidden')){
    setSettingsMenuOpen(false);
    $('#settings-button')?.focus();
    return
  }
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){
    event.preventDefault();
    $('#agent-search')?.focus()
  }
});
document.querySelectorAll('.relationship-toolbar [data-relationship-mode]').forEach(button=>button.onclick=()=>setRelationshipMode(button.dataset.relationshipMode));
$('#workflow-template-select').onchange=renderWorkflowDialog;
$('#relationship-enforcement').onchange=async event=>{
  const enabled=event.currentTarget.checked;
  try{
    const project=await api(`/api/projects/${state.project.id}/relationship-policy`, {
      method:'PUT', body:JSON.stringify({enforce_relationships:enabled})
    });
    state.project=project;
    state.projects=state.projects.map(item=>item.id===project.id?project:item);
    renderWorkflowDialog()
  }
  catch(err){
    event.currentTarget.checked=!enabled;
    alert(err.message)
  }
};
$('#workflow-template-save-form').onsubmit=async event=>{
  event.preventDefault();
  const name=$('#workflow-template-name').value.trim();
  if(!name)return;
  try{
    clearTimeout(layoutTimer); clearTimeout(edgeTimer);
    await Promise.all([
      api(`/api/projects/${state.project.id}/layout`, {method:'PUT',body:JSON.stringify({items:state.layout})}),
      api(`/api/projects/${state.project.id}/edges`, {method:'PUT',body:JSON.stringify({edges:state.edges})}),
    ]);
    await api(`/api/projects/${state.project.id}/workflow-templates`, {method:'POST',body:JSON.stringify({name})});
    $('#workflow-template-name').value='';
    await loadWorkflowTemplates(); renderWorkflowDialog()
  }
  catch(err){alert(err.message)}
};
document.querySelector('.workflow-template-apply button[type="button"]')?.addEventListener('click', async()=>{
  const id=$('#workflow-template-select').value;
  if(!id)return alert('Choose a saved workflow first.');
  const template=state.workflowTemplates.find(item=>item.id===Number(id));
  if(!confirm(`Apply “${template?.name||'this workflow'}”? This replaces the current command/report relationships and repositions matching agents.`))return;
  try{
    const applied=await api(`/api/projects/${state.project.id}/workflow-templates/${id}/apply`, {method:'POST'});
    state.layout=applied.layout; state.edges=applied.edges;
    renderFlowchart(); renderWorkflowDialog();
    if(applied.skipped_roles.length)alert(`Applied the workflow. No matching agent was found for: ${applied.skipped_roles.join(', ')}.`)
  }
  catch(err){alert(err.message)}
});
$('#workflow-template-delete').onclick=async()=>{
  const id=$('#workflow-template-select').value, template=state.workflowTemplates.find(item=>item.id===Number(id));
  if(!id||!confirm(`Delete saved workflow “${template?.name||''}”?`))return;
  try{await api(`/api/projects/${state.project.id}/workflow-templates/${id}`, {method:'DELETE'}); await loadWorkflowTemplates(); renderWorkflowDialog()}
  catch(err){alert(err.message)}
};
const relationshipKind=$('#relationship-kind');
if(relationshipKind){
  if(!relationshipKind.querySelector('option[value="supervisor"]'))relationshipKind.insertAdjacentHTML('beforeend','<option value="supervisor">Supervisor -> Employee</option><option value="bidirectional">Interconnected (both ways)</option>');
  const relationshipAddHelp=document.createElement('small');
  relationshipAddHelp.id='relationship-add-help';
  relationshipAddHelp.className='workflow-detail';
  relationshipKind.closest('form')?.after(relationshipAddHelp);
  const updateRelationshipAddHelp=()=>relationshipAddHelp.textContent=relationshipKind.value==='supervisor'
    ? 'Supervisor -> Employee: choose the supervisor first and the employee second.'
    : relationshipKind.value==='bidirectional'
      ? 'Interconnected: either agent may be selected first; both agents will command and report to each other.'
      : relationshipKind.value==='command'
        ? 'Commands: choose the commanding agent first.'
        : 'Reports to: choose the reporting agent first.';
  relationshipKind.onchange=updateRelationshipAddHelp;
  updateRelationshipAddHelp()
}
$('#relationship-add-form').onsubmit=event=>{
  event.preventDefault();
  const sourceRole=$('#relationship-source').value, targetRole=$('#relationship-target').value, relationship=$('#relationship-kind').value;
  if(sourceRole===targetRole)return alert('Choose two different agents.');
  if(relationship==='supervisor'||relationship==='bidirectional'){
    if(!addRelationship(sourceRole,targetRole,relationship))return alert('That relationship already exists.');
    renderWorkflowDialog(); renderFlowchart(); return
  }
  const edge={source_role:sourceRole,target_role:targetRole,relationship};
  if(state.edges.some(item=>item.source_role===edge.source_role&&item.target_role===edge.target_role&&item.relationship===edge.relationship))return alert('That relationship already exists.');
  state.edges.push(edge); saveEdges(); renderWorkflowDialog(); renderFlowchart()
};
$('#close-dialog').onclick=()=>$('#context-dialog').close();
$('#close-code-terminal').onclick=()=>$('#code-terminal-dialog').close();
$('#tools-button').onclick=()=>openToolsDialog().catch(err=>alert(err.message));
$('#close-tools').onclick=()=>$('#tools-dialog').close();
$('#version-control-configure').onclick=async()=>{
  try{
    state.pendingGitAgent=null;
    state.pendingGitEnableAgent=null;
    openGitSetup(await loadGitStatus(), null)
  }
  catch(err){alert(err.message)}
};
$('#version-consolidate-branches').onclick=()=>runVersionConsolidation();
$('#version-graph-zoom-out').onclick=()=>zoomVersionGraph(1.15);
$('#version-graph-zoom-in').onclick=()=>zoomVersionGraph(0.87);
$('#version-graph-reset').onclick=resetVersionGraphView;
$('#version-commit-target-branch').onchange=event=>{state.versionCommitTargetBranch=event.target.value};
$('#version-rebase-commit').onclick=()=>runVersionCommitAction('rebase');
$('#version-merge-commit').onclick=()=>runVersionCommitAction('merge');
$('#version-merge-all-branches').onclick=()=>runVersionCommitAction('merge-all');
$('#version-revert-commit').onclick=()=>runVersionCommitAction('revert');
$('#version-control-agent-git-button').onclick=()=>{
  const panel=$('#version-agent-git-submenu'), button=$('#version-control-agent-git-button'), open=panel.classList.contains('hidden');
  panel.classList.toggle('hidden',!open); panel.setAttribute('aria-hidden',String(!open)); button.setAttribute('aria-expanded',String(open));
  if(open){$('#version-agent-search').focus();panel.scrollIntoView({behavior:'smooth',block:'start'})}
};
$('#close-version-agent-git').onclick=()=>{
  $('#version-agent-git-submenu').classList.add('hidden');
  $('#version-agent-git-submenu').setAttribute('aria-hidden','true');
  $('#version-control-agent-git-button').setAttribute('aria-expanded','false');
  $('#version-control-agent-git-button').focus()
};
$('#version-agent-search').oninput=event=>{state.versionAgentSearch=event.target.value;renderVersionAgentList(state.gitOverview)};
$('#git-changes-button').onclick=()=>openGitChanges().catch(err=>alert(err.message));
$('#close-git-changes').onclick=()=>$('#git-changes-dialog').close();
$('#configure-git').onclick=async()=>{
  try{
    state.pendingGitAgent=null;
    state.pendingGitEnableAgent=null;
    openGitSetup(await loadGitStatus(), null)
  }
  catch(err){alert(err.message)}
};
$('#close-git-setup').onclick=()=>{
  state.pendingGitAgent=null;
  state.pendingGitEnableAgent=null;
  $('#git-setup-dialog').close()
};
$('#git-setup-form').onsubmit=async e=>{
  e.preventDefault();
  const submit=$('#git-setup-submit'), statusBox=$('#git-setup-status');
  if(submit.disabled)return;
  const pending=state.pendingGitAgent;
  const pendingEnable=state.pendingGitEnableAgent;
  submit.disabled=true;
  submit.classList.add('loading');
  submit.textContent='Saving…';
  submit.setAttribute('aria-busy','true');
  statusBox.classList.remove('git-setup-error');
  try{
    state.gitStatus=await api(`/api/projects/${state.project.id}/git`, {
      method:'PUT',
      body:JSON.stringify({
        main_branch:$('#git-branch-input').value,
        initialize:$('#git-initialize-input').checked,
        remote:$('#git-remote-name-input').value,
        remote_url:$('#git-remote-url-input').value
      })
    });
    if(pending)await createAgent(pending);
    else if(pendingEnable)await saveExistingAgentGitEnabled(pendingEnable, true);
    if(!$('#version-control').classList.contains('hidden'))await loadVersionControl()
    $('#git-setup-dialog').close();
    state.pendingGitAgent=null;
    state.pendingGitEnableAgent=null
  }
  catch(err){
    statusBox.textContent=`Could not configure Git: ${err.message}`;
    statusBox.classList.add('git-setup-error')
  }
  finally{
    submit.disabled=false;
    submit.classList.remove('loading');
    submit.textContent='Use Git workflow';
    submit.removeAttribute('aria-busy')
  }
};
$('#version-branch-form').onsubmit=async e=>{
  e.preventDefault();
  try{
    await api(`/api/projects/${state.project.id}/git/branches`, {
      method:'POST', body:JSON.stringify({name:$('#version-branch-name').value,source:$('#version-branch-source').value})
    });
    $('#version-branch-name').value='';
    await loadVersionControl()
  }
  catch(err){alert(err.message)}
};
$('#new-toolset').onclick=()=>fillToolsetEditor(null);
$('#toolset-search').oninput=e=>{state.toolsetSearch=e.target.value;renderToolsetsList()};
$('#add-tool-definition').onclick=()=>addToolDefinition();
$('#generate-toolset').onclick=async e=>{
  const prompt=$('#toolset-prompt').value.trim(), button=e.currentTarget;
  if(!prompt)return alert('Describe the command-line toolset you want Codex to create.');
  button.classList.add('loading');
  try{
    const draft=await api('/api/toolsets/generate', {method:'POST', body:JSON.stringify({prompt})});
    fillToolsetEditor(draft)
  }
  catch(err){alert(err.message)}
  finally{button.classList.remove('loading')}
};
$('#toolset-form').onsubmit=async e=>{
  e.preventDefault();
  const definitions=[...document.querySelectorAll('.tool-definition-card')].map(card=>({
    name:card.querySelector('.tool-name').value,
    filename:card.querySelector('.tool-filename').value,
    description:card.querySelector('.tool-description').value,
    inputs:card.querySelector('.tool-inputs').value,
    outputs:card.querySelector('.tool-outputs').value,
    output_format:card.querySelector('.tool-output-format').value,
    env_vars:card.querySelector('.tool-env-vars').value.split(',').map(value=>value.trim()).filter(Boolean),
    result_template:card.querySelector('.tool-result-template').value||'{stdout}',
    source:card.querySelector('.tool-source').value
  }));
  if(!definitions.length)return alert('Add at least one tool to this toolset.');
  const existing=$('#toolset-existing-slug').value,
  payload={
    name:$('#toolset-name').value,
    slug:$('#toolset-slug').value,
    description:$('#toolset-description').value,
    details:$('#toolset-details').value,
    tools:definitions,
    roles:selectedToolsetRoles(),
    project_id:state.project.id
  },
  submit=e.submitter||$('#toolset-form button[type="submit"]');
  submit.disabled=true;
  try{
    const saved=await api(existing?`/api/toolsets/${encodeURIComponent(existing)}`:'/api/toolsets', {
      method:existing?'PUT':'POST', body:JSON.stringify(payload)
    });
    await loadToolsets();
    await selectToolset(saved.slug)
  }
  catch(err){alert(err.message)}
  finally{submit.disabled=false}
};
$('#toolset-delete').onclick=async()=>{
  const slug=$('#toolset-existing-slug').value, toolset=toolsetBySlug(slug);
  if(!slug||!toolset||!confirm(`Delete toolset “${toolset.name}” and its files from .agents/tools/${slug}?`))return;
  try{
    await api(`/api/toolsets/${encodeURIComponent(slug)}?project_id=${state.project.id}`, {method:'DELETE'});
    state.activeToolsetSlug=null;
    await loadToolsets();
    await selectToolset(state.toolsets[0]?.slug||null)
  }
  catch(err){alert(err.message)}
};
$('#skills-button').onclick=()=>openSkillsDialog();
$('#close-skills').onclick=()=>$('#skills-dialog').close();
$('#open-skill-marketplace').onclick=()=>{
  $('#skills-marketplace-dialog').showModal();
  if(!state.marketplace.length)renderMarketplace()
};
$('#close-skill-marketplace').onclick=()=>$('#skills-marketplace-dialog').close();
$('#marketplace-search-button').onclick=searchMarketplace;
$('#marketplace-query').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();searchMarketplace()}};
$('#skill-library-search').oninput=e=>{state.skillSearch=e.target.value;renderSkillsList()};
$('#skill-library-sort').onchange=e=>{state.skillSort=e.target.value;renderSkillsList()};
$('#new-skill').onclick=()=>selectSkill(null);
$('#skill-form').onsubmit=async e=>{
  e.preventDefault();
  let requiredSecrets;
  try{
    requiredSecrets=JSON.parse($('#skill-required-secrets').value||'[]');
    if(!Array.isArray(requiredSecrets))throw new Error('Required secrets must be a JSON array.');
  }
  catch(err){alert(`Required secrets must be a valid JSON array. ${err.message}`);return}
  const id=$('#skill-id').value,
  payload={
    name:$('#skill-name').value,
    slug:$('#skill-slug').value,
    version:$('#skill-version').value||'1.0.0',
    summary:$('#skill-summary').value,
    body:$('#skill-body').value,
    compatibility:$('#skill-compatibility').value,
    license:$('#skill-license').value,
    allowed_tools:$('#skill-allowed-tools').value,
    required_secrets:requiredSecrets,
    created_by:'human'
  },
  submit=e.submitter||$('#skill-form button[type="submit"]');
  submit.disabled=true;
  try{
    const saved=await api(id?`/api/skills/${id}`:'/api/skills', {method:id?'PUT':'POST', body:JSON.stringify(payload)});
    await api(`/api/skills/${saved.id}/assignments`, {method:'PUT', body:JSON.stringify({project_id:state.project.id, roles:selectedSkillRoles()})});
    await loadSkills();
    selectSkill(saved.id)
  }
  catch(err){alert(err.message)}
  finally{submit.disabled=false}
};
$('#generate-skill').onclick=async e=>{
  const prompt=$('#skill-prompt').value.trim(), button=e.currentTarget;
  if(!prompt)return alert('Describe the reusable skill first.');
  button.classList.add('loading');
  try{
    const draft=await api('/api/skills/generate', {method:'POST', body:JSON.stringify({prompt})});
    fillSkillEditor(draft);
    state.activeSkillId=null;
    renderSkillsList()
  }
  catch(err){alert(err.message)}
  finally{button.classList.remove('loading')}
};
$('#skill-delete').onclick=async()=>{
  const id=$('#skill-id').value, skill=skillById(id);
  if(!id||!skill||!confirm(`Delete the reusable skill “${skill.name}”? This removes its assignments from every workspace.`))return;
  try{
    await api(`/api/skills/${id}`, {method:'DELETE'});
    await loadSkills();
    selectSkill(state.skills[0]?.id||null)
  }
  catch(err){alert(err.message)}
};
$('#runtime-settings').onclick=openRuntime;
$('#close-runtime').onclick=()=>$('#runtime-dialog').close();
$('#load-models').onclick=()=>loadModels($('#model-input').value);
$('#provider-select').onchange=()=>{
  $('#key-env-input').value='';
  updateRuntimeFields();
  loadModels()
};
$('#model-input').onchange=()=>fillEfforts($('#effort-input'), state.runtimeModels, $('#model-input').value);
$('#chat-model').onchange=()=>{
  const model=$('#chat-model').value,
  projectId=state.project?.id,
  role=state.active;
  saveChatOverride(projectId, role, model, '');
  fillEfforts($('#chat-effort'), state.chatModels, model||activeAgent().runtime.model, '', 'Agent default');
};
$('#chat-effort').onchange=()=>{
  const projectId=state.project?.id,
  role=state.active,
  model=$('#chat-model').value;
  saveChatOverride(projectId, role, model, $('#chat-effort').value);
};
$('#agent-template-select').onchange=e=>{
  const template=state.templates.find(t=>t.id===Number(e.target.value));
  if(!template)return;
  $('#agent-name-input').value=template.agent_name||template.name.replace(/\s+template$/i, '');
  $('#agent-role-input').value='';
  $('#agent-brief-input').value=template.brief;
  $('#agent-instructions-input').value=template.instructions
};
document.querySelectorAll('#agent-context-menu [data-agent-action]').forEach(button=>button.onclick=async e=>{
  e.stopPropagation();
  const agent=state.contextAgent, action=button.dataset.agentAction;
  if(!agent)return;
  closeAgentContextMenu();
  try{
    if(action==='runtime'){
      state.active=agent.id;
      await openRuntime()
    }
    else if(action==='git')await toggleExistingAgentGit(agent)
    else if(action==='template'){
      const name=prompt('Template name', `${agent.name} template`);
      if(name!==null){
        await api(`/api/agents/${agent.id}/template?project_id=${state.project.id}`, {
          method: 'POST', body: JSON.stringify({
            name,
            project_id: state.project.id
          })
        });
        await loadTemplates()
      }
    }
    else if(action==='delete')await deleteAgentFromMenu(agent)
  }
  catch(err){
    alert(err.message)
  }
});
document.addEventListener('pointerdown', e=>{
  if(!e.target.closest('#agent-context-menu'))closeAgentContextMenu()
});
window.addEventListener('blur', closeAgentContextMenu);
$('#new-project').onclick=()=>{
  $('#project-form').reset();
  $('#project-dialog').showModal()
};
$('#close-project').onclick=()=>$('#project-dialog').close();
$('#project-form').onsubmit=async e=>{
  e.preventDefault();
  const project=await api('/api/projects', {
    method: 'POST', body: JSON.stringify({
      name: $('#project-name-input').value, root_path: $('#project-path-input').value, description: $('#project-description-input').value
    })
  });
  $('#project-dialog').close();
  state.projects=await api('/api/projects');
  await selectProject(project.id)
};
$('#open-workspace').onclick=()=>showWorkspace();
$('#home-button').onclick=showDashboard;
async function loadContext(){
  if(!state.project){
    state.context=[];
    renderContext();
    return
  }
  state.context=await api(`/api/context?project_id=${state.project.id}`);
  renderContext()
}
async function loadConnections(){
  const data=await api('/api/connections');
  for(const id of ['codex', 'openai', 'google', 'anthropic', 'compatible']){
    const el=$(`#${id}-connection`),
    connected=data[id].connected;
    el.textContent=connected?'Connected': 'Not connected';
    el.className=`connection-state ${connected?'connected':''}`
  }
  const output=data.codex.login_output||data.codex.detail||'';
  $('#codex-login-output').innerHTML=renderAnsiTerminal(output);
  $('#codex-login-output').style.display=output?'block': 'none';
  return data
}
async function refreshProviders(){
  state.providers=await api('/api/providers');
  renderAgents();
  renderFlowchart()
}
$('#connections-button').onclick=async()=>{
  setSettingsMenuOpen(false);
  $('#connections-dialog').showModal();
  await loadConnections()
};
$('#close-connections').onclick=()=>$('#connections-dialog').close();
$('#connect-codex').onclick=async e=>{
  const button=e.currentTarget;
  button.classList.add('loading');
  try{
    await api('/api/connections/codex', {
      method: 'POST'
    });
    for(let i=0;
    i<30&&$('#connections-dialog').open;
    i++){
      const status=await loadConnections();
      if(status.codex.connected)break;
      await new Promise(r=>setTimeout(r, 2000))
    }
    await refreshProviders()
  }
  catch(err){
    alert(err.message)
  }
  finally{
    button.classList.remove('loading')
  }
};
$('#disconnect-codex').onclick=async()=>{
  if(confirm('Disconnect the local Codex CLI account?')){
    await api('/api/connections/codex', {
      method: 'DELETE'
    });
    await loadConnections();
    await refreshProviders()
  }
};
document.querySelectorAll('[data-connect]').forEach(button=>button.onclick=async()=>{
  const provider=button.dataset.connect, input=$(`#${provider}-credential`), credential=input.value.trim();
  if(!credential)return alert('Paste the provider credential first.');
  button.classList.add('loading');
  try{
    await api(`/api/connections/${provider}`, {
      method: 'PUT', body: JSON.stringify({
        credential
      })
    });
    input.value='';
    await loadConnections();
    await refreshProviders()
  }
  catch(err){
    alert(err.message)
  }
  finally{
    button.classList.remove('loading')
  }
});
document.querySelectorAll('[data-disconnect]').forEach(button=>button.onclick=async()=>{
  const provider=button.dataset.disconnect;
  if(confirm(`Remove the saved ${provider} credential?`)){
    await api(`/api/connections/${provider}`, {
      method: 'DELETE'
    });
    await loadConnections();
    await refreshProviders()
  }
});
async function init(){
  try{
    [state.providers,
    state.projects,
    state.templates]=await Promise.all([api('/api/providers'), api('/api/projects'), api('/api/agent-templates')]);
    renderProjects();
    await loadTemplates();
    const stored=Number(localStorage.getItem('multiagent-project'));
    await selectProject(state.projects.some(p=>p.id===stored)?stored: state.projects[0].id);
    const h=await api(`/health?project_id=${state.project.id}`);
    $('#status').textContent=`${h.agents} agents · MCP online`
  }
  catch(err){
    $('#status').textContent=err.message;
    $('#status').classList.add('error');
  }
}
function setupChatEnhancements(){
  const heading=document.querySelector('.agent-heading'), form=$('#chat-form');
  if($('#history-search')){
    const input=$('#history-search');
    input.oninput=()=>{state.historySearch=input.value;renderMessages()};
    if(!$('#clear-history-search')){
      const clear=document.createElement('button'); clear.type='button'; clear.id='clear-history-search'; clear.className='composer-action'; clear.textContent='Clear'; clear.title='Clear search';
      clear.onclick=()=>{input.value='';state.historySearch='';renderMessages();input.focus()}; input.parentElement.append(clear)
    }
  }else if(heading){
    const tools=document.createElement('div'); tools.className='chat-tools';
    tools.innerHTML='<label class="history-search"><span>Search chat</span><input id="history-search" type="search" placeholder="Search this transcript" autocomplete="off"><button id="clear-history-search" type="button" title="Clear search" aria-label="Clear search">×</button></label><span id="history-count" class="history-count"></span>';
    heading.insertBefore(tools, heading.querySelector('#external-access-button'));
    const input=tools.querySelector('input');
    input.oninput=()=>{state.historySearch=input.value;renderMessages()};
    tools.querySelector('button').onclick=()=>{input.value='';state.historySearch='';renderMessages();input.focus()};
  }
  if(form&&!$('#clear-composer')){
    const send=$('#send-button'), actions=document.createElement('div'); actions.className='composer-actions';
    const clear=document.createElement('button'); clear.type='button'; clear.id='clear-composer'; clear.className='composer-action'; clear.textContent='Clear'; clear.title='Clear draft';
    clear.onclick=()=>{$('#message').value='';state.replyTo[state.active]=null;renderCommands();renderComposer();$('#message').focus()};
    form.insertBefore(actions,send); actions.append(clear,send);
  }
}
applyTheme(preferredTheme());
setupChatEnhancements();
init();
setInterval(()=>{
  if(state.project&&state.active&&!state.busy.has(state.active)&&!$('#workspace').classList.contains('hidden'))loadHistory(state.active);
  refreshVersionControlIfVisible();
  monitorAgentRuns().catch(err=>console.warn('Could not monitor agent runs', err))
}, 1500);

const COMMAND_HISTORY_KEY='multiagent-provider-command-history';
const CHAT_OVERRIDES_KEY='multiagent-chat-overrides';
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
  pendingGitAgent: null,
  pendingGitEnableAgent: null,
  marketplace: [],
  project: null,
  layout: [],
  edges: [],
  templates: [],
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
  commandIndex: 0,
  drawingLink: null,
  runPollPromise: null,
  runWatchers: {},
  chatOverrides: loadChatOverrides(),
  runtimeModelRequest: 0,
  chatControlsRequest: 0
};
const $=s=>document.querySelector(s);
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
    }
    catch{}throw new Error(errorText(d?.detail, `Request failed (${r.status})`))
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
  $('#dashboard').classList.remove('hidden');
  renderFlowchart()
}
function showWorkspace(role=state.active){
  $('#dashboard').classList.add('hidden');
  $('#workspace').classList.remove('hidden');
  selectAgent(role)
}
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
  $('#git-setup-status').textContent=noRepository
    ? 'This project folder is not a Git repository. Choose its main branch and explicitly confirm initialization.'
    : `Repository: ${status.repository}\nCurrent branch: ${status.current_branch||'(detached HEAD)'}. Git-enabled agents work on role-named branches and merge into the selected main branch.`;
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
function renderFlowchart(){
  if(!state.project)return;
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
    state.edges.filter(edge=>edge.source_role===item.role).forEach(edge=>{
      const target=state.agents.find(a=>a.id===edge.target_role);
      const chip=document.createElement('span');
      chip.className=`relationship-chip ${edge.relationship}`;
      chip.textContent=`${edge.relationship==='command'?'→':'⇢'} ${target?.name||edge.target_role}`;
      const remove=document.createElement('button');
      remove.type='button';
      remove.textContent='×';
      remove.title='Remove relationship';
      remove.onclick=e=>{
        e.stopPropagation();
        state.edges=state.edges.filter(candidate=>candidate!==edge);
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
    if(e.target.closest('.relationship-control'))return;
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
    node.onpointerup=()=>{
      node.classList.remove('dragging');
      node.onpointermove=null;
      node.onpointerup=null;
      saveLayout()
    }
  }
}
function enableLinkDrawing(handle, item, relationship){
  handle.onpointerdown=e=>{
    e.preventDefault();
    e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    const chart=$('#flowchart'),
    rect=chart.getBoundingClientRect();
    state.drawingLink={
      role: item.role,
      relationship,
      x: e.clientX-rect.left+chart.scrollLeft,
      y: e.clientY-rect.top+chart.scrollTop
    };
    handle.classList.add('active');
    drawLines();
    handle.onpointermove=move=>{
      state.drawingLink.x=move.clientX-rect.left+chart.scrollLeft;
      state.drawingLink.y=move.clientY-rect.top+chart.scrollTop;
      drawLines()
    };
    handle.onpointerup=up=>{
      const target=document.elementFromPoint(up.clientX, up.clientY)?.closest('.flow-node');
      if(target&&target.dataset.role!==item.role){
        const edge={
          source_role: item.role,
          target_role: target.dataset.role,
          relationship
        };
        if(!state.edges.some(e=>e.source_role===edge.source_role&&e.target_role===edge.target_role&&e.relationship===edge.relationship)){
          state.edges.push(edge);
          saveEdges()
        }
        renderFlowchart()
      }
      state.drawingLink=null;
      handle.classList.remove('active');
      handle.onpointermove=null;
      handle.onpointerup=null;
      drawLines()
    }
  }
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
function drawLines(){
  const svg=$('#flow-lines');
  if(!svg)return;
  const chart=$('#flowchart');
  svg.setAttribute('width', Math.max(chart.clientWidth, chart.scrollWidth));
  svg.setAttribute('height', Math.max(chart.clientHeight, chart.scrollHeight));
  svg.innerHTML='<defs><marker id="report-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto"><path class="connection-arrow report" d="M 1 1 L 11 6 L 1 11 z"/></marker><marker id="command-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto"><path class="connection-arrow command" d="M 1 1 L 11 6 L 1 11 z"/></marker></defs>';
  state.edges.forEach(edge=>{
    const from=nodeBox(edge.source_role), to=nodeBox(edge.target_role);
    if(from&&to){
      const p=edgePoints(from, to);
      appendArrow(svg, p.x1, p.y1, p.x2, p.y2, edge.relationship)
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
      },
      p=edgePoints(from, pointer);
      appendArrow(svg, p.x1, p.y1, state.drawingLink.x, state.drawingLink.y, state.drawingLink.relationship, true)
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
  state.agents.forEach(a=>{
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
  state.providerCommands=[];
  const a=activeAgent();
  if(!a){
    $('#agent-name').textContent='No agents yet';
    $('#agent-brief').textContent='Add a team member to this workspace to start a conversation.';
    $('#avatar').textContent='+';
    $('#runtime-badge').textContent='';
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
  renderActiveSkillSummary();
  renderAgents();
  renderMessages();
  renderComposer();
  loadChatControls();
  if(state.project)loadHistory(id).then(()=>recoverActiveRun(id))
}
function normalizeMessage(m, role=state.active){
  const kind=m.speaker==='user'?'user': m.speaker==='error'?'error': m.speaker==='native'?'native': m.speaker==='app'?'app': 'agent';
  const sourceRole=m.source_role||'';
  const messageKind=m.message_kind||'';
  const compiledParts=messageKind==='command'?compiledCommandParts(m.content):null;
  const sourceAgent=sourceRole?state.agents.find(agent=>agent.id===sourceRole):null;
  return{
    id: m.id,
    kind,
    text: m.content,
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
    delivery_status: m.delivery_status||'',
    by: sourceAgent?.name||(
      kind==='agent'?state.agents.find(a=>a.id===role)?.name:
      kind==='native'?'Codex': kind==='error'?'Provider error': 'Workspace'
    )
  }
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
  try{
    const out=await api(`/api/agents/${role}/history?project_id=${projectId}`);
    if(state.project?.id!==projectId)return false;
    state.messages[role]=out.messages.map(m=>normalizeMessage(m, role));
    if(state.active===role)renderMessages();
    return true
  }
  catch(err){
    if(state.active===role)localMessage(`Could not load history: ${err.message}`);
    return false
  }
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
  items=(state.messages[state.active]||[]).filter(m=>!m.internal),
  previousTop=box.scrollTop,
  wasAtBottom=!box.scrollHeight||box.scrollHeight-box.scrollTop-box.clientHeight<64;
  box.innerHTML=items.length?'': '<div class="empty"><strong>Start a conversation</strong><span>Type / to browse commands. This role keeps its own transcript.</span></div>';
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
  box.scrollTop=wasAtBottom?box.scrollHeight: previousTop
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
        if(run.status==='error'&&run.error&&!run.result?.assistant_message)(state.messages[role]??=[]).push({
          kind: 'error', text: run.error, by: 'Workspace', created_at: run.updated_at, attachments: []
        });
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
  activity.textContent=activityState?.text||'';
  // An active run blocks provider execution, not composing. Messages sent
  // while busy are persisted as queued runs and submitted when this agent is
  // free, so the send control must remain usable.
  $('#message').disabled=!agent;
  $('#chat-model').disabled=!agent;
  $('#chat-effort').disabled=!agent;
  $('#attach-button').disabled=!agent||$('#attach-button').dataset.enabled!=='true';
  $('#send-button').disabled=!agent||!$('#message').value.trim();
  $('#send-button').innerHTML=busy?'Queue <span>↗</span>': 'Send <span>↗</span>'
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
  box.innerHTML=state.context.length?'': '<p style="color:var(--muted);font-size:12px">No shared context yet.</p>';
  state.context.forEach(x=>{
    const d=document.createElement('article');
    d.className='context-card';
    const roles=x.roles.length?x.roles: ['All agents'];
    d.innerHTML=`<h3></h3><p></p><div class="tags">${roles.map(r=>`<span class="tag">${r}</span>`).join('')}</div>`;
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
  $('#provider-select').innerHTML=state.providers.map(p=>`<option value="${p.id}">${p.name}${p.available?'':' — not configured'}</option>`).join('');
  $('#provider-select').value=r.provider;
  $('#base-url-input').value=r.base_url;
  $('#key-env-input').value=r.api_key_env;
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
$('#add-agent-dashboard').onclick=openAgentDialog;
$('#close-agent').onclick=()=>$('#agent-dialog').close();
$('#close-dialog').onclick=()=>$('#context-dialog').close();
$('#close-code-terminal').onclick=()=>$('#code-terminal-dialog').close();
$('#tools-button').onclick=()=>openToolsDialog().catch(err=>alert(err.message));
$('#close-tools').onclick=()=>$('#tools-dialog').close();
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
  const pending=state.pendingGitAgent;
  const pendingEnable=state.pendingGitEnableAgent;
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
    $('#git-setup-dialog').close();
    state.pendingGitAgent=null;
    state.pendingGitEnableAgent=null;
    if(pending)await createAgent(pending);
    else if(pendingEnable)await saveExistingAgentGitEnabled(pendingEnable, true)
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
    $('#status').style.background='#fde2dc'
  }
}
init();
setInterval(()=>{
  if(state.project&&state.active&&!state.busy.has(state.active)&&!$('#workspace').classList.contains('hidden'))loadHistory(state.active);
  monitorAgentRuns().catch(err=>console.warn('Could not monitor agent runs', err))
}, 1500);

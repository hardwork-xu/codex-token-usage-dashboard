'use strict';
// Dependency-free synthetic DOM regression checks. No browser, network, or account data.
// This verifies rendering/interaction logic; real browser QA is still needed for layout.
// Run with: node tests/test_web_ui.cjs
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[2] || require('path').join(__dirname,'..','web','index.html'), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Check the full, unmodified product script.
let document;
class Element {
  constructor(tag, text = '') { this.tagName = tag.toUpperCase(); this.childNodes = []; this.parentNode = null; this.attrs = {}; this.dataset = {}; this.style = {}; this.listeners = {}; this._text = text; this.hidden = false; this.open = false; this.value = ''; }
  get value() { return this._value || ''; }
  set value(v) { this._value = String(v); }
  get children() { return this.childNodes.filter(n => n.tagName !== '#TEXT'); }
  get isConnected() { return this === document?.root || !!this.parentNode?.isConnected; }
  get textContent() { return this._text + this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) { this.replaceChildren(); this._text = String(v); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get classList() { return {toggle:(name,force) => { const set = new Set(this.className.split(/\s+/).filter(Boolean)), on = force ?? !set.has(name); on ? set.add(name) : set.delete(name); this.className = [...set].join(' '); return on; }}; }
  setAttribute(k,v) { this.attrs[k] = String(v); if (k === 'hidden') this.hidden = true; if (k === 'open') this.open = true; if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c) => c.toUpperCase())] = String(v); }
  getAttribute(k) { if (k.startsWith('data-')) return this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c) => c.toUpperCase())] ?? null; return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...nodes) { for (let n of nodes) { if (typeof n === 'string') n = new Element('#text',n); n.parentNode = this; this.childNodes.push(n); } }
  replaceChildren(...nodes) { for (const n of this.childNodes) n.parentNode = null; this.childNodes = []; this._text = ''; this.append(...nodes); }
  matches(s) { if (s.startsWith('.')) return this.className.split(/\s+/).includes(s.slice(1)); if (s.startsWith('#')) return this.attrs.id === s.slice(1); if (s.startsWith('[')) { const match = s.match(/^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/); return match && (match[2] === undefined ? this.getAttribute(match[1]) !== null : this.getAttribute(match[1]) === match[2]); } return this.tagName.toLowerCase() === s.toLowerCase(); }
  querySelectorAll(s) { return this.children.flatMap(n => [...(n.matches(s) ? [n] : []),...n.querySelectorAll(s)]); }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name) { for (const fn of this.listeners[name] || []) fn({target:this,preventDefault(){}}); }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; } close() { this.open = false; }
}
document = {root:new Element('root'),activeElement:null,hidden:true,createElement:tag => new Element(tag),createTextNode:text => new Element('#text',text),addEventListener(){}};
document.getElementById = id => document.root.querySelector('#' + id);
document.querySelectorAll = s => document.root.querySelectorAll(s);
document.querySelector = s => document.root.querySelector(s);
const stack = [document.root], voidTags = new Set(['META','LINK','INPUT','BR','HR','IMG']);
for (const token of source.replace(/<style>[\s\S]*?<\/style>/,'').replace(/<script>[\s\S]*?<\/script>/,'').match(/<[^>]+>|[^<]+/g)) {
  if (token.startsWith('<!')) continue;
  if (token.startsWith('</')) { stack.pop(); continue; }
  if (token.startsWith('<')) {
    const tag = token.match(/^<([\w-]+)/)?.[1]; if (!tag) continue;
    const node = new Element(tag);
    for (const a of token.slice(tag.length+1,-1).matchAll(/([\w:-]+)(?:="([^"]*)"|='([^']*)')?/g)) node.setAttribute(a[1],a[2] ?? a[3] ?? '');
    stack.at(-1).append(node); if (!voidTags.has(node.tagName) && !token.endsWith('/>')) stack.push(node);
  } else stack.at(-1).append(document.createTextNode(token));
}
const context = vm.createContext({document,console,URL,Intl,Date,localStorage:{getItem(){return null},setItem(){}},setInterval(){},setTimeout(){},clearTimeout(){},AbortController,fetch(){throw new Error('Synthetic checks must not request the network');}});
vm.runInContext(script,context);
const run = code => vm.runInContext(code,context), el = id => document.getElementById(id);
const tokens = total => ({total,input:total,output:0,cachedInput:0,cacheWriteInput:0,reasoningOutput:0});
const entry = (model,total,amount='0.2') => ({model,label:model === 'gpt-6-astra' ? 'GPT-6 Astra' : model === 'gpt-5.6-luna' ? 'GPT-5.6 Luna' : 'UNSAFE UNKNOWN LABEL',tokens:tokens(total),amount,credits:amount === null ? null : '5',usd:amount,partial:amount === null,unpricedTokens:amount === null ? total : 0,turnCount:1});
const models = [entry('gpt-6-astra',12400000),entry('gpt-5.6-luna',1000000,'0.8'),entry(null,600000,null)];
const turn = {id:'synthetic-turn-a',threadId:'synthetic-thread-a',turnNumber:1,startedAt:1720000000,endedAt:1720000010,status:'complete',quality:'complete',model:null,pricingMetadataStatus:'mixed',tokens:tokens(14000000),models,pricing:{status:'partial',amount:'1',credits:'25',model:null}};
const period = {status:'partial',partial:true,startDate:'2026-01-01',endDate:'2026-02-01',tokens:tokens(14000000),models,amount:'1',credits:'25',turnCount:1,unpricedTokens:600000,unpricedTurnCount:1};
const fixture = {monitoring:{status:'active'},settings:{pricingMode:'official',currencyCode:'USD',currencyName:'美元',currencySymbol:'$',subscriptionRenewalDay:1,currencyPerUsd:'1'},periods:{today:period,subscription:period},conversations:[{id:'synthetic-thread-a',title:'合成任务 A',displayId:'TEST-A',activeTurnCount:0,turnCount:1,sourceType:'main',reading:{complete:true}}],turns:[turn],quota:{buckets:[]}};
let checks = 0;
function check(name, fn) { fn(); checks++; console.log('PASS',name); }
function update(value) { context.fixture = structuredClone(value); run('state = fixture; connected = true; render();'); }
update(fixture);
check('12.4M preserves decimal and exact accessible count',() => { assert.match(el('today-models').textContent,/12\.4M/); assert.match(el('today-models').textContent,/12,400,000 Tokens/); assert.equal(el('today-models').querySelector('.model-tokens').title,'12,400,000 Tokens'); });
check('local shares use full denominator including unknown',() => { const lines = el('today-models').querySelectorAll('.model-share-line'); assert.match(lines[0].textContent,/≈88\.6%/); assert.match(lines[1].textContent,/≈7\.1%/); assert.match(lines[2].textContent,/≈4\.3%/); assert.match(lines[0].getAttribute('aria-label'),/12,400,000 \/ 14,000,000/); });
check('unknown model preserved and unpriced tokens stay visible',() => { const unknown = el('today-models').children[2]; assert.match(unknown.textContent,/未确认模型/); assert.doesNotMatch(unknown.textContent,/UNSAFE/); assert.match(unknown.textContent,/600K/); assert.match(unknown.textContent,/暂无估价/); assert.match(unknown.textContent,/600,000 Tokens 未计价/); });
check('latest and pricing show mixed model label',() => { assert.match(el('latest-meta').textContent,/多模型 · 2 个 · 含未确认用量/); assert.match(el('pricing-context').textContent,/多模型 · 2 个/); assert.equal(el('latest-model-details').hidden,false); assert.equal(el('latest-model-details').querySelectorAll('.model-row').length,3); });
check('history has native accessible disclosure with all model rows',() => { const details = el('history-body').querySelector('details'); assert.ok(details); assert.equal(details.querySelector('summary').getAttribute('aria-label'),'第 1 次提问：查看题内模型明细'); assert.equal(details.querySelectorAll('.model-row').length,3); });
check('disclosure state and keyboard focus survive refresh',() => { const details = el('latest-model-details').querySelector('details'); details.open = true; details.dispatch('toggle'); details.querySelector('summary').focus(); run('render();'); const next = el('latest-model-details').querySelector('details'); assert.notEqual(next,details); assert.equal(next.open,true); assert.equal(document.activeElement,next.querySelector('summary')); });
check('history disclosure state and focus survive refresh',() => { const details = el('history-body').querySelector('details'); details.open = true; details.dispatch('toggle'); details.querySelector('summary').focus(); run('render();'); const next = el('history-body').querySelector('details'); assert.equal(next.open,true); assert.equal(document.activeElement,next.querySelector('summary')); });
check('explicitly separates tokens from official quota/Credits shares',() => { assert.match(el('latest-model-details').textContent,/Token 占比不是官方订阅额度或 Credits 占比/); assert.match(document.root.textContent,/不能据此反推额度/); });
check('tier-only mixed metadata does not imply mixed models',() => { const one = structuredClone(fixture); one.turns[0].models = [entry('gpt-6-astra',14000000)]; one.turns[0].model='gpt-6-astra'; update(one); assert.match(el('latest-meta').textContent,/Astra/); assert.doesNotMatch(el('latest-meta').textContent,/多模型/); });
check('unknown usage is never replaced by legacy default model',() => { const unknown = structuredClone(fixture); unknown.turns[0].model='gpt-6-astra'; unknown.turns[0].models=[entry(null,14000000,null)]; update(unknown); assert.match(el('latest-meta').textContent,/未确认模型/); assert.doesNotMatch(el('latest-meta').textContent,/Astra/); });
check('known plus unknown does not assert a second known model',() => { const mixed = structuredClone(fixture); mixed.turns[0].models=[entry('gpt-6-astra',13400000),entry(null,600000,null)]; update(mixed); assert.match(el('latest-meta').textContent,/Astra · 含未确认用量/); assert.doesNotMatch(el('latest-meta').textContent,/多模型/); });
for (const [code,symbol,name] of [['CNY','¥','人民币'],['USD','$','美元'],['HKD','HK$','港元']]) check('currency render ' + code,() => { const f=structuredClone(fixture); f.settings={...f.settings,currencyCode:code,currencySymbol:symbol,currencyName:name}; update(f); assert.ok(el('today-models').querySelector('.model-details').textContent.includes(symbol+'0.20')); assert.ok(el('latest-model-details').querySelector('.model-details').textContent.includes(symbol+'0.20')); assert.equal(document.querySelectorAll('[data-currency-code]').find(b => b.dataset.currencyCode===code).getAttribute('aria-pressed'),'true'); });
check('legacy API without models remains usable',() => { const old=structuredClone(fixture); delete old.turns[0].models; old.turns[0].model='gpt-6-astra'; delete old.periods.today.models; delete old.periods.subscription.models; update(old); assert.match(el('latest-meta').textContent,/Astra/); assert.equal(el('latest-model-details').hidden,true); assert.equal(el('history-body').querySelector('details'),null); assert.equal(el('today-models-empty').textContent,'模型明细等待服务更新。'); });
check('unknown price keeps period tokens and partial warning',() => { const f=structuredClone(fixture); f.periods.today.status='unavailable'; f.periods.today.amount=null; f.periods.today.credits=null; update(f); assert.equal(el('today-tokens').textContent,'14,000,000'); assert.equal(el('today-amount').textContent,'暂无估价'); assert.match(el('today-note').textContent,/部分统计/); });
check('zero missing and invalid share denominator stays unavailable',() => { assert.equal(run('tokenShare(0,0)'),null); assert.equal(run('tokenShare(2,null)'),null); assert.equal(run('tokenShare(2,1)'),null); assert.equal(run('tokenShare(0,10).label'),'0%'); });
check('tiny shares do not claim zero or 100 percent',() => { assert.equal(run('tokenShare(1,100000).label'),'<0.1%'); assert.equal(run('tokenShare(99999,100000).label'),'>99.9%'); });
check('rounded compact tokens are marked approximate',() => { assert.equal(run('compactTokens(12401234)'),'≈12.4M'); assert.equal(run('compactTokens(12400000)'),'12.4M'); });
check('reading progress and selected conversation stay stable',() => { const f=structuredClone(fixture); f.conversations[0].reading={complete:false,bytesRead:10,fileBytes:100}; f.conversations.push({id:'synthetic-thread-b',title:'合成任务 B',displayId:'TEST-B',activeTurnCount:1,sourceType:'main',reading:{complete:true}}); f.turns.push({...structuredClone(turn),id:'synthetic-turn-b',threadId:'synthetic-thread-b',startedAt:1800000000,status:'running'}); update(f); assert.equal(run('selectedConversationId'),'synthetic-thread-a'); assert.equal(el('reading-notice').hidden,false); assert.match(el('latest-heading').textContent,/已读到/); assert.match(el('running-conversations').textContent,/多模型 · 2 个/); });
check('model label content is inserted as text',() => { const f=structuredClone(fixture); f.periods.today.models[0].label='<img src=x onerror=alert(1)>'; update(f); assert.match(el('today-models').textContent,/<img src=x/); assert.equal(el('today-models').querySelector('img'),null); });
console.log('PASS JavaScript syntax; ' + checks + ' synthetic DOM checks; no network, account data, or real browser used.');
const apiFixture=structuredClone(fixture);
apiFixture.csrfToken='synthetic-only-token';
apiFixture.settings={...apiFixture.settings,pricingMode:'api',usdPerCredit:'0.07',ratePerMillion:'77',exchangeRates:{USD:'1',CNY:'7',HKD:'8'}};
for (const owner of [apiFixture.turns[0],apiFixture.periods.today,apiFixture.periods.subscription]) {
  owner.apiContextUncertainTokens=1000000; owner.apiLongContextTokens=12400000;
  owner.credits=null;
  owner.models[0].apiLongContextTokens=12400000;
  owner.models[1].apiContextUncertainTokens=1000000;
  for (const row of owner.models) {
    row.credits=null;
    if (row.model) row.standardRates={unit:'usd_per_million_tokens',uncachedInput:'2.5',cachedInput:'0.25',output:'15',rateDate:'2026-09-27',sourceUrl:'https://developers.openai.com/api/docs/models/synthetic-model'};
  }
}
apiFixture.turns[0].pricing={status:'partial',amount:'1',usd:'1',credits:null,estimateBasis:'api_slices',apiContextUncertainTokens:1000000,apiLongContextTokens:12400000,sourceUrl:'https://developers.openai.com/api/docs/pricing',rateDate:'2026-09-27'};
update(apiFixture);
check('API labels at period current history and model levels',()=>{for (const id of ['today-money-label','subscription-money-label','pricing-heading','history-pricing-heading','today-models','latest-model-details']) assert.match(el(id).textContent,/API 等价费用（估算）/);});
check('API mode has no prominent Credit totals',()=>{assert.equal(el('credit-value').hidden,true); assert.equal(el('today-credits').hidden,true); assert.equal(el('subscription-credits').hidden,true); assert.equal(el('history-body').querySelectorAll('.credit-cell').length,0); assert.doesNotMatch(el('today-models').querySelector('.model-details').textContent,/Credits/);});
check('API model rates come from USD protocol and link source',()=>{const row=el('today-models').children[0]; assert.match(row.textContent,/API Standard 基准单价（USD \/ 百万 Tokens）：普通输入 2.5 · 缓存输入 0.25 · 输出 15/); assert.match(row.textContent,/2026-09-27/); const link=row.querySelector('a'); assert.equal(link.href,'https://developers.openai.com/api/docs/models/synthetic-model'); assert.equal(link.rel,'noopener noreferrer'); assert.equal(link.target,'_blank');});
check('API long and uncertain contexts are separately explained',()=>{assert.match(el('today-models').children[0].textContent,/12,400,000 Tokens 使用长上下文档位/); assert.match(el('today-models').children[0].textContent,/2 倍，输出为 1.5 倍/); assert.match(el('today-models').children[1].textContent,/1,000,000 Tokens 的请求／会话上下文未确认/); assert.match(el('today-pricing-summary').textContent,/1,000,000 Tokens 的请求／会话上下文未确认/); assert.match(el('pricing-note').textContent,/12,400,000 Tokens 使用长上下文档位/); assert.match(el('pricing-note').textContent,/1,000,000 Tokens 的请求／会话上下文未确认/);});
check('API scope keeps cache assumption and excludes fees tax subscription',()=>{assert.match(el('pricing-note').textContent,/已观察到的缓存命中比例/); assert.match(el('cost-disclaimer').textContent,/不含工具附加费、税费/); assert.match(el('cost-disclaimer').textContent,/不是订阅扣款/);});
check('API null-price unknown tokens remain excluded and visible',()=>{const row=el('today-models').children[2]; assert.match(row.textContent,/600K/); assert.match(row.textContent,/暂无估价/); assert.match(row.textContent,/600,000 Tokens 未计价/); assert.match(el('today-money-label').textContent,/可计价部分/);});
check('API hides credit conversion but retains FX and legacy settings',()=>{run('openSettings()'); assert.equal(el('credit-conversion-settings').hidden,true); assert.equal(el('official-settings').hidden,false); assert.equal(el('custom-settings').hidden,true); assert.equal(run('fullSettings().usdPerCredit'),'0.07'); assert.equal(run('fullSettings().ratePerMillion'),'77'); assert.equal(run('fullSettings().pricingMode'),'api'); assert.equal(document.querySelectorAll('[data-settings-currency]').find(b=>b.dataset.settingsCurrency==='CNY').disabled,false);});
for (const [code,symbol,name] of [['CNY','¥','人民币'],['USD','$','美元'],['HKD','HK$','港元']]) check('API currency render and switch enabled '+code,()=>{const f=structuredClone(apiFixture);f.settings={...f.settings,currencyCode:code,currencySymbol:symbol,currencyName:name};update(f);assert.ok(el('cost-value').textContent.startsWith(symbol));assert.ok(el('today-models').querySelector('.model-details').textContent.includes(symbol+'0.20'));const button=document.querySelectorAll('[data-currency-code]').find(b=>b.dataset.currencyCode===code);assert.equal(button.disabled,false);assert.equal(button.getAttribute('aria-pressed'),'true');});
check('API default selected when new settings lack mode',()=>{const f=structuredClone(apiFixture);delete f.settings.pricingMode;update(f);assert.equal(run('pricingMode()'),'api');assert.equal(run('fullSettings().pricingMode'),'api');assert.match(el('pricing-heading').textContent,/API/);});
check('API normal-context rates do not show speculative uncertainty',()=>{const f=structuredClone(apiFixture);f.turns[0].pricing.apiContextUncertainTokens=0;f.turns[0].pricing.apiLongContextTokens=0;f.turns[0].pricing.estimateBasis='api_standard';update(f);assert.doesNotMatch(el('pricing-note').textContent,/中点|2 倍/);});
check('API old protocol without models keeps total and waiting detail',()=>{const f=structuredClone(apiFixture);delete f.turns[0].models;delete f.periods.today.models;update(f);assert.equal(el('cost-value').textContent,'$1.00');assert.equal(el('latest-model-details').hidden,true);assert.equal(el('today-models-empty').textContent,'模型明细等待服务更新。');});
check('legacy official Credit rates remain visible',()=>{const f=structuredClone(fixture);f.periods.today.models[0].standardRates={unit:'credits_per_million_tokens',uncachedInput:'62.5',cachedInput:'6.25',output:'375'};update(f);run('openSettings()');assert.equal(el('credit-conversion-settings').hidden,false);assert.equal(el('credit-value').hidden,false);assert.equal(el('today-credits').hidden,false);assert.match(el('today-models').textContent,/Credits \/ 百万 Tokens/);assert.equal(el('history-body').querySelectorAll('.credit-cell').length,1);});
check('custom mode keeps its own units and disables shortcut FX',()=>{const f=structuredClone(fixture);f.settings={...f.settings,pricingMode:'custom',ratePerMillion:'3',currencyCode:'CUSTOM',currencyName:'点',currencySymbol:''};update(f);run('openSettings()');assert.equal(el('custom-settings').hidden,false);assert.equal(el('official-settings').hidden,true);assert.equal(el('credit-conversion-settings').hidden,true);assert.ok(document.querySelectorAll('[data-currency-code]').every(b=>b.disabled));assert.match(el('cost-value').textContent,/点/);assert.doesNotMatch(el('today-models').querySelector('.model-details').textContent,/API/);});

const quotaNow=Math.floor(Date.now()/1000);
const freshQuota={status:'fresh',updatedAt:quotaNow-20,lastAttemptAt:quotaNow-20,expiresAt:quotaNow+100,staleAfterSeconds:120,refreshing:false,error:null,buckets:[{id:'synthetic-main',label:'合成订阅额度',windows:[{windowMinutes:300,remainingPercent:100,resetsAt:quotaNow+3600}]}]};
function withQuota(quota) { const f=structuredClone(apiFixture); f.quota=structuredClone(quota); update(f); }
check('period cards put monetary total before exact token count',()=>{update(apiFixture);const values=document.querySelectorAll('.period-values');assert.equal(values.length,2);for(const value of values){assert.equal(value.children[0].className,'period-money-block');assert.equal(value.children[1].className,'period-token-block');}assert.equal(el('today-tokens').textContent,'14,000,000');});
check('model categories keep cached input out of ordinary input',()=>{const f=structuredClone(apiFixture);f.periods.today.models[0].tokens={...tokens(105),input:100,cachedInput:70,output:5};update(f);const categories=el('today-models').querySelectorAll('.model-category');assert.equal(categories[0].textContent,'普通输入30');assert.equal(categories[1].textContent,'缓存输入70');assert.equal(categories[2].textContent,'输出5');});
check('model API rateDate contract is shown with source',()=>{update(apiFixture);const row=el('today-models').children[0];assert.match(row.querySelector('.model-rates').textContent,/2026-09-27/);assert.equal(row.querySelector('.model-rates').open,false);});
check('rate disclosure and focus survive polling',()=>{const details=el('today-models').querySelector('.model-rates');details.open=true;details.dispatch('toggle');details.querySelector('summary').focus();run('render()');const next=el('today-models').querySelector('.model-rates');assert.equal(next.open,true);assert.equal(document.activeElement,next.querySelector('summary'));});
check('coverage gaps remain visible next to aggregate costs',()=>{const f=structuredClone(apiFixture);f.monitoring.coverage={registeredTasks:12,readableTasks:5,unreadableTasks:7,backfillingTasks:2};update(f);assert.match(el('coverage-summary').textContent,/7 个任务的记录无法读取/);assert.match(el('coverage-summary').textContent,/2 个正在补读/);assert.match(el('coverage-summary').className,/attention/);assert.match(document.root.textContent,/本任务此题的已记录用量；子任务另列/);});
check('fresh official quota shows current percentage and distinct scope',()=>{withQuota(freshQuota);assert.equal(el('quota-container').hidden,false);assert.match(el('quota-container').textContent,/剩余100%/);assert.equal(el('quota-last-known').hidden,true);assert.match(document.querySelector('.quota-scope').textContent,/不是 API 账户余额/);});
check('16-hour stale quota hides current 100 percent',()=>{withQuota({...freshQuota,status:'stale',updatedAt:quotaNow-16*3600,expiresAt:quotaNow-16*3600+120,error:'合成错误：找不到 Codex CLI'});assert.equal(el('quota-container').hidden,true);assert.equal(el('quota-container').textContent,'');assert.doesNotMatch(el('quota-status').textContent,/100%/);assert.equal(el('quota-last-known').hidden,false);assert.equal(el('quota-last-known').open,false);assert.match(el('quota-last-known-summary').textContent,/查看上次成功记录/);assert.match(el('quota-cached-container').textContent,/当时剩余100%/);assert.match(document.querySelector('.quota-historical-note').textContent,/不能作为当前剩余额度/);assert.match(el('quota-error').textContent,/找不到 Codex CLI/);});
check('refresh error overrides an otherwise fresh quota snapshot',()=>{withQuota({...freshQuota,error:'合成网络错误'});assert.equal(el('quota-container').hidden,true);assert.match(el('quota-status').textContent,/读取失败/);assert.equal(el('quota-error-details').hidden,false);});
check('expired fresh snapshot is hidden even before a new backend state',()=>{withQuota({...freshQuota,updatedAt:quotaNow-500,expiresAt:quotaNow-1});assert.equal(el('quota-container').hidden,true);assert.match(el('quota-status').textContent,/已过期/);});
check('fallback TTL hides old data when expiresAt is absent',()=>{const q={...freshQuota,updatedAt:quotaNow-500};delete q.expiresAt;withQuota(q);assert.equal(el('quota-container').hidden,true);});
check('expired quota reset cannot remain current',()=>{const q=structuredClone(freshQuota);q.buckets[0].windows[0].resetsAt=quotaNow-1;withQuota(q);assert.equal(el('quota-container').hidden,true);});
check('future timestamp is not presented as successful historical data',()=>{withQuota({...freshQuota,updatedAt:quotaNow+600});assert.equal(el('quota-container').hidden,true);assert.equal(el('quota-last-known').hidden,true);});
check('malformed timestamp cannot crash quota rendering',()=>{withQuota({...freshQuota,updatedAt:1e20});assert.equal(el('quota-container').hidden,true);assert.match(el('quota-updated').textContent,/记录时间异常/);});
check('legacy quota API without freshness status stays historical',()=>{const q=structuredClone(freshQuota);delete q.status;withQuota(q);assert.equal(el('quota-container').hidden,true);assert.equal(el('quota-last-known').hidden,false);});
check('unavailable quota does not manufacture zero or 100 percent',()=>{withQuota({status:'unavailable',buckets:[],updatedAt:null,error:null});assert.equal(el('quota-container').textContent,'');assert.equal(el('quota-last-known').hidden,true);assert.doesNotMatch(el('quota-status').textContent,/%/);});
check('refreshing stale quota remains hidden and honestly pending',()=>{withQuota({...freshQuota,status:'stale',refreshing:true});assert.equal(el('quota-container').hidden,true);assert.match(el('quota-status').textContent,/正在读取/);assert.match(el('refresh-quota').textContent,/更新中/);assert.equal(el('refresh-quota').disabled,true);});
check('refreshing fresh quota identifies the displayed value as last success',()=>{withQuota({...freshQuota,refreshing:true});assert.equal(el('quota-container').hidden,false);assert.match(el('quota-status').textContent,/最近成功读取的记录/);});
check('connection loss hides the last fresh quota',()=>{withQuota(freshQuota);run('connected=false;renderQuota()');assert.equal(el('quota-container').hidden,true);assert.match(el('quota-status').textContent,/未连接/);});
check('quota recovers after a successful fresh state',()=>{withQuota({...freshQuota,error:'合成失败'});withQuota(freshQuota);assert.equal(el('quota-container').hidden,false);assert.equal(el('quota-last-known').hidden,true);assert.equal(el('quota-error-details').hidden,true);});
(async()=>{
  update(apiFixture);run('openSettings()');
  el('usd-per-credit').value='not-used-by-api';
  el('rate-per-million').value='not-used-by-api';
  context.posts=[];run('request = async (path,options) => { posts.push({path,body:JSON.parse(options.body)}); return {ok:true}; }; refreshAfterSettings = async () => {};');
  await el('settings-form').listeners.submit[0]({preventDefault(){}});
  check('API save preserves inactive Credit and custom settings',()=>{assert.equal(context.posts.length,1);const posted=context.posts[0].body;assert.equal(posted.pricingMode,'api');assert.equal(posted.usdPerCredit,'0.07');assert.equal(posted.ratePerMillion,'77');assert.equal(posted.exchangeRates.CNY,'7');assert.equal(el('settings-error').hidden,true);});
  for(const code of ['CNY','HKD']) {
    context.posts=[];
    await document.querySelectorAll('[data-currency-code]').find(b=>b.dataset.currencyCode===code).listeners.click[0]({});
    check('API shortcut posts full settings '+code,()=>{assert.equal(context.posts.length,1);const posted=context.posts[0].body;assert.equal(posted.currencyCode,code);assert.equal(posted.pricingMode,'api');assert.equal(posted.usdPerCredit,'0.07');assert.equal(posted.ratePerMillion,'77');});
  }

  withQuota(freshQuota);
  context.refreshReply={ok:true,refresh:{status:'throttled',retryAfterSeconds:30}};
  run('request=async()=>refreshReply; quotaRefreshRetryAt=0;');
  await el('refresh-quota').listeners.click[0]({});
  check('throttled refresh reports cooldown instead of claiming started',()=>{assert.match(el('toast').textContent,/冷却/);assert.doesNotMatch(el('toast').textContent,/已开始/);assert.equal(el('refresh-quota').disabled,true);assert.match(el('refresh-quota').textContent,/秒后可刷新/);});
  run('quotaRefreshRetryAt=0;'); context.refreshReply={ok:true,refresh:{status:'running'}};
  await el('refresh-quota').listeners.click[0]({});
  check('running refresh is reported as already running',()=>assert.equal(el('toast').textContent,'官方额度正在读取中。'));
  context.refreshReply={ok:true,refresh:{status:'started'}};
  await el('refresh-quota').listeners.click[0]({});
  check('started refresh does not claim completed fresh data',()=>assert.equal(el('toast').textContent,'已开始读取官方额度。'));
  run("request=async()=>{throw new Error('合成刷新失败')};");
  await el('refresh-quota').listeners.click[0]({});
  check('failed refresh request hides previously fresh percentage',()=>{assert.equal(el('quota-container').hidden,true);assert.match(el('quota-status').textContent,/读取失败/);assert.equal(el('quota-error').textContent,'合成刷新失败');});
  console.log('PASS JavaScript syntax; '+checks+' total synthetic DOM checks including API/official/custom; no network or real data.');
})().catch(error=>{console.error(error);process.exitCode=1;});

const sandboxPrice = value => new Intl.NumberFormat('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:9}).format(Number(value)||0)+' ₽';
const sandboxAssets = {
  RUB: {name:'Рубли', detail:'Остаток на счёте', color:'#dce4d3', mark:'₽'},
  TGLD: {name:'Золото', detail:'Фонд TGLD', color:'#d0ac61', mark:'Au'},
  TMOS: {name:'Российские акции', detail:'Фонд TMOS', color:'#65866a', mark:'РФ'},
  SBER: {name:'Сбербанк', detail:'Акция SBER', color:'#254f3e', mark:'С'},
  LKOH: {name:'ЛУКОЙЛ', detail:'Акция LKOH', color:'#a7bd8c', mark:'Л'}
};
const sandboxRiskNames = ['Осторожный','Умеренный','Сбалансированный','Динамичный'];
const sandboxRiskNotes = ['Меньше колебаний','Небольшая доля акций','Равновесие активов','Больше акций и риска'];
const sandboxFallbackStrategies = [
  {id:'reserve', risk_level:1, weights:{RUB:85,TGLD:15}},
  {id:'moderate', risk_level:2, weights:{RUB:60,TGLD:20,TMOS:10,SBER:5,LKOH:5}},
  {id:'balanced', risk_level:3, weights:{RUB:40,TGLD:20,TMOS:25,SBER:10,LKOH:5}},
  {id:'growth', risk_level:4, weights:{RUB:10,TGLD:20,TMOS:35,SBER:20,LKOH:15}}
];
const sandboxActiveStatuses = new Set(['draft','submitting','new','pending','partially_filled','unknown']);
const sandboxStatusNames = {draft:'В очереди',submitting:'Отправляем',new:'У брокера',pending:'У брокера',partially_filled:'Частично',filled:'Куплено',cancelled:'Отменена',rejected:'Отклонена',blocked:'Не отправлена',unknown:'Проверяем'};
let sandboxBusy = false, sandboxRequestVersion = 0, sandboxPollTimer = null, sandboxPollBusy = false;
let sandboxDraft = null, sandboxDraftOwner = null, sandboxError = '', sandboxProgress = '';
let sandboxEditing = false, sandboxResetKey = null, sandboxResetThenBuild = false;

function invalidateSandboxRequests() {
  sandboxRequestVersion++;sandboxDraft=null;sandboxDraftOwner=null;sandboxError='';sandboxProgress='';
  if (sandboxPollTimer) clearInterval(sandboxPollTimer);
  sandboxPollTimer=null;
}
function sandboxHasActiveOrders() {
  const s = state?.sandbox || {};
  const orders=s.orders || [];
  return orders.some(o=>sandboxActiveStatuses.has(o.status)) || (Boolean(s.demo_config) && orders.some(o=>o.status==='filled') && s.snapshot_reconciled!==true);
}
function syncSandboxPolling() {
  if (route!=='sandbox' || !sandboxHasActiveOrders()) {
    if (sandboxPollTimer) clearInterval(sandboxPollTimer);
    sandboxPollTimer = null;
    return;
  }
  if (!sandboxPollTimer) sandboxPollTimer = setInterval(pollSandboxState, 2500);
}
async function pollSandboxState() {
  if (sandboxPollBusy || sandboxBusy || route!=='sandbox' || modal?.open) return;
  if (document.activeElement?.matches('input,select,textarea')) return;
  if (document.querySelector('#sandbox-token')?.value) return;
  const version = sandboxRequestVersion, owner = state?.user?.email;
  sandboxPollBusy = true;
  try {
    const next = await api('state', undefined, 'GET');
    if (version!==sandboxRequestVersion || state?.user?.email!==owner || route!=='sandbox') return;
    if (JSON.stringify(state.sandbox)!==JSON.stringify(next.sandbox)) {state=next;render();}
  } catch {} finally {sandboxPollBusy=false;}
}
function sandboxConfig() {
  const owner = state?.user?.email;
  if (!sandboxDraft || owner!==sandboxDraftOwner) {
    const s = state?.sandbox || {}, saved = s.demo_config || {}, old = s.policy?.profile || {};
    sandboxDraft = {
      strategy_id:saved.strategy_id || s.policy?.strategy_id || 'balanced',
      goal_name:saved.goal_name || old.goal_name || 'Капитал на будущее',
      target_amount:saved.target_amount ?? old.goal_amount ?? 20000000,
      horizon_years:saved.horizon_years ?? old.horizon_years ?? 5,
      monthly_contribution:saved.monthly_contribution ?? old.monthly_contribution ?? 50000
    };
    sandboxDraftOwner=owner;
    sandboxError='';
    sandboxEditing=false;
  }
  return sandboxDraft;
}
function sandboxStrategy() {
  const list = state?.sandbox?.demo_strategies?.length ? state.sandbox.demo_strategies : sandboxFallbackStrategies;
  return list.find(x=>x.id===sandboxConfig().strategy_id) || list[2];
}
function sandboxRiskMeter(level) {
  return `<span class="sb-risk-meter" aria-label="Уровень риска ${level} из 4">${[1,2,3,4].map(n=>`<i class="${n<=level?'on':''}"></i>`).join('')}</span>`;
}
function sandboxConnect() {
  const s=state.sandbox || {}, c=s.connection || {};
  if (c.connected) return `<div class="sb-connection"><span class="sb-connected-dot"></span><span>Т-Инвестиции подключены</span><button type="button" class="sb-text-button" data-action="sandbox-disconnect">Сменить токен</button></div>`;
  return `<form id="sandbox-connect-form" class="sb-connect"><div class="sb-connect-copy">${icon('shield')}<div><b>Подключите Т-Инвестиции</b><span>Токен песочницы</span></div></div><div class="sb-token-field"><label class="sr-only" for="sandbox-token">Токен песочницы</label><input id="sandbox-token" name="token" type="password" placeholder="Вставьте токен песочницы" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="2048" required><button type="submit" class="btn">Подключить ${icon('arrow')}</button></div></form>`;
}
function sandboxRiskChoices() {
  const cfg=sandboxConfig();
  const list=state.sandbox?.demo_strategies?.length ? state.sandbox.demo_strategies : sandboxFallbackStrategies;
  return `<div class="sb-section-title"><span class="sb-step">01</span><h2>Какой риск вам ближе?</h2></div><div class="sb-risks" role="radiogroup" aria-label="Стратегия по уровню риска">${list.map(x=>`<button type="button" class="sb-risk-card ${cfg.strategy_id===x.id?'selected':''}" data-action="sandbox-risk" data-strategy-id="${esc(x.id)}" role="radio" aria-checked="${cfg.strategy_id===x.id}">${sandboxRiskMeter(x.risk_level)}<span class="sb-risk-name">${sandboxRiskNames[x.risk_level-1]}</span><span class="sb-risk-note">${sandboxRiskNotes[x.risk_level-1]}</span><span class="sb-risk-check" aria-hidden="true"></span></button>`).join('')}</div>`;
}
function sandboxGoalForm() {
  const cfg=sandboxConfig();
  const presets=[1,3,5,10,15,20,30,40];
  if (!presets.includes(Number(cfg.horizon_years))) presets.push(Number(cfg.horizon_years));
  presets.sort((a,b)=>a-b);
  return `<section class="sb-goal"><div class="sb-section-title"><span class="sb-step">02</span><h2>На что копим?</h2></div><form id="sandbox-goal-form"><div class="form-field"><label for="sb-goal-name">Ваша цель</label><input id="sb-goal-name" name="goal_name" value="${esc(cfg.goal_name)}" placeholder="Например, дом у моря" maxlength="80" required></div><div class="sb-goal-grid"><div class="form-field"><label for="sb-target">Нужно накопить, ₽</label><input id="sb-target" name="target_amount" type="number" min="1000" max="1000000000" step="1000" value="${esc(cfg.target_amount)}" required></div><div class="form-field"><label for="sb-years">Срок</label><select id="sb-years" name="horizon_years">${presets.map(n=>`<option value="${n}" ${selected(n,Number(cfg.horizon_years))}>${n} ${plural(n,'год','года','лет')}</option>`).join('')}</select></div></div><div class="form-field sb-monthly"><label for="sb-monthly">Добавлять каждый месяц, ₽</label><input id="sb-monthly" name="monthly_contribution" type="number" min="0" max="10000000" step="1000" value="${esc(cfg.monthly_contribution)}" required></div><div class="sb-goal-math" id="sandbox-goal-math">${sandboxGoalMath()}</div><button type="submit" class="btn sb-build">Показать покупки ${icon('arrow')}</button><span class="sb-button-caption">Сначала состав и расходы, затем ваше подтверждение</span></form></section>`;
}
function sandboxGoalMath(total=10000000, saved=null) {
  const cfg=saved || sandboxConfig(), months=Number(cfg.horizon_years)*12;
  const contributions=Math.max(0,Number(cfg.monthly_contribution)||0)*months;
  const target=Math.max(0,Number(cfg.target_amount)||0), end=total+contributions;
  return `<div><span>Старт</span><b>${money(total)}</b></div><span class="sb-math-plus">+</span><div><span>Ваши взносы</span><b>${money(contributions)}</b></div><div class="sb-goal-result"><span>Без доходности</span><b>${money(end)}</b></div>${target>end?`<p>До цели ${money(target-end)}. Можно увеличить взнос или срок.</p>`:'<p>Взносы покрывают цель без допущения о доходности.</p>'}`;
}
function sandboxDonut(weights, center, caption) {
  let start=0;
  const parts=Object.entries(weights).filter(([,n])=>n>0).map(([key,n])=>{
    const end=start+Number(n), segment=`${sandboxAssets[key]?.color || '#84917d'} ${start}% ${end}%`;
    start=end;return segment;
  });
  return `<div class="sb-donut" style="background:conic-gradient(${parts.join(',')})" role="img" aria-label="Распределение портфеля"><div><span>${caption}</span><b>${center}</b><small>₽</small></div></div>`;
}
function sandboxComposition() {
  const strategy=sandboxStrategy(), weights=strategy.weights, level=strategy.risk_level;
  return `<div class="sb-composition-top"><span class="eyebrow">Будущий портфель</span><span class="sb-mini-risk">${sandboxRiskMeter(level)} ${sandboxRiskNames[level-1]}</span></div>${sandboxDonut(weights,'10 000 000','Стартовый капитал')}<div class="sb-allocation">${Object.entries(weights).filter(([,weight])=>weight>0).map(([key,weight])=>`<div class="sb-allocation-row"><span class="sb-dot" style="background:${sandboxAssets[key]?.color}"></span><span>${sandboxAssets[key]?.name || esc(key)}</span><b>${percent(weight)}</b><small>${money(10000000*weight/100)}</small></div>`).join('')}</div><p class="sb-composition-note">Выше риск — больше доля акций и сильнее возможные колебания.</p>`;
}
function sandboxBuilder() {
  return `<section class="sb-builder">${sandboxRiskChoices()}<div class="sb-builder-grid">${sandboxGoalForm()}<aside class="sb-composition" id="sandbox-composition">${sandboxComposition()}</aside></div></section>`;
}
function sandboxPurchaseRows(orders, actual=false) {
  return orders.map(o=>{
    const ticker=String(o.ticker||'').replace(/@$/,''), asset=sandboxAssets[ticker] || {};
    const value=actual ? Number(o.quantity)*Number(o.price) : Number(o.notional);
    const quantity=o.quantity;
    return `<div class="sb-purchase"><span class="sb-asset-mark" style="--asset-color:${asset.color || '#84917d'}">${asset.mark || esc(ticker.slice(0,2))}</span><div class="sb-purchase-name"><b>${esc(actual?o.name:(asset.name || o.name || ticker))}</b><span>${esc(ticker)} · ${num(quantity,4)} шт.${!actual?' · '+num(o.lots,0)+' лот.':''}</span></div><div class="sb-purchase-value"><b>${money(value)}</b><span>${actual?'Оценка позиции':(o.direction==='sell'?'Продажа':'Покупка')+' · лимит '+sandboxPrice(o.limit_price)}</span></div></div>`;
  }).join('');
}
function sandboxReview(proposal) {
  const cfg=state.sandbox.demo_config || sandboxConfig();
  const feeKnown=proposal.commission_status==='known' && Number.isFinite(proposal.commission);
  const assets=(proposal.orders || []).reduce((sum,o)=>sum+Number(o.notional||0),0);
  const cash=Number(proposal.source_snapshot?.cash||0)-Number(proposal.total||0);
  return `<section class="sb-review" id="sandbox-review"><div class="sb-review-heading"><div><span class="eyebrow">Готово к покупке</span><h2>${esc(cfg.goal_name)}</h2><p>${sandboxRiskNames[(sandboxStrategy().risk_level||3)-1]} · ${cfg.horizon_years} ${plural(Number(cfg.horizon_years),'год','года','лет')} · цель ${money(cfg.target_amount)}</p></div><button class="sb-text-button" type="button" data-action="sandbox-edit">Изменить выбор</button></div><div class="sb-review-layout"><div class="sb-purchase-list">${sandboxPurchaseRows(proposal.orders || [])}</div><aside class="sb-payment"><span class="eyebrow">Ваше подтверждение</span><div><span>В активы</span><b>${money(assets)}</b></div><div><span>Комиссия</span><b>${feeKnown?money(proposal.commission):'Неизвестна — покупка недоступна'}</b></div><div><span>Запас на расходы</span><b>${money(Math.max(0,proposal.total-assets-(proposal.commission||0)))}</b></div><div class="sb-payment-total"><span>Бюджет покупки</span><strong>${money(proposal.total)}</strong></div><small>В рублях останется не меньше ${money(Math.max(0,cash))}.</small><button type="button" class="btn sb-buy" data-action="sandbox-execute" ${!feeKnown || proposal.expired?'disabled':''}>${proposal.expired?'Нужно обновить расчёт':'Купить активы '+icon('arrow')}</button><span class="sb-button-caption">${proposal.expired?'Вернитесь к выбору и рассчитайте снова.':'Виртуальный счёт Т-Инвестиций'}</span></aside></div><div class="sb-price-time">Расчёт ${date(proposal.created_at)} · действует до ${date(proposal.expires_at)}${proposal.quote_policy==='sandbox_last_close'?' · последние доступные цены':''}</div></section>`;
}
function sandboxExecution(proposal) {
  const s=state.sandbox, orders=(s.orders||[]).filter(o=>o.proposal_id===proposal.id);
  const filled=orders.filter(o=>o.status==='filled').length;
  const active=orders.some(o=>sandboxActiveStatuses.has(o.status));
  const allFilled=orders.length>0 && filled===orders.length;
  const reconciled=allFilled && s.snapshot_reconciled===true;
  const title=reconciled?'Портфель собран.':allFilled?'Покупки исполнены. Сверяем портфель.':active?'Собираем ваш портфель.':'Проверим результат покупок.';
  const goal=s.demo_config || sandboxConfig();
  return `<section class="sb-execution" id="sandbox-execution"><div class="sb-result-hero ${reconciled?'complete':''}"><span class="sb-result-icon">${reconciled?icon('check'):icon('wallet')}</span><div><span class="eyebrow">${reconciled?'Всё на своих местах':'Т-Инвестиции'}</span><h2>${title}</h2><p>Исполнено ${filled} из ${orders.length}${reconciled?' · позиции сверены':''}</p></div>${reconciled?'<span class="sb-complete-badge">Готово</span>':'<span class="sb-live-dot"></span>'}</div><div class="sb-execution-track">${orders.map(o=>`<div class="sb-order-state ${o.status==='filled'?'filled':''}"><span class="sb-order-state-icon">${o.status==='filled'?icon('check'):'<i></i>'}</span><b>${esc(String(o.ticker||'').replace(/@$/,''))}</b><span>${sandboxStatusNames[o.status] || 'Проверяем'}</span>${o.error?`<small>${esc(o.error)}</small>`:''}</div>`).join('')}</div>${s.reconcile_error?`<div class="sb-inline-error">${esc(s.reconcile_error)}</div>`:''}${reconciled?sandboxResult(goal):`<p class="sb-wait-note">${active?'Статусы обновляются автоматически. Заявки исполняются, когда достигается лимитная цена.':'Обновите счёт, чтобы получить подтверждённые позиции.'}</p><button type="button" class="btn secondary" data-action="sandbox-refresh">Обновить счёт</button>`}</section>`;
}
function sandboxResult(goal) {
  const s=state.sandbox, snapshot=s.snapshot || {}, total=Number(snapshot.total_value||0);
  const positions=snapshot.positions || [], weights={RUB:total?snapshot.cash/total*100:100};
  positions.forEach(p=>weights[p.ticker]=total?Number(p.quantity)*Number(p.price)/total*100:0);
  return `<div class="sb-result-grid"><aside class="sb-result-summary"><span class="eyebrow">Ваш портфель</span><strong>${money(total)}</strong><span>на ${date(snapshot.fetched_at)}</span>${sandboxDonut(weights,num(total,0),'Стоимость счёта')}<div class="sb-cash-result"><span>В рублях</span><b>${money(snapshot.cash)}</b></div></aside><div class="sb-result-assets"><h3>Теперь в вашем портфеле</h3>${sandboxPurchaseRows(positions,true)}<div class="sb-result-goal"><div class="sb-section-title">${icon('target')}<h3>${esc(goal.goal_name)}</h3></div><div class="sb-goal-math">${sandboxGoalMath(total,goal)}</div></div></div></div><div class="sb-finish"><div><b>Попробуйте другой уровень риска</b><span>Новый сценарий начнётся с 10 млн ₽.</span></div><button type="button" class="btn light" data-action="sandbox-reset">Начать заново ${icon('arrow')}</button></div>`;
}
function sandboxPage() {
  const cfg=sandboxConfig(), s=state.sandbox || {}, proposal=s.proposal;
  const currentDemo=Boolean(s.demo_config);
  const reviewing=currentDemo && proposal && !proposal.confirmed_at && !sandboxEditing;
  const completedProposal=proposal || {id:s.proposal_id || s.orders?.[0]?.proposal_id};
  const executing=currentDemo && !reviewing && (s.orders||[]).some(o=>o.proposal_id===completedProposal.id) && !sandboxEditing;
  return `<div class="sandbox-demo"><header class="sb-hero"><div><span class="eyebrow">Песочница · Т-Инвестиции</span><h1>Ваша цель.<br><em>Ваш уровень риска.</em></h1><p>Соберите портфель на 10 млн ₽ и посмотрите, как он устроен.</p></div><div class="sb-hero-actions">${s.account?`<button type="button" class="sb-text-button" data-action="sandbox-reset" ${sandboxHasActiveOrders()?'disabled':''}>Начать заново ↗</button>`:''}<span class="sb-virtual-label">Виртуальный счёт</span></div></header>${sandboxConnect()}${sandboxError?`<div class="sb-inline-error" role="alert">${esc(sandboxError)}</div>`:''}${sandboxProgress?`<div class="sb-loading" role="status"><span class="sb-spinner"></span>${esc(sandboxProgress)}</div>`:''}${reviewing?sandboxReview(proposal):executing?sandboxExecution(completedProposal):sandboxBuilder()}${s.account?`<div class="sb-footer"><a href="/api/sandbox/report" download>История покупок ↗</a><span>Т-Инвестиции · Песочница</span></div>`:''}</div>`;
}
async function sandboxMutate(path, data, target, done) {
  if (sandboxBusy) return;
  const version=++sandboxRequestVersion, owner=state?.user?.email;
  sandboxBusy=true;sandboxError='';
  const buttons=[...document.querySelectorAll('.sandbox-demo button,#sandbox-reset-confirm')].map(b=>[b,b.disabled]);
  buttons.forEach(([b])=>b.disabled=true);
  try {
    const next=await api('sandbox/'+path,data);
    if (version!==sandboxRequestVersion || state?.user?.email!==owner) return;
    sandboxProgress='';updateState(next);
    if (done) await done(next);
    return next;
  } catch (error) {
    if (version===sandboxRequestVersion && state?.user?.email===owner) {
      sandboxProgress='';sandboxError=error.message;
      const box=target?.closest('form')?.querySelector('.form-error');
      if (box) {box.textContent=error.message;box.classList.add('visible');}
      else if (modal.open) formError(error);
      else render();
    }
  } finally {
    if (version===sandboxRequestVersion) {
      sandboxBusy=false;
      buttons.forEach(([b,disabled])=>{if(b.isConnected)b.disabled=disabled;});
      document.querySelectorAll('.sandbox-demo button[disabled]').forEach(b=>{if(b.dataset.busyDisabled)b.disabled=false;});
    }
  }
}
function sandboxRefreshComposition() {
  const chart=document.querySelector('#sandbox-composition'), math=document.querySelector('#sandbox-goal-math');
  if (chart) chart.innerHTML=sandboxComposition();
  if (math) math.innerHTML=sandboxGoalMath();
}
function sandboxResetDialog(andBuild=false) {
  sandboxResetKey=crypto.randomUUID();sandboxResetThenBuild=andBuild;
  showModal('Начать с чистого счёта?', `<form id="sandbox-reset-form">${errorBox}<div class="sb-reset-amount">10 000 000 <small>₽</small></div><p class="modal-subtitle">Создадим новый виртуальный счёт. Прежний портфель и история сохранятся.</p><button id="sandbox-reset-confirm" type="submit" class="btn wide">${andBuild?'Создать счёт и показать покупки':'Начать заново'} ${icon('arrow')}</button></form>`);
}
async function sandboxBuild() {
  if (sandboxBusy) return;
  const form=document.querySelector('#sandbox-goal-form');
  if (form && !form.reportValidity()) return;
  if (!state.sandbox?.connection?.connected) {
    sandboxError='Подключите токен песочницы — и покажем конкретные покупки.';render();
    document.querySelector('#sandbox-token')?.focus();return;
  }
  if ((state.sandbox.snapshot?.positions || []).length) {sandboxResetDialog(true);return;}
  const draft={...sandboxConfig()};sandboxEditing=false;
  if (!state.sandbox.account) {
    sandboxProgress='Создаём счёт с 10 млн ₽';render();
    const created=await sandboxMutate('create',{key:crypto.randomUUID()});
    if (!created) return;
  }
  sandboxProgress='Подбираем лоты и проверяем стоимость покупок';render();
  const result=await sandboxMutate('demo-preview',draft);
  if (result) document.querySelector('#sandbox-review')?.scrollIntoView({block:'start',behavior:'smooth'});
}
async function sandboxAction(action,target) {
  if (sandboxBusy) return;
  if (action==='sandbox-risk') {
    sandboxConfig().strategy_id=target.dataset.strategyId;sandboxEditing=true;sandboxError='';render();return;
  }
  if (action==='sandbox-edit') {sandboxEditing=true;sandboxError='';render();document.querySelector('.sb-builder')?.scrollIntoView({block:'start',behavior:'smooth'});return;}
  if (action==='sandbox-reset') {
    if (!state.sandbox?.connection?.connected) {sandboxError='Подключите токен песочницы, чтобы создать новый счёт.';render();document.querySelector('#sandbox-token')?.focus();return;}
    sandboxResetDialog();return;
  }
  if (action==='sandbox-refresh') return sandboxMutate('refresh',{},target);
  if (action==='sandbox-disconnect') return sandboxMutate('disconnect',{},target);
  if (action==='sandbox-execute') {
    const p=state.sandbox?.proposal;
    if (!p || p.confirmed_at || p.expired) return;
    sandboxProgress='Передаём подтверждённые покупки в Т-Инвестиции';render();
    return sandboxMutate('execute',{proposal_id:p.id},target,()=>document.querySelector('#sandbox-execution')?.scrollIntoView({block:'start',behavior:'smooth'}));
  }
}
document.addEventListener('input', event=>{
  if (event.target.form?.id!=='sandbox-goal-form') return;
  const cfg=sandboxConfig(), key=event.target.name;
  cfg[key]=key==='goal_name'?event.target.value:Number(event.target.value);
  sandboxEditing=true;sandboxRefreshComposition();
},true);
document.addEventListener('change', event=>{
  if (event.target.form?.id!=='sandbox-goal-form') return;
  const cfg=sandboxConfig(), key=event.target.name;
  cfg[key]=key==='goal_name'?event.target.value:Number(event.target.value);
  sandboxEditing=true;sandboxRefreshComposition();
},true);
document.addEventListener('click', event=>{
  const target=event.target.closest('[data-action^="sandbox-"]');
  if (!target) return;
  event.preventDefault();event.stopImmediatePropagation();sandboxAction(target.dataset.action,target);
},true);
document.addEventListener('submit', async event=>{
  const form=event.target;
  if (!['sandbox-connect-form','sandbox-goal-form','sandbox-reset-form'].includes(form.id)) return;
  event.preventDefault();event.stopImmediatePropagation();
  if (!form.reportValidity()) return;
  if (form.id==='sandbox-connect-form') {
    const token=form.elements.token.value;form.elements.token.value='';
    return sandboxMutate('connect',{token},form.querySelector('[type=submit]'));
  }
  if (form.id==='sandbox-goal-form') return sandboxBuild();
  if (form.id==='sandbox-reset-form') {
    const andBuild=sandboxResetThenBuild, draft={...sandboxConfig()};
    const next=await sandboxMutate('reset-demo',{key:sandboxResetKey},form.querySelector('[type=submit]'));
    if (!next) return;
    modal.close();sandboxResetKey=null;sandboxEditing=true;
    if (andBuild) {sandboxDraft=draft;return sandboxBuild();}
    sandboxDraft={...draft};render();document.querySelector('.sandbox-demo')?.scrollIntoView({block:'start',behavior:'smooth'});
  }
},true);

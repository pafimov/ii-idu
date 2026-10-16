const app = document.querySelector('#app');
const modal = document.querySelector('#modal');
const colors = {stocks:'#294d40',bonds:'#90aa79',funds:'#c6d8a4',gold:'#d4bc87',cash:'#e5e9dc'};
const labels = {stocks:'Акции',bonds:'Облигации',funds:'Фонды',gold:'Золото',cash:'Деньги'};
const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  wallet:'<path d="M4 6V5a2 2 0 0 1 2-2h12v4H6a2 2 0 0 1 0-4M4 6v13a2 2 0 0 0 2 2h14V7H6a2 2 0 0 1-2-1Z"/><path d="M20 12h-5v5h5"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  leaf:'<path d="M20 3C8 1 1 9 6 16s16 1 14-13Z"/><path d="m4 21 10-10"/>',
  shield:'<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Z"/><path d="m8 11 3 3 5-5"/>',
  check:'<path d="m5 12 4 4L20 5"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  download:'<path d="M12 3v12m-4-4 4 4 4-4M4 17v4h16v-4"/>',
  upload:'<path d="M12 16V4m-4 4 4-4 4 4M4 17v4h16v-4"/>',
  plus:'<path d="M12 4v16M4 12h16"/>',
  settings:'<circle cx="12" cy="12" r="3"/><path d="m9 3-1 3-3 1-2 3 2 3v4l3 2 3-1 3 3 3-2 1-3 3-1v-4l-3-2-1-3-3-1-3 1Z"/>',
  logout:'<path d="M10 4H4v16h6m-1-8h12m-4-4 4 4-4 4"/>',
  play:'<path d="m8 4 12 8-12 8V4Z"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  edit:'<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z"/>',
  sandbox:'<path d="M4 5h16v4H4zM6 9v10m12-10v10M4 19h16M8 13h8"/>',
};
let state = null;
let route = location.hash.slice(1) || 'overview';
let wizardStep = 0;
let wizardReturnRoute = 'profile';
let wizardDraftKey = '';
let toastTimer;
let pending = false;

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
const logo = (mobile=false) => `<a class="logo ${mobile?'mobile-logo':''}" href="${state?'#overview':'#'}" aria-label="ИИ-ИДУ, главная"><span class="logo-mark"><i></i><i></i><i></i></span>ИИ-ИДУ</a>`;
const money = value => new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0}).format(Number(value)||0)+' ₽';
const num = (value, digits=1) => new Intl.NumberFormat('ru-RU',{maximumFractionDigits:digits}).format(Number(value)||0);
const percent = value => num(value)+'%';
const plural = (n, one, few, many) => n % 100 >= 11 && n % 100 <= 14 ? many : n % 10 === 1 ? one : n % 10 >= 2 && n % 10 <= 4 ? few : many;
function fieldError(key, value) {
  const names={ticker:'Тикер',name:'Название',asset_class:'Класс актива',sector:'Сектор',quantity:'Количество',price:'Цена сейчас',average_price:'Средняя цена',currency:'Валюта',goal_name:'Цель',goal_amount:'Сумма цели',target_amount:'Сумма цели',strategy_id:'Стратегия',horizon_years:'Срок',monthly_contribution:'Пополнение',reserve_months:'Резерв',email:'Почта',password:'Пароль',consent:'Согласие',positions:'Позиции',csv:'CSV'};
  const parts=key.split('.');
  const row=parts[0]==='row'?`Строка ${parts[1]}, `:parts[0]==='positions'&&parts.length>1?`Позиция ${Number(parts[1])+1}, `:'';
  return row+(names[parts.at(-1)]||'Поле')+': '+value;
}
const date = value => value ? new Date(value).toLocaleString('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}) : 'нет снимка';
const amount = p => Number(p.quantity)*Number(p.price);
const selected = (a,b) => a===b?'selected':'';
const checked = v => v?'checked':'';
const errorBox = '<div class="form-error" role="alert"></div>';
const btn = (text,action,style='') => `<button class="btn ${style}" data-action="${action}">${text}</button>`;

async function api(path, data, method='POST') {
  const options = {method,credentials:'same-origin',headers:{}};
  if (method!=='GET') { options.headers['Content-Type']='application/json'; options.body=JSON.stringify(data ?? {}); }
  const response = await fetch('/api/'+path,options);
  const result = await response.json().catch(()=>({error:'Сервер вернул непонятный ответ. Попробуйте ещё раз.'}));
  if (!response.ok) {
    const fields = Object.entries(result.fields || {}).map(([key,value])=>fieldError(key,value)).join(' ');
    const error = new Error((result.error || 'Не удалось выполнить действие.')+(fields?' '+fields:''));
    error.status=response.status;
    throw error;
  }
  return result;
}

function toast(text) {
  const el=document.querySelector('#toast'); el.textContent=text; el.classList.add('visible');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('visible'),4300);
}
function formError(error) {
  const el=modal.querySelector('.form-error');
  if (el) {el.textContent=error.message || error;el.classList.add('visible');el.scrollIntoView({block:'nearest'});} else toast(error.message || error);
}
function showModal(title, content, wide=false, subtitle='') {
  if (modal.open) modal.close();
  modal.innerHTML=`<div class="modal-shell ${wide?'wide-modal':''}"><button class="icon-btn modal-close" data-action="close" aria-label="Закрыть">${icon('close')}</button><div class="eyebrow">ИИ-ИДУ · Личный кабинет</div><h2 id="modal-title">${title}</h2>${subtitle?`<p class="modal-subtitle">${subtitle}</p>`:''}${content}</div>`;
  modal.showModal();
  setTimeout(()=>modal.querySelector('input:not([type=radio]),button:not(.modal-close)')?.focus(),60);
}
function updateState(result) {state=result.state || result;render();}
function navigate(next) {location.hash=next;if(route===next)render();}

function landing() {
  app.innerHTML=`<div class="landing"><header class="land-nav">${logo()}<nav><a href="#how-it-works">Как это работает</a><a href="#principles">Наш подход</a><div class="nav-actions">${btn('Войти','login','ghost')}${btn('Посмотреть пример ↗','demo','secondary')}</div></nav></header>
  <main><section class="land-hero"><div><span class="eyebrow">Инвестиции с ясной головой</span><h1>Ваш портфель.<br><em>На вашей стороне.</em></h1><p class="intro">Узнайте, куда вложены ваши деньги, какой риск вы принимаете и как ваш портфель связан с вашей целью.</p><div class="hero-actions">${btn('Разобраться в портфеле '+icon('arrow'),'register')}${btn('Посмотреть пример','demo','secondary')}</div><p class="micro">Пример доступен без регистрации и подключения брокера.</p><div class="micro" style="margin-top:26px;display:flex;gap:9px;align-items:flex-start">${icon('shield')}<span>Аналитика портфеля и торговля виртуальными деньгами.</span></div></div>
  <div class="hero-art" aria-label="Пример анализа портфеля"><div class="art-label"><span>Всё на своих местах</span><span>01 / Обзор</span></div><div class="preview-card"><div style="display:flex;justify-content:space-between"><h3>Ваш портфель</h3><span class="tag">Пример</span></div><div class="preview-total">500 000 ₽</div><div class="preview-donut"><div class="donut" style="background:conic-gradient(#294d40 0% 30.2%,#90aa79 30.2% 49.8%,#c6d8a4 49.8% 60.3%,#d4bc87 60.3% 88.3%,#e5e9dc 88.3% 100%)"><div class="donut-center"><b>5</b><span>классов активов</span></div></div><div class="preview-legend">${[['stocks',30.2],['bonds',19.6],['funds',10.5],['gold',28],['cash',11.7]].map(([k,v])=>`<div class="legend-row"><span class="dot" style="background:${colors[k]}"></span>${labels[k]}<strong>${v}%</strong></div>`).join('')}</div></div><div class="preview-note">${icon('leaf')}Один взгляд. Понятная картина.</div></div><div class="float-note">Не просто цифры. <b>Связь с вашей целью.</b></div></div></section>
  <section class="landing-section" id="how-it-works"><div class="section-top"><h2>От «что у меня?» к «что дальше?»</h2><span class="eyebrow">Три простых шага</span></div><div class="steps-grid"><div><div class="step-number">01 —</div><h3>Соберите картину</h3><p>Подключите Т-Инвестиции только для чтения или добавьте позиции через CSV и вручную. Вы увидите состав, доли и концентрацию портфеля.</p></div><div><div class="step-number">02 —</div><h3>Расскажите о цели</h3><p>Короткая анкета о сроке, пополнениях и отношении к риску. План останется у вас под рукой.</p></div><div><div class="step-number">03 —</div><h3>Откройте песочницу</h3><p>Изучите сценарии падения и пройдите путь до виртуальной сделки. Каждую заявку можно проверить перед отправкой.</p></div></div></section>
  <section class="landing-section" id="principles"><div class="section-top"><h2>Сначала понимание. Потом действие.</h2>${btn('Открыть пример '+icon('arrow'),'demo','secondary')}</div><p class="micro" style="max-width:760px">Здесь нет прогнозов цены, обещаний доходности и доступа к вашим деньгам. Аналитика использует введённые вами оценки. В песочнице — виртуальные деньги и отдельный счёт. Для реального портфеля доступно персональное подключение Т-Инвестиций только для чтения. Языковая модель не подключена.</p></section></main><footer class="land-footer"><span>ИИ-ИДУ © 2026 · </span><span>Ваша цель. Ваши решения. Понятные цифры.</span></footer></div>`;
}

function navLink(key,label,glyph) {return `<a href="#${key}" class="${route===key?'active':''}" ${route===key?'aria-current="page"':''}>${icon(glyph)}${label}</a>`;}
function render() {
  if(!state?.user){if(typeof syncSandboxPolling==='function')syncSandboxPolling();landing();return;}
  if(route==='practice'){route='sandbox';location.hash='sandbox';}
  if(!['overview','portfolio','profile','sandbox','activity','settings','broker'].includes(route))route='overview';
  const titles={overview:'Обзор',portfolio:'Мой портфель',profile:'Мой профиль',sandbox:'Песочница',activity:'История',settings:'Настройки',broker:'Т-Инвестиции'};
  app.innerHTML=`<div class="shell"><aside class="sidebar">${logo()}<div class="side-caption">МОИ ИНВЕСТИЦИИ</div><nav class="side-nav">${navLink('overview','Обзор','grid')}${navLink('portfolio','Портфель','wallet')}${navLink('profile','Мой профиль','user')}${navLink('broker','Брокер','shield')}${navLink('sandbox','Песочница','sandbox')}</nav><div class="side-bottom"><div class="side-help"><b>Контроль остаётся у вас</b><p>Проверьте параметры заявки до отправки.</p><a href="#activity">История действий ↗</a></div><div class="user-chip"><div class="avatar">${esc(state.user.name?.slice(0,1).toUpperCase() || 'И')}</div><div class="user-text"><b>${esc(state.user.name)}</b><small>${state.user.is_demo?'Демоаккаунт':'Аккаунт'}</small></div><a class="icon-btn" href="#settings" aria-label="Настройки">${icon('settings')}</a></div></div></aside><main class="main"><header class="topbar">${logo(true)}<div class="breadcrumb">Личный кабинет <span>/ &nbsp;${titles[route]}</span></div><div class="top-actions">${state.sandbox?.connection?.connected?'<span class="tag amber">Песочница</span>':''}<a class="btn secondary small" href="${route==='sandbox'?'/api/sandbox/report':'/api/report'}" download>${icon('download')}Отчёт</a><a class="icon-btn" href="#settings" aria-label="Настройки аккаунта">${icon('settings')}</a></div></header><div id="page">${({overview:overview,portfolio:portfolioPage,profile:profilePage,sandbox:sandboxPage,activity:activityPage,settings:settingsPage,broker:brokerPage}[route])()}</div><p class="footnote">ИИ-ИДУ © 2026</p></main></div>`;
  if(typeof syncSandboxPolling==='function')syncSandboxPolling();
}

function heading(title,subtitle,action='') {return `<div class="page-head"><div><h1>${title}</h1><p>${subtitle}</p></div>${action}</div>`;}
function demoBanner() {return state.user.is_demo?`<div class="demo-banner">${icon('info')}<span>${state.portfolio?.source==='demo'?'Примерный портфель. Добавьте свои позиции через «Добавить данные».':'Гостевой доступ. Создайте аккаунт, чтобы продолжить с портфелем и анкетой.'}</span>${btn('Создать аккаунт ↗','register','secondary small')}</div>`:'';}
function emptyPortfolio() {return `<section class="panel empty">${icon('wallet')}<h2>Начнём с вашего портфеля</h2><p>Добавьте активы и денежный остаток. Сразу покажем структуру, доли и возможные точки внимания.</p><div class="empty-actions">${btn(icon('plus')+'Добавить портфель','connect')}${btn('Открыть пример','sample','secondary')}</div><p class="micro" style="margin-top:20px;margin-bottom:0">Поддерживаются рублёвые оценки.</p></section>`;}
function allocationPanel() {
  const allocation=state.analytics?.allocation || [];
  let start=0;const gradient=allocation.filter(a=>a.value>0).map(a=>{const end=start+Number(a.percent);const s=`${colors[a.key]||'#a7b695'} ${start}% ${end}%`;start=end;return s;}).join(',');
  return `<section class="panel"><div class="panel-title"><h2>Из чего состоит портфель</h2><span class="eyebrow" style="font-size:9px">По классам</span></div><div class="allocation-chart"><div class="donut" style="background:conic-gradient(${gradient || '#e7ecdf 0% 100%'})"><div class="donut-center"><b>${allocation.filter(a=>a.value>0).length}</b><span>${plural(allocation.filter(a=>a.value>0).length,'класс активов','класса активов','классов активов')}</span></div></div><div class="preview-legend">${allocation.filter(a=>a.value>0).map(a=>`<div class="legend-row"><span class="dot" style="background:${colors[a.key]||'#a7b695'}"></span>${esc(a.label || labels[a.key])}<strong>${percent(a.percent)}</strong></div>`).join('')}</div></div></section>`;
}
function goalPanel() {
  const p=state.profile,g=state.analytics?.goal;
  if(!p)return `<section class="panel goal-panel"><span class="eyebrow">Не просто инвестировать</span><h2>Двигаться<br>к своей цели.</h2><p style="margin-top:14px">Задайте срок и сумму. Посчитаем, как далеко вас приведут регулярные пополнения без допущений о доходности.</p>${btn('Расскажите о себе '+icon('arrow'),'profile-wizard','light')}<div class="goal-bottom" style="margin-top:23px"><span>3 коротких шага</span>${icon('target')}</div></section>`;
  const progress=g?.progress ?? Math.min(100,(state.analytics?.total_value||0)/p.goal_amount*100);
  return `<section class="panel goal-panel"><span class="eyebrow">Моя большая цель</span><h2>${esc(p.goal_name)}</h2><div class="goal-values"><b>${money(state.analytics?.total_value)}</b><span>из ${money(p.goal_amount)}</span></div><div class="progress" role="progressbar" aria-label="Накоплено на цель" aria-valuenow="${Math.min(100,progress)}" aria-valuemin="0" aria-valuemax="100"><div style="width:${Math.min(100,progress)}%"></div></div><p>Накоплено ${percent(progress)}. При пополнении на ${money(p.monthly_contribution)} в месяц за ${num(p.horizon_years)} лет получится ${money(g?.projected_no_growth)} без учёта доходности и инфляции.</p><div class="goal-bottom"><span>${num(p.horizon_years)} лет · ${money(p.monthly_contribution)} / мес.</span><button class="icon-btn" data-action="profile-wizard" aria-label="Изменить цель">${icon('edit')}</button></div></section>`;
}
function holdingsTable(limit) {
  const positions=[...(state.portfolio?.positions||[])].sort((a,b)=>amount(b)-amount(a));
  return `<div class="table-wrap"><table><thead><tr><th>Актив</th><th class="desktop-col">Класс</th><th>Стоимость</th><th>Доля</th><th class="desktop-col">К цене покупки</th></tr></thead><tbody>${positions.slice(0,limit||100).map(p=>{const share=state.analytics?.total_value?amount(p)/state.analytics.total_value*100:0;const pnl=((p.source_price??p.price)-(p.principal_average_price??p.average_price))*p.quantity;return `<tr><td><div class="asset"><span class="asset-symbol" style="background:${colors[p.asset_class]||'#e5e9dc'}22">${esc(p.ticker?.slice(0,4))}</span><div><b>${esc(p.name)}</b><small>${esc(p.ticker)} · ${num(p.quantity,4)} ${p.asset_class==='cash'?'₽':'шт.'}</small></div></div></td><td class="desktop-col">${labels[p.asset_class]||esc(p.asset_class)}</td><td>${money(amount(p))}</td><td>${percent(share)}<div class="bar-tiny"><i style="width:${Math.min(100,share)}%"></i></div></td><td class="desktop-col ${pnl<0?'negative':'positive'}">${pnl>0?'+':''}${money(pnl)}</td></tr>`;}).join('')}</tbody></table></div>`;
}
function overview() {
  const a=state.analytics||{};
  return `${heading('Всё важное. На одном экране.','Ваш портфель, цель и точки внимания',btn(icon('plus')+'Добавить данные','connect','secondary small'))}${demoBanner()}${brokerSnapshotNotice()}${!state.portfolio?.positions?.length?emptyPortfolio()+`<div class="two-col" style="margin-top:22px">${goalPanel()}<section class="panel"><div class="panel-title"><h2>Что вы увидите</h2></div><p class="paper-intro">Доли активов, отрасли, концентрацию на отдельных бумагах и связь накоплений с вашей целью.</p><p class="micro">Подготовьте количество и рублёвую цену каждой позиции. Историю операций загружать не нужно.</p></section></div>`:`<div class="summary-grid"><div class="stat"><div class="stat-label">Стоимость портфеля ${icon('wallet')}</div><div class="stat-value">${money(a.total_value)}</div><div class="stat-note">${state.portfolio.positions.length} ${plural(state.portfolio.positions.length,'позиция','позиции','позиций')} · ${portfolioSourceLabel()}</div></div><div class="stat"><div class="stat-label">Изменение к цене покупки ${icon('info')}</div><div class="stat-value ${a.pnl<0?'negative':'positive'}">${a.pnl>0?'+':''}${money(a.pnl)}</div><div class="stat-note">${a.pnl_percent===null?'Нет базы для расчёта %':percent(a.pnl_percent)} · без комиссий и выплат</div></div><div class="stat"><div class="stat-label">Акции и фонды ${icon('leaf')}</div><div class="stat-value">${percent(a.risk?.stock_percent)}</div><div class="stat-note">Крупнейшая бумага: ${percent(a.risk?.concentration_percent)}</div></div></div><div class="two-col">${allocationPanel()}${goalPanel()}</div>${insightCards()}<section class="panel"><div class="panel-title"><h2>Активы в портфеле</h2><a href="#portfolio" class="text-link">Все позиции ↗</a></div>${holdingsTable(5)}<div class="panel-foot">${portfolioTimeNote()}</div></section>`}`;
}
function insightCards() {return `<div class="insight-grid">${(state.analytics?.insights||[]).slice(0,3).map(i=>`<article class="insight ${['warning','high','warn'].includes(i.severity)?'warn':''}"><div class="insight-top">${icon(i.severity==='info'?'leaf':'info')}<h3>${esc(i.title)}</h3></div><p>${esc(i.text)}</p></article>`).join('')}</div>`;}
function portfolioPage() {
  const a=state.analytics||{};
  return `${heading('Ваш портфель, по полочкам.','Состав, концентрация и условные сценарии',btn(icon('edit')+'Редактировать','manual','secondary small'))}${demoBanner()}${brokerSnapshotNotice()}${!state.portfolio?.positions?.length?emptyPortfolio():`<section class="panel" style="margin-bottom:22px"><div class="panel-title"><h2>Все активы · ${money(a.total_value)}</h2><a href="/api/portfolio/export" class="text-link" download>Скачать CSV ↗</a></div>${holdingsTable()}<div class="panel-foot">Источник: ${portfolioSourceLabel()} · ${date(state.portfolio.fetched_at||state.portfolio.updated_at)}</div></section><div class="two-col">${allocationPanel()}<section class="panel"><div class="panel-title"><h2>По секторам</h2><span class="eyebrow" style="font-size:9px">Без раскрытия фондов</span></div><div class="sector-list">${(a.sectors||[]).map(s=>`<div><div class="sector-label"><span>${esc(s.label)}</span><b>${percent(s.percent)}</b></div><div class="sector-track"><div style="width:${Math.min(100,s.percent)}%"></div></div></div>`).join('')}</div></section></div><div class="two-col"><section class="panel"><div class="panel-title"><h2>А если рынок упадёт?</h2>${icon('info')}</div><div class="stress-list">${(a.stress||[]).map(s=>`<div class="stress-item"><div class="stress-line"><span>${esc(s.label)}</span><strong class="negative">−${money(Math.abs(s.loss_value))}</strong></div><div class="stress-track"><div style="width:${Math.min(100,Math.abs(s.loss_percent)*3)}%"></div></div><div class="stat-note">Условное изменение портфеля: −${percent(Math.abs(s.loss_percent))}</div><div class="stat-note">${Object.entries(s.assumptions||{}).map(([key,value])=>`${labels[key]} ${Number(value)?'−':''}${percent(value)}`).join(' · ')}</div></div>`).join('')}</div></section><details class="panel"><summary class="panel-title"><h2>Методика расчёта</h2></summary><p class="paper-intro">Доля позиции — её стоимость, делённая на весь портфель. Для оценки концентрации проверяем крупнейшую позицию без денежных остатков. Условный риск высокий при доле акций и фондов выше 60% или одной бумаги выше 40%; умеренный, если любой из этих показателей выше 25%; иначе низкий. Это заданные пороги, без оценки кредитного и других рыночных рисков.</p><p class="micro">Результат к цене покупки = количество × (цена без текущего НКД − средняя цена покупки без текущего НКД). В ручном снимке используются введённые цены. Он не учитывает дивиденды, купоны, продажи, налоги и комиссии; это не доходность за период.</p><p class="micro">Стресс = сумма стоимостей классов × условный шок класса. Исторических данных и оценки волатильности в этой версии нет. Шоки не являются прогнозом или вероятностью; валюты, дефолты, ликвидность и состав фондов не моделируются.</p><a class="btn secondary small" href="/api/report" download>${icon('download')}Скачать расчёты</a></details></div>${insightCards()}`}`;
}
function profilePage() {
  const p=state.profile;
  if(!p)return `${heading('Какая у вас цель?','Давайте познакомим цифры с вашими планами')}<section class="panel empty">${icon('target')}<h2>У каждого портфеля своя задача</h2><p>Ответьте на несколько вопросов о цели, сроке и финансовом резерве. Анкета занимает три коротких шага.</p>${btn('Заполнить профиль '+icon('arrow'),'profile-wizard')}</section>`;
  const risks={low:'Сохранение капитала',medium:'Баланс риска и роста',high:'Готовность к колебаниям'};
  const experiences={none:'Первый опыт',some:'Есть опыт',experienced:'Большой опыт'};
  return `${heading('Деньги работают на вашу цель.','Ответы можно изменить в любой момент',btn(icon('edit')+'Изменить','profile-wizard','secondary small'))}<div class="two-col">${goalPanel()}<section class="panel"><div class="panel-title"><h2>Ваши ответы о риске</h2><span class="tag">Анкета</span></div><div class="info-grid">${[['Отношение к риску',risks[p.risk_tolerance]],['Опыт',experiences[p.experience]],['Резерв',num(p.reserve_months)+' мес.'],['Дорогие долги',p.expensive_debt?'Есть':'Нет'],['При падении портфеля на 20%',{sell:'Предпочту продать',wait:'Подожду',buy:'Рассмотрю пополнение'}[p.loss_reaction]],['Исключённые секторы',(p.excluded_sectors||[]).join(', ')||'Не указаны']].map(([k,v])=>`<div class="info-row"><small>${k}</small><b>${esc(v)}</b></div>`).join('')}</div></section></div><section class="panel"><div class="panel-title"><h2>План без предположений о рынке</h2></div><div class="info-grid"><div class="info-row"><small>При текущих пополнениях к концу срока</small><b>${money(state.analytics?.goal?.projected_no_growth)}</b></div><div class="info-row"><small>Нужно пополнять для цели без роста капитала</small><b>${money(state.analytics?.goal?.monthly_needed)} / месяц</b></div></div><p class="micro" style="margin:20px 0 0">Расчёт: текущий портфель + ежемесячное пополнение × срок в месяцах. Доходность, инфляция, комиссии и изменение взносов не учитываются. Анкета не заменяет профиль, согласованный с регулируемым участником.</p></section>`;
}
function activityPage() {return `${heading('История под рукой.','Изменения портфеля и операции песочницы')}<section class="panel"><div class="panel-title"><h2>События</h2><a class="text-link" href="/api/report" download>Выгрузить снимок ↗</a></div>${(state.events||[]).length?state.events.map(e=>`<div class="timeline-item"><span class="timeline-point"></span><div><b>${esc(e.title||e.kind)}</b><p>${esc(typeof e.detail==='string'?e.detail:JSON.stringify(e.detail||{}))}</p><time>${date(e.created_at)}</time></div></div>`).join(''):'<p class="micro">Пока нет событий. Добавьте портфель или заполните профиль.</p>'}</section>`;}
function settingsPage() {return `${heading('Ваши данные. Ваш контроль.','Управление аккаунтом и выгрузками')}<div class="settings-list"><section class="panel settings-row"><div><h3>${esc(state.user.name)}</h3><p>${esc(state.user.is_demo?'Демо без регистрации':state.user.email)}</p></div>${state.user.is_demo?btn('Создать аккаунт','register','secondary'):btn('Выйти','logout','secondary')}</section><section class="panel settings-row"><div><h3>Скачать свои данные</h3><p>Снимок портфеля, ответы анкеты, расчёты и история в JSON.</p></div><a class="btn secondary small" href="/api/report" download>${icon('download')}Скачать отчёт</a></section><section class="panel settings-row"><div><h3>История действий</h3><p>Изменения профиля, импорты и сделки в песочнице.</p></div><a class="btn secondary small" href="#activity">Открыть историю ↗</a></section><section class="panel settings-row"><div><h3>Удалить аккаунт и данные</h3><p>Удаление портфеля, профиля, истории и текущих сессий необратимо.</p></div>${btn('Удалить данные','delete-account','danger small')}</section>${state.user.is_demo?`<section class="panel settings-row"><div><h3>Завершить демонстрацию</h3><p>Вернуться на первый экран.</p></div>${btn('Выйти из демо','logout','secondary small')}</section>`:''}</div>`;}

function authModal(register=false) {
  showModal(register?'Портфель останется с вами.':'С возвращением.',`<form id="auth-form">${errorBox}${register?`<div class="form-field"><label for="auth-name">Как к вам обращаться</label><input id="auth-name" name="name" autocomplete="given-name" maxlength="60" placeholder="Ваше имя" required></div>`:''}<div class="form-field"><label for="auth-email">Почта</label><input id="auth-email" type="email" name="email" autocomplete="email" maxlength="254" placeholder="you@example.com" required></div><div class="form-field"><label for="auth-password">Пароль</label><input id="auth-password" type="password" name="password" autocomplete="${register?'new-password':'current-password'}" minlength="8" maxlength="128" placeholder="Не менее 8 символов" required></div>${register&&state?.user?.is_demo?selectField('Перенести из гостевого режима','copy_demo','yes',[['yes','Портфель и анкету'],['no','Ничего']]):''}<button class="btn wide" type="submit">${register?'Создать аккаунт':'Войти'} ${icon('arrow')}</button><p class="auth-bottom">${register?'Уже есть аккаунт?':'Впервые здесь?'} <button class="text-link" type="button" data-action="${register?'login':'register'}">${register?'Войти':'Зарегистрироваться'}</button></p></form>`,false,register?'Создайте аккаунт, чтобы продолжить с портфелем и анкетой.':'Войдите, чтобы открыть портфель.');
  modal.querySelector('form').dataset.register=String(register);
  modal.querySelector('form').dataset.returnRoute=route==='broker'?'broker':'';
}
function connectModal() {
  showModal('Как добавим портфель?',`<div class="connect-options"><button class="connect-option" data-action="import">${icon('upload')}<span><b>Загрузить CSV</b><small>Таблица с позициями в нашем формате. Сначала покажем предварительный просмотр.</small></span>${icon('arrow')}</button><button class="connect-option" data-action="manual">${icon('edit')}<span><b>Добавить вручную</b><small>Активы, количество, цена и денежный остаток.</small></span>${icon('arrow')}</button><button class="connect-option" data-action="broker-info">${icon('shield')}<span><b>Подключение Т-Инвестиций</b><small>Реальный состав и цены вашего счёта. Доступ только для чтения.</small></span>${icon('info')}</button></div>`,false,'Выберите чтение Т-Инвестиций или добавьте снимок самостоятельно. Пароль от банка не нужен.');
}


function field(label,name,value,type='text',attrs='') {return `<div class="form-field"><label for="field-${name}">${label}</label><input id="field-${name}" name="${name}" value="${esc(value)}" type="${type}" ${attrs}></div>`;}
function selectField(label,name,value,options) {return `<div class="form-field"><label for="field-${name}">${label}</label><select id="field-${name}" name="${name}">${options.map(([key,text])=>`<option value="${esc(key)}" ${selected(key,value)}>${esc(text)}</option>`).join('')}</select></div>`;}
async function profileWizard(returnRoute='profile') {
  wizardReturnRoute=returnRoute;
  const sandboxProfile=returnRoute==='sandbox';
  if(sandboxBusy)return;
  const owner=state?.user?.email,reviewVersion=++sandboxRequestVersion;let reviewed;sandboxBusy=true;
  try{reviewed=await api('sandbox/profile-review',{});}catch(error){toast(error.message);return;}finally{sandboxBusy=false;}
  if(reviewVersion!==sandboxRequestVersion||state?.user?.email!==owner)return;updateState(reviewed);
  const draftKey='profile-draft-'+(sandboxProfile?'sandbox-':'')+(state.user.email||'demo');wizardDraftKey=draftKey;
  let draft={};try{draft=JSON.parse(sessionStorage.getItem(draftKey)||'{}');}catch{}
  const baseProfile=sandboxProfile?state.sandbox?.profile:state.profile;
  const p={goal_name:'Финансовая свобода',goal_amount:2000000,horizon_years:5,monthly_contribution:15000,risk_tolerance:'medium',experience:'some',reserve_months:3,expensive_debt:false,loss_reaction:'wait',excluded_sectors:[],...baseProfile,...draft};
  wizardStep=0;
  showModal('Что для вас важно?',`<form id="profile-form">${errorBox}<div class="wizard-progress"><i class="done"></i><i></i><i></i></div><section class="wizard-step" data-step="0"><div class="eyebrow" style="margin-bottom:18px">Шаг 1 из 3 · Ваша цель</div>${field('На что хотите накопить?','goal_name',p.goal_name,'text','maxlength="80" required')}<div class="form-grid">${field('Сумма цели, ₽','goal_amount',p.goal_amount,'number','min="1000" max="1000000000" step="100" required')}${field('Срок, лет','horizon_years',p.horizon_years,'number','min="1" max="40" step="1" required')}</div>${field('Ежемесячное пополнение, ₽','monthly_contribution',p.monthly_contribution,'number','min="0" max="10000000" step="100" required')}<p class="field-hint">Не закладываем доходность. Покажем, какой результат дают текущие накопления и пополнения.</p></section><section class="wizard-step" data-step="1" hidden><div class="eyebrow" style="margin-bottom:18px">Шаг 2 из 3 · Финансовая опора</div><div class="form-grid">${field('На сколько месяцев есть резерв?','reserve_months',p.reserve_months,'number','min="0" max="120" step="1" required')}${selectField('Опыт инвестирования','experience',p.experience,[['none','Первый опыт'],['some','Уже инвестирую'],['experienced','Большой опыт']])}</div>${selectField('Есть непогашенный дорогой долг?','expensive_debt',String(p.expensive_debt),[['false','Нет'],['true','Да']])}<p class="field-hint">Для сценария песочницы используем резерв от 3 месяцев и отсутствие дорогих долгов. Это ограничения для доступа к стратегиям песочницы.</p>${field('Исключённые секторы, через запятую','excluded_sectors',(p.excluded_sectors||[]).join(', '),'text','maxlength="200" placeholder="Например: Нефть и газ"')}</section><section class="wizard-step" data-step="2" hidden><div class="eyebrow" style="margin-bottom:18px">Шаг 3 из 3 · Отношение к риску</div><label style="font-size:12px;display:block;margin-bottom:12px">Что вам ближе?</label><fieldset class="choice-fieldset"><legend>Отношение к риску</legend><div class="choice-grid">${[['low','Бережно','Сохранить капитал'],['medium','Сбалансированно','Риск и рост'],['high','С колебаниями','Готов к просадкам']].map(([v,t,s])=>`<label class="choice"><input type="radio" name="risk_tolerance" value="${v}" ${checked(p.risk_tolerance===v)}><b>${t}</b>${s}</label>`).join('')}</div></fieldset>${selectField('Если портфель потеряет 20%, вы скорее…','loss_reaction',p.loss_reaction,[['sell','Предпочту продать активы'],['wait','Подожду и сохраню план'],['buy','Рассмотрю пополнение']])}<div class="confirm-summary" id="profile-summary"></div></section><div class="modal-actions"><button class="btn ghost" type="button" data-action="wizard-back">Отмена</button><button class="btn" type="button" data-action="wizard-next">Дальше ${icon('arrow')}</button></div></form>`,false,'Профиль свяжет накопления, срок и риск. Кнопка «Отмена» закроет анкету.');
  modal.querySelector('#profile-form').addEventListener('input',()=>saveProfileDraft(draftKey));
  modal.querySelectorAll('.wizard-step .eyebrow').forEach(node=>node.tabIndex=-1);
}
function profileData() {
  const fd=new FormData(modal.querySelector('#profile-form'));
  return {goal_name:fd.get('goal_name').trim(),goal_amount:Number(fd.get('goal_amount')),horizon_years:Number(fd.get('horizon_years')),monthly_contribution:Number(fd.get('monthly_contribution')),reserve_months:Number(fd.get('reserve_months')),experience:fd.get('experience'),risk_tolerance:fd.get('risk_tolerance'),loss_reaction:fd.get('loss_reaction'),expensive_debt:fd.get('expensive_debt')==='true',excluded_sectors:fd.get('excluded_sectors').split(',').map(x=>x.trim()).filter(Boolean)};
}
function saveProfileDraft(key='profile-draft-'+(state.user.email||'demo')) {try{sessionStorage.setItem(key,JSON.stringify(profileData()));}catch{}}
function wizardNavigate(step) {
  if(step>wizardStep){const inputs=modal.querySelectorAll(`[data-step="${wizardStep}"] input,[data-step="${wizardStep}"] select`);for(const input of inputs)if(!input.reportValidity())return;}
  if(step<0){sessionStorage.removeItem(wizardDraftKey||'profile-draft-'+(state.user.email||'demo'));modal.close();return;}
  wizardStep=step;modal.querySelectorAll('.wizard-step').forEach(s=>s.hidden=Number(s.dataset.step)!==step);modal.querySelector(`[data-step="${step}"] .eyebrow`)?.focus();
  modal.querySelectorAll('.wizard-progress i').forEach((s,i)=>s.classList.toggle('done',i<=step));
  modal.querySelector('[data-action="wizard-back"]').textContent=step?'Назад':'Отмена';
  const next=modal.querySelector('[data-action="wizard-next"]');next.innerHTML=step===2?'Сохранить профиль '+icon('check'):'Дальше '+icon('arrow');
  if(step===2){const p=profileData();modal.querySelector('#profile-summary').innerHTML=`<p><b>${esc(p.goal_name)}</b> · ${money(p.goal_amount)}</p><p>${num(p.horizon_years)} лет · пополнения ${money(p.monthly_contribution)} / мес.</p><p>Резерв: ${num(p.reserve_months)} мес. · Дорогие долги: ${p.expensive_debt?'есть':'нет'}</p>`;}
}

function manualModal() {
  showModal('Ваши позиции.',`<form id="manual-form">${errorBox}${state.portfolio?.source==='tinvest'?'<div class="notice warning">Ручной ввод заменит брокерский снимок и отключит подключение в этой сессии. Для облигаций в обе цены включён текущий НКД; после ручной правки он отдельно не хранится, а процент изменения считается от введённой средней цены.</div>':''}<div class="notice">Укажите рублёвую цену одной единицы. Для денежного остатка: тикер RUB, количество = сумма, цена и средняя цена = 1. Цены не подгружаются с биржи.</div><div id="manual-rows"></div><button class="btn secondary small" data-action="add-row" type="button">${icon('plus')}Ещё позиция</button><div class="modal-actions">${btn('Отмена','close','ghost')}<button class="btn" type="submit">Обновить портфель ${icon('check')}</button></div><p class="field-hint" style="margin-top:16px">Новый ввод заменит текущий список позиций целиком. Счёт песочницы отделён от портфеля.</p></form>`,true,'Количество, средняя цена покупки и ваша оценка сейчас.');
  const positions=state.portfolio?.positions||[];
  (positions.length?positions:[{ticker:'',name:'',asset_class:'stocks',sector:'Другое',quantity:1,price:'',average_price:''}]).forEach(addManualRow);
}
function addManualRow(p={ticker:'',name:'',asset_class:'stocks',sector:'Другое',quantity:1,price:'',average_price:''}) {
  if(modal.querySelectorAll('.manual-row').length>=100){toast('Не более 100 позиций.');return;}
  const id=crypto.randomUUID();
  const row=document.createElement('div');row.className='manual-row';
  const f=(name,label,value,type='text',attrs='')=>`<div class="form-field"><label for="${id}-${name}">${label}</label><input id="${id}-${name}" data-field="${name}" value="${esc(value)}" type="${type}" ${attrs} required></div>`;
  row.innerHTML=`${f('ticker','Тикер',p.ticker,'text','maxlength="30"')}${f('name','Название',p.name,'text','maxlength="100"')}<div class="form-field"><label for="${id}-class">Класс</label><select id="${id}-class" data-field="asset_class">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${selected(k,p.asset_class)}>${v}</option>`).join('')}</select></div>${f('quantity','Количество',p.quantity,'number','min="0.000001" max="1000000000" step="any"')}${f('price','Цена сейчас, ₽',p.price,'number','min="0" max="1000000000" step="any"')}${f('average_price','Средняя цена, ₽',p.average_price,'number','min="0" max="1000000000" step="any"')}<button class="icon-btn" data-action="remove-row" type="button" aria-label="Удалить позицию">${icon('close')}</button><div class="row-sector">${f('sector','Сектор',p.sector||'Другое','text','maxlength="60"')}</div>`;
  modal.querySelector('#manual-rows').append(row);
}

function importModal() {
  showModal('Один файл. Весь портфель.',`<form id="import-form">${errorBox}<div class="notice">Поддерживается наш формат CSV с рублёвыми ценами. Это не универсальный импорт брокерских отчётов. <a class="text-link" href="/api/sample.csv" download>Скачать образец ↗</a></div><div class="file-zone"><input type="file" id="csv-file" accept=".csv,text/csv" aria-label="Выберите CSV-файл"><p>До 200 КБ · до 100 позиций · запятая или точка с запятой</p></div><div class="form-field"><label for="csv-text">Или вставьте содержимое файла</label><textarea id="csv-text" name="csv" required spellcheck="false" placeholder="ticker,name,asset_class,sector,quantity,price,average_price,currency&#10;RUB,Деньги,cash,Деньги,25000,1,1,RUB"></textarea><span class="field-hint">Классы: stocks, bonds, funds, gold, cash. Валюта: RUB. Цена облигации — рубли за одну бумагу, не проценты номинала.</span></div><button class="btn secondary small" type="button" data-action="preview-import">Проверить и посмотреть</button><div id="import-preview"></div><div class="modal-actions">${btn('Отмена','close','ghost')}<button class="btn" id="import-submit" type="submit" disabled>Заменить портфель</button></div><p class="field-hint" style="margin-top:16px">После подтверждения файл целиком заменит позиции. Ошибка проверки не меняет текущий портфель.</p></form>`,true,'Сначала проверим колонки и покажем первые строки.');
  modal.querySelector('#csv-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;if(file.size>200000){formError(new Error('Файл больше 200 КБ.'));return;}modal.querySelector('#csv-text').value=await file.text();previewImport();});
  modal.querySelector('#csv-text').addEventListener('input',()=>{modal.querySelector('#import-submit').disabled=true;modal.querySelector('#import-preview').textContent='';});
}
function parseCSV(text) {
  text=text.replace(/^\uFEFF/,'');const first=text.split(/\r?\n/)[0];const sep=first.includes(';')?';':',';
  const rows=[];let row=[],cell='',quote=false;
  for(let i=0;i<text.length;i++){const c=text[i];if(c==='"'){if(quote&&text[i+1]==='"'){cell+='"';i++;}else quote=!quote;}else if(c===sep&&!quote){row.push(cell);cell='';}else if(c==='\n'&&!quote){row.push(cell.replace(/\r$/,''));if(row.some(x=>x.trim()))rows.push(row);row=[];cell='';}else cell+=c;}
  if(quote)throw new Error('В CSV не закрыта кавычка.');row.push(cell.replace(/\r$/,''));if(row.some(x=>x.trim()))rows.push(row);return rows;
}
function previewImport() {
  try{const raw=modal.querySelector('#csv-text').value;if(new TextEncoder().encode(raw).length>200000)throw new Error('Данные больше 200 КБ.');const rows=parseCSV(raw),head=rows.shift()?.map(x=>x.trim());const required=['ticker','name','asset_class','sector','quantity','price','average_price','currency'];if(!head||head.length!==required.length||new Set(head).size!==head.length||required.some(k=>!head.includes(k)))throw new Error('Нужны все колонки: '+required.join(', '));if(!rows.length||rows.length>100)throw new Error('CSV должен содержать от 1 до 100 позиций.');if(rows.some(r=>r.length!==head.length))throw new Error('Количество полей в строках не совпадает с заголовком.');modal.querySelector('#import-preview').innerHTML=`<div class="import-preview"><div class="micro" style="margin-bottom:12px">${rows.length} ${plural(rows.length,'позиция','позиции','позиций')} · первые строки. Числа и классы проверит сервер перед заменой портфеля.</div><table><thead><tr>${head.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,5).map(r=>`<tr>${r.map(x=>`<td>${esc(x)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;modal.querySelector('#import-submit').disabled=false;modal.querySelector('.form-error').classList.remove('visible');}catch(error){formError(error);modal.querySelector('#import-submit').disabled=true;}
}

async function act(action,target) {
  switch(action){
    case 'close':modal.close();return;
    case 'login':authModal();return;
    case 'register':authModal(true);return;
    case 'connect':connectModal();return;
    case 'broker-info':modal.close();navigate('broker');return;
    case 'broker-new-token':openBrokerTokenModal();return;
    case 'broker-refresh':return brokerRefresh(target);
    case 'broker-disconnect':return brokerDisconnect(target);
    case 'profile-wizard':return await profileWizard();
    case 'manual':manualModal();return;
    case 'add-row':addManualRow();return;
    case 'remove-row':target.closest('.manual-row').remove();return;
    case 'import':importModal();return;
    case 'preview-import':previewImport();return;
    case 'wizard-back':wizardNavigate(wizardStep-1);return;
    case 'wizard-next':if(wizardStep<2){wizardNavigate(wizardStep+1);return;}modal.querySelector('#profile-form').requestSubmit();return;
    case 'delete-account':showModal('Удалить все данные?',`<form id="delete-form">${errorBox}<p class="micro">Профиль, портфель и операции песочницы будут удалены. Вернуть их из приложения не получится.</p><div class="modal-actions">${btn('Оставить аккаунт','close','secondary')}<button class="btn danger" type="submit">Удалить аккаунт и данные</button></div></form>`);return;
  }
  if(pending)return;pending=true;target.disabled=true;
  try{
    if(action==='demo'){updateState(await api('demo',{}));navigate('overview');}
    if(action==='logout'){invalidateBrokerRequests();invalidateSandboxRequests();if(state.sandbox?.connection?.connected)try{await api('sandbox/disconnect',{});}catch{}await api('logout',{});state=null;location.hash='';render();}
    if(action==='sample'){showModal('Загрузить пример?',`<form id="sample-form">${errorBox}<p class="micro">Вымышленные позиции заменят ваш текущий портфель. Профиль и песочница останутся без изменений.</p><div class="modal-actions">${btn('Отмена','close','secondary')}<button class="btn" type="submit">Загрузить пример</button></div></form>`);}
  }catch(error){formError(error);}finally{pending=false;target.disabled=false;}
}

document.addEventListener('click',event=>{const target=event.target.closest('[data-action]');if(!target)return;event.preventDefault();act(target.dataset.action,target);});
modal.addEventListener('click',event=>{if(event.target===modal){const r=modal.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)modal.close();}});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id.startsWith('broker-')){event.preventDefault();return handleBrokerSubmit(form);}if(!form.closest('#modal'))return;event.preventDefault();if(pending)return;
  if(!form.reportValidity())return;pending=true;
  const submit=form.querySelector('button[type="submit"],[data-action="wizard-next"]');if(submit)submit.disabled=true;
  try{
    const fd=new FormData(form);
    if(form.id==='auth-form'){invalidateBrokerRequests();invalidateSandboxRequests();updateState(await api(form.dataset.register==='true'?'register':'login',{name:fd.get('name')?.trim(),email:fd.get('email').trim(),password:fd.get('password'),consent:true,copy_demo:fd.get('copy_demo')==='yes'}));modal.close();navigate(form.dataset.returnRoute || (state.profile?'overview':'profile'));toast(form.dataset.register==='true'?'Аккаунт создан.':'Вы вошли в аккаунт.');}
    if(form.id==='profile-form'){const sandboxProfile=wizardReturnRoute==='sandbox';updateState(await api(sandboxProfile?'sandbox/profile':'profile',{...profileData(),...(sandboxProfile?{}:{confirmed:true})}));sessionStorage.removeItem(wizardDraftKey||'profile-draft-'+(state.user.email||'demo'));modal.close();navigate(wizardReturnRoute);toast(sandboxProfile?'Цель сохранена. Выберите стратегию.':'Профиль обновлён. Добавьте портфель или откройте песочницу.');}
    if(form.id==='manual-form'){const positions=[...form.querySelectorAll('.manual-row')].map(row=>{const p={currency:'RUB'};row.querySelectorAll('[data-field]').forEach(input=>p[input.dataset.field]=['quantity','price','average_price'].includes(input.dataset.field)?Number(input.value):input.value.trim());return p;});updateState(await api('portfolio',{positions},'PUT'));modal.close();navigate('portfolio');toast('Портфель обновлён. Аналитика пересчитана.');}
    if(form.id==='import-form'){updateState(await api('portfolio/import',{csv:fd.get('csv')}));modal.close();navigate('portfolio');toast('Портфель загружен. Аналитика готова.');}
    if(form.id==='delete-form'){if(state.sandbox?.connection?.connected)await api('sandbox/disconnect',{});await api('account',{confirmed:true},'DELETE');state=null;sessionStorage.clear();modal.close();location.hash='';render();toast('Аккаунт и его данные удалены.');}
    if(form.id==='sample-form'){updateState(await api('portfolio/sample',{}));modal.close();navigate('overview');toast('Загружен вымышленный портфель.');}
  }catch(error){formError(error);}finally{pending=false;if(submit)submit.disabled=false;}
});
window.addEventListener('hashchange',()=>{if(!state)return;route=location.hash.slice(1)||'overview';render();window.scrollTo({top:0,behavior:'instant'});});
async function boot(){try{state=await api('state',undefined,'GET');render();}catch(error){if(route==='sandbox'&&error.status===401){try{state=await api('demo',{});render();return;}catch{}}if(error.message.includes('Failed to fetch'))toast('Не удалось связаться с сервером. Обновите страницу.');state=null;landing();if(route==='broker'&&error.status===401)authModal(false);}}
boot();

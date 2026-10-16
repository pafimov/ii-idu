let brokerRequestVersion = 0;

function invalidateBrokerRequests() { brokerRequestVersion += 1; }
function portfolioSourceLabel() {
  return {demo:'Пример',csv:'CSV',manual:'Ручной ввод',tinvest:'Т-Инвестиции · чтение'}[state.portfolio?.source] || 'Снимок';
}
function brokerSnapshotNotice() {
  const p=state.portfolio;
  if(p?.source!=='tinvest') return '';
  const b=state.broker||{}, warnings=p.warnings||[];
  return `<div class="notice broker-snapshot"><div class="broker-notice-line"><span><b>${esc(p.account_name||'Счёт Т-Инвестиции')}</b><br>${money(p.broker_total)} · ${date(p.fetched_at||p.updated_at)}</span>${b.connected&&b.account_id===p.account_id?btn('Обновить','broker-refresh','secondary small'):'<a class="btn secondary small" href="#broker">Подключить</a>'}</div><details><summary>Особенности цен</summary><p class="micro">Время получения не показывает свежесть котировок. На закрытом рынке возможна цена последней сессии. Стоимость облигаций включает НКД; изменение к цене покупки рассчитано без текущего НКД, купонов и расходов.</p></details>${b.last_error?`<p class="negative">${esc(b.last_error)} Портфель не изменён.</p>`:''}${warnings.length?`<details><summary>Детали данных: ${warnings.length}</summary><ul>${warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}</div>`;
}
function portfolioTimeNote() {
  return state.portfolio?.source==='tinvest'?`Снимок: ${date(state.portfolio.fetched_at||state.portfolio.updated_at)}.`:`Добавлено: ${date(state.portfolio.updated_at)}.`;
}
function brokerTokenForm() {
  return `<form id="broker-connect-form">${errorBox}<div class="form-field"><label for="broker-token">Read-only токен Т-Инвестиции</label><input id="broker-token" name="token" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="2048" required placeholder="Вставьте токен"><span class="field-hint"><a class="text-link" href="https://developer.tbank.ru/invest/intro/intro/token" target="_blank" rel="noopener noreferrer">Как получить токен ↗</a>. Токен используется только для чтения счёта и очищается после отправки.</span></div><button class="btn wide" type="submit">Проверить доступ ${icon('arrow')}</button></form>`;
}
function brokerPage() {
  const b=state.broker||{}, p=state.portfolio||{};
  const intro=`<div class="notice">${icon('shield')} Доступ только для чтения. Заявки и переводы недоступны.</div>`;
  const instructions=`<section class="panel"><div class="panel-title"><h2>Подключение</h2></div><ol class="broker-steps"><li><a class="text-link" href="https://www.tbank.ru/invest/settings/" target="_blank" rel="noopener noreferrer">Откройте настройки Т-Инвестиций ↗</a> и создайте токен T-Invest API.</li><li>Выберите права Read-only и доступ к нужному счёту.</li><li>Вставьте токен и выберите счёт для анализа.</li></ol><a class="text-link" href="https://developer.tbank.ru/invest/intro/intro/token" target="_blank" rel="noopener noreferrer">Инструкция по токенам ↗</a></section>`;
  let content;
  if(state.user.is_demo) content=`<section class="panel empty">${icon('user')}<h2>Войдите, чтобы подключить счёт</h2><p>Подключение Т-Инвестиций доступно в аккаунте.</p><div class="empty-actions">${btn('Создать аккаунт','register')}${btn('Войти','login','secondary')}</div></section>`;
  else if(!b.connected) content=`<section class="panel"><div class="panel-title"><h2>Подключить Т-Инвестиции</h2><span class="tag amber">Только чтение</span></div>${p.source==='tinvest'?'<p class="micro">Показывается последний снимок. Для обновления подключитесь снова.</p>':''}${brokerTokenForm()}</section>`;
  else content=`<section class="panel"><div class="panel-title"><h2>Выберите счёт</h2><span class="tag">Только чтение</span></div><form id="broker-select-form">${errorBox}<div class="form-field"><label for="broker-account">Счёт для анализа</label><select id="broker-account" name="account_id" required>${(b.accounts||[]).map(a=>`<option value="${esc(a.id)}" ${selected(a.id,b.account_id)}>${esc(a.name)} · …${esc(a.id.slice(-4))}</option>`).join('')}</select></div><button class="btn wide" type="submit">Загрузить этот счёт ${icon('download')}</button></form>${b.account_id?`<div class="broker-current"><b>${esc(b.account_name)}</b><p class="micro">${p.source==='tinvest'&&p.account_id===b.account_id?'Последний снимок: '+date(p.fetched_at):'Показан портфель из другого источника.'}</p>${btn('Обновить снимок','broker-refresh','secondary small')} <a class="btn ghost small" href="#portfolio">Открыть аналитику ↗</a></div>`:''}<div class="modal-actions">${btn('Заменить токен','broker-new-token','ghost small')}${btn('Отключить','broker-disconnect','danger small')}</div>${b.last_error?`<div class="notice amber">${esc(b.last_error)} Портфель не изменён.</div>`:''}</section>`;
  return `${heading('Портфель из Т-Инвестиций','Обновление вручную · только чтение')}${intro}<div class="two-col">${content}${instructions}</div>${p.source==='tinvest'?brokerSnapshotNotice():''}<p class="field-hint">Отключение удаляет токен из текущей сессии. Чтобы отозвать его у брокера, удалите токен в настройках Т-Инвестиций. Неподдерживаемый состав отклоняется целиком.</p>`;
}
function openBrokerTokenModal() { showModal('Новый read-only токен',brokerTokenForm(),false,'Текущий снимок портфеля не изменится.'); }
async function brokerMutation(path,data,target,onSuccess) {
  const version=++brokerRequestVersion, owner=state?.user?.email;
  if(target)target.disabled=true;
  try {const result=await api('broker/'+path,data);if(version!==brokerRequestVersion||state?.user?.email!==owner)return;updateState(result);if(onSuccess)onSuccess();}
  catch(error){if(version!==brokerRequestVersion||state?.user?.email!==owner)return;const box=target?.closest('form')?.querySelector('.form-error');if(box){box.textContent=error.message;box.classList.add('visible');box.scrollIntoView({block:'nearest'});}else toast(error.message);}
  finally {if(target)target.disabled=false;}
}
async function handleBrokerSubmit(form) {
  if(!form.reportValidity())return;
  const target=form.querySelector('button[type=submit]'),data=new FormData(form);
  if(form.id==='broker-connect-form'){
    const input=form.querySelector('[name=token]'),token=input.value;input.value='';
    await brokerMutation('connect',{token,consent:true},target,()=>{if(modal.open)modal.close();navigate('broker');toast('Выберите счёт для анализа.');});
  } else if(form.id==='broker-select-form') {
    await brokerMutation('select',{account_id:data.get('account_id'),consent:true},target,()=>{navigate('portfolio');toast('Снимок сверён с итогом брокера.');});
  }
}
async function brokerRefresh(target) {await brokerMutation('refresh',{},target,()=>toast('Снимок обновлён. Свежесть котировок может отличаться.'));}
async function brokerDisconnect(target) {await brokerMutation('disconnect',{},target,()=>{if(modal.open)modal.close();navigate('broker');toast('Доступ отключён. Для отзыва удалите токен в кабинете Т-Инвестиций.');});}

# Требования и матрица проверок sandbox MVP

## Обновление 0.4.0 / D-010

Новый sandbox demo намеренно использует отдельный риск/goal config без полной анкеты и без Apply/Resume/Stop в UI. Одно подтверждение точного proposal, policy/config binding и все проверки каждой отправки сохранены. Это не выполнение полного FR-2 suitability и не изменение исходных DOCX; реальная модель остаётся целевой.

В текущей поставке четыре risk choices, live allocation preview, простой goal form, точные покупки/комиссия, actual statuses, snapshot_reconciled и новый старт10млн. reset сохраняет старый счёт/аудит и блокируется при неизвестном исходе. 132 теста OK/9,700с; браузер/FakeSandbox путь и360px пройдены. Детали [VERIFICATION.md](VERIFICATION.md). Таблицы ниже сохраняют трассировку всей целевой спецификации и исторического профильного API, не объявляют demo полным покрытием.

**Статус:** соответствие реализации целевым PRD/SRS/US/RTM, 16.10.2026. Результаты 132 программных теста и фактического sandbox-прогона приведены в [VERIFICATION.md](VERIFICATION.md). Эта карта сохраняет исходные критерии и отдельно отмечает незавершённые части.

## Использованные требования

Прочитаны все четыре текущих файла редакции 2026-10-12:

- `output/requirements/2.1_PRD.docx` — ценность, цели BG/M, границы и пилотные пороги;
- `output/requirements/2.2_SRS.docx` — 13 FR, 8 NFR и 55 критериев приёмки;
- `output/requirements/2.3_UserStories.docx` — US-1–US-13 и те же критерии;
- `output/requirements/2.4_RTM.docx` — трассировка требований и 66 вариантов проверок в 21 группе.

Эта карта показывает, что архитектурный срез должен поддержать и где sandbox может дать ограниченное свидетельство. Она не заменяет выполнение всей RTM. Отдельный сценарий пользователя «стартовые 10 млн ₽ → цель → стратегия → sandbox» является тестовым входом, а не новой величиной M-3: PRD задаёт 2 млн ₽ под управлением на 30-й день учебного пилота. Стартовый баланс виртуальный; четыре исполнения подтверждены именно sandbox API и не учитываются как реальные активы или результат пилота.

## Что уже работает и что остаётся

| Требования | Реализовано в поставке | Следующий целевой этап |
|---|---|---|
| FR-1 | Аккаунт, привязка sandbox-счёта, read-only реального портфеля, изоляция владельцев | Delegated identity/consent, согласованный production contract |
| FR-2 | Трёхшаговый отдельный профиль, подтверждение и версии | Диалог и модель индивидуальной пригодности |
| FR-3 | Четыре фиксированные стратегии, ограничения, резерв, версии правил | Утверждённые universe/risk policy и персональная модель |
| FR-4–FR-6 | Точные предложения/расходы, отдельное согласие, fingerprint, повторный gate каждой отправки | Production source freshness и контрактные проверки реального adapter |
| FR-7 | Durable UUID, статусы/частичные количества, unknown blocks, worker и сверка позиций | Более широкие failure/contract tests, real-money execution/reconciliation |
| FR-8 | Позиции, состав, отклонения, объяснения из чисел, план взносов | История результатов и утверждённая методология риска/benchmark |
| FR-9 | Первичное виртуальное пополнение, расчёт/предложение ребалансировки, продажи перед покупками | Произвольные пополнения в UI и end-to-end сценарии последовательного sell/buy |
| FR-10 | Пересмотр останавливает неотправленное; новые версии/согласие | Полная приёмка сценариев смены цели |
| FR-11 | Индивидуальные Stop/Resume и отмена активного остатка | Общая остановка с ролью сотрудника |
| FR-12 | Owner-scoped append-only API audit и выгрузка | Роли сотрудника, защищённое хранение и retention; каскад удаления аккаунта требует решения |
| FR-13 | События sandbox хранятся отдельно от реального портфеля | Полный сбор M-1–M-4, дедупликация, daily/day-30 snapshots и пилот |
| NFR-1/5/6 | Health, persisted ledger/stop, блокировка при ошибке | Нагрузка, 30-дневное окно, failure injection/DR за час |
| NFR-2/3/4 | Sandbox quote policy, UUID/no auto retry, session/owner guards, временные секреты | Production timestamps, полная recovery suite, SSO/RBAC/пентест |
| NFR-7/8 | LLM не авторизует заявки; мобильный UI проверен на 360 px | Диалог/redaction/adversarial suite и пользовательское исследование |

## Функциональная трассировка

| Требование SRS | Архитектурный модуль и инвариант | Основные проверки RTM | Граница sandbox |
|---|---|---|---|
| FR-1, подключение счёта | Auth/account binding; owner-scoped broker adapter; реальный адаптер read-only, sandbox account отдельный | TC-1.1–1.5; AC-1.1–1.4 | Можно проверить выбор sandbox account и изоляцию; не подтверждается реальная авторизация или раскрытие production данных |
| FR-2, цель и профиль | Profile service; неподтверждённые ответы не меняют active profile; append-only версии | TC-2.1–2.3; AC-2.1–2.2; TC-10.1 | Можно проверить поток данных и версии на synthetic profile; не доказывает suitability для реального клиента |
| FR-3, правила портфеля | Versioned policy registry + deterministic strategy engine; невозможные ограничения не ослабляются | TC-3.1–3.2; AC-3.1–3.2; TC-10.1 | Sandbox демонстрирует расчёт на заданных входах; universe/risk policy ещё требуют владельцев и согласования |
| FR-4, proposal и расходы | Immutable proposal; точные активы/цена/количество/fees/validity/hash; неизвестные обязательные расходы блокируют отправку | TC-4.1–4.3; AC-4.1–4.2; TC-9.5; TC-10.1 | Sandbox подтверждает UX и snapshot/hash; не подтверждает реальные цены, комиссии и плату сервиса |
| FR-5, согласие и отправка | Confirmation binding к owner+account+proposal hash+versions+expiry; один явный UI action на данную заявку/серию | TC-5.1–5.5; AC-5.1–5.4; TC-9.5; TC-10.1; TC-11.4 | Отдельное sandbox подтверждение не даёт production полномочий; без точного действующего подтверждения сетевого вызова нет |
| FR-6, pre-trade проверки | Deterministic policy gate на каждую заявку; результаты и input versions сохраняются; fail-closed | TC-6.1–6.4; AC-6.1–6.2; TC-9.5; TC-10.1; TC-11.4 | Можно проверять синтетические лимиты и остановку; нельзя заявить выполнение NFR-2 без source timestamps |
| FR-7, статусы и сверка | Durable order ledger + state machine + scheduler/reconciler; unknown блокирует счёт; принятие не равно исполнению | TC-7.1–7.3; AC-7.1–7.2; TC-N3.2; TC-N6.1 | Можно проверить sandbox lifecycle по контракту; не подтверждает реальные fills/settlement и производственную сверку |
| FR-8, портфель и объяснения | Portfolio projection из подтверждённых snapshots/fills; explanation из сохранённых данных | TC-8.1–8.3; AC-8.1–8.2; TC-N1.1 | Synthetic portfolio должен быть маркирован; sandbox не доказывает market outcome или доходность |
| FR-9, пополнение и ребалансировка | Scheduler создаёт только proposal, не отправляет; любой proposal проходит FR-4/5/6 | TC-9.1–9.5; AC-9.1–9.3 | Сценарно проверяется trigger/расчёт; автоматическое sandbox-исполнение требует отдельной явной кнопки и точного согласия |
| FR-10, смена цели | Version transition; остановка pending unsent intents; новые policy/proposal/consent обязательны | TC-10.1–10.2; AC-10.1–10.2 | Проверяется локально/sandbox; уже отправленные заявки остаются на reconciliation |
| FR-11, остановка | Persistent stop flag сначала блокирует новые permits; account/global scope + role checks; cancel только доступных заявок | TC-11.1–11.5; AC-11.1–11.5; TC-N4.4 | Можно проверить локальный и sandbox stop path; нельзя обещать отмену заявки только по disconnect или logout |
| FR-12, журнал | Append-only audit; owner/role access; correction links to source event | TC-12.1–12.3; AC-12.1–12.3; TC-N4.1; TC-N6.1 | Локальный журнал не эквивалентен production audit storage/retention controls |
| FR-13, pilot events and metrics | Event IDs/dedup; daily/day-30 snapshots; event-derived M-1–M-4 | TC-13.1–13.5; AC-13.1–13.4 | Синтетические события только для тестов. Они не считаются участниками, активами, сделками или показателями пилота |

## Нефункциональная трассировка и известные пробелы

| Требование SRS | Архитектурная опора | Связанные проверки | Ограничение/готовность |
|---|---|---|---|
| NFR-1, response time (2/5/15 сек. p95 при 10 пользователях) | Измеряемые API/engine spans; раздельное время внешнего API | TC-N1.1 | Нужен нагрузочный стенд и реальные измерения; sandbox latency не гарантирует prod latency |
| NFR-2, source freshness: snapshot 60 сек., quote 30 сек. | Поля `source_as_of`/sequence и отдельный `fetched_at`; блокировка при неизвестном возрасте | TC-N2.1–N2.3 | **Открытый production contract:** для доступного Portfolio API время получения не является source time. Повторный fetch не обнуляет возраст. До контрактного timestamp от брокера/market-data провайдера real order gate остаётся закрытым. Sandbox snapshot time не закрывает этот пробел |
| NFR-3, идемпотентность | Durable intent, unique proposal-step, стабильный UUID, reconcile before retry | TC-N3.1–N3.2 | Sandbox полезен для отказов/повторов; проверять поведение idempotency API контрактными тестами |
| NFR-4, доступ/секреты | Owner-scoped auth, least privilege, separate RAM vaults, scrubbed logs, role checks | TC-N4.1–N4.4 | Нужны threat model, тесты межпользовательской изоляции и реального retention/storage контура |
| NFR-5, uptime 99% за 30 суток и fail-closed | Наблюдаемость, health checks, stop path, closed gate при dependency failure | TC-N5.1–N5.2 | Не доказывается unit/integration тестами; 30-дневного эксплуатационного окна пока нет |
| NFR-6, восстановление за 1 час | Persisted state, startup reconciliation, event replay, unknown blocks | TC-N6.1 | Нужен failure injection и проверка восстановления, включая потерю ответа и partial fill |
| NFR-7, LLM boundary | Нет LLM route к policy decision/order submission; outputs не autoritative | TC-N7.1–N7.3 | Текущий расчёт должен быть детерминированным; если LLM добавят, нужен adversarial suite |
| NFR-8, 360 px usability/stop within 2 actions | UI review at target width and usability test | TC-N8.1–N8.2 | Visual verification на 360 px пройдена: ширина документа 360 px, кнопка Stop доступна. Исследование понимания с участниками не выполнено |

Особенно важно для sandbox freshness: можно отдельно задать сценарную политику, например запретить sandbox заявку при старше одного торгового дня портфельном snapshot или при отсутствии актуальных котировок. Результат должен иметь ярлык `sandbox`, указывать доступный timestamp и не утверждать прохождение AC-N2.1/AC-N2.2. Дневная политика не эквивалентна лимитам SRS 60/30 секунд.

## Минимальная sandbox acceptance suite

Эти проверки добавляются к покрытию SRS; сами по себе они не заменяют 66 вариантов RTM.

| ID | Проверка и ожидаемый результат |
|---|---|
| SB-1 Host isolation | Sandbox client допускает только точный sandbox hostname и фиксированные SandboxService methods. Неверный host/configuration завершает запрос отказом; ни DNS/path/redirect/exception path не ведёт к prod. |
| SB-2 Credential separation | Sandbox vault принимает sandbox secret только в памяти и с owner/session binding. Secret отсутствует в DB, responses, state, audit, reports, exception text и логах; logout/disconnect/restart удаляют credential. Read-only vault не передаёт token в sandbox adapter; права введённого sandbox token проверяются API брокера, тип token не определяется по его строке. |
| SB-3 Strategy reproducibility | Одинаковые входы дают одинаковый расчёт состава/количеств. Hash фиксирует конкретное предложение с его ID и временем. 10 млн ₽ — стартовый виртуальный баланс; арифметика цели не подставляет прогноз доходности. |
| SB-4 Immutable consent | Любое изменение account, состава, количества, цены/лимита, profile/policy version или expiry требует нового proposal и подтверждения; отсутствие/отзыв подтверждения не вызывает сеть. |
| SB-5 UUID and double submission | До вызова устойчиво записан один UUID на proposal-step. Двойной клик/HTTP retry переиспользует intent, а уникальное ограничение предотвращает второй UUID на тот же шаг. Timeout переводит в unknown, не создаёт новый intent. |
| SB-6 Accepted vs filled | Ответ accepted/NEW не обновляет executed quantity и portfolio. Только подтверждённый order state/fills обновляют исполненный объём; partial quantities и остаток разделены. |
| SB-7 Unknown/reconciliation | Неизвестный response/status, inconsistent snapshot или timeout удерживают account gate. Reconcile читает по тому же order ID; повторная отправка заблокирована до однозначной сверки. |
| SB-8 Stop/logout race | Остановка атомарно запрещает неотправленные intents; racing stale response не переоткрывает выполнение. Logout очищает secret, но уже отправленная заявка остаётся в ledger до reconcile. |
| SB-9 UI action boundary | На интерфейсе только компактный sandbox badge и явная кнопка конкретного предложения; лишние checkboxes/учебные и технические тексты убраны из главного пути и остаются в документации. |
| SB-10 Event/KPI labeling | Повторная доставка dedup по event ID; отдельный duplicate order не схлопывается. Synthetic sandbox fills/events не включаются в реальный M-1–M-4; target не отображается как измеренный факт. |
| SB-11 Freshness contract | `fetched_at` и `source_as_of` не смешиваются. При отсутствующем production source timestamp real execution блокируется; sandbox-specific daily rule явно маркируется как отдельная проверка. |

## Набор RTM, который надо сохранить

В таблице перечислены все 21 группы и варианты текущей RTM; каждый вариант должен иметь исполнение/артефакт результата перед заявлением о полном покрытии.

| Группа RTM | Варианты |
|---|---|
| TC-1 подключение счёта | TC-1.1–1.5 |
| TC-2 профиль | TC-2.1–2.3 |
| TC-3 правила портфеля | TC-3.1–3.2 |
| TC-4 proposal/расходы | TC-4.1–4.3 |
| TC-5 согласие/отправка | TC-5.1–5.5 |
| TC-6 проверки перед поручением | TC-6.1–6.4 |
| TC-7 статусы/сверка | TC-7.1–7.3 |
| TC-8 portfolio/explanations | TC-8.1–8.3 |
| TC-9 пополнение/ребалансировка | TC-9.1–9.5 |
| TC-10 изменение цели/версий | TC-10.1–10.2 |
| TC-11 остановка/возобновление/global stop | TC-11.1–11.5 |
| TC-12 audit/access | TC-12.1–12.3 |
| TC-13 пилотные показатели | TC-13.1–13.5 |
| TC-N1 latency/load | TC-N1.1 |
| TC-N2 freshness | TC-N2.1–N2.3 |
| TC-N3 duplicate/recovery | TC-N3.1–N3.2 |
| TC-N4 access/secrets/roles | TC-N4.1–N4.4 |
| TC-N5 availability/fail-closed | TC-N5.1–N5.2 |
| TC-N6 restoration | TC-N6.1 |
| TC-N7 LLM boundaries | TC-N7.1–N7.3 |
| TC-N8 mobile usability | TC-N8.1–N8.2 |

## Статус доказательств

Ниже приведена первичная навигационная связь проверок с текущими файлами. Наличие кода или теста не доказывает полный проход требования; результаты тестов относятся только к ревизии, на которой их запускали. Пройдены 132 unit/service tests; sandbox-вызовы в них подменены fake gateway. Отдельно пройден фактический sandbox-сценарий: 10 млн, четыре filled, сверка всех количеств и денег после перезапуска. Свидетельства разделены в [VERIFICATION.md](VERIFICATION.md). Нагрузочная проверка, 10-пользовательский p95, 30-дневная доступность, восстановление по NFR-6 и полный проход RTM не заявлены.

| Sandbox-проверка | Текущая реализация | Наблюдаемые тесты и пробелы |
|---|---|---|
| SB-1 Host isolation | `mvp/sandbox.py` | `test_fixed_sandbox_host_tls_no_proxy_redirect_and_write_allowlist`; не доказывает доступность реального sandbox API или поведение при авариях внешнего сервиса |
| SB-2 Credential separation | `mvp/broker_sessions.py`, `mvp/app.py`, `mvp/sandbox.py` | `test_vault_and_owner_sessions_are_isolated`, `test_no_broker_secret_or_raw_session_is_in_database_or_public_state`, `test_http_errors_and_native_exception_text_never_leak_token`; не является аудитом памяти/логов production-хоста |
| SB-3 Strategy reproducibility | `mvp/portfolio_policy.py`, `mvp/sandbox_service.py` | `test_rules_expire_after_thirty_days_and_keep_explicit_reserve`, `test_rebalancing_uses_whole_lots_and_preserves_target_cash`; веса фиксированы; отдельная арифметика взносов оценивает разрыв до цели без доходности, инфляции и расходов |
| SB-4 Immutable consent | `mvp/sandbox_store.py`, `mvp/sandbox_service.py`, `mvp/static/sandbox.js` | Код сохраняет и сверяет SHA-256 fingerprint при подтверждении и отправке; `test_tampered_proposal_quantity_with_same_id_cannot_be_confirmed`, `test_tampered_proposal_price_cannot_reuse_existing_confirmation`, `test_ledger_quantity_or_price_cannot_change_approved_proposal`. Это прикладная проверка целостности, не пользовательская цифровая подпись; в браузере проверены показанные количества/цены/расходы и отдельное подтверждение серии |
| SB-5 UUID and double submission | `mvp/sandbox_store.py`, `mvp/sandbox_service.py`, `mvp/sandbox.py` | `test_request_uuid_and_submitting_record_exist_before_post`, `test_unknown_post_preserves_uuid_blocks_series_and_is_reconciled`, `test_post_order_is_limit_with_saved_uuid_and_never_retries`; контрактные повторы на реальном sandbox отдельно не подтверждены |
| SB-6 Accepted vs filled | `mvp/sandbox.py`, `mvp/sandbox_store.py`, `mvp/sandbox_service.py` | `test_accepted_order_is_not_filled_and_blocks_next_order`, `test_series_continues_in_reviewed_order_only_after_fill`, `test_ledger_does_not_reverse_terminal_statuses`; нужны дополнительные проверки частичных исполнений и всех допустимых переходов |
| SB-7 Unknown/reconciliation | `mvp/sandbox_service.py`, `mvp/sandbox_store.py` | `reconcile_positions` сравнивает снимок с подтверждёнными исполненными количествами; `test_unknown_post_preserves_uuid_blocks_series_and_is_reconciled`, `test_lagging_filled_positions_block_publication_and_new_sends`. Длительная недоступность и все варианты неизвестного ID не покрыты |
| SB-8 Stop/logout race | `mvp/sandbox_service.py`, `mvp/broker_sessions.py`, `mvp/app.py` | Финальный gate отправки и отдельный guard отмены используют owner lock/vault; `test_stop_during_service_precheck_prevents_post`, `test_stop_during_client_precheck_prevents_post_at_final_gate`, `test_restart_preserves_stop_and_historical_request_ids`. `test_stop_returns_during_admitted_post_and_prevents_next_post` и `test_public_states_remain_available_while_other_owner_post_waits` дополнительно проверяют Stop во время допущенного POST и чтение другого владельца. Полное пространство межпоточных logout/disconnect/cancel не доказано |
| SB-9 UI action boundary | `mvp/static/sandbox.js` | Пройден фактический UI-путь и 360 px; checkbox нет, детали свёрнуты, режим песочницы обозначен. Исследования на участниках пока нет |
| SB-10 Event/KPI labeling | `mvp/sandbox_store.py`, `mvp/sandbox_service.py` | Есть локальный sandbox audit; отдельный сбор M-1–M-4 и исключение synthetic-событий из реального пилота в этих модулях не реализованы |
| SB-11 Freshness contract | `mvp/sandbox.py`, `mvp/sandbox_service.py` | `test_stale_quotes_strict_and_explicit_sandbox_policy`; sandbox quote policy не закрывает production NFR-2, а снимок портфеля не содержит проверенного source timestamp |

16.10.2026 фактический sandbox API принял и отметил filled для четырёх заявок серии (TGLD, TMOS, SBER, LKOH). Итоговый снимок всех четырёх количеств и денег сверён, в том числе после перезапуска финального сервера около 17:55–17:56 МСК. Это исполненные sandbox заявки без реальных денег; реальных денежных сделок и production execution не было. Полнота PRD/SRS не заявляется, пока все применимые варианты RTM не выполнены и для неприменимых к локальному sandbox cases не зафиксировано основание. Sandbox не создаёт доказательство production freshness, реальных денежных исполнений, broker SLA, юридического статуса, внешней интеграции или соблюдения закона.

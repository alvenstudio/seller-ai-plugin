---
name: promotion
description: "Реклама Ozon через Seller AI: кампании «Оплата за клик» и «Оплата за заказ» (выбранные и все товары), их статистика, аналитика продвижения по типам, кампаниям и товарам, общий ДРР товара (весь рекламный расход к всей выручке), расходы по дням, история изменений, биддер Seller AI («Целевой общий расход»): цели товаров, ДРР из плановой юнит-экономики, разгон, тестовый режим, почему биддер так решил; ставки, бюджеты и даты кампаний, включение и выключение кампаний, товаров и «Морковска». Использовать когда пользователь спрашивает про рекламу, ДРР, ставки, CPC, CPO, кампании, биддер, расход на продвижение или просит что-то в рекламе поменять."
---

# Продвижение: кампании, ДРР, биддер

Источник — зеркало рекламы Seller AI и его биддер (seller-ai-docs `architecture/promotion-api.md`, `bidder.md`,
`research/ads/contract_v1.md` §8). Деньги — рубли (ставки CPC — целые рубли, не микрорубли), проценты — числа (`14` = 14 %), дни —
по Москве. **ДРР в Seller AI — общий ДРР**: весь рекламный расход товара ÷ вся его выручка по заказам (реклама + органика,
FBO + FBS, gross, по цене продавца). «ДРР в продвижении» — расход ÷ рекламные продажи; это разные числа, всегда называть, какое.

## Сначала — работает ли реклама и биддер

1. `stores_ozon_access_get`: `warnings[]` по важности. **`bidder_blind` (critical) — биддер ведёт кампании, а Performance-ключ не
   работает: ставки не меняются, расход не контролируется** (в `campaigns` — сколько кампаний, `since` — с какого момента нет
   данных). Сказать это первым, до любых цифр, и предложить обновить ключ Performance в настройках подключения. Так же первыми —
   `performance_key_refused`, `seller_key_expired` / `seller_key_invalid` (без ключа Seller заказы не грузятся, биддер не видит
   продаж). `features[]` с `available: false` — какие действия недоступны и почему (`missing_methods`, `performance_not_connected`).
   Предупреждение `key_warning` к результату любого инструмента магазина — то же самое.
2. `promotion_summary_get`: роль (`owner`, `manager`, `viewer`), `can_write`, `mutations.enabled` (выключено — команды не уйдут,
   причина в `reason`), `limits` (`cpc_bid_max`, `target_cir_min/max`, `weekly_budget_per_sku_min`), открытые операции.
3. Ответ 409 `not_ready` или `sync_pending` — «реклама ещё загружается», не «расхода нет».

## Чтение

| Вопрос | Инструмент | Главное в ответе |
|---|---|---|
| Сводка по типам, кампаниям, категориям, товарам за период; общий ДРР | `promotion_analytics_get` (`date_from`, `date_to`, `slice=type\|campaign\|category\|product`, `types`, `campaigns`, `skus`, `compare`, `split_strategy`, `without_models`) | `total` и `rows[]`: `metrics {expense, sales, orders, drr_promo, drr, gmv, views, clicks, ctr, to_cart, conversion, order_cost, click_cost}`, `delta`, `days[]`, `weeks[]`; `gmv.covered: false` — ДРР неизвестен (отправления периода не загружены) |
| Кампании «Оплата за клик» | `promotion_campaigns_list` (`tab`, `states`, `strategies`, `placements`, `sort`) | `state`, `strategy`, `placement`, `budget {type, amount, spent_this_week, remaining}`, `stats` (кабинетные итоги кампании), `mode` (`manual` / `target_total_drr`), `automation` |
| Карточка кампании | `promotion_campaigns_get` | `facts {drr_promo, drr_total, products_participating, cpo_rate_fixed, ad_spend, drr_ad_spend, revenue_active}` |
| Товары кампании, ставки | `promotion_campaign_products_list` | `bid` (наблюдаемая, ₽; 0 у автостратегии), `bid_min`, `bid_max`, `competitive_bid`, `target_cir`, `cpo {enabled, display_bid_pct, reduced, fixed_bid_pct, carrots_additive_pct}`, `stats {expense, revenue, drr, drr_total, gmv_total, ad_spend, drr_ad_spend}`, `bidder` |
| «Оплата за заказ: выбранные товары» | `promotion_cpo_products_list` (`enabled`, `bid_kind=reduced\|regular`, `carrots`) | `enabled`, `bid_pct`, `bid_rub`, `fixed_bid_pct`, `regular_bid_pct`, `reduced`, `display_bid_pct`, `cpc_campaigns`, `stats {cpo, combo}`, `drr` |
| «Оплата за заказ: все товары» | `promotion_cpo_all_get` | `state`, `state_since`, `bid {value}` (5 / 7 / 9 %), `stats {expense, revenue, drr, active_days}` |
| Расход по дням и кампаниям («Детализация расходов») | `promotion_expenses_get` (`types=cpc,cpo_selected,cpo_all`) | `items[] {date, campaign_id, title, type, money_spent, bonus_spent}`, `totals`, `missing` (дни, которые Ozon ещё не отдал) |
| Кто и что менял | `promotion_history_list` (`campaign_id`, `section=cpc\|cpo\|external`) | `items[] {at, source: seller_ai\|ozon, event, details}` |
| Алерты биддера и проверок Seller AI | `promotion_bidder_alerts_list` (`state=current\|open\|all`, `actionable`, `kinds`, `campaign_id`, `sku`, `date_from`, `date_to`) | `items[] {kind, scope, actionable, state, campaign_title, sku, name, message, details, raised_at, last_seen_at}`, `kinds[]` — счётчики |
| Наши операции | `promotion_operations_list`, `promotion_operations_get` | статус команды (ниже) |

**Какой ДРР где.** Общий ДРР товара по всем видам рекламы — `promotion_analytics_get` со `slice=product`, `metrics.drr` (база — `gmv`
товара). В таблице кампании `stats.drr_total` — расход **этой** кампании к выручке товара; у кампании биддера `stats.drr_ad_spend` —
весь рекламный расход товара (эта и другие кампании, комбо, CPO выбранные и все товары) к его продажам, пока кампания работала.
`facts.drr_total` карточки — расход кампании к выручке всего магазина (как `generalDrr` кабинета).

## Механика Ozon, которую нужно знать

- **«Оплата за клик»**: «Поиск» (`TOP_PROMOTION`) и «Поиск и рекомендации» (`SEARCH_AND_CATEGORY`). Стратегии Ozon: `TARGET_BIDS`
  «Средняя стоимость клика» (ручная ставка товара; ставка и есть цена клика, Ozon перестраивается за 15–45 минут), `TARGET_CIR`
  «Целевой расход» (Ozon сам ставит ставки по «Целевому ДРР» товара), `MAX_CLICKS` / `TOP_MAX_CLICKS` — автостратегии (ставок
  товаров нет: не делать вид, что это ручная ставка). Стратегию и название после создания Ozon не меняет. Бюджет — недельный, не меньше
  2 000 ₽ на товар; в кампании до 500 товаров.
- **«Оплата за заказ: выбранные товары»** — процент с заказов **с продвижения**, не со всех: только CPO — фиксированная ставка
  категории (раньше 23 %, с «Морковском» больше); товар ещё и в кампании за клики «Активна» или «Приостановлена» — сниженная 10 %
  («комбо»; в отчётах встречаются 10–12 % с «Морковском»). Выключение, архив или завершение кампании за клики **сразу поднимает**
  ставку CPO до фиксированной (`facts.cpo_rate_fixed`). Ставки брать из данных (`display_bid_pct`, `fixed_bid_pct`), не из констант.
  **Рублёвую ставку CPO (`bid_rub`) не советовать править** — это расчёт Ozon от цены. Советовать режим: только CPO / комбо, с
  «Морковском» или без. Подключить товар к «Оплате за заказ» Ozon даёт не всем: нужны продажи за последние ~2 недели (товар
  в кампании за клики — исключение); можно ли — `available` и `hint` строки `promotion_cpo_products_list`, причину назвать
  словами Ozon.
- **«Оплата за заказ: все товары»** — отдельный инструмент магазина 5 / 7 / 9 %, независим от выбранных товаров: при обоих расходы
  складываются.
- **CPO списывается по заказу** в течение 30 дней после того, как покупатель открыл карточку из продвижения; отмены и возвраты CPO не
  возвращают; внешние площадки списываются только после 14 дней без возврата (~5 % расхода приходит через 2–4 недели). Поэтому ДРР
  последних дней по CPO ещё растёт.
- **Списание за день** («Оплата за клик», «Продвижение товара») Ozon проводит утром следующего дня; статистика Seller AI видна сразу.

## Биддер Seller AI: «Целевой общий расход»

Кампания `mode = target_total_drr` (на Ozon — `TARGET_BIDS`): ставками каждого товара управляет биддер Seller AI, держа **общий ДРР
товара** у его цели. Коридор — от −2 до +1 п. п. от цели как ориентир среднего за 28 дней; недельный ДРР товара скачет на 50–70 %
(акции, остатки, спрос) — по одному дню выводов не делать.

- **Состояние кампании** — `automation` в списке и карточке: `execution_enabled` (false — тестовый режим: решения пишутся, ставки
  не меняются), `status` (`running`, `test`, `frozen`, `waiting`), `reason` (почему заморожен; первым — молчание биддера
  `bidder_silent`: нет прохода больше 15 минут — кампания крутится на последних ставках без контроля ДРР), `breaker_state`
  (`paused_by_breaker` — биддер остановил кампанию, потому что не работает ключ Seller, и включит сам), `unacked_alerts`,
  `paused_products`. Сводные алерты биддера магазина — пункт `bidder_alert` в `attention` у `home_summary_get` (`parts=now`).
- **Алерты** — `promotion_bidder_alerts_list`: по умолчанию текущие (открыты и подняты снова за 24 часа — то, что держится сейчас),
  `state=open` — и устаревшие (давно не поднимались: скорее всего, уже не держатся), `state=all` — и закрытые (история).
  `actionable: true` — решает продавец (`bidder_silent`, `strategy_changed`, `campaign_dark`, `external_bid_fight`, `double_run`,
  `pause_failed`, `infeasible`, `target_unreachable`, `floor_overshoot`, `ad_drr_ceiling`): их — первыми, с `message` и тем, что
  сделать; остальные — рабочие заметки биддера (перерасход и снижение ставки, защитный режим, обучение), он справляется сам —
  пересказывать, только если спросили. `ads_suspended` — Ozon приостановил рекламу магазина (`details.since`), `ads_restored` —
  доступ вернулся, а выключенные Ozon кампании ещё стоят. Отметить алерт прочитанным нельзя: его закрывают биддер и проверки.
- **Товар** — `bidder` в `promotion_campaign_products_list`: `target_total_drr` (цель в силе, %), `target_source` (`manual`, `plan`,
  `ramp`), `manual_target_total_drr`, `plan {drr, usable}`, `ramp {units, target_total_drr, sold, left}`, `learning {active,
  settled_days, needed_days}` (обучение: 4 устоявшихся дня после смены цели, пока их меньше — ставка не меняется), `status` (`new`,
  `stable`, `noisy`, `chaotic`, `frozen`, `external` — ставку меняли мимо биддера, он вернёт свою, `safe`, `infeasible`, `paused`),
  `bid_applied`, `last_decision {kind, bid_before, bid_after, apply_status, reason}`, `drr` (окно регулятора), `aim` (во что ставка
  целится сегодня: `day`, `rest`, `today_drr`, `debt`, `credit`), `pause`.
- **Почему так решили** — `promotion_campaign_products_bidder_get` (`campaign`, `sku`, `limit`): решения новыми первыми — `kind`,
  `bid_before` → `bid_after`, `applied`, `apply_status` (`applied`, `shadow` — тестовый режим, `pending`, `failed`, `rate_limited`,
  `skipped`), `reason` (готовая фраза), `explanation` (окно, ДРР по частям, цель для кликов `t_cpc`, шаг, границы). Пересказывать
  `reason` и ключевые числа объяснения, не выдумывать своё.
- **Что знает биддер о CPO.** CPO и комбо входят в общий ДРР: биддер целит клики в `T_cpc = цель − cpo_drr`. Если «Оплата за заказ»
  одна уже даёт ДРР не ниже цели (`T_cpc ≤ 0`, алерт «цель недостижима»), биддер держит минимальную ставку и CPC сам не выключает:
  выключение CPC подняло бы ставку CPO с 10 % до фиксированной — это решение пользователя.
- **Ставка на минимуме, а ДРР выше цели** (`floor_overshoot`) — снижать некуда: поднять цель, убрать товар из кампании или выключить
  его рекламу; биддер может и сам временно вывести товар (`pause`).
- Ставки в такой кампании меняет только биддер: `promotion_campaign_products_bids_set` отвечает 422 `bidder_managed`. Товар,
  добавленный в кампанию в кабинете Ozon, биддер удаляет из неё (решение владельца 10.10.2026).

## Изменения (команды и настройки)

**Согласие.** Полная просьба — согласие на обратимую команду: «поставь курагам ставку 12 ₽ в кампании X», «цель биддера 14 %
для фиников», «запусти кампанию X», «бюджет 5 000 в неделю», «кампания X до 30 ноября», «включи Морковск для фиников» —
отправить и показать было → стало. Один вопрос — когда чего-то не хватает (какая кампания, какой товар), просьба неоднозначна
или у команды есть последствие, которого пользователь не назвал: остановка кампании за клики поднимает ставку CPO её
комбо-товаров до фиксированной, смена цели начинает обучение биддера заново, перенос начала идущей кампании на будущий день
останавливает её показы до этого дня. Необратимые — удаление товаров из кампании (`promotion_campaign_products_remove`) и из
«Оплаты за заказ» (`promotion_cpo_products_remove`) — всегда показать и ждать явного «да».

Общий порядок: одна команда на намерение с одним `idempotency_key` (или ключ выведет MCP-сервер из аргументов). Ответ 200 — операция закончилась
(смотреть `status`), 202 — ещё идёт: `promotion_operations_get` (или `operations_get`), пока не закончится. Готово только при
`succeeded` — значение подтверждено чтением Ozon после ответа. `waiting_quota` — ждёт квоты Ozon (лимиты на операции с кампаниями с
14.09.2026), сама уйдёт; `partial` — часть товаров не принята, назвать какие (`items[].state`, `reason`); `outcome_unknown` — исход
неизвестен, **не отправлять повторно**: `promotion_operations_check` и ждать сверки. Отказ 409 `stale_expected` — значение уже
изменилось: перечитать и показать заново.

**Настройки биддера** (наши данные, Ozon не вызывается, ответ синхронный):

- Цель товара — `promotion_campaign_products_targets_set`: `items[{sku, target_total_drr, expected_target_total_drr}]` (проценты,
  один знак), либо `plan: true|false` — «ДРР из плановой юнит-экономики» (цель = «ДРР» плана карточки; план без годного значения —
  не задан, 0 или меньше 1 % — оставляет свою цель, `plan.usable: false`), либо `ramp {units, target_total_drr}` — «Разгон»: следующие
  N штук заказов по X %, потом обычная цель (`ramp: null` снимает). Предупредить: смена цели начинает обучение заново.
- «ДРР из плана» в кампании Ozon «Целевой расход» — `promotion_campaign_products_plan_targets_set` (`items[{sku, enabled}]`): биддер
  держит «Целевой ДРР» товара в Ozon равным плану (вне 5–100 % — не отправляет, `state: unusable`).
- Тестовый режим кампании — `promotion_campaigns_automation_set` (`execution_enabled`), только владелец.

**Команды в Ozon:**

- Ставки ручной кампании — `promotion_campaign_products_bids_set`: `items[{sku, bid, expected_bid}]` (`bid` — целые рубли от
  `bid_min` товара до `bid_max`, не выше 200; у «Целевого расхода» — `target_cir` %, у «Вывода в топ» — `top_position`). После
  `succeeded` показать наблюдаемую ставку из `promotion_campaign_products_list` (Seller AI подтверждает её чтением Ozon — отдельная
  проверка не нужна).
- Бюджет и даты — `promotion_campaigns_update`: `budget {type: weekly, amount}`, `from_date` («Дата начала»), `to_date` («Дата
  окончания») — дни по Москве `ГГГГ-ММ-ДД`, хотя бы одно из трёх; `expected` — что видел пользователь (бюджет и/или даты из
  `promotion_campaigns_get`). Без `budget` бюджет Ozon не трогается. Начало — не раньше сегодня, конец — не раньше сегодня и
  не раньше начала; кампанию в архиве или завершённую не изменить; снять дату окончания Ozon не даёт. Готово, когда чтение
  кампании показало новые даты (`succeeded`); `failed` с «…не применил изменение» — Ozon дату не принял, так и сказать.
  Безлимит — только владелец. Дневной и общий бюджет Ozon у продавца не принимает (дневной устарел, общий — только у
  автоматических кампаний брендов и агентств): их только читать (`daily`, `total`).
- Запуск / остановка — `promotion_campaigns_activate`, `promotion_campaigns_deactivate` (остановка поднимет ставку CPO товаров
  комбо до фиксированной: если пользователь этого не назвал — сказать и спросить один раз).
- Товары — `promotion_campaign_products_add` (`items[{sku, bid}]`), `promotion_campaign_products_remove` (`skus`, необратимо:
  только после явного «да»).
- Новая кампания — `promotion_campaigns_create` (`title`, `skus`, `budget`, `strategy`, `placement`, `target_cir` или
  `target_total_drr`, `activate`, `combo`); 409 `sku_managed_elsewhere` — товар уже ведёт другая наша кампания. Частично созданную
  продолжает `promotion_operations_continue` (дубликата не будет).
- «Оплата за заказ» — `promotion_cpo_products_enable` / `_disable` / `_add` / `_remove` (`skus`; `_remove` необратимо —
  после явного «да»); «Морковск» — `promotion_cpo_products_carrots_enable` / `_disable` (`skus`, до 1000): только выбранные
  товары с включённым продвижением (иначе 422 по `skus[i]` — сказать почему); Ozon добавляет к их ставке «Оплаты за заказ»
  надбавку (`carrots_additive_pct`, по его правилам 2 %, только с оплаченных заказов) — назвать её в итоге. Отказ Ozon по
  товару — `items[].reason`: `carrot_error_invalid_price` (цена ниже минимальной — по правилам Ozon 100 ₽),
  `carrot_error_invalid_product` (не прошёл модерацию), `carrot_error_other`. Готово, когда `carrots_status` товара —
  `CARROTS_STATUS_ENABLED` / `CARROTS_STATUS_DISABLED`; «все товары» —
  `promotion_cpo_all_activate` (`bid` 5 / 7 / 9), `promotion_cpo_all_deactivate`, `promotion_cpo_all_bid_set` (ставку «всех
  товаров» Ozon обратно не отдаёт: операция кончается `unverified`, текущей её не называть).
- Свежие данные кампании по запросу — `promotion_campaigns_refresh` (не чаще раза в 30 с на кампанию).

## Чего нет

- **Ставку «Оплаты за заказ» отдельного товара и рекомендованные ставки CPO в API Ozon больше нет** (Ozon снял эти методы):
  ставка фиксированная (`fixed_bid_pct`; в комбо — 10 %), советовать режим, а не ставку.
- Дневной бюджет и бюджет на весь период Ozon продавцу задать не даёт (в API дневной устарел, общий — только для
  автоматических кампаний брендов и агентств): бюджет кампании за клики — недельный или без ограничения.
- Пока недоступно в Seller AI (делается в кабинете Ozon): статистика медийной, баннерной и видеорекламы, отчёт по поисковым
  запросам кампаний.

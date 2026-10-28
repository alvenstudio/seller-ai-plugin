# Причины и коды: что значит и что делать

Пользователю коды не показывать — переводить словами.

## Расчёт черновика (`supplies_drafts_calculate`, `calculation.clusters[]`)

| `reason` | Что значит | Что делать |
|---|---|---|
| `NOT_AVAILABLE_MATRIX` | товар не заходит в кластер | убрать кластер, объём — по методу плана |
| `NOT_AVAILABLE_ROUTE` | нет маршрута из этой точки отгрузки | другая точка отгрузки |
| `NOT_AVAILABLE_TIMESLOT_FOR_DROP_OFF_POINT` / `…_STORAGE_WAREHOUSE` / `…_BOTH_WAREHOUSES` | нет таймслотов у точки / склада / у обоих | сдвинуть окно дат, повторить позже или убрать кластер |
| `PARTIAL_MATRIX_AVAILABLE` (`availability: partial`) | примет часть: `accepted.units` из `requested_units` | сказать на подтверждении, сколько возьмёт |
| `INCOMPATIBLE_WAREHOUSE` | склады кластера не принимают товар; у новой карточки — карточка ещё не разошлась по складам | новая карточка — повторить через 2–3 мин; иначе убрать кластер |
| `ITEMS_COUNT_MORE_THAN_MAX` | слишком много товара для кластера | уменьшить количество кластера |
| `OUT_OF_ASSORTMENT`, `NO_PRICE`, `EMPTY_BARCODE`, `MULTIPLICITY`, `SKU_IS_RESTRICTED` | товар не в ассортименте, нет цены, нет штрихкода, не кратно упаковке, ограничен к поставке | поправить карточку или количество |

`availability: null` — кластер не оценён (черновик Ozon упал, не назвав его): пересчитать без явных виновников.
`timeslots_allowed: false` — таймслоты черновика Ozon не отдаст, пока в нём есть кластер `none`.

## Ошибки Seller AI

| HTTP / `code` | Когда | Что делать |
|---|---|---|
| 409 `revision_conflict` | черновик изменился (`revision` — текущая) | перечитать `supplies_drafts_get`, повторить правку на новой ревизии |
| 409 `draft_not_editable` / `draft_submitted` | по черновику создаётся или создана заявка | смотреть операцию (`operation_id`), не подавать снова |
| 409 `calculation_in_progress` | расчёт уже идёт | ждать `supplies_drafts_get` |
| 409 `calculation_required` (`none`, `expired`, `stale_revision`, `unavailable_clusters`) | нет годного расчёта текущей ревизии | пересчитать (`expired` — черновик Ozon живёт 30 мин) |
| 422 `window_too_long` | окно таймслотов больше 27 дней | два окна |
| 422 `search_too_short` | поиск точки короче 4 символов | длиннее; ПВЗ — полным названием |
| 429 `rate_limited` (`retry_after`) | квота черновиков Ozon: 2 в минуту, 50 в час, 500 в сутки на аккаунт | продолжить после `retry_after` |
| 502 `upstream_error` | Ozon не ответил на живое чтение | повторить чтение позже |
| 409 `seller_unavailable` | нет рабочего ключа Ozon у магазина | `stores_ozon_access_get`; пользователю — перевыпустить ключ |

## Отказы Ozon в операции (`operation.message`)

| Код Ozon | Смысл |
|---|---|
| `TIMESLOT_NOT_AVAILABLE` | слот заняли — взять другой из свежего списка (самый ранний разбирают за секунды) |
| `DRAFT_DOES_NOT_EXIST`, `DRAFT_INCORRECT_STATE` | черновик Ozon истёк — пересчитать |
| `INVALID_CLUSTERS_COUNT` | в заявку ушли не все кластеры расчёта — пересчитать |
| `INACTIVE_CONTRACT`, `CONTRACTOR_CAN_NOT_CREATE_ORDER` | Ozon не даёт создавать заявки — договор в кабинете |
| `MINIMUM_VOLUME_IN_LITRES_INVALID`, `INVALID_VOLUME` | объём ниже минимального |
| `SUPPLY_CONTENT_NOT_VALID` | склад не принимает часть товара — уменьшать сверху вниз |
| `INBOUND_NO_CAPACITY` | на складе нет места; после грузомест — любое увеличение |
| `SUPPLY_LOCKED`, `ORDER_LOCKED`, `OTHER_ASYNCHRONOUS_OPERATION_IN_PROGRESS` | Ozon ещё применяет прошлое изменение — новая команда через минуту |
| `WAREHOUSE_LIMITS_EXCEED` | больше 30 коробок или 40 палет в поставку — остаток отдельной заявкой |
| `HAS_UTD`, `UTD_IS_UPLOADED` | загружен УПД — аннулировать в ЭДО |

Заявка `CANCELLED` сразу после создания — в черновике стояло 1 шт.: указывать реальное количество.

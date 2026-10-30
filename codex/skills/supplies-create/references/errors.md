# Коды причин и отказов поставок

Частые коды — в шагах `SKILL.md`. Здесь — остальные. Пользователю коды не показывать — говорить словами.

## Расчёт черновика (`calculation.clusters[].reason`)

| `reason` | Что значит | Что делать |
|---|---|---|
| `NOT_AVAILABLE_MATRIX` | товар не заходит в кластер | убрать кластер, объём — по методу плана |
| `NOT_AVAILABLE_ROUTE` | нет маршрута от этой точки отгрузки | другая точка отгрузки или убрать кластер |
| `NOT_AVAILABLE_TIMESLOT_FOR_DROP_OFF_POINT`, `…_STORAGE_WAREHOUSE`, `…_BOTH_WAREHOUSES` | нет слотов у точки, у склада или у обоих | сдвинуть окно дат, повторить позже или убрать кластер |
| `PARTIAL_MATRIX_AVAILABLE` | примет часть (`accepted.units` из `requested_units`) | назвать при подтверждении, сколько возьмёт |
| `INCOMPATIBLE_WAREHOUSE` | склады кластера не принимают товар | новая карточка — повторить через 2–3 минуты; иначе убрать кластер |
| `ITEMS_COUNT_MORE_THAN_MAX` | слишком много товара для кластера | уменьшить количество кластера |
| `OUT_OF_ASSORTMENT`, `NO_PRICE`, `EMPTY_BARCODE`, `MULTIPLICITY`, `SKU_IS_RESTRICTED` | не в ассортименте, нет цены, нет штрихкода, не кратно упаковке, ограничен к поставке | поправить карточку или количество |

`availability: null` — кластер не оценён (черновик Ozon упал, не назвав его): пересчитать без явных виновников.
`timeslots_allowed: false` — слотов не будет, пока в черновике есть кластер `none`.

## Ошибки Seller AI

| Код | Что делать |
|---|---|
| 409 `revision_conflict` | черновик изменился: перечитать `supplies_drafts_get`, повторить правку на новой `revision` |
| 409 `draft_not_editable` | по черновику создаётся или создана заявка: следить за операцией, не подавать снова |
| 409 `calculation_in_progress` | расчёт уже идёт: ждать `supplies_drafts_get` |
| 409 `supplies_stale` | Seller AI не смог перечитать заявку перед командой: ничего не отправлено, повторить позже |
| 422 `window_too_long` | окно слотов больше 27 дней: два окна |
| 422 `search_too_short` | поиск точки короче 4 символов; ПВЗ — полным названием |

## Отказы Ozon в операции (`message`)

| Код | Что делать |
|---|---|
| `DRAFT_DOES_NOT_EXIST`, `DRAFT_INCORRECT_STATE`, `INVALID_CLUSTERS_COUNT` | черновик Ozon истёк или в заявку ушли не все кластеры расчёта: пересчитать |
| `INVALID_ROUTE`, `INVALID_STORAGE_WAREHOUSE` | маршрут из этой точки или склад хранения не подходят: пересчитать; маршрут — другая точка |
| `MINIMUM_VOLUME_IN_LITRES_INVALID`, `INVALID_VOLUME` | объём не подходит (ниже минимального): поправить объём и пересчитать |
| `SUPPLY_CONTENT_NOT_VALID` | склад не берёт часть товара: уменьшать сверху вниз |
| `WAREHOUSE_LIMITS_EXCEED` | больше 30 коробок или 40 палет в поставку: остаток отдельной заявкой |
| `ORDER_SKU_LIMIT` | в поставке больше 5 000 шт: разделить объём на несколько заявок |
| `INVALID_QUANTITY_OR_QUANT` | количество не кратно кванту товара: поправить |
| `INVALID_DRAFT_BUNDLE_ID` | Ozon не принял правку состава: перечитать заявку (`bundle_id`) и править заново |
| `SAME_SKU` (422 `unchanged`) | состав не изменился: менять нечего |
| `SUPPLY_LOCKED`, `ORDER_LOCKED`, `OTHER_ASYNCHRONOUS_OPERATION_IN_PROGRESS` | Ozon ещё применяет прошлое изменение: новая команда через минуту |
| `INACTIVE_CONTRACT`, `CONTRACTOR_CAN_NOT_CREATE_ORDER` | Ozon не даёт создавать заявки: договор в кабинете Ozon |
| `SUPPLY_BELONG_TO_ANOTHER_CONTRACTOR` | заявка другого кабинета: работать в том магазине |
| `HAS_UTD`, `UTD_IS_UPLOADED` | загружен УПД: аннулировать его в ЭДО |
| `OUT_OF_ALLOWED_RANGE` | слот вне допустимого окна: взять слот из свежего списка без изменений |
| `INCOMPATIBLE_ORDER_FLOW` | в этом состоянии заявки слот не ставится: перечитать заявку и слоты |
| `PICKUP_ORDER_LIMIT_EXCEEDED` | исчерпан суточный лимит заявок с забором: другой день |
| `CANT_DELETE_ALL_CARGOES` | все места поставки убрать нельзя: оставить хотя бы одно |
| `SUPPLY_CARGOES_IS_FINALIZED`, `SUPPLY_CARGOES_LOCKED` | места уже не меняются — только в кабинете; их меняет другой процесс — через минуту |

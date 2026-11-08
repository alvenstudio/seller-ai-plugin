# Hermes Agent

Плагин — клон маркетплейса в `~/.hermes/plugins/seller-ai/` (у каждого профиля свой: `HERMES_HOME`).

## Как узнать

- Навык лежит в `~/.hermes/plugins/seller-ai/`; команда `hermes`.
- Терминал агента — инструмент `terminal`, часто удалённый (Docker, SSH, облачная песочница): там нет ни `hermes`, ни папки
  плагина. Скрипт тогда отвечает `needs_user`: обновить из чата Hermes — `/plugins update seller-ai`.

## Обновить сейчас

`hermes plugins update seller-ai` — в новых версиях с `--yes`, скрипт смотрит `hermes plugins update --help`. Проверить без
изменений — `hermes plugins check-updates --json` (так делает `--dry-run`). В чате — `/plugins update`.

## Автообновление

- `hermes gateway` раз в `plugins.auto_update_check_hours` часов (24 по умолчанию, 0 — выключено) проверяет обновления и
  ставит их только при `plugins.auto_apply: true` в `~/.hermes/config.yaml` (по умолчанию `false`, у каждого профиля
  свой). Обновления, которые добавляют инструменты, хуки или права, сам не ставит. Без запущенного gateway проверок нет.
- В новых версиях Hermes — `hermes plugins auto-update on`: скрипт выполняет её сам и сверяет `config.yaml`.
- В v0.22.2 такой команды нет, а YAML скрипт не правит (в stdlib нет разбора YAML): `auto_update: needs_user` и строка
  для пользователя:

  ```yaml
  plugins:
    auto_apply: true
  ```

## Версия на диске

- `~/.hermes/plugins/seller-ai/plugin.json`; источник и ревизия — `~/.hermes/plugins/.install-metadata.json`. `pinned:
  true` — закреплён, обновления не придут: переставить без `--ref` (`hermes plugins install … --force`).
- Отметка — `.git/FETCH_HEAD` клона; при `auto_apply` и `~/.hermes/plugin-update-checks/last-run` (время последней
  проверки gateway). Моложе 30 минут — `skipped`.

## Как новая версия доходит до сессии

Обновление из чата (`/plugins update`) доходит до открытых чатов; из терминала или по расписанию — до новых сессий.

## Руками, если скрипт не помог

- Плагина нет: `hermes plugins install alvenstudio/seller-ai-plugin`, `hermes plugins enable seller-ai`; MCP-сервер со
  входом — по README плагина (`hermes mcp add … --auth oauth`, `hermes mcp login seller-ai`).
- Обновление не прошло — вывод `hermes plugins update seller-ai` в `notes`; из чата — `/plugins update seller-ai`.

Источники (08.11.2026): hermes-agent.nousresearch.com/docs/user-guide/features/plugins; исходники Hermes v0.22.2 —
`hermes_cli/plugins_cadence.py` (проверка gateway, `last-run`, `auto_apply` только для git-плагинов),
`hermes_cli/config_defaults.py` (`auto_update_check_hours: 24`, `auto_apply: false`), `hermes_cli/plugins_cmd_update.py`
(`update`, `check-updates --json`).

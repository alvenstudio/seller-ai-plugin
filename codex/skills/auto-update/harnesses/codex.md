# Codex CLI и приложение ChatGPT (Codex)

CLI `codex` и приложение ChatGPT (его вкладка Codex работает через app-server) ставят плагин в одну папку `~/.codex/`.
Codex берёт копию плагина в своём формате — папку `codex/` маркетплейса.

## Как узнать

- Навык лежит в `~/.codex/plugins/cache/<маркетплейс>/seller-ai/<версия>/` (`CODEX_HOME` переносит `~/.codex`).
- Команды агента идут в песочнице: в окружении `CODEX_SANDBOX` (macOS — `seatbelt`) или
  `CODEX_SANDBOX_NETWORK_DISABLED=1`.

## Обновить сейчас

`codex plugin marketplace upgrade seller-ai` — сверяет ревизию маркетплейса (`git ls-remote`), при новой клонирует его
заново и переставляет плагин. В TUI: `/plugins` → маркетплейс seller-ai → Ctrl+U.

В песочнице по умолчанию сети нет, а запись вне рабочей папки требует одобрения, поэтому скрипт там не обновляет сам, а
отвечает `needs_user` с этой командой. Выполни её с одобрением пользователя (запуск вне песочницы) или попроси
пользователя выполнить её в терминале. Без песочницы скрипт выполняет её сам и сверяет версии до и после.

## Автообновление

Включено всегда, ключа нет: каждый запуск CLI или app-server обновляет Git-маркетплейсы из `~/.codex/config.toml`
(`[marketplaces.seller-ai]`, `source_type = "git"`). Долго работающий app-server повторно не обновляет: приложение ChatGPT,
открытое несколько дней, остаётся на версии дня запуска — его нужно перезапустить. Маркетплейс из локальной папки
(`source_type = "local"`) не обновляется вовсе (`auto_update: unavailable`) — добавить заново из GitHub:
`codex plugin marketplace add alvenstudio/seller-ai-plugin`.

## Версия на диске

- `~/.codex/plugins/cache/seller-ai/seller-ai/<версия>/.codex-plugin/plugin.json`.
- Снимок маркетплейса — `~/.codex/.tmp/marketplaces/seller-ai/`, его ревизия — `.codex-marketplace-install.json`. Этот
  файл переписывается только при новой ревизии: его время — отметка последнего обновления (моложе 30 минут — `skipped`).

## Как новая версия доходит до сессии

Новая сессия CLI или новый чат; приложение ChatGPT — перезапустить (документация: перезапуск, чтобы установка взяла новые
файлы).

## Руками, если скрипт не помог

- Нет команды `codex` (только приложение): в приложении — каталог плагинов; или закрыть и открыть приложение — при старте
  оно обновит маркетплейс само.
- Плагина нет: `codex plugin marketplace add alvenstudio/seller-ai-plugin`, `codex plugin add seller-ai@seller-ai`,
  `codex mcp login seller-ai`.

Источники (08.11.2026): developers.openai.com/codex/plugins/build (папка установки, `codex plugin marketplace upgrade`,
перезапуск приложения), github.com/openai/codex/pull/17425 (обновление Git-маркетплейсов при запуске), исходники Codex
`marketplace_upgrade.rs` и `manager.rs` (сверка ревизии и замена снимка только при новой, задача при старте) и TUI
`/plugins` (Ctrl+U), `codex plugin marketplace upgrade --help` (0.164.0).

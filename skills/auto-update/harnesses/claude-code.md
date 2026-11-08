# Claude Code: терминал, IDE, вкладка Code приложения Claude

Терминал (`claude`), расширения VS Code и JetBrains и вкладка Code приложения Claude — один Claude Code с общими файлами
`~/.claude/`: плагин, поставленный в одном, есть и в остальных.

## Как узнать

- В окружении `CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT` (`cli`, `claude-desktop`, `claude-vscode`…).
- Навык лежит в `~/.claude/plugins/cache/<маркетплейс>/seller-ai/<версия>/` — установка из маркетплейса
  (`seller-ai@seller-ai`). Папка `~/.claude/plugins/synced/…/seller-ai/` — плагин из аккаунта claude.ai
  (`seller-ai@synced`, его обновляет claude.ai — `claude-app.md`).
- `claude plugin list --json` — `id`, `version`, `scope`, `installPath`, `lastUpdated` каждой установки.

## Обновить сейчас

Скрипт выполняет `claude plugin marketplace update seller-ai --json` и `claude plugin update seller-ai@seller-ai --scope
user --json` и сверяет версии до и после. В приложении Claude он берёт команду той же сборки, что ведёт сессию
(`CLAUDE_CODE_EXECPATH`), иначе `claude` из PATH.

Руками, в сессии: `/plugin` → Installed → seller-ai → Update now, или Marketplaces → seller-ai → Update marketplace.

## Автообновление

- У официальных маркетплейсов Anthropic включено, у сторонних (наш) по умолчанию выключено.
- Ключ — `autoUpdate` у записи маркетплейса. Главнее запись в `~/.claude/settings.json` → `extraKnownMarketplaces`
  (`claude plugin marketplace add` пишет маркетплейс туда), за ней `~/.claude/plugins/known_marketplaces.json` (его пишет
  переключатель `/plugin` → Marketplaces → seller-ai → Enable auto-update):

  ```json
  "extraKnownMarketplaces": {
    "seller-ai": {
      "source": {"source": "git", "url": "https://github.com/alvenstudio/seller-ai-plugin.git"},
      "autoUpdate": true
    }
  }
  ```

- Скрипт ставит `autoUpdate: true` в `settings.json`; записи маркетплейса там нет — добавляет её с тем же `source`, что в
  `known_marketplaces.json`. Изменение — в `changed`. Файл не читается как JSON — не трогает (`auto_update: needs_user`).
  Файл-ссылку (dotfiles) правит по месту, ссылка остаётся.
- Проход автообновления — один раз за сессию, через случайные до 10 минут после первого сообщения; таймера нет. Он
  обновляет копии плагинов на диске, а открытая сессия держит загруженные: отсюда проверка раз в 30 минут.
- `DISABLE_AUTOUPDATER=1`, `DISABLE_UPDATES=1` или `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` выключают проход целиком;
  `FORCE_AUTOUPDATE_PLUGINS=1` оставляет обновления плагинов. Переменные скрипт не меняет — `auto_update: needs_user`.
  Исключение: приложение Claude само запускает каждую сессию с `DISABLE_AUTOUPDATER=1` (Claude Code обновляет оно), и
  плагины в нём обновляются: 08.11.2026 сессия приложения через 25 секунд после старта обновила оба маркетплейса с
  `autoUpdate` и поставила seller-ai 1.16.0.

## Версия на диске

- `~/.claude/plugins/installed_plugins.json` → `plugins["seller-ai@seller-ai"][].version` и `installPath`; файлы версии —
  `~/.claude/plugins/cache/seller-ai/seller-ai/<версия>/`.
- Копия маркетплейса — `~/.claude/plugins/marketplaces/seller-ai/` (её `plugin.json` — последняя загруженная версия).
- Отметка «проверяли недавно» — `lastUpdated` маркетплейса в `known_marketplaces.json`: моложе 30 минут, и в копии
  маркетплейса нет версии новее установленной — скрипт ничего не запускает (`skipped`).
- `CLAUDE_CONFIG_DIR` переносит `~/.claude`, `CLAUDE_CODE_PLUGIN_CACHE_DIR` — `~/.claude/plugins`; скрипт их учитывает.

## Как новая версия доходит до сессии

Открытая сессия работает на загруженной версии до `/reload-plugins` или новой сессии (`claude plugin update`: «restart
required to apply»). Ввести `/reload-plugins` может только пользователь — попроси его. Прежняя папка версии остаётся ещё
14 дней, так что навыки сессии работают и после обновления. Скрипт, запущенный из прежней папки, видит, что на диске новее
(`needs_user` с просьбой о `/reload-plugins`).

## Руками, если скрипт не помог

- Нет команды `claude` в PATH: `/plugin` → Installed → seller-ai → Update now; автообновление — `/plugin` → Marketplaces →
  seller-ai → Enable auto-update.
- Плагин поставлен не из маркетплейса (`seller-ai@skills-dir` из `~/.claude/skills/`, `seller-ai@inline` из
  `--plugin-dir`) — сам не обновится: `claude plugin install seller-ai --marketplace alvenstudio/seller-ai-plugin`,
  включить автообновление, старую копию убрать, `/reload-plugins`.
- Ошибка обновления маркетплейса (git, сеть) — `claude plugin marketplace update seller-ai` в терминале покажет причину.

Источники (08.11.2026): code.claude.com/docs/en/plugins/install («Keep plugins updated»),
code.claude.com/docs/en/plugins/loading («When auto-update runs», «Find plugins on disk», «Cleanup of previous
versions»), code.claude.com/docs/en/env-vars
(`DISABLE_AUTOUPDATER`, `FORCE_AUTOUPDATE_PLUGINS`, `CLAUDE_CODE_PLUGIN_CACHE_DIR`), `claude plugin update --help`
(2.1.304).

# VS Code с GitHub Copilot и Copilot CLI

Два независимых списка плагинов: у редактора VS Code — свой клон маркетплейса, у Copilot CLI — своя папка установок
(VS Code видит и её). Скрипт: `--harness vscode` или `--harness copilot-cli`.

## VS Code (редактор)

**Как узнать.** В терминале VS Code `TERM_PROGRAM=vscode`; плагин — клон маркетплейса
`~/.vscode/agent-plugins/github.com/alvenstudio/seller-ai-plugin/` (Insiders — `~/.vscode-insiders/…`).

**Обновить сейчас.** Палитра команд → «Chat: Update Plugins», или Agent Customizations → Plugins → «Check for and Apply
Updates». Скрипт сам не обновляет — это внутренняя папка VS Code, — а сверяет клон с репозиторием маркетплейса
(`git ls-remote`, только чтение) и говорит, есть ли новее.

**Автообновление.** Примерно раз в 24 часа, пока в пользовательских настройках не выключен `extensions.autoUpdate` (по
умолчанию включён). Ключ общий для всех расширений — скрипт его не меняет: выключен — `auto_update: needs_user`, и
обновлять «Chat: Update Plugins».

**Версия на диске.** `plugin.json` клона; отметка — `.git/FETCH_HEAD` клона (моложе 30 минут — `skipped`).

**До сессии.** VS Code следит за файлами плагинов и подхватывает изменения; если навык прежний — новая сессия чата или
«Developer: Reload Window».

## Copilot CLI

**Как узнать.** Навык в `~/.copilot/installed-plugins/seller-ai/seller-ai/` (`COPILOT_HOME` переносит `~/.copilot`).

**Обновить сейчас.** `copilot plugin update seller-ai`; скрипт перед ней выполняет `copilot plugin marketplace update
seller-ai` и сверяет версии. В сессии — `/plugin update seller-ai`.

**Автообновление.** Только по согласию, при старте интерактивной сессии (или `-p`) в доверенной папке:

```json
"extraKnownMarketplaces": {
  "seller-ai": {"source": {"source": "github", "repo": "alvenstudio/seller-ai-plugin"}, "autoUpdate": true}
}
```

в `~/.copilot/settings.json`. Скрипт ставит этот ключ и пишет его в `changed`. Не меняет общие выключатели:
`COPILOT_AUTO_UPDATE=false` и `autoUpdate: false` верхнего уровня `settings.json` (он же выключает обновления самого CLI) —
тогда `auto_update: needs_user`. Сессии SDK и сервера и запуски в CI не обновляются.

**Версия на диске.** `plugin.json` установленной копии; отметка — время этого файла (последняя установка).

**До сессии.** Новая сессия или `/restart`.

## Руками, если скрипт не помог

- Плагина нет: в чате Copilot `/plugin marketplace add alvenstudio/seller-ai-plugin`, затем
  `/plugin install seller-ai@seller-ai`; в VS Code — `"chat.plugins.marketplaces": ["alvenstudio/seller-ai-plugin"]`.
- Плагин из прямой установки (`~/.copilot/installed-plugins/_direct/…`) не обновляется: поставить из маркетплейса.

Источники (08.11.2026): code.visualstudio.com/docs/copilot/customization/agent-plugins («Chat: Update Plugins»,
`extensions.autoUpdate`), исходники VS Code (agent plugins: клоны `<папка>/github.com/<owner>/<repo>`, проверка раз в ~24
часа, слежение за файлами), docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference (команды,
`installed-plugins`, автообновление по согласию),
docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference (`settings.json`).

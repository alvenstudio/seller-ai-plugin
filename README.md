# Seller AI — плагин для ИИ-агентов

Плагин [Seller AI](https://seller.alven.studio) для работы с магазинами Ozon из ИИ-агентов: заказы, поставки FBO, товары,
цены, акции, продвижение, юнит-экономика, прибыль и отчёты. В нём MCP-сервер Seller AI (`https://seller.alven.studio/mcp`) и
навыки — пошаговые процедуры над его инструментами.

Токена в плагине нет: при подключении агент открывает вход в Seller AI (OAuth), и вы сами выбираете магазины и права. Ключи
Ozon агенту не нужны — данные и действия у Seller AI.

Этот репозиторий — маркетплейс одного плагина. Обновления приходят отсюда: ставьте плагин из маркетплейса, а не из архива,
тогда новые версии навыков приедут сами. Инструменты MCP-сервера обновляются на сервере и не требуют обновления плагина.

## Установка

### Claude — claude.ai, Claude Desktop, Cowork, Claude Code

1. **Настроить → Плагины → Добавить → Добавить маркетплейс**, адрес `alvenstudio/seller-ai-plugin`.
2. Включите у маркетплейса **«Синхронизировать автоматически»**.
3. Установите плагин **Seller AI**, откройте его вкладку **Коннекторы** и подключите Seller AI — откроется вход и выбор магазинов.

Плагин сохраняется в аккаунте: он есть в чате, в Cowork и в Claude Code (как `seller-ai@synced`) без отдельной установки.

### Claude Code из терминала

```bash
claude plugin install seller-ai --marketplace alvenstudio/seller-ai-plugin
```

Затем в сессии: `/plugin` → **Marketplaces** → `seller-ai` → **Enable auto-update** (для сторонних маркетплейсов
автообновление по умолчанию выключено) и `/mcp` → `seller-ai` → вход.

### ChatGPT и Codex

```bash
codex plugin marketplace add alvenstudio/seller-ai-plugin
codex plugin add seller-ai@seller-ai
codex mcp login seller-ai
```

Маркетплейс появится и в каталоге плагинов приложения ChatGPT (Codex), плагин и вход — там же. Обновления:
`codex plugin marketplace upgrade seller-ai`. Codex ставит отсюда копию плагина из папки `codex/` в своём формате: пакету
Agent Plugins он показывает модели не больше 64 КБ описаний инструментов MCP, а у Seller AI их больше.

### VS Code и GitHub Copilot

В чате Copilot: `/plugin marketplace add alvenstudio/seller-ai-plugin`, затем `/plugin install seller-ai@seller-ai`. Или в
настройках VS Code:

```json
"chat.plugins.marketplaces": ["alvenstudio/seller-ai-plugin"]
```

Обновления: `/plugin update seller-ai` или «Extensions: Check for Extension Updates».

### Cursor

Команда: **Dashboard → Plugins → Import from Repo**, адрес `https://github.com/alvenstudio/seller-ai-plugin`, включите
**Auto Refresh**. Без командного маркетплейса — склонируйте репозиторий в `~/.cursor/plugins/local/seller-ai` и перезапустите
Cursor (обновление — `git pull`).

### Hermes Agent

```bash
hermes plugins install alvenstudio/seller-ai-plugin
hermes plugins enable seller-ai
hermes mcp add seller-ai --url https://seller.alven.studio/mcp --auth oauth
hermes mcp login seller-ai
```

Плагин даёт навыки; сервер с входом по OAuth Hermes берёт из своего конфига (в пакете Agent Plugins способа входа не
указать), запись конфига заменяет серверную запись плагина. Автообновление навыков — `plugins.auto_apply: true` в конфиге
Hermes; иначе `hermes plugins update seller-ai`.

### Другие агенты

Подключите MCP-сервер `https://seller.alven.studio/mcp` (Streamable HTTP, вход по OAuth). Клиентам без OAuth нужен токен из
«Настройки → MCP и API» на сайте: заголовок `Authorization: Bearer sai_…`. Навыки сервер отдаёт и сам — клиентам с
расширением MCP Skills.

## Версии

Версия плагина — в `plugin.json`; каждая новая версия — тег `vX.Y.Z`. Навык «База знаний Ozon» (`ozon-edu`) в этот
репозиторий не входит: его отдаёт MCP-сервер Seller AI.

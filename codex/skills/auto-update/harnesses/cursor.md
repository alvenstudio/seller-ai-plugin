# Cursor

Плагин приходит из командного маркетплейса (импорт репозитория в дашборде) или лежит своей копией в
`~/.cursor/plugins/local/seller-ai`.

## Как узнать

- В окружении `CURSOR_TRACE_ID` или `CURSOR_AGENT`.
- Копия из маркетплейса — `~/.cursor/plugins/<…>/<маркетплейс>/seller-ai/<коммит>/`; своя копия —
  `~/.cursor/plugins/local/seller-ai`.

## Командный маркетплейс

- **Обновить сейчас.** Клиент забирает новую версию при запуске и при фокусе окна: перезапустить Cursor или переключиться
  в его окно. Индекс маркетплейса обновляет администратор: Dashboard → Settings → Plugins → Team Marketplaces → Refresh.
- **Автообновление.** «Enable Auto Refresh» там же: после пуша индекс обновляется сам, не чаще раза в 10 минут; нужно
  Cursor GitHub App с доступом к репозиторию. Ключа в файлах нет, с машины пользователя не видно, включено ли.
- **Скрипт** сверяет коммит копии с репозиторием маркетплейса (`git ls-remote`; репозиторий — `SELLER_AI_PLUGIN_REPO`,
  по умолчанию публичный `alvenstudio/seller-ai-plugin`): отстала — `needs_user` с шагами выше.
- **`/add-plugin <url>`** закрепляет плагин на первом коммите — такой не обновится: удалить и поставить из маркетплейса
  команды.

## Своя копия

- **Обновить.** `git -C ~/.cursor/plugins/local/seller-ai pull --ff-only` — скрипт выполняет сам, затем «Developer: Reload
  Window». Не клон git — заменить клоном.
- **Автообновления нет** (`auto_update: unavailable`): его заменяет эта проверка раз в 30 минут работы.
- **Версия** — `plugin.json` копии; отметка — `.git/FETCH_HEAD`.

## Руками, если скрипт не помог

`pull` не прошёл (локальные правки, другая ветка) — `git -C ~/.cursor/plugins/local/seller-ai status`, разобраться и
повторить. Плагина нет: команде — Dashboard → Plugins → Import from Repo, адрес
`https://github.com/alvenstudio/seller-ai-plugin`, Auto Refresh; одному — `git clone` в `~/.cursor/plugins/local/seller-ai`
и перезапуск Cursor.

Источники (08.11.2026): cursor.com/docs/plugins, forum.cursor.com — «Cursor 2.6: Team Marketplaces for Plugins»
(импорт, Auto Refresh, GitHub App, не чаще раза в 10 минут) и «Plugin Update & Version Management» (загрузка клиентом при
старте и фокусе, `/add-plugin` закреплён на коммите).

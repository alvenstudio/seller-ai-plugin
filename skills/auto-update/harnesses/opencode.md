# OpenCode

Маркетплейса у OpenCode нет: плагин — клон `alvenstudio/seller-ai-plugin` (обычно `~/.config/opencode/seller-ai-plugin`),
а навыки подключены ссылками в `~/.config/opencode/skills/` или массивом `skills` в `opencode.json`.

## Как узнать

- Навык лежит в клоне, на который ведут ссылки `~/.config/opencode/skills/<навык>`, или папка `skills/` клона есть в
  `skills` файла `~/.config/opencode/opencode.json`.
- В окружении бывает `OPENCODE` или `OPENCODE_CLIENT`.

## Обновить сейчас

`git -C ~/.config/opencode/seller-ai-plugin pull --ff-only`; затем ссылки: на навыки, которых не было, скрипт ставит сам,
ссылки на удалённые из плагина убирает — всё в `changed`. С массивом `skills` ссылки не нужны, новые навыки видны сразу:

```json
{"skills": ["~/.config/opencode/seller-ai-plugin/skills"]}
```

## Автообновление

Нет (`auto_update: unavailable`): его заменяет эта проверка раз в 30 минут работы.

## Версия на диске

`plugin.json` клона; отметка — `.git/FETCH_HEAD` (моложе 30 минут — `skipped`).

## Как новая версия доходит до сессии

OpenCode собирает список навыков на каждом шаге модели: новые навыки видны сразу; навык, прочитанный в этой сессии раньше,
перечитай или начни новую сессию.

## Руками, если скрипт не помог

- `pull` не прошёл (локальные правки, другая ветка) — `git -C <клон> status`, разобраться и повторить.
- Клона нет или это не клон git: `git clone https://github.com/alvenstudio/seller-ai-plugin
  ~/.config/opencode/seller-ai-plugin` и `skills` в `opencode.json`, как выше.

Источники (08.11.2026): opencode.ai/docs/skills (папки навыков, массив `skills` в `opencode.json`, список навыков на каждом
шаге модели).

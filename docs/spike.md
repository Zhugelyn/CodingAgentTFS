# Spike S00: OpenHands SDK на нативной Windows

Дата: 2026-10-01. Код: `spikes/s00/` (не в git). Трассы: `run1.log`, `run2.log`, `run3.log`.

## Вывод
**Работает как есть, без Docker, WSL и замены терминала.** Критерий S00 выполнен: агент прочитал `Ario.csproj` в RX, выполнил `dotnet build` (успешно, 0 ошибок), правок в RX нет (`git status` чистый).
Обязательный обход: отключить мета-поле `summary` у действий (см. ниже).

## Окружение
| Что | Значение |
|---|---|
| Python / uv / dotnet | 3.13.2 / 0.7.19 / 10.0.400 |
| `openhands-sdk`, `openhands-tools` | 1.50.1 (litellm 1.103.1) |
| Модель | `openrouter/qwen/qwen3.8-27b:free` (есть на OpenRouter, контекст 262144, поддерживает tools) |
| Терминал | штатный PowerShell-бэкенд SDK (`windows_terminal.py`), tmux не нужен |

## Находки
1. **SSL.** Python за корпоративным прокси падает с `CERTIFICATE_VERIFY_FAILED` (self-signed в цепочке). Лечится `truststore.inject_into_ssl()` в начале процесса (зависимость `truststore`).
2. **Поле `summary` ломает tool calling Qwen.** SDK добавляет к каждому действию необязательное поле `summary`; модель выдаёт `summaryCheck` → `Extra inputs are not permitted`. Прогон с дефолтами: 13 ошибок из 17 действий, 269 с, **123k токенов** (почти весь бюджет 128k). С отключением: 3 действия, 19 с, **18k токенов**. Обход: `openhands.sdk.tool.tool._create_action_type_with_summary = lambda a: a` до создания агента (приватный API, закрепить версию SDK и покрыть тестом).
3. **Ошибки валидации тоже тратят итерации** (идут в счётчик шагов).
4. **Лимит шагов:** `Conversation(max_iteration_per_run=N)`. При превышении: статус `ConversationExecutionStatus.ERROR` и `ConversationErrorEvent` (сообщение «Agent reached maximum iterations limit»). Оркестратор должен отличать его от прочих ошибок.
5. **Токены:** `conv.conversation_stats.get_combined_metrics().accumulated_token_usage` (prompt/completion/cache/reasoning). Жёсткого лимита токенов в SDK не найдено: проверять самим (по событиям/метрикам между шагами).
6. **События:** `Conversation(callbacks=[fn])` получает `SystemPromptEvent`, `ActionEvent`, `ObservationEvent`, `MessageEvent`, `AgentErrorEvent`, `ConversationErrorEvent`. Достаточно для `events.jsonl`.
7. **Память проекта / rules:** `AgentContext(skills=[Skill(name=..., content=..., trigger=None)])` подмешивается всегда; агент процитировал кодовое слово из него. Автозагрузку из рабочей папки отключаем (`load_*_skills=False`), чтобы не читать чужие файлы.
8. **Запрет инструментов:** набор задаётся явно: `Agent(tools=[Tool(name=...)], include_default_tools=["FinishTool"])`. Получили ровно `terminal, glob, grep, finish`. Это ограничивает набор, но терминал остаётся общим: границы команд и путей обеспечивает S02.
9. **Токены и скорость:** бесплатная модель отвечает ~5 с на шаг; кэш промпта ~90%.

## Влияние на план
- Этап 1/2: зависимости `truststore`, патч `summary`; различать остановку по лимиту шагов и прочие ошибки.
- Лимит токенов (128k) реализуем сами поверх метрик.
- D02: правила/навыки агентов оформлять как `Skill` с `trigger=None`.
- Не проверено: `file_editor` на CRLF/BOM, `dotnet build` всего `DirectumX.sln`, поведение при 429 на `:free`, переход на модель без `:free`.

## Замечание о целевой платформе
Финальное развёртывание: Docker на Linux-ноде. Этот спайк проверил только Windows; на Linux SDK использует tmux, его нужно проверить отдельно. См. memory.md, раздел 3.

---
name: directum-rx-qa-search-core
description: "Модуль Sungero.QASearchCore — вопросно-ответный поиск (RAG): области поиска, индексация документов через очередь, поиск ответов и чанков, инструмент file_search для ИИ-агента. Use when working with QASearchCore, QASearchArea*, QASearchIndexQueueItem, RagExtensions, AIAgentQASearch, индексацией документов."
---

# Модуль Sungero.QASearchCore

QASearchCore отправляет документы и справку Directum RX во внешний сервис RAG. Затем он ищет в них ответы на вопросы пользователя и ИИ-агента. RAG — сервис, который хранит текст документов в индексах и находит фрагменты по смыслу вопроса.

Путь: `RX/src/Packages/Sungero.QASearchCore`. Guid модуля: `Constants.Module.QASearchCoreGuid`. Лицензию проверяет модуль `Sungero.QASearchLic` (`QASearchLicGuid`).
Зависимости из `Module.mtd`: `Sungero.Docflow` (минимум 26.2.0.1) и решение `DirectumRX`. Код также вызывает `Commons.PublicFunctions.Module` (параметры системы).
Слоя Isolated нет. Есть Shared, Server, ClientBase, Libraries (`RagExtensions`, `ConfigSettings`), Settings.

Сущности: см. [ENTITIES.md](ENTITIES.md).

## Схема

```
Папка (QASearchAreaDocument)        Справка (QASearchAreaHelp)
        |                                   |
        v                                   v
 Job PrepareDataForQASearch  (в Module.mtd: Daily=Repeat, TimePeriod=15; единицу не проверял)
   1 ScheduleIndexing -> QASearchIndexedEntity + QASearchIndexQueueItem(Awaiting)
   2 SyncSearchAreas  <-> индексы сервиса RAG
   3 IndexScheduledQueueItems -> ProcessState = InProcess
        |
        v
 AsyncHandler ProcessIndexQueueItem -> ProcessDocumentForIndexing
        |                               (Connector, RagExtensions.dll)
        v
   Сервис RAG (индекс на область)
        ^
        |  CreateSearchTask / GetSearchTaskInfo / SearchChunks
 Пользователь, веб-API, ИИ-агент (AIAgentQASearch, инструмент file_search)
```

## Как вызвать функции из другого модуля

1. Вызывай только функции с `[Public]` из `Module.mtd`: `QASearchCore.PublicFunctions.Module.QASearchEnabled()`.
2. Читай публичные константы через `Sungero.QASearchCore.PublicConstants.Module.X`.
3. Передавай и читай результат через публичные структуры (`PublicStructures`).
4. Перед поиском проверь `QASearchEnabled()`. Функция возвращает `false`, если адрес сервиса пуст, неверен или нет готовых областей.

Публичные константы: `QASearchServiceUrlParamName`, `QASearchAccessTokenParamName`, `QASearchSettingsResponsibleRole`, `QASearchCoreGuid`, `QASearchLicGuid`, `CompoundPromptSids.*` (`UserQuestionByDocuments`, `AdminHelp`, `UserHelp`, `DeveloperHelp`), `FileSearchToolName`.

## Функции модуля

Все функции модуля лежат в `ModuleServerFunctions.cs` и соседних файлах слоя Server. Клиентских и Shared-функций уровня модуля нет (`ModuleSharedFunctions.cs` пуст).

| Функция | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| `QASearchEnabled` | Server | `[Public]` GET | Проверяет, что поиск доступен | нет |
| `CreateSearchTask` | Server | `[Public]` POST | Создаёт асинхронную задачу поиска ответа в RAG | `question`, `searchAreaIds` |
| `GetSearchTaskInfo` | Server | `[Public]` | Возвращает статус и результат задачи | `taskId` |
| `GetReadySearchAreas` | Server | `[Public]` | Возвращает области, готовые к поиску | нет |
| `ConfigureHelpQASearch` | Server | `[Public]` | Настраивает поиск по справке | нет |
| `SearchChunks` | Server | `public static` | Ищет чанки (фрагменты текста) в индексах | `questions`, `indexNames`, `maxNumResults` |
| `AIAgentQASearch` | Server | `public virtual` | Выполняет инструмент `file_search` ИИ-агента | `tool`, `query`, `vector_store_ids`, `max_num_results` |
| `GetQASearchServiceUrl` / `GetQASearchAccessToken` | Server | `public static` | Читают адрес и токен сервиса | нет |
| `CheckQASearchConnection` | Server | `public static` | Проверяет соединение с сервисом | `connector` |
| `GetRagConnector` (3 перегрузки) | Server | `public static` | Создаёт коннектор к RAG | `serviceUrl`, `timeoutInSeconds`, `accessToken` |
| `GetIndexNames`, `CreateIndex`, `DeleteIndex` | Server | `public static` | Работают с индексами сервиса | `connector`, `indexName` |
| `ScheduleIndexing` | Server | `public static` | Планирует индексацию по истории изменений | нет |
| `ScheduleIndexByDocumentChanges`, `ScheduleIndexByDocumentTypeRightsChanges` | Server | `public static` | Планируют индексацию по изменению версий и прав | `periodBegin`, `periodEnd` |
| `ScheduleIndexByFolderChanges` | Server | `public static` | Планирует добавление и удаление документов папки | `searchAreas` |
| `ScheduleEntitiesIndexing` | Server | `public static` | Ставит сущности в очередь | `indexedEntities`, `needIndexBody` |
| `IndexScheduledQueueItems` | Server | `public` | Запускает обработку элементов `Awaiting` | `searchAreas` |
| `SyncSearchAreas` | Server | `public static` | Синхронизирует области с индексами | `connector`, `indices` |
| `ProcessDocumentForIndexing` | Server | `public static` | Отправляет документ в индекс | `indexName`, `queueItem`, `document`, `allowedRecipients`, `deniedRecipients` |
| `GetDocumentsNotValidForQASearch` | Server | `public static` | Находит документы, которые нельзя индексировать | `documents` |
| `GetDocumentExtensionsForIndexing` | Server | `public virtual` | Возвращает допустимые расширения | нет |
| `GetQueueItemsInProcessLimit` | Server | `public virtual` | Возвращает лимит элементов «в обработке» | нет |
| `TryDeleteIndexedEntity`, `TryDeleteIndexQueueItem` | Server | `public static` | Безопасно удаляют запись | сущность |
| `DeleteObsoleteIndexQueueItems` | Server | `public static` | Удаляет старые элементы очереди | нет |
| `Get/SetLastQAIndexingQueueDate` | Server | `public static` | Читают и пишут дату последней обработки | `lastRun` |
| `QASearchAreaDocument.CreateSearchAreaFolderIfNotExist`, `RenameSearchAreaFolderIfNameChanged`, `GrantAccessRightsOnSearchAreaFolders`, `RevokeAccessRightsOnSearchAreaFolders` | Server | `public virtual` | Управляют папкой области и правами | `recipient` (для Revoke) |
| `QASearchAreaDocument.GetFolderItemsCount`, `GetSearchAreaDocuments` | Server | `public virtual` | Считают и выбирают документы папки | нет |
| `QASearchAreaDocument.CreateNoticeForSearchAreaResponsible` | Server | `[Remote]` | Создаёт уведомление ответственному | нет |
| `QASearchAreaDocument.ShowSavingConfirmationDialog` | Client | `public virtual` | Спрашивает подтверждение перед сохранением | `e` |
| `QASearchCompoundPrompt.GetLinkedSearchAreas` | Server | `public` | Возвращает области промпта | нет |
| `QASearchCompoundPrompt.ValidateRequiredPrompts`, `ValidateParamsDuplication` | Server | `public override` | Проверяют промпты и параметры | `paramNames` |
| `QASearchCompoundPrompt.FillPredifinedParams`, `GetAllowedParams`, `GetCompoundPromptRequiredParams` | Shared | `public` | Подставляют и перечисляют параметры промпта | `prompt` |
| `QASearchIndexQueueItem.TrySetProcessState` | Server | `public` | Меняет состояние элемента с блокировкой | `state` |
| `QASearchIndexedEntity.AddEntityToIndexQueue` | Server | `public virtual` | Добавляет сущность в очередь | `needIndexBody` |

## Фоновые процессы и обработчики

- Job `PrepareDataForQASearch` (`ModuleJobs.cs`): весь конвейер индексации. Запускается только при наличии лицензии и адреса сервиса.
- AsyncHandler `ProcessIndexQueueItem`: индексирует один элемент очереди. Повторы ограничены параметром системы `QASearchMaxRetryCountOfProcessIndexQueueItem`. После лимита элемент получает `Error`.
- AsyncHandler `ScheduleSearchAreaDocumentsReindexing`: ставит в очередь все документы области. Запускается из `AfterSave` области, если в `e.Params` стоит `NeedReindexDocumentsParamName`.
- `ModuleInitializer`: версии `FirstInitializing`, `Initializing262`, `Initializing263` (параметр `InitQASearchUpdate`). Создаёт промпты, области справки, роль ответственных, инструмент `file_search`.
- Параметры системы (справочник Commons): `QASearchQueueItemsInProcessLimit`, `QASearchMaxHistoryCheckPeriodInDays`, `QASearchMaxRetryCountOfProcessIndexQueueItem`.
- Настройки сервиса: ключи `SEARCH_SERVICE_URL`, `SEARCH_SERVICE_ACCESS_TOKEN` в конфиге; таймаут `QASearchAIAgentTimeoutInSeconds` (по умолчанию 60 секунд).

## Обзор сущностей

- `QASearchAreaBase` — абстрактная область поиска; `QASearchAreaDocument` — область из папки; `QASearchAreaHelp` — область справки.
- `QASearchCompoundPrompt` и две табличные части — промпты для языковой модели.
- `QASearchIndexedEntity` — что уже в индексе; `QASearchIndexQueueItem` — очередь индексации.
- Полное описание: [ENTITIES.md](ENTITIES.md).

## Типичные задачи

Как найти ответ из другого модуля:
1. Вызови `PublicFunctions.Module.QASearchEnabled()`. Если `false`, остановись.
2. Вызови `GetReadySearchAreas()` и выбери `Id` областей.
3. Вызови `CreateSearchTask(question, searchAreaIds)` и сохрани `Task.Id` каждой задачи.
4. Опрашивай `GetSearchTaskInfo(taskId)`, пока не появится `Result` или `ErrorMessage`.

Как добавить допустимое расширение файла:
1. Открой `GetDocumentExtensionsForIndexing` в `DocumentValidationFunctions.cs`.
2. Перекрой функцию в прикладном решении через `override` или добавь значение.
3. Запусти переиндексацию области.

Как добавить шаг инициализации:
1. Добавь константу версии в `Constants.Module.Init.QASearchCore`.
2. Добавь метод `InitializingXXX` в `ModuleInitializer.cs` и вызови его в `Initializing`.
3. Не меняй код старых версий.

Как пересоздать индекс области:
1. Измени состав папки области или нажми сохранение с подтверждением (`ShowSavingConfirmationDialog`).
2. `AfterSave` запустит `ScheduleSearchAreaDocumentsReindexing`.
3. Следи за `QASearchIndexQueueItem` в состоянии `Awaiting`/`InProcess`/`Error`.

## Ограничения и подводные камни

- Если с последнего запуска прошло больше `QASearchMaxHistoryCheckPeriodInDays`, `ScheduleIndexing` переиндексирует все области. Это тяжёлая операция.
- Элемент очереди создаётся пачками по `DefaultIndexQueueCreationBatchSize` (999). Не повышай значение: платформа не рекомендует больше ~1000 объектов в сессии.
- `GetAwaitingQueueItems` и `CreateSearchTask` используют `Contains` по списку Id. Платформа ограничивает такой список 64000 значениями.
- Если на сущность есть два элемента очереди (с телом и без), `PrepareIndexQueueItems` оставляет элемент с `NeedIndexBody` и удаляет второй.
- Удалить область поиска нельзя: `DeleteEntity` переопределено в `QASearchAreaBase`.
- Константы `MaxRetryCountOfProcessIndexQueueItem`, `IndexQueueCreationBatchSizeParamName`, `QueueItemsInProcessLimitParamName`, `DefaultQueueItemsInProcessLimit`, `MaxHistoryCheckPeriodInDays` помечены `[Obsolete]`. Используй параметры системы Commons.
- `AIAgentQASearch` намеренно использует имена параметров snake_case (совместимость с OpenAI). Не переименовывай их.
- `AIAgentQASearch` ищет только в активных областях с `IsIndexed` и в областях справки. Если список `vector_store_ids` не пуст, он сужает выбор.
- При ошибке сервиса `AIAgentQASearch` бросает `AppliedCodeException` с текстом ошибки.
- Не проверено: полное содержимое `ProcessDocumentForIndexing` и поведение `SyncSearchAreas` при закрытых областях; внутренности `RagExtensions.dll` не читались.
- Не правь `*.g.cs`. Для новых классов и сущностей используй DDS.

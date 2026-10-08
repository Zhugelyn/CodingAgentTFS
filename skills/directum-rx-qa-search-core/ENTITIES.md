# Сущности модуля Sungero.QASearchCore

Источник: `Shared/*/*.mtd`. Область поиска (search area) — набор документов или справки, который сервис RAG хранит как один индекс (index).

## Области поиска

### QASearchAreaBase
- Тип: абстрактный справочник (`DatabookEntry`). Родитель всех областей поиска.
- Назначение: хранит общие данные области. Действие `DeleteEntity` переопределено, удалять область нельзя.
- Свойства: `Name`, `Status`, `IndexName` (имя индекса в сервисе), `CompoundPrompt` (ссылка на промпт), `IsIndexed`, `Sid`, `Note`.

### QASearchAreaDocument
- Тип: справочник, наследник `QASearchAreaBase`.
- Назначение: область из документов, которые лежат в папке. Пользователь создаёт её сам.
- Свойства: `Folder` (папка с документами), `Responsible` (ответственный сотрудник), `CompoundPrompt`, `IndexName`.
- Действия: `ShowFolder` (открыть папку), `NotifyResponsible` (создать уведомление ответственному).
- Обработчики: при создании `IndexName` получает новый Guid; при сохранении создаётся папка, выдаются права, ответственный получает право на чтение.

### QASearchAreaHelp
- Тип: справочник, наследник `QASearchAreaBase`.
- Назначение: область по справке (администратора, пользователя, разработчика). Создаёт инициализатор модуля.
- Свойства: `CompoundPrompt`, `IndexName`. Собственных действий нет.

## Промпты

### QASearchCompoundPrompt
- Тип: справочник, наследник `CompoundPromptBase` (модуль Commons).
- Назначение: набор промптов, по которым языковая модель отвечает на вопрос.
- Свойства: `Status`, `IsSystem`, `Sid`, `Temperature`, `SystemPrompt`, `UserPrompt`, `IsAdvanced`, `GenAIModel`, `SearchType`, коллекции `Prompts` и `RequestParams`.
- Действие: `ShowLinkedSearchAreas` (показать области, которые используют промпт).

### QASearchCompoundPromptPrompts
- Тип: табличная часть (коллекция `Prompts`).
- Назначение: строки промптов. Родитель — `QASearchCompoundPrompt`. Свойство `CompoundPromptBase`.

### QASearchCompoundPromptRequestParams
- Тип: табличная часть (коллекция `RequestParams`).
- Назначение: параметры запроса. Родитель — `QASearchCompoundPrompt`. Свойство `CompoundPromptBase`.

## Индексация

### QASearchIndexedEntity
- Тип: сущность (`Entity`), не справочник.
- Назначение: запись о том, что документ уже отдан в индекс области.
- Свойства: `Name`, `EntityType` (тип документа), `EntityId`, `SearchAreaId`.

### QASearchIndexQueueItem
- Тип: сущность (`Entity`).
- Назначение: элемент очереди индексации. Задание создаёт его, асинхронный обработчик обрабатывает.
- Свойства: `Name`, `IndexedEntityId`, `ProcessState`, `NeedIndexBody` (индексировать тело документа), `Updated`.

## Перечисления

| Перечисление | Где | Значения |
|---|---|---|
| `QASearchIndexQueueItem.ProcessState` | элемент очереди | `Awaiting`, `InProcess`, `Error` |
| `QASearchCompoundPrompt.SearchType` | промпт | `Documents`, `AdminHelp`, `DevHelp`, `UserHelp` |

## Структуры модуля

Публичные (объявлены в `PublicStructures` в `Module.mtd`):

| Структура | Ключевые поля |
|---|---|
| `SearchResult` | `Task` (TaskInfo), `ErrorMessage` |
| `TaskInfo` | `Id`, `Status`, `IndexName`, `SearchAreaId`, `SearchAreaName`, `ErrorMessage`, `Result` |
| `ResultAnswer` | `Answer`, `Entities` |
| `ChunkSearchResult` | `Entities`, `ErrorMessage` |
| `Entity` | `EntityId`, `EntityName`, `EntityLink`, `EntityExtension`, `Chunks` |
| `Chunk` | `Text`, `Score` |
| `SearchAreaInfo` | `Id`, `Name` |

Внутренние (без `[Public]`): `DocumentsValidationError` (`Ids`, `ErrorMessage`), `DocumentsValidationErrors` (`Errors`), `DocumentReadAccessInfo` (`AllowedRecipients`, `ForbiddenRecipients`), `IndexDocumentResult` (`Success`, `NeedRetry`).

В модуле нет документов, задач, заданий, уведомлений-сущностей и обложек. Уведомление ответственному создаётся функцией `CreateNoticeForSearchAreaResponsible`.

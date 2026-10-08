---
name: directum-rx-commons
description: Модуль Sungero.Commons Directum RX — базовые справочники (страна, регион, город, валюта, НДС, параметры), ИИ-помощник (роли, инструменты, контекстные действия), GenAI, факты Ario, Elasticsearch, ExternalEntityLink. Use when working in Packages/Sungero.Commons or calling Commons.PublicFunctions/PublicConstants from other modules.
---

# Модуль Sungero.Commons

Путь: `RX/src/Packages/Sungero.Commons`.
Commons — самый нижний прикладной модуль. Он хранит общие справочники и общие сервисы: ИИ-помощник, GenAI, распознавание Ario, поиск Elasticsearch.
Другие модули (Company, Parties, Docflow и т.д.) вызывают его функции через `[Public]`. Commons не должен зависеть от верхних модулей.
Список сущностей: см. [ENTITIES.md](ENTITIES.md).

## Схема

```
 Верхние модули (Docflow, Parties, Company, Intelligence, ...)
        |  Commons.PublicFunctions.Module.X(...)
        |  Commons.PublicConstants.Module.Y
        v
 +--------------------- Sungero.Commons ----------------------+
 | ClientBase  -> UI: Remote-вызовы, диалоги, обсуждения ИИ    |
 | Shared      -> mtd, resx, константы, структуры, shared-ф-ии |
 | Server      -> логика, [Remote], [Public], jobs/async       |
 | Isolated    -> JSONParser (разбор JSON в изолированной зоне)|
 | Libraries   -> Discussions, ElasticsearchExtensions         |
 +----------------+----------------------+---------------------+
                  |                      |
        Elasticsearch (REST)      GenAI-сервисы (GigaChat,
        индексы rxsearch_*        YandexGPT, OpenAI)
```

Поток индексации: сущность изменена -> верхний модуль вызывает `CreateIndexEntityAsyncHandler` -> асинхронный обработчик `IndexEntity` -> `ElasticsearchIndexEntity`.

## Темы модуля

1. Территории и финансы: Country, Region, City, Currency, VatRate.
2. Параметры: Parameter, ParametersGroup.
3. ИИ-помощник: AIAgentRole, AIAgentTool, AIAgentContextAction, AIRequestQueueItem.
4. GenAI: GenAIService, GenAIModel, SystemPromptBase, CompoundPromptBase.
5. Распознавание Ario: EntityRecognitionInfo, ClassifierTrainingSession.
6. Интеграция: ExternalEntityLink.

Зависимость в `Module.mtd`: только один модуль решения (Id `e4fe1153-...`). Jobs в модуле нет. Версия модуля — в `Module.mtd`.

## Как вызывать функции из другого модуля

- Функция с `[Public]` доступна как `Commons.PublicFunctions.Module.Имя(...)`.
- Функция сущности доступна как `Commons.PublicFunctions.Сущность.Имя(...)`.
- Константа с `[Public]` доступна как `Commons.PublicConstants.Module.Имя`.
- Серверную функцию с `[Remote]` клиент вызывает как `Functions.Module.Remote.Имя(...)`.
- Структуры вызывай как `Commons.PublicStructures.Module.ArioFact.Create()` и заполняй свойства по одному.

```csharp
var currency = Sungero.Commons.PublicFunctions.Currency.Remote.GetDefaultCurrency();
var facts = Sungero.Commons.PublicFunctions.Module.GetFacts(facts, "Counterparty");
```

## Таблица функций

Модуль: `Commons.PublicFunctions.Module`, если не указана сущность.

### Сущности и сервис

| Имя | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| GetExternalEntityLinks | Server | Public | Возвращает связи с внешней системой | uuid, sysid |
| IsAllExternalEntityLinksDeleted | Server | Public | Проверяет, что все связи сущности удалены | entity |
| GetFinalTypeName | Server | Public | Возвращает имя конечного типа сущности | entity |
| GetEntityVersion | Server | Public | Возвращает версию записи из таблицы | tableName, entityId |
| GetModuleFunctions | Server | Public | Ищет функции модуля через reflection | moduleName, className, functionName, bindingFlags |
| ExecuteFunction | Server | Public | Вызывает функцию модуля по имени | moduleName, className, functionName, parameters |
| AddFolderLinkToRootFolder / RemoveFolderLinkFromRootFolder | Server | Public | Добавляет или удаляет ссылку на папку в корневой папке | rootFolderId, folderId |
| GetTenantTimeWithUtcLabel / GetUtcLabel | Server | Public | Форматирует время с меткой UTC | serverTime, utcOffset |
| IsServerCultureRussian | Server | Public | Проверяет русскую культуру сервера | — |
| EscapeHtmlTags | Server | Public | Экранирует HTML-теги | text |
| GetValueAsString | Shared | Public | Приводит значение к строке | propertyValue |
| GetEntityParamsValue, GetLongEntityParamsValue, GetBooleanEntityParamsValue | Shared | Public | Читают параметр сущности | entity, paramName |
| GetAndRemoveLongParam | Shared | Public | Читает и удаляет параметр | entity, paramName |
| AddOrUpdateEntityParams, DeleteEntityParams, EntityParamsContainsKey | Shared | Public | Меняют или проверяют параметры сущности | entity, paramName, paramValue |
| GetVatAmountFromTotal | Shared | Public | Считает НДС от суммы с НДС | totalAmount, vatRate |
| GetListValueFromJson | Isolated (JSONParser) | Public | Читает массив из JSON | json, jsonKey |
| CreateNewCity | Server / Client | Remote / — | Создаёт город / открывает карточку | — |

### Факты Ario

| Имя | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| GetFacts | Server | Public | Выбирает факты по имени (и полю) | facts, factName, fieldName |
| GetOrderedFacts / GetOrderedFactsByType / GetOrderedFactsByFieldPriorities | Server | Public | Сортирует факты по вероятности или приоритету | facts, factName, ... |
| GetField / GetFields | Server | Public | Возвращает поле(я) факта | fact, fieldName(s) |
| GetFieldValue / GetFieldDateTimeValue / GetFieldNumericalValue | Server | Public | Возвращает значение поля | fact, fieldName |
| GetFieldProbability / GetAggregateFieldsProbability | Server | Public | Считает вероятность поля или набора полей | fact, weightedFields |
| GetFactLabel | Server | Public | Возвращает метку факта для свойства | fact, propertyName |
| GetPreviousPropertyRecognitionResults | Server | Public | Возвращает прошлый результат распознавания | fact, propertyName, ... |
| EntityRecognitionInfo: GetEntityRecognitionInfo | Server | Public, Remote(IsPure) | Возвращает запись распознавания сущности | entity |
| EntityRecognitionInfo: LinkFactAndProperty, LinkFactFieldsAndProperty | Server | Public | Связывает факт и свойство | fact, propertyName, ... |

### Elasticsearch

| Имя | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| IsElasticsearchEnabled / IsElasticsearchConfigured | Server | Public | Проверяет включение и настройку | — |
| GetElasticsearchUrl, GetIndexName | Server | Public | Возвращает URL и имя индекса | entityName |
| ElasticsearchCreateIndex, ElasticsearchBulk | Server | Public | Создаёт индекс, заполняет пакетом | indexName, configJson / contentJson |
| ElasticsearchIndexEntity, ElasticsearchRemoveEntity | Server | Public | Добавляет или удаляет запись | indexName, entityId, contentJson |
| ElasticsearchUpdateIndexSettings, Open/CloseIndex, IsIndexExist | Server | Public | Управляют индексом | indexName |
| CreateIndexEntityAsyncHandler, CreateRemoveEntityFromIndexAsyncHandler | Server | Public | Ставят асинхронный обработчик в очередь | entityName, entityId, contentJson |
| GetTermQuery, GetTermsQuery, GetMatchQuery, GetMatchFuzzyQuery, GetWildcardQuery, GetMatchPhraseQuery, GetBoolQuery | Server | Public | Строят JSON запроса | fieldName, value |
| ExecuteElasticsearchQuery | Server | Public | Выполняет запрос | factQueryData |
| TrimSpecialSymbols, GetLegalFormSynonyms, SynonymsParse | Server | Public | Готовят текст и синонимы | text |

### ИИ-помощник и GenAI

| Имя | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| OpenNewDiscussionWithAIAgent | Client | Public | Открывает новое обсуждение с ИИ-помощником | agentRoleExternalId, boundEntity, message |
| OpenExistingDiscussion, AddEntity | Client | — | Открывает обсуждение, добавляет сущность | discussionId, message, entity |
| CreateDiscussionWithAIAgent | Server | Remote | Создаёт обсуждение | agentRoleExternalId, boundEntity, message |
| AIAgentGetOrCreateAIAgentRole | Server | Public | Возвращает роль или создаёт | roleInfo |
| AIAgentCreateOrUpdateAIAgentRoleIgnoreTools | Server | Public | Создаёт или обновляет роль без инструментов | roleInfo |
| AddAgentRoleTools (3 перегрузки) | Server | Public | Добавляет инструменты в роль | toolSids / tools, roleAssistant |
| GetAIAgentToolBySid | Server | Public, Remote(IsPure) | Возвращает инструмент по Sid | sid |
| AIAgentContextAction: GetAllowedEntityTypes | Server | Public | Возвращает допустимые типы сущностей | — |
| AIAgentContextAction: GetAvailableContextActions | Server | Public | Возвращает действия для типа | originalEntityType (Guid) |
| AIAgentContextAction: GetAvailableContextAction | Server | Public | Возвращает действие по ExternalId | externalId |
| AIAgentContextAction: GetOrCreateContextAction, UpdateContextAction | Server | Public | Создаёт или обновляет действие | actionInfo (ContextActionInfo) |
| AIAgentTool: CallTool, ValidateParameters | Server | — | Вызывает инструмент, проверяет параметры | paramValues |
| AIAgentTool: GetMcpInputSchema, GetAnnotations | Server | — | Формируют описание для MCP | — |
| AIRequestQueueItem: CreateRequest (3 перегрузки) | Server | Public | Создаёт запрос в очереди GenAI | compoundPrompt, entity / entityId+entityType, arguments |
| AIRequestQueueItem: GetAIRequestQueueItems | Server | Public | Возвращает запросы пользователя | entity, compoundPromptIds, author |
| GetSystemGenAIService, GetSystemGenAIModel | Server | Public | Возвращают системные сервис и модель | — |
| GenAIService: Encrypt/DecryptGenAIServiceAccessToken | Server | Public (static) | Шифрует и расшифровывает токен | token |
| Currency: GetDefaultCurrency | Server | Public, Remote(IsPure) | Возвращает валюту по умолчанию | — |
| City: GetCityFromAddress | Server | Public | Ищет город по адресу | address |
| ExternalEntityLink: GetEntity, GetExternalByEntityType, GetInternalByEntityType | Server | Public | Работают со связями | entityType |

## Обработчики и фоновые процессы

- Async-обработчики: `IndexEntity` и `RemoveEntityFromIndex`. Задержка 15 мин, стратегия `ExponentialDelayStrategy`.
- Максимум повторов: `Constants.Module.IndexingAsyncsRetryCount` = 6.
- Инициализатор `ModuleInitializer` создаёт системные параметры, настройки GenAI, города из ФИАС, права на `AIRequestQueueItem`.
- Новую инициализацию добавляй версионным методом (`Initializing271` и т.п.) через `ModuleVersionInit`.

## Обзор сущностей

Подробности — [ENTITIES.md](ENTITIES.md). 28 файлов метаданных: 16 сущностей и 12 коллекций (табличных частей). Сущности сгруппированы по темам: территории и финансы, параметры, ИИ-помощник, GenAI, распознавание Ario, интеграция.

## Типичные задачи

Добавить системное контекстное действие ИИ-помощника:
1. Заполни структуру `ContextActionInfo` (Sid, Name, EntityTypeGuid, MessageText, AIAgentRoleSid).
2. Вызови `AIAgentContextAction` `GetOrCreateContextAction(actionInfo)` в инициализаторе своего модуля.
3. Проверь запись в справочнике: свойства системной записи недоступны, кроме состояния.

Выдать факты Ario в свойство документа:
1. Выбери факты через `GetFacts(facts, factName)`.
2. Прочитай значение через `GetFieldValue(fact, fieldName)`.
3. Свяжи факт и свойство через `LinkFactAndProperty` у `EntityRecognitionInfo`.

Добавить индексацию сущности в Elasticsearch:
1. Проверь `IsElasticsearchEnabled()`.
2. Получи имя индекса через `GetIndexName(entityName)`.
3. Вызови `CreateIndexEntityAsyncHandler(entityName, entityId, contentJson, allowCreateRecord)`.

Вызвать ИИ-помощника с клиента:
1. Вызови `Commons.PublicFunctions.Module.OpenNewDiscussionWithAIAgent(agentRoleExternalId, entity, message)`.
2. Функция возвращает Id обсуждения.

## Ограничения и подводные камни

- Системная запись `AIAgentContextAction` определяется по непустому `Sid` (`IsSystem` ставится в `BeforeSave`). `SetPropertyStates` блокирует все свойства, кроме `Status`.
- При копировании `AIAgentContextAction` обработчик `CreatingFrom` исключает `Sid`, `IsSystem`, `ExternalId`. Копия получает новый `ExternalId` в `Created`.
- Для `Contains()` в запросах используй лимит `MaxQueryIds` = 999 (константа модуля).
- `ExecuteFunction` и `GetModuleFunctions` работают через reflection. Ошибки имени проявятся только во время выполнения.
- Если индексация падает 6 раз подряд, обработчик пишет ошибку в лог и прекращает повторы.
- `IsIntelligenceEnabled` помечена `[Obsolete]` (версия 4.10). Используй `Docflow.PublicFunctions.Module.Remote.IsModuleAvailableByLicense(...)`.
- `GetPromptList`/`GetPromptResult` (MCP prompts) в этой версии скилла не проверены.
- Не правь `*.g.cs`. Новые типы сущностей создаёт разработчик через DDS.
- Новые строки интерфейса добавляй в `*.resx`, `*.ru.resx` и `ResourcesKeys` в mtd.

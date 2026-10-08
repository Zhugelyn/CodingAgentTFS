---
name: directum-rx-integration-ai-agent
description: Модуль Sungero.IntegrationAIAgent — MCP-сервер Directum RX для ИИ-помощника (JSON-RPC, инструменты, промпты, ресурсы, контекстные действия). Use when working with HandleMcpRequest, MCP, AIAgentTool, AIAgentRole, AIAgentContextAction, prompts/list, prompts/get, resources/read, "Обсудить с помощником".
---

# Модуль Sungero.IntegrationAIAgent

Модуль работает как MCP-сервер (Model Context Protocol) внутри Directum RX. MCP — это протокол, по которому ИИ-помощник запрашивает данные и вызывает инструменты. Модуль принимает запрос в формате JSON-RPC, выполняет его и возвращает JSON-ответ. В модуле нет своих сущностей. Он использует справочники модуля `Sungero.Commons` (инструменты, роли, контекстные действия).

Код модуля: `D:\Projects\master\RX\src\Packages\Sungero.IntegrationAIAgent`. Подробности о сущностях: [ENTITIES.md](ENTITIES.md).

## Схема

```
ИИ-помощник (клиент MCP)
        | JSON-RPC (POST)
        v
[Public] ModuleFunctions.HandleMcpRequest(value)
   | проверка лицензии -> разбор McpRequestBase -> ValidateRequest -> ValidateCallId
   v
GetResponse: выбор по request.Method
   |-- initialize                  -> GetInitializationResult
   |-- notifications/initialized   -> GetInitializedResponse (list_changed)
   |-- tools/list                  -> Commons.AIAgentGetToolList
   |-- tools/call                  -> GetToolCallResult -> Commons.AIAgentTool.CallTool
   |-- prompts/list                -> GetPromptList (встроенные + контекстные действия)
   |-- prompts/get                 -> GetPromptResult
   |-- resources/list              -> GetResourceList
   |-- resources/templates/list    -> GetResourceTemplateList
   `-- resources/read              -> AIAgentResourceRead
                                       |-- config://tools, config://assistants
                                       |-- content://?url=...  (тело документа / JSON записи справочника)
                                       `-- meta://?url=...     (метаданные)
Зависимости: Commons (основные), Docflow (параметры, лицензия), Company, Content.
```

## Как работать с модулем

- Модуль имеет один вход: `HandleMcpRequest`. Он помечен `[Public(WebApiRequestType = RequestType.Post)]`. Клиент вызывает его как Web API.
- Из другого модуля вызывай так: `Sungero.IntegrationAIAgent.PublicFunctions.Module.HandleMcpRequest(json)`.
- Публичные константы: `PublicConstants.Module.AIAgentDiscussionEnabled`, `ModuleGuid`, `McpServer.ProtocolVersion`, `McpServer.ServerName`.
- Клиентского, isolated и shared кода в модуле нет. Файлы `ModuleSharedFunctions.cs`, `ModuleClientFunctions.cs`, `ModuleJobs.cs`, `ModuleAsyncHandlers.cs` пустые.
- Внешняя сборка `Sungero.IntegrationAIExtensions` (в `Libraries`, область Server) содержит классы запросов и ответов: `McpRequestBase`, `AIAgentResponse`, `PromptList`, `ResourceList` и другие.
- Инициализатор `ModuleInitializer` вызывает `FirstInitializing` при первой установке версии `26.3.0.0`. Он добавляет параметр `AIAgentDiscussionEnabled` (значение `False`) и создает системное контекстное действие "Кратко рассказать о сотруднике".
- Настройки: параметр Docflow `AIAgentDiscussionEnabled` включает промпт "Обсудить с помощником" и контекстные промпты.
- Лицензия: `HandleMcpRequest` проверяет модуль `IntegrationAIAgent` через `Docflow.PublicFunctions.Module.Remote.IsModuleAvailableByLicense`.
- Пользовательский JSON сущности: добавь в серверные функции типа сущности функцию `GetAIAgentProperties` или `GetAIAgentJson` (без параметров). Модуль найдет её через reflection.

## Функции

Все функции находятся в `Server/ModuleServerFunctions.cs` и `Server/ModuleServerFunctions/Resources/`. Публичная функция только одна.

| Функция | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| HandleMcpRequest | Server | [Public], Post | Принимает MCP-запрос, возвращает JSON-ответ | `string value` |
| SerializeResult | Server | public virtual | Сериализует объект результата | `object result` |
| GetResponse | Server | private | Выбирает обработчик по `Method` | `McpRequestBase request` |
| GetInitializationResult | Server | private | Возвращает версию протокола, имя сервера, версию модуля, флаг контекстных промптов | нет |
| GetToolCallResult | Server | private | Находит активный `AIAgentTool`, проверяет параметры, вызывает инструмент | `McpRequestBase request` |
| GetPromptList | Server | private | Склеивает встроенные промпты и контекстные действия | `McpRequestBase request` |
| GetBuiltInPromptList | Server | private | Возвращает `get_current_user_data` и `discuss` по контексту сущности | `McpRequestBase request` |
| GetContextActionPromptList | Server | private | Возвращает промпты из справочника контекстных действий | `string entityUri` |
| GetPromptResult | Server | private | Выбирает промпт по имени | `McpRequestBase request` |
| GetCurrentUserDataPromptResult | Server | private | Возвращает Id, имя, дату, день недели, подразделение, НОР пользователя | `object callId` |
| GetDiscussPromptResult | Server | private | Возвращает ссылку на сущность и `assistantId` роли "Свободное общение с ИИ" | `callId, arguments` |
| GetContextActionPromptResult | Server | private | Возвращает текст контекстного действия со ссылкой и `assistantId` его роли | `callId, externalId, arguments` |
| GetResourceList, GetResourceTemplateList | Server | private | Возвращают список ресурсов и шаблонов | нет |
| AIAgentResourceRead | Server | private | Читает ресурс по URI, ловит `EntityStrictAccessException` | `McpRequestBase request` |
| GetResourceToolList, GetResourceAIAgentRoleList | Server | private | Формируют `config://tools` и `config://assistants` | нет |
| GetEntityContentResourceResponse | Server | private | Возвращает тело документа или JSON записи справочника | `callId, resourceUri` |
| GetEntityMetadataResourceResponse | Server | private | Возвращает имя, расширение, ссылку | `callId, resourceUri` |
| GetRootEntity | Server | private static | Получает сущность с проверкой UI-фильтра, прав чтения и строгого доступа | `long id, Guid type` |
| GetEntityAIAgentPropertyValues | Server | private static | Собирает словарь "имя свойства - значение" | `entity, propertyInfos` |

Приватные хелперы разбора URI, ответов и reflection (`Helpers/`) в таблицу не входят.

## Сущности

Своих сущностей нет. Структуры модуля: `EntityResourceRequestParams` (Url, Type, Id), `LinkedEntityIdentity` (Id, TypeGuid), устаревшая `DocumentResourceRequestParams`. Модуль читает сущности Commons: `AIAgentTool`, `AIAgentRole`, `AIAgentContextAction`. См. [ENTITIES.md](ENTITIES.md).

## Типичные задачи

1. Добавить встроенный промпт:
   1. Добавь имя в `Constants.Module.McpPrompt`.
   2. Добавь метод `Get...PromptDefinition` и вызов в `GetBuiltInPromptList`.
   3. Добавь `case` в `GetPromptResult` и метод `Get...PromptResult`.
   4. Добавь строки в `Module.resx` и `Module.ru.resx` и ключ в `ResourcesKeys` в `Module.mtd`.
2. Создать системное контекстное действие:
   1. Открой `ModuleInitializer`.
   2. Создай структуру `Sungero.Commons.Structures.AIAgentContextAction.ContextActionInfo.Create()`.
   3. Заполни `Sid`, `Name`, `Description`, `EntityTypeGuid`, `MessageText`, `AIAgentRoleSid`.
   4. Вызови `Commons.PublicFunctions.AIAgentContextAction.GetOrCreateContextAction`.
3. Изменить JSON сущности для помощника:
   1. Добавь в серверные функции сущности `GetAIAgentJson()` (возвращает `string`) или `GetAIAgentProperties()` (возвращает `List<Commons.Structures.Module.IEntityPropertyInfo>`).
   2. Не добавляй параметры. Модуль не найдет функцию с параметрами.
4. Включить контекстные промпты: установи параметр Docflow `AIAgentDiscussionEnabled` в `True`.
5. Добавить новый MCP-метод:
   1. Добавь имя в `Constants.Module.McpMethod`.
   2. Добавь ветку в `GetResponse`.

## Ограничения и подводные камни

- Клиент должен передать `id` запроса. Для `notifications/initialized` ответ другого вида (`list_changed`). В коде это помечено как HACK.
- `GetEntityTypeMetadata` читает тип сущности только из ссылки с параметрами `type` и `id`.
- Если `_meta.entityUri` не передан, `prompts/list` возвращает `get_current_user_data` и `discuss`. Если передан, `get_current_user_data` не возвращается.
- Промпт `discuss` пропадает, если параметр `AIAgentDiscussionEnabled` выключен или роль "Свободное общение с ИИ" закрыта.
- Тело документа больше 7 340 032 байт (`MaxAllowedDocumentSize`) вызывает исключение.
- Сущность со строгим доступом (`StrictMode != None`) вызывает `EntityStrictAccessException`. Клиент получает ошибку `ResourceNotFound`.
- Имена дней недели в `get_current_user_data` только на русском языке.
- Ошибка внутри `GetAIAgentJson` или `GetAIAgentProperties` превращается в общее сообщение `InternalErrorWhileGeneratingResponse`.
- Запись справочника отдается как `.json`, документ как бинарное тело в base64.
- Не проверено: назначение зависимости с Id `ec7b606a-...`; содержимое unit-тестов в `D:\Projects\master\RX\src\Tests\IntegrationAIAgent.Unit.Tests`.

# Сущности и структуры модуля IntegrationAIAgent

Модуль не содержит типов сущностей: нет документов, справочников, задач, заданий и уведомлений. В пакете нет папок сущностей, в `Module.mtd` нет описаний типов.

## Структуры модуля (Shared/ModuleStructures.cs)

| Имя | Тип | Назначение | Свойства |
|---|---|---|---|
| EntityResourceRequestParams | структура | Параметры запроса ресурса сущности | Url (string), Type (Guid), Id (long) |
| LinkedEntityIdentity | структура | Идентификатор связанной сущности (сильная или слабая ссылка) | Id (long), TypeGuid (Guid) |
| DocumentResourceRequestParams | структура, `[Obsolete]` | Параметры запроса ресурса документа. Используй `EntityResourceRequestParams` | Url, Type, Id |

`PublicStructures` в `Module.mtd` пусты: другие модули эти структуры не видят.

## Сущности других модулей, которые использует код

| Имя | Модуль | Тип | Как использует модуль |
|---|---|---|---|
| AIAgentTool | Sungero.Commons | справочник | Инструмент ИИ-помощника. Поля: ToolName, Description, Status, IntentExamples, HandlerParams, Sid |
| AIAgentRole | Sungero.Commons | справочник | Роль помощника. Поля: Name, ExternalId, Description, Greeting, SystemPrompt, RoleIcon, Tools, SpecialTools, IsQASearchEnabled, Sid |
| AIAgentContextAction | Sungero.Commons | справочник | Контекстное действие. Поля: ExternalId, Name, Discription, MessageText, AIAgentRole |

Поля перечислены по использованию в коде модуля. Полные метаданные смотри в `Sungero.Commons`.

# Сущности Sungero.Commons

Все сущности — справочники (тип `DatabookEntry`), если не указано другое. Данные взяты из `*.mtd`. Подчинённые коллекции записаны как `Сущность@Коллекция`.

## 1. Территории и финансы

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| Country | Страны | Name, Code |
| Region | Регионы страны | Name, Country, Code |
| City | Населённые пункты | Name, Region, Country |
| Currency | Валюты | Name, AlphaCode, NumericCode, ShortName, FractionName, IsDefault |
| VatRate | Ставки НДС | Name, Rate, Sid |

## 2. Параметры

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| Parameter | Системный параметр с типизированным значением | Name, Sid, Code, Group, ValueType, Value, Boolean/Date/Double/Identifier/Integer/String/PasswordValue, Note |
| ParametersGroup | Группа параметров | Name, Sid, Note |

`Parameter.ValueType`: IntegerValue, DoubleValue, IdentifierValue, StringValue, BooleanValue, DateValue, PasswordValue. Действия: ShowDuplicates, SetPassword.

## 3. ИИ-помощник

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| AIAgentRole | Роль ИИ-помощника | Name, RoleIcon, Description, Greeting, SystemPrompt, Tools, SpecialTools, IsQASearchEnabled, Sid, IsSystem, ExternalId |
| AIAgentRole@Tools | Обычные инструменты роли | AIAgentRole, Tool |
| AIAgentRole@SpecialTools | Специальные инструменты роли с настройками | AIAgentRole, Tool, ToolOptions |
| AIAgentTool | Инструмент, который помощник вызывает | Name, ToolName, Description, ToolType, HandlerName, HandlerModule, OriginalEntityType, EntityTypeName, HandlerParams, IntentExamples, IsExecConfirmRequired, Sid, IsSystem |
| AIAgentTool@Constants | Постоянные значения параметров инструмента | AIAgentTool, Name, Value |
| AIAgentTool@HandlerParams | Параметры функции-обработчика | Name, Type, Description, IsRequired, SourceTool |
| AIAgentTool@IntentExamples | Примеры запросов пользователя | AIAgentTool, Example |
| AIAgentContextAction | Контекстное действие помощника для типа сущности | Name, Discription, EntityType, OriginalEntityType, MessageText, AIAgentRole, Sid, IsSystem, ExternalId |
| AIRequestQueueItem | Элемент очереди запросов к GenAI | Name, UserId, EntityId, EntityType, CompoundPromptId, Response, Updated, ProcessState |
| AIRequestQueueItem@PromptStringArguments | Строковые аргументы промпта | Name, Value |
| AIRequestQueueItem@PromptBinaryArguments | Бинарные аргументы промпта | Name, Value |
| AIRequestQueueItem@AdditionalParams | Дополнительные параметры | Key, Value |

`AIAgentTool.ToolType`: GetEntityInfo, GetEntityIds, ExecuteFunction, GetEntityCard. Действия AIAgentTool: CopyChildEntity, AddChildEntity, DeleteChildEntity, DeleteEntity, SelectEntityType. У AIAgentContextAction есть действие SelectEntityType.

## 4. GenAI

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| GenAIService | Подключение к сервису языковых моделей | Name, ServiceUrl, AccessToken, AuthorizationUrl, ApiType, GigaChatAPIScope, AuthorizationScheme, IsSystem, Sid |
| GenAIModel | Языковая модель сервиса | Name, TokenSize, ContextSize, GenAIService, Note, Sid, IsSystem |
| SystemPromptBase | Системный промпт | Name, PromptText, GenAIModel, ResponseFormat, IsSystem, Sid |
| CompoundPromptBase | Составной промпт из нескольких шагов | Name, Prompts, SystemPrompt, UserPrompt, Temperature, GenAIModel, RequestParams, IsAdvanced, IsSystem, Sid |
| CompoundPromptBase@Prompts | Шаги составного промпта | Number, Prompt, Role |
| CompoundPromptBase@RequestParams | Имена параметров запроса | Name |

`GenAIService.ApiType`: GigaChat, YandexGPT, OpenAI. `AuthorizationScheme`: Bearer, ApiKey. `GigaChatAPIScope`: Personal, Business, Corporate.
В папке `Sungero.Commons.Server` есть ещё `GenAIRequest`, `GenAIRequestTemplateBase`, `EntityProcessingCompoundPromptBase`. Их mtd не найден в `Shared` — назначение не проверено.

## 5. Распознавание Ario

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| EntityRecognitionInfo | Результат распознавания сущности | Name, RecognizedClass, ClassProbability, VerifiedClass, EntityId, EntityType, Facts, AdditionalClassifiers, DocTypeClassifierTrainingStatus, DocTypeClassifierTrainingSession, Created, Verified |
| EntityRecognitionInfo@Facts | Факты и их связь со свойствами | FactId, FieldId, PropertyName, PropertyValue, FactName, FieldName, FieldValue, FieldProbability, Position, VerifiedValue, FactLabel |
| EntityRecognitionInfo@AdditionalClassifiers | Результаты дополнительных классификаторов | ClassifierID, PredictedClass, Probability |
| ClassifierTrainingSession | Сессия обучения классификатора | Name, ClassifierId, OldModelId, NewModelId, FMeasure, ArioTaskId, TrainingStatus, ClassifierType |

## 6. Интеграция

| Сущность | Назначение | Ключевые свойства |
|---|---|---|
| ExternalEntityLink | Связь записи RX с записью внешней системы | EntityType, EntityId, ExtEntityType, ExtEntityId, ExtSystemId, SyncDate, IsDeleted, Name |

## 7. Публичные структуры модуля

ArioFact, ArioFactField, ArioFactElasticsearchData, ArioFieldElasticsearchData, ArioSignature, ArioStamp, EntityActionData, EntityPropertyInfo, EntityValidationResult, TextParameter, TextParameterFormatter, TextParameterMatch. Файл: `Sungero.Commons.Shared/ModuleStructures.cs`.
Структуры сущностей (например `AIAgentContextAction.ContextActionInfo`, `ObjectType`) лежат в `*Structures.cs` рядом с сущностью.

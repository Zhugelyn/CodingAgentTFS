# Сущности модуля GenAIProcessing

Все сущности объявлены в `Sungero.GenAIProcessing.Shared/<Имя>/<Имя>.mtd`. Базовые типы `CompoundPromptBase` и `SystemPromptBase` находятся в `Sungero.Commons`.
Свойства указаны так, как они заданы в mtd самой сущности. Унаследованные свойства не перечислены.

## Составные промпты

Составной промпт — шаблон запроса к ИИ: модель, системный промпт, пользовательский текст, параметры. Табличные части `Prompts` (вложенные промпты) и `RequestParams` (параметры запроса) есть у каждого промпта.

| Сущность | Тип / база | Назначение | Ключевые свойства |
|---|---|---|---|
| `EntityProcessingCompoundPromptBase` | Справочник, абстрактный, база `Commons.CompoundPromptBase` | Общая база промптов обработки сущностей | GenAIModel, SystemPrompt, UserPrompt, Prompts (Role), RequestParams, PromptGroup |
| `DocProcessingCompoundPrompt` | Справочник, база `EntityProcessingCompoundPromptBase` | Промпт проверки документа (договоры) | SystemPrompt (`DocProcessingSystemPrompt`), Prompts, RequestParams |
| `ProcessKindCompoundPrompt` | Справочник, база `CompoundPromptBase` | Промпт для блоков процесса | IsSystem, SystemPrompt, UserPrompt, IsAdvanced, GenAIModel, Sid, Temperature, Description. Действие `ShowLinkedProcessKinds` |
| `SummaryGenerationCompoundPrompt` | Справочник, база `CompoundPromptBase` | Промпт аннотации документа по типу задачи | TaskTypeName, TaskTypeGuid, DocumentKinds, Status, Sid, Temperature, IsSystem, IsAdvanced. Действие `SelectTaskType` |
| `DocGenerationCompoundPrompt` | Справочник, база `CompoundPromptBase` | Промпт генерации текста документа | DocumentKinds, Status, Sid, Temperature, UserPrompt, IsAdvanced, IsSystem |
| `OutgoingLetterGenerationCompoundPrompt` | Справочник, база `CompoundPromptBase` | Промпт генерации исходящего письма | UserTextBased, IncomingLetterBased, DocumentKinds, Sid, Temperature |

Табличные части видов документов: `DocGenerationCompoundPromptDocumentKinds`, `SummaryGenerationCompoundPromptDocumentKinds`, `OutgoingLetterGenerationCompoundPromptDocumentKinds`. Свойство: `DocumentKind` (`Docflow.DocumentKind`).
Табличные части `...Prompts` и `...RequestParams` есть для: `DocGeneration`, `DocProcessing`, `EntityProcessingBase`, `OutgoingLetterGeneration`, `ProcessKind`, `SummaryGeneration`.

## Системный промпт

| Сущность | Тип / база | Назначение | Ключевые свойства |
|---|---|---|---|
| `DocProcessingSystemPrompt` | Справочник, база `Commons.SystemPromptBase` | Системный промпт (роль ассистента) для проверки документов | Id, Status |

## Правила проверки и группы

| Сущность | Тип / база | Назначение | Ключевые свойства |
|---|---|---|---|
| `AIProcessingRuleBase` | Справочник, абстрактный | База правил проверки сущностей | Name, Description, Status, IsSystem, Prompts. Действия `DeleteEntity`, `AddPrompts` |
| `DocAIProcessingRule` | Справочник, база `AIProcessingRuleBase` | Правило проверки документа | Prompts (ссылки на `DocProcessingCompoundPrompt`). Действие `AddPrompts` |
| `EntityProcessingPromptGroup` | Справочник | Группа промптов обработки | Name, Description, Status, IsSystem. Действие `DeleteEntity` |

Табличные части: `AIProcessingRuleBasePrompts` (Prompt), `DocAIProcessingRulePrompts` (Prompt).

## Очередь и результаты проверки

| Сущность | Тип / база | Назначение | Ключевые свойства |
|---|---|---|---|
| `EntityAIProcessingQueueItem` | Справочник-очередь | Элемент очереди проверки сущности по правилу | Name, RuleId, EntityId, EntityType, UserId, ProcessingStatus (Created, InProcess, Success, Error), Requests, AdditionalParams |
| `EntityAIProcessingInfo` | Справочник | Результат проверки сущности | Name, EntityId, EntityType, BinaryBody, CreateDate, ErrorMessage, ProcessingStatus (InProcess, Error, Success), Rule, AssociatedApplication, Responses, AdditionalParams |

Табличные части очереди: `EntityAIProcessingQueueItemRequests` (RequestId), `EntityAIProcessingQueueItemAdditionalParams` (Key, Value).
Табличные части результата: `EntityAIProcessingInfoResponses` (CompoundPromptId, Response, Status), `EntityAIProcessingInfoAdditionalParams` (Key, Value).

## Структуры модуля (PublicStructures)

`AIProcessingParsedResponse` (Status), `AIProcessingReportData` (ReportHeader, EntityHeader, ResponseGroups), `AIProcessingReportLine` (Name, Value), `AIProcessingReportResponse`, `AIProcessingReportResponseGroup`, `AIProcessingReportText`, `ExtractedText`, `TaskType`. Все публичные, большая часть изолированные. Поля у части структур не проверены.

## Блоки процесса

| Блок | Входные свойства | Выходные свойства |
|---|---|---|
| `GenAIDocumentProcessingBlock` | Prompt, ProcessingDocument | ExecutionResult (Error), ProcessedDocument |
| `GenAITextProcessingBlock` | Prompt, ProcessingText | ExecutionResult (Error), ProcessedText |
| `ProcessDocAIRuleBlock` | Period, Deadline, DocAIProcessingRule, EntityAIProcessingInfos, EntityAIProcessingQueueItems, ProcessingDocuments, ProcessingState | ExecutionResult, EntityAIProcessingInfoResults, CriticalRiskCount, HighRiskCount |

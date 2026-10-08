---
name: directum-rx-genai-processing
description: Модуль GenAIProcessing в Directum RX — обработка документов и текстов генеративным ИИ - аннотации, генерация текста документов, правила проверки, составные промпты, запросы к ИИ. Use when working with GenAIProcessing, CompoundPrompt, AIRequestQueueItem, EntityAIProcessingInfo, аннотация, генеративный ИИ, правила проверки ИИ.
---

# Модуль Sungero.GenAIProcessing

Модуль отправляет документы и тексты в генеративный ИИ и принимает ответы. Он создает аннотации к документам, генерирует тексты документов и писем, проверяет договоры по правилам.
Модуль хранит составные промпты (шаблоны запросов), правила проверки и результаты проверок.
Сами запросы и модели ИИ живут в модуле `Sungero.Commons` (`AIRequestQueueItem`, `GenAIModel`, `CompoundPromptBase`).

Путь: `D:\Projects\master\RX\src\Packages\Sungero.GenAIProcessing`. Код модуля: `GnAIPrc`.

## Схема

```
Клиент (кнопка, блок процесса)
   |  [Public] клиентские функции: CreateSummaryOnUserRequest, ShowGenerationResultDialog
   v
Server: Functions.Module --- GetOrCreateRequest ---> Commons.AIRequestQueueItem (запрос)
   |                                                        |
   |  асинхронный обработчик SendAIRequest / GenerateText   |
   v                                                        v
ExtractTextFromEntity --> Isolated: DocTextExtractor   ProcessAIRequestQueueItem
 (Word, PDF, Ario)        (текст из тела документа)      --> GenAIModel --> GenAIService
                                                             |
   ответ <---------------------------------------------------+
   v
Аннотация / текст документа / EntityAIProcessingInfo (результат правила)
   v
Isolated: AIProcessingReportGenerator (отчет по проверке)
```

Зависимости (Module.mtd): DirectumRX (решение), Commons, Docflow, DocflowApproval, Contracts, Parties (минимум 25.3.0.0).

## Как работать с модулем

Термины. Составной промпт — шаблон запроса с системным и пользовательским текстом. Правило — набор промптов для проверки документа. Запрос — запись `Commons.AIRequestQueueItem`. АО — асинхронный обработчик.

- Из другого модуля вызывай функции так: `GenAIProcessing.PublicFunctions.Module.<Имя>(...)`.
- Константы вызывай так: `GenAIProcessing.PublicConstants.Module.<Имя>`. Sid промптов лежат в группах `CompoundPromptsSids`, `DocProcessingCompoundPromptSids`, `SystemPromptBaseSids`.
- Функции сущностей вызывай так: `GenAIProcessing.PublicFunctions.<Сущность>.<Имя>(...)`.
- Клиентские функции не вызывай из серверного кода.
- Библиотека `Sungero.GenAIProcessingExtensions.dll` (Libraries, Scope Both) — состав не проверен.

### Фоновые процессы

| Имя | Вид | Что делает |
|---|---|---|
| `SendAIRequest` | АО | Берет текст сущности, заполняет аргументы запроса, вызывает `ProcessAIRequestQueueItem` |
| `GenerateText` | АО | Генерирует текст документа по запросу |
| `PrepareAnnotation` | АО | Готовит аннотацию документа |
| `ProcessAIRule` | АО | Ведет `EntityAIProcessingQueueItem`: отправляет запросы по промптам правила, опрашивает статус |
| `GenerateAIProcessingInfo` | АО | Создает `EntityAIProcessingInfo` из результатов запросов |
| `DeleteAIRequestQueueItems` | Job | Раз в месяц удаляет старые запросы |
| `DeleteAIProcessingInfos` | Job | Раз в неделю удаляет результаты старше срока хранения (параметр из Commons) |

Задержка АО `SendAIRequest`, `GenerateText`, `PrepareAnnotation`, `GenerateAIProcessingInfo`: у первых трех Exponential, у `GenerateAIProcessingInfo` Regular (2). `ProcessAIRule` — Regular, `MaxRetryCount` 30.

### Инициализация

`ModuleInitializer.FirstInitializing` выполняет шаги:
1. `FillPersonalSettings` — ставит `NeedAutoSummarize = true` в персональных настройках.
2. `CreateEntityProcessingPromptGroups` — создает группы промптов.
3. `CreateSystemBasePrompts` — создает системные промпты.
4. `CreateSystemCompoundPrompts` — создает составные промпты на модели по умолчанию.
5. `CreateAIProcessingRules` — создает правила.
6. `GrantRightsOnDatabooks` — выдает права на справочники.

### Блоки процесса

`GenAIDocumentProcessingBlock`, `GenAITextProcessingBlock`, `ProcessDocAIRuleBlock` — скриптовые блоки. Их свойства описаны в [ENTITIES.md](ENTITIES.md).

## Таблица функций (Functions.Module)

| Функция | Слой | Атрибут | Что делает | Параметры |
|---|---|---|---|---|
| `CreateAndProcessAIRequestQueueItem` | Server | Public | Создает запрос, сразу выполняет, возвращает ответ | text, textParam, compoundPrompt, documentKind, entity |
| `ProcessAIRequestQueueItem` | Server | Public | Отправляет запрос в модель и сохраняет ответ | request |
| `CheckGenAIConnection` (2 перегрузки) | Server | Public (WebApi Post для modelId) | Проверяет связь с моделью | modelId или model |
| `GetAvailableTaskTypes` | Server | Public | Возвращает типы задач, доступные для аннотаций | — |
| `ExtractTextFromDocument` | Server | Public, Remote | Достает текст последней версии документа | document |
| `GetGeneratedText` | Server | Public, Converter | Возвращает сгенерированный текст документа | document |
| `GetTemplatesForGeneratedText` | Server | Public, Remote(IsPure) | Возвращает шаблоны для сгенерированного текста | documentId |
| `GetOrCreateRequest` | Server | Public, Remote | Находит запрос пользователя или создает новый | entity, compoundPrompt |
| `CreateAIRequestQueueItem` | Server | Public, Remote | Создает запрос (без сохранения) | entityId, entityType, compoundPromptId |
| `GetAIRequestQueueItem` (2 перегрузки) | Server | Public, Remote(IsPure) | Ищет запрос автора по промпту или списку промптов | entity, compoundPromptId(s), author |
| `GetProcessedDocument` | Server | Public, Remote(IsPure) | Возвращает документ, обработанный в блоке | workflowEntity |
| `CreateReportLine` | Server | Public | Создает строку отчета проверки | lineHeader, lineValue, entity |
| `CreateDocGenerationCompoundPrompt` | Server | Public | Создает промпт генерации текста документа | model, sid, name, temperature, documentKindGuids, prompts |
| `CreateDocSummaryCompoundPrompt` | Server | Public | Создает промпт аннотации | model, sid, name, temperature, typeTaskGuid, documentKindGuids, prompts |
| `GetOrCreateOutgoingLetterGenerationCompoundPrompt` | Server | Public | Находит или создает промпт исходящего письма | model, sid, name, temperature, incomingLetterBased, userTextBased, prompts |
| `CheckDocumentVersionReadability` (2 перегрузки) | Shared | Public | Возвращает текст ошибки, если версию нельзя прочитать | document, isInvariantCulture |
| `CheckIsOfficialAndReadableDocument` | Shared | Public | Проверяет, что документ официальный и читаемый | electronicDocument |
| `IsRichDocumentExtension` | Shared | Public | Проверяет, что расширение относится к форматам Word | extension |
| `GetCreateSummaryHintTexts` | Shared | Public | Возвращает тексты подсказок | — |
| `UncapitalizeString` | Shared | Public | Делает первую букву строчной | input |
| `GetTaskType` | Shared | Public | Возвращает Guid типа задачи | workflowEntity |
| `CreateSummaryOnUserRequest` | Client | Public | Создает аннотацию по кнопке пользователя | workflowEntity |
| `CreateSummaryAutomatically` | Client | Public | Создает аннотацию автоматически | workflowEntity |
| `PasteSummary` | Client | Public | Вставляет аннотацию | workflowEntity |
| `AddSummaryToActiveText` | Client | Public | Добавляет аннотацию в активный текст | workflowEntity, document, taskTypeGuid |
| `HasGenAILicense` | Client | Public | Проверяет лицензию на ИИ | — |
| `StartGenerateTextAsyncHandler` | Client | Public | Запускает АО `GenerateText` | request, document |
| `ShowGenerationResultDialog` | Client | Public | Показывает диалог результата генерации | document |
| `ShowLastEntityAIProcessingInfo` (2 перегрузки) | Client | Public (Hyperlink для documentId) | Открывает последний результат проверки | documentId или document |

Функции сущностей с `[Public]`:
- `DocAIProcessingRule.GetLastEntityAIProcessingInfo(document)` — Remote(IsPure).
- `OutgoingLetterGenerationCompoundPrompt.GetActiveCompoundPrompts` — Remote(IsPure).
- `SummaryGenerationCompoundPrompt.HasOtherActiveCompoundPromptsForTaskType(taskTypeGuid)` и `CanAutoSummarize(task)` — Remote(IsPure).

Isolated:
- `DocTextExtractor.GetText(stream)` и `DeleteHeadersAndFooters(stream, extension)` (doc, docx).
- `AIProcessingReportGenerator.Generate(reportData, extension)` и `ParseResponse(response)`.

## Сущности

Модуль содержит 12 основных сущностей (справочники) и их табличные части. Группы:
- Составные промпты: `EntityProcessingCompoundPromptBase`, `DocProcessingCompoundPrompt`, `ProcessKindCompoundPrompt`, `SummaryGenerationCompoundPrompt`, `DocGenerationCompoundPrompt`, `OutgoingLetterGenerationCompoundPrompt`.
- Правила проверки: `AIProcessingRuleBase`, `DocAIProcessingRule`, `EntityProcessingPromptGroup`.
- Системный промпт: `DocProcessingSystemPrompt`.
- Очередь и результаты: `EntityAIProcessingQueueItem`, `EntityAIProcessingInfo`.

См. [ENTITIES.md](ENTITIES.md).

## Типичные задачи

Получить ответ ИИ по тексту из другого модуля:
1. Найди активный `CompoundPromptBase` по `Sid`.
2. Вызови `PublicFunctions.Module.CreateAndProcessAIRequestQueueItem(text, textParam, prompt, documentKind, entity)`.
3. Обработай `AppliedCodeException`: функция бросает ее при ошибке запроса.

Добавить системный промпт аннотации:
1. Добавь константу Sid в `ModuleConstants.cs` с атрибутом `[Public]`.
2. Вызови `CreateDocSummaryCompoundPrompt` из `ModuleInitializer` (см. `CreateSystemCompoundPrompts`).
3. Добавь тексты промпта в ресурсы и в `ResourcesKeys` (не проверено, как именно хранятся тексты).

Показать результат проверки документа:
1. Вызови на клиенте `PublicFunctions.Module.ShowLastEntityAIProcessingInfo(document)`.

Запустить правило проверки:
1. Создай `EntityAIProcessingQueueItem` функцией `CreateEntityAIProcessingQueueItem`.
2. Запусти АО вызовом `CreateProcessAIRuleAsyncHandler(queueItemId)`.

## Ограничения

- `CreateAndProcessAIRequestQueueItem` работает синхронно и бросает исключение, если запрос завершился ошибкой.
- `ExtractTextFromDocument` бросает `AppliedCodeException`, если у документа нет версий, расширение не поддерживается или нет текстового слоя.
- `SendAIRequest` не пробрасывает исключения: он пишет ошибку в запрос через `SetError` и логирует.
- `ProcessAIRule` держит `args.Retry = true`, пока хоть один запрос в состоянии `InProcess`. Предел — 30 повторов.
- `GetOrCreateRequest` может вернуть новый запрос без `Save`: сохраняй его сам.
- Инициализация бросает `AppliedCodeException`, если модель ИИ по умолчанию не найдена в Commons.
- `GetGeneratedText` помечена конвертером `GetGeneratedTextConverterName`. Не меняй ее сигнатуру.
- `CreateEntityAIProcessingQueueItem`, `CreateProcessAIRuleAsyncHandler` и АО-методы без `[Public]`: из другого модуля они недоступны.

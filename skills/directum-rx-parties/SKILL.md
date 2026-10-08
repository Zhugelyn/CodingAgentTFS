---
name: directum-rx-parties
description: Модуль Sungero.Parties — контрагенты Directum RX (организации, банки, персоны, контакты), обмен через абонентские ящики, проверка контрагентов в Контур.Фокус, маркеры надёжности, поиск дублей, Elasticsearch. Use when working with Parties, Counterparty, Company, Bank, Person, Contact, ИНН/КПП, дубли контрагентов, DueDiligence.
---

# Модуль Sungero.Parties

Parties хранит контрагентов: организации, банки, персон и их контактных лиц. Модуль проверяет реквизиты и ищет дубли. Он загружает отчёты и маркеры надёжности из Контур.Фокус. Он хранит настройки эл. обмена контрагента.

Путь: `RX/src/Packages/Sungero.Parties`. Версия в `Module.mtd`: 26.3.0.15. Guid модуля: `243b34ec-8425-4c7e-b66f-27f7b9c8f38d`.
Зависимости из `Module.mtd`: `Sungero.Commons` (минимум 3.4.0.0), `Sungero.ExchangeCore` (минимум 3.4.0.0), решение `DirectumRX`, ещё один модуль с guid `ec7b606a-21ee-4f16-aba8-ab8c2af76d12` (имя не определено). Код также вызывает `Docflow`, `Company`, `DueDiligenceLic`.
Все слои есть: ClientBase, Shared, Server, Isolated (`MarkersParser`), Libraries (`CompanyData`, `PartiesDueDiligenceExtensions`, `PhoneNumbers`), Settings.

Сущности: см. [ENTITIES.md](ENTITIES.md).

## Схема

```
Counterparty (abstract) ── ExchangeBoxes (ТЧ) ──> ExchangeCore.BusinessUnitBox
 ├── CompanyBase (abstract) ── HeadCompany
 │    ├── Company ── Kind ──> CounterpartyKind
 │    └── Bank
 └── Person ── IdentityKind ──> IdentityDocumentKind
Contact ──> Company, Person

Проверка контрагентов (нужна лицензия DueDiligenceLic):
 DueDiligenceServiceConnection (Uri, ApiKey)
   |-- async: Express/Financial/StatementReportLoadingFromDueDiligenceService -> CounterpartyDocument (PDF)
   |         защита от дублей запросов: DueDiligenceQueueItem
   |-- Job GetAndSyncMarkers -> SyncMarkersQueueItem -> async SyncMarkers
   |         (Isolated MarkersParser разбирает JSON) -> MarkerValueBase / ChangedMarkerQueueItem
   '-- Job SendChangedMarkersReports -> CounterpartyMonitoringRule (маркеры, ответственные)
 CounterpartyDueDiligenceHistory (Result = DueDiligenceStatus)
```

## Как вызвать функции из другого модуля

1. Вызывай функции модуля так: `Sungero.Parties.PublicFunctions.Module.GetCounterpartyIds(tin, trrc, name)`.
2. Вызывай функции сущности так: `Sungero.Parties.PublicFunctions.Counterparty.GetDuplicateCounterparties(tin, trrc, name)`.
3. Читай константы так: `Sungero.Parties.PublicConstants.Module.LegalEntityTinLength`, `...RolesGuid.DueDiligenceResponsibleRole`.
4. Передавай данные через публичные структуры: `PublicStructures.CounterpartyDueDiligenceHistory.HistoryRecordCreationInfo`.
5. Из клиента вызывай серверную функцию через `Functions.Module.Remote.X(...)` (нужен `[Remote]`).
6. Помни: экземплярная функция сущности принимает сущность первым аргументом: `PublicFunctions.Person.GetFullName(person, declension)`.

Публичные константы модуля: `LegalEntityTinLength` (10), `PersonTinLength` (12), `EntrepreneurPsrnLength` (15), `EmailRegex` (устарела), `HomepageRegex`, `InitialsRegex`, `InitializeExternalLinkSystem`, `RolesGuid.*` (5 ролей), `IdentityDocumentKindsGuid.*` (SID 15 видов документов), `IdentityDocumentKindPropsPattern.*` (шаблоны проверки), `AIAgentToolsInfo.CompanyExecFunction`.

## Слои, обработчики, задания

- **Server**: функции в `ModuleServerFunctions/` (разбиты по темам: `1C`, `AIAgentTool`, `CounterpartyDuplicates`, `ElasticSearch`, `IdentityDocuments`, `Reports`, `Shell`, `CounterpartyDueDiligence/*`, `Obsolete`).
- **Shared**: `ModuleSharedFunctions.cs` — email, СНИЛС, фамилия с инициалами, скрытие телефона.
- **ClientBase**: `ModuleClientFunctions.cs` — действия обложки, приглашение к обмену, переход на сайт. Клиентские функции не имеют `[Public]`.
- **Isolated**: область `MarkersParser` разбирает JSON-ответы Контур.Фокус (7 функций `Deserialize*` и `GetJsonElementsCount`).
- **Jobs** (`ModuleJobs.cs`): `GetAndSyncMarkers` (получить и рассчитать маркеры) и `SendChangedMarkersReports` (отправить отчёты об изменённых маркерах). Расписание в `Module.mtd` — `MonthSchedule: Monthly`, `StartAt` 00:00 и 07:00 (точное правило повтора не проверено).
- **Async handlers** (`ModuleAsyncHandlers.cs`): `UpdateContactName(PersonId)`, `ExpressReportLoadingFromDueDiligenceService`, `FinancialReportLoadingFromDueDiligenceService`, `StatementReportLoadingFromDueDiligenceService` (все с `CounterpartyId`), `SyncMarkers(QueueItemId)`.
- **Инициализация** (`ModuleInitializer.cs`, событие `InitializingServer`): создаёт роли, сайты проверки, статусы «Approved»/«Rejected», подключение к сервису, предустановленные группы маркеров, правила мониторинга, виды документов личности. Обновляет банки из справочника ЦБ (SQL). Выполняется по версиям: `Initializing261`, `Initializing262`, `Initializing263`.
- **Публичные функции инициализации**: `PublicInitializationFunctions.Module.CreateIdentityDocumentKind(...)`, `GrantRightsOnCounterpartyKind(role)`.
- **Обработчики сущностей**: `CounterpartyHandlers.BeforeSave` проверяет ИНН, ОГРН, ОКПО и дубли. `PersonHandlers.AfterSave` запускает асинхронное обновление имён сотрудника и контакта. `CompanyBaseHandlers`/`ContactHandlers.AfterSave` ставят сущность в индекс Elasticsearch.

## Функции модуля

Атрибут `GET`/`POST` — `WebApiRequestType` для вызова через веб-API. Слой `Server` — файл в `Sungero.Parties.Server`.

| Функция | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| `FindCounterparty` | Server | `[Public, Remote(IsPure)]` | Ищет контрагента по ссылке из 1С, иначе по ИНН/КПП | `uuid, tin, trrc, sysid` |
| `GetCounterpartyIds` / `GetCompanyIds` | Server | `[Public]` GET | Возвращает ИД найденных контрагентов / организаций | `tin, trrc, name` |
| `GetPersonIds` | Server | `[Public]` GET | Возвращает ИД найденных персон | `tin, name` |
| `GetBankIdsFromContracts` | Server | `[Public]` GET | Возвращает ИД банков из договоров | нет |
| `GetDuplicateCounterparties` (2 перегрузки), `GetDuplicateCounterpartiesFromList` | Server | `[Remote(IsPure)]` | Ищет дубли контрагентов по ИНН, КПП, имени | `tin, trrc, name, excludedCounterpartyId, excludeClosed` |
| `CanEditIdentityDocuments` | Server | `[Remote(IsPure)]` | Проверяет право править документы личности | нет |
| `CreateCompany`, `CreateContact`, `CreatePerson` | Server | `[Remote]` | Создаёт сущность для действий обложки | нет |
| `CounterpartiesAvailableForExchange` | Server | `[Remote]` | Возвращает контрагентов с `CanExchange == true` | нет |
| `GetContactsFromCompany` | Server | `[Remote(IsPure)]` | Возвращает контакты организации | `company` |
| `GetEmailByTinTrrc` | Server | `[Remote(IsPure)]` | Возвращает email первого контрагента по ИНН/КПП | `tin, trrc` |
| `FindCompanyInExchangeServices` | Server | `[Remote]` | Ищет организацию в сервисах обмена | `tin, trrc, boxes` |
| `FindCounterpartyByOrganizationId` | Server | `[Remote]` | Ищет контрагента по ИД в сервисе обмена | `box, organizationId` |
| `ReindexCompanyBases`, `ReindexContacts` | Server | `[Public]` | Пересоздаёт индекс Elasticsearch и индексирует пачками | нет |
| `UpdateCompanyIndexSynonyms` | Server | `[Public]` POST | Обновляет синонимы в индексе организаций | `synonyms` |
| `GetCompanyIndexSynonyms` | Server | `[Public]` GET | Возвращает синонимы индекса | нет |
| `NormalizePhone`, `NormalizeSite` | Server | `[Public]` | Приводит телефон и сайт к виду для поиска | `phone` / `site` |
| `GetFullNameVariants` | Server | `[Public]` | Строит варианты порядка ФИО | `sourceName` |
| `GetCompanyExecFunctionInfo`, `GetCompanyExecFunctionToolInfo` | Server | `[Public]` | Описывают инструмент ИИ-агента «поиск организаций» | нет |
| `AIAgentRefreshCompanyExecFunctionTool` | Server | `[Public]` POST | Обновляет инструмент ИИ-агента | нет |
| `AIAgentGetContactInfo` | Server | `[Public]` | Ищет контакты для ИИ-агента | `tool, personName, companyName, jobTitle` |
| `GetCompanyAIAllowedProperty`, `GetContactAIAllowedProperty`, `GetPersonAIAllowedProperty` | Server | `[Public]` | Возвращают свойства, разрешённые ИИ-агенту | `getSubEntityProperty` (только у Company) |
| `GetResponsibilitiesReportData` | Server | `[Public]` | Даёт строки отчёта полномочий сотрудника | `employee` |
| `GetDocumentKindForReport` | Server | `[Public]` | Возвращает вид документа отчёта по external link | `reportKind` |
| `IsCurrentUserIncludedInDueDiligenceResponsibilitiesRole` | Server | `[Public]` GET | Проверяет роль «Ответственные за проверку» | нет |
| `GetDueDiligenceResponsibilitiesRoleName` | Server | `[Public]` GET | Возвращает имя этой роли | нет |
| `GetLastCounterpartyDueDiligenceHistory`, `GetCounterpartyDueDiligenceHistory` | Server | `[Public]` GET | Возвращают последнюю / всю историю проверок как DTO | `counterpartyId` |
| `IsExpressReportInProgress`, `IsFinancialReportInProgress`, `IsStatementReportInProgress` | Server | `[Public]` GET | Проверяют, идёт ли загрузка отчёта | `counterpartyId` |
| `LoadAvailableApiMethodsFromDueDiligenceService` | Server | `[Public]` | Возвращает методы API, доступные по ключу | нет |
| `IsStatRequestExecutedSuccessfully` | Server | `[Public]` | Проверяет соединение запросом `stat` | `setting` |
| `EmailIsValid`, `EmailsAreValid`, `SplitEmails` | Shared | `[Public]`, `[Obsolete]` | Обёртки над методами `Commons` | `emailAddress(es)` |
| `CreateMailtoHyperlink` | Shared | `[Public]` | Строит ссылку `mailto:` (первый адрес в to, остальные в cc) | `emailsAddresses, subject, body` |
| `GetFormattedInila` | Shared | `[Public]` | Форматирует СНИЛС `XXX-XXX-XXX YY` | `inila` |
| `GetSurnameAndInitialsInTenantCulture` | Shared | `[Public]` | Возвращает «Фамилия И.О.» в культуре тенанта | `firstName, middleName, lastName` |
| `IsPersonPhoneHidden` | Shared | `[Public]` | Читает параметр «скрывать личные телефоны» | нет |
| `CheckAnyBusinessUnitBoxes`, `GetCompanyDataServiceURL` | Server | `[Remote, Obsolete]` | Устарели с версии 26.2 | нет |

## Публичные функции сущностей

Вызов: `Parties.PublicFunctions.<Сущность>.<Функция>(...)`. «Экз.» — экземплярная функция, первый аргумент — сущность.

| Функция | Слой | Атрибут | Что делает | Ключевые параметры |
|---|---|---|---|---|
| `Counterparty.CheckTin` | Shared | `[Public, Remote(IsPure)]` | Проверяет ИНН, возвращает текст ошибки или пустую строку | `tin, forCompany` |
| `Counterparty.CheckAccountLength`, `CheckNceoLength` | Shared | `[Public]` | Проверяют длину счёта и ОКПО | `account` / `nceo` |
| `Counterparty.GetCounterpartyWithSameTinWarning` | Shared | `[Public]` | Возвращает предупреждение о том же ИНН | `tin, trrc, companyId` |
| `Counterparty.GetCounterpartyDuplicatesErrorText` (экз.) | Shared | `[Public]` | Возвращает текст ошибки дублей | нет |
| `Counterparty.GetDuplicateCounterparties`, `GetDuplicateCounterpartiesFromList` | Shared | `[Public]` | Вызывают серверный поиск дублей (`excludeClosed = true` по умолчанию) | `tin, trrc, name` |
| `Counterparty.GetExchangeCounterparty`, `GetActiveCounterpartyWithExchange` | Shared | `[Public, Remote(IsPure)]` | Возвращают активных контрагентов с обменом по НОР | `businessUnit` |
| `Counterparty.GetDistributionListCounterparty` | Shared | `[Public, Remote(IsPure)]` | Возвращает контрагента «Список рассылки» | нет |
| `Counterparty.SaveForcibly` (экз.) | Server | `[Public]` | Сохраняет, пропуская проверку дублей | нет |
| `Counterparty.LoadExpressReport`, `LoadFinancialReport`, `LoadStatementReport` (экз.) | Server | `[Public]` | Запускают асинхронную загрузку отчёта | `asyncStartNotify` |
| `Counterparty.CreateReportDocument` (экз.) | Server | `[Public]` | Создаёт `CounterpartyDocument` из PDF и выдаёт права | `documentKind, documentStream` |
| `Counterparty.GetLastLoadedReportForToday` (экз.) | Server | `[Public]` | Возвращает отчёт, загруженный сегодня | `documentKindId` |
| `Counterparty.AppendInfoToSummary` (экз.) | Server | `[Public]` | Добавляет блок в сводку документа | `stateBlock, headersStyle` |
| `Counterparty.GetTypeDisplayValue` | Shared | `[Public, Obsolete]` | Устарела, есть аналог в `Commons` | `entity, declension` |
| `CompanyBase.CheckTRRC`, `CheckNceoLength` | Shared | `[Public]` | Проверяют КПП и ОКПО | `trrc` / `nceo` |
| `CompanyBase.IsSelfEmployed` (экз.) | Shared | `[Public]` | Определяет ИП по длине ОГРН | нет |
| `CompanyBase.TrimLegalFormsInCompanyName` | Server | `[Public]` | Убирает правовую форму из названия | `companyName` |
| `CompanyBase.GetIndexingJson` (экз.) | Server | `[Public]` | Строит JSON документа для Elasticsearch | нет |
| `Bank.CheckSwift`, `CheckBicLength`, `CheckCorrLength`, `CheckCorrAccountForNonresident` | Shared | `[Public]` | Проверяют реквизиты, возвращают текст ошибки | `swift` / `bic` / `corr` |
| `Bank.GetBankIds`, `SetRequiredProperties` | Shared | `[Public]` | Возвращают ИД банков; задают обязательность свойств | нет |
| `Bank.GetBanksWithSameBic`, `GetBanksWithSameSwift` (экз.) | Server | `[Public, Remote]` | Возвращают банки с тем же БИК / SWIFT | `excludeClosed` |
| `Person.CheckInila`, `RemoveInilaSpecialSymbols`, `GetFormattedInila` | Shared | `[Public]` | Проверяют и форматируют СНИЛС | `inila` |
| `Person.GetFullName`, `GetLastNameAndInitials` (экз.) | Shared | `[Public]` | Возвращают ФИО в падеже | `declensionCase` |
| `Person.GetGender` (экз.) | Shared | `[Public]` | Определяет пол | нет |
| `Person.HasDuplicatesByPersonalPhone` (экз.) | Shared | `[Public]` | Проверяет дубль личного телефона | `newPhone` |
| `Person.SetPhoneHash` (экз.) | Server | `[Public, Remote]` | Шифрует телефон в `PhoneHash` | `phone` |
| `Person.ValidateAndFormatPhoneNumber` (экз.) | Server | `[Public, Remote]` | Проверяет телефон библиотекой PhoneNumbers, формат INTERNATIONAL | `phone` |
| `Person.GetPersonalPhone` (экз.), `GetDecryptedPersonalPhone` | Server | `[Public]` | Возвращают расшифрованный личный телефон | `phoneHash` |
| `Contact.GetContactByName` | Server | `[Public]` | Возвращает контакт, если найден ровно один | `name, counterparty` |
| `Contact.GetContactsByName` | Server | `[Public]` | Ищет контакты по ФИО или краткому имени | `name, personShortName, counterparty` |
| `Contact.GetContactsByNameFuzzy` (2 перегрузки) | Server | `[Public]` | Нечёткий поиск через Elasticsearch | `fullName, counterpartyId(s)` |
| `Contact.GetSingleContactFilteredByInitials`, `EqualsInitials` | Server | `[Public]` | Фильтруют по инициалам | `contactIds, initialFirstName, initialMiddleName` |
| `Contact.CreateUpdateContactNameAsyncHandler` | Server | `[Public]` | Ставит в очередь обновление имени контакта | `personId` |
| `Contact.GetIndexingJson` (экз.) | Server | `[Public]` | Строит JSON документа для индекса | нет |
| `CounterpartyDueDiligenceHistory.Create` | Server | `[Public]` | Создаёт запись истории и сохраняет | `historyRecordCreationInfo` |
| `CounterpartyDueDiligenceHistory.PerformDueDiligenceUpdate` | Server | `[Public]` | Проверяет роль и срок, затем создаёт запись | `historyRecordCreationInfo` |
| `DueDiligenceQueueItem.Create`, `Delete`, `IsReportInProgress` | Server | `[Public]` | Управляют очередью загрузки отчётов | `counterpartyId, documentKindId` |
| `DueDiligenceServiceConnection.Get` | Server | `[Public]` | Возвращает настройку подключения | нет |
| `DueDiligenceServiceConnection.ValidateSettings` (экз.) | Shared | `[Public]` | Проверяет ключ и статус | нет |
| `DueDiligenceStatus.GetActive`, `GetDuplicates` | Server | `[Public]` | Возвращают активные статусы / дубли | нет |
| `DueDiligenceWebsite.GetDefaultDueDiligenceWebsite`, `IsDefaultDueDiligenceWebsiteSet` | Server | `[Public, Remote(IsPure)]` | Возвращают сайт проверки по умолчанию | нет |
| `DueDiligenceWebsite.CheckDueDiligenceWebsiteUrl` | Shared | `[Public]` | Проверяет адрес сайта | `url` |

## Типичные задачи

**Найти контрагента или дубли по ИНН**
1. Вызови `Parties.PublicFunctions.Module.GetCounterpartyIds(tin, trrc, name)` или `PublicFunctions.Counterparty.GetDuplicateCounterparties(...)`.
2. Передай `trrc` только для организации.
3. Получи сущности через `Counterparties.GetAll(x => ids.Contains(x.Id))`.

**Проверить ИНН перед сохранением**
1. Вызови `PublicFunctions.Counterparty.CheckTin(tin, forCompany)`.
2. Если строка не пустая, добавь её как ошибку (`e.AddError`).

**Загрузить отчёт Контур.Фокус**
1. Проверь `IsExpressReportInProgress(counterpartyId)` (или `Financial`/`Statement`).
2. Вызови `PublicFunctions.Counterparty.LoadExpressReport(counterparty, "текст старта")`.
3. Жди результат: `CounterpartyDocument` с PDF создаст асинхронный обработчик.

**Записать результат проверки контрагента**
1. Создай структуру `HistoryRecordCreationInfo.Create()` и задай свойства по одному.
2. Вызови `PublicFunctions.CounterpartyDueDiligenceHistory.PerformDueDiligenceUpdate(info)`. Нужна роль «Ответственные за проверку контрагентов».

**Создать новый вид документа, удостоверяющего личность**
1. Добавь вызов `PublicInitializationFunctions.Module.CreateIdentityDocumentKind(...)` в инициализатор своего модуля.
2. Задай SID, шаблоны номера и серии.

**Найти контакт по ФИО**
1. Для точного совпадения вызови `Contact.GetContactByName(name, counterparty)`.
2. Для нечёткого поиска вызови `Contact.GetContactsByNameFuzzy(fullName, counterpartyId)`.

## Ограничения и подводные камни

- `Counterparty`, `CompanyBase`, `MarkerBase`, `MarkerValueBase` абстрактны. Создавай `Companies`, `Banks`, `People`.
- `GetDuplicateCounterparties` возвращает пустой список, если нет ни ИНН, ни имени. С КПП она также считает дублем организацию с пустым КПП.
- `GetDueDiligenceResponsibilitiesRoleName` не проверяет `null`. Если роли нет, вызов бросит `NullReferenceException`.
- `NormalizePhone` и `NormalizeSite` не проверяют `null` в параметре.
- `BeforeSave` проверяет дубли только при сохранении из UI. `SaveForcibly` пропускает проверку через параметр сущности.
- `GetDistributionListCounterparty` читает компанию внутренней сессией через рефлексию (в коде помечено HACK), чтобы обойти фильтрацию.
- `GetAndSyncMarkers` завершается без ошибки, если нет лицензии `DueDiligenceLic`, нет ключа API, нет отслеживаемых контрагентов или сегодня нерабочий день.
- Ответ сервиса 404 при загрузке отчёта не бросает ошибку пользователю: обработчик отправляет сообщение «отчёт не найден». Элемент `DueDiligenceQueueItem` удаляется в `finally`.
- `GetContactsByNameFuzzy` работает через Elasticsearch. Без индекса результат не гарантирован (не проверено).
- Нельзя использовать `EmailsAreValid`, `EmailIsValid`, `SplitEmails`, `EmailRegex`: они устарели с 26.1. Используй `Commons.PublicFunctions.Module.IsEmailFormatMatch`, `SplitEmails`.
- Если личные телефоны скрыты (`IsPersonPhoneHidden`), `GetPersonalPhone` расшифровывает `PhoneHash`, а не читает `Phone`.
- Не правь файлы `*.g.cs` и не создавай новые классы вручную: их создаёт DDS.

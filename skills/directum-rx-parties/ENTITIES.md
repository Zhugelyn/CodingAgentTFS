# Сущности модуля Sungero.Parties

Источник: `Sungero.Parties.Shared/*/*.mtd` (папку `VersionData` не читал). Всего 33 типа: 23 справочника (из них 4 абстрактных) и 10 табличных частей.
Звёздочка `*` у свойства означает обязательное свойство в метаданных. Если у типа нет своей строки «Базовый тип», он наследует `DatabookEntry`.
Обозначения: ТЧ — табличная часть. ИНН = `TIN`, КПП = `TRRC`, ОГРН = `PSRN`, СНИЛС = `INILA`.

## Иерархия контрагентов

```
DatabookEntry
 └── Counterparty (абстрактный)          — общие свойства контрагента
      ├── CompanyBase (абстрактный)      — ИНН + КПП, головная организация
      │    ├── Company                   — организация
      │    └── Bank                      — банк
      └── Person                         — персона (физлицо)

Contact (DatabookEntry) -> Company*, Person (ссылки)
```

### Counterparty (Контрагент)
- Тип: абстрактный справочник, база `DatabookEntry`. Хранит общие данные контрагента и настройки обмена.
- Ключевые свойства: `Name*`, `TIN`, `City`, `Region`, `LegalAddress`, `PostalAddress`, `Phones`, `Email`, `Homepage`, `Nonresident`, `PSRN`, `NCEO`, `NCEA`, `Account`, `Bank`, `Code`, `ExternalId`, `Kind`, `IsForeignAgent`, `IsSme`, `Reliability` (High/Medium/Low), `IsMonitoringEnabled`, `CanExchange`.
- ТЧ `ExchangeBoxes` (тип `CounterpartyExchangeBoxes`) — сведения об обмене через абонентские ящики.
- Действия: `SendInvitation`, `AcceptInvitation`, `RejectInvitation`, `ShowDuplicates`, `ForceDuplicateSave`, `ShowCounterpartyDocuments`, `ShowDueDilHistory`, `SetDueDiligence`, `EditLastDueDiligence`, `LoadExpressReport`, `LoadFinancialReport`, `LoadStatementReport`, `LoadAllReports`, `SubscribeToMonitoring`, `UnsubscribeFromMonitoring`, `SubscribeCounterpartiesToMonitoring`, `UnsubscribeCounterpartiesFromMonitoring`, `WriteLetter`, `GoToWebsite`, `EditExchange`, `ShowDepartmentBoxes`, `CanExchange`.

### CompanyBase (Базовая организация)
- Тип: абстрактный справочник, база `Counterparty`. Добавляет реквизиты юридического лица.
- Ключевые свойства: `TRRC` (КПП), `LegalName`, `HeadCompany` (головная организация), `IsCardReadOnly`, `Note`.
- ТЧ `ExchangeBoxes` (`CompanyBaseExchangeBoxes`).
- Действия: `AddChildEntity`, `ShowDuplicates`, `SendInvitation`, `OpenContacts`, `FillFromService` (заполнение по ИНН/ОГРН через внешний сервис), `OpenOnDueDiligenceWebsite`.

### Company (Организация)
- Тип: справочник, база `CompanyBase`. Основной контрагент-организация.
- Ключевые свойства: наследует всё от `CompanyBase`. Переопределяет `Reliability`, `Kind`, `Responsible`, `PSRN`, `Account`, `City`.
- ТЧ `ExchangeBoxes` (`CompanyExchangeBoxes`). Формы: `Card`, `ExchangeBoxes`, `DueDiligencePage`.
- Действия: `SubscribeToMonitoring`, `UnsubscribeFromMonitoring`, `SubscribeCounterpartiesToMonitoring`, `UnsubscribeCounterpartiesFromMonitoring`.

### Bank (Банк)
- Тип: справочник, база `CompanyBase`. Банк с реквизитами для платежей.
- Ключевые свойства: `BIC*`, `CorrespondentAccount`, `SWIFT`, `IsSystem`, `NCEO`, `NCEA`, `Account`.
- ТЧ `ExchangeBoxes` (`BankExchangeBoxes`). Действие: `ShowDuplicates`.

### Person (Персона)
- Тип: справочник, база `Counterparty`. Физическое лицо с документом, удостоверяющим личность.
- Ключевые свойства: `LastName*`, `FirstName*`, `MiddleName`, `ShortName`, `DateOfBirth`, `Sex` (Male/Female), `INILA`, `Citizenship`, `IdentityKind`, `IdentitySeries`, `IdentityNumber`, `IdentityDateOfIssue`, `IdentityExpirationDate`, `IdentityAuthority`, `IdentityAuthorityCode`, `BirthPlace`, `Phone`, `IsPhoneSet`, `PhoneHash`.
- ТЧ `ExchangeBoxes` (`PersonExchangeBoxes`). Формы: `Card`, `ExchangeBoxes`, `IdentityDocument`.
- Действия: `ShowDuplicates`, `SetPhone`, `UpdatePhone`, `ClearPhone`, `ShowUsers`.

### Contact (Контактное лицо)
- Тип: справочник, база `DatabookEntry`. Контакт сотрудника контрагента.
- Ключевые свойства: `Name*`, `Company*` (ссылка на `CompanyBase`), `Person`, `Department`, `JobTitle`, `Phone`, `Fax`, `Email`, `Homepage`, `SigningReason`, `Note`.
- Действия: `WriteLetter`, `GoToWebsite`, `ShowDuplicates`.

### Табличные части обмена (`*ExchangeBoxes`)
- `CounterpartyExchangeBoxes` — ТЧ контрагента. Свойства: `Box*`, `Status*` (Active/Closed/ApprovingByUs/ApprovingByCA), `OrganizationId*`, `InvitationText`, `IsDefault`, `CounterpartyBox`, `IsRoaming`, `FtsId`, `CounterpartyBranchId`, `CounterpartyParentBranchId`.
- `CompanyBaseExchangeBoxes`, `CompanyExchangeBoxes`, `BankExchangeBoxes`, `PersonExchangeBoxes` — наследники с типизированной ссылкой `Counterparty`.

## Справочники классификации

- `CounterpartyKind` (Вид контрагента) — справочник. Свойства: `Name*`, `Status`, `Sid`, `Note`.
- `IdentityDocumentKind` (Вид документа, удостоверяющего личность) — справочник. Свойства: `Name*`, `ShortName*`, `Code*`, `SID`, `SpecifyIdentitySeries`, `SpecifyIdentityExpirationDate`, `SpecifyIdentityAuthorityCode`, `SpecifyBirthPlace`, `IdentitySeriesPattern`, `IdentityNumberPattern`, `IdentityAuthorityCodePattern`. Действие: `DeleteEntity`.

## Проверка контрагентов (Контур.Фокус)

- `DueDiligenceServiceConnection` (Подключение к сервису проверки контрагентов) — справочник. Свойства: `Uri*`, `ApiKey`, `Name`, `Status`, `Note`. Действия: `SetApiKey`, `CheckConnection`.
- `DueDiligenceWebsite` (Сайт проверки контрагентов) — справочник. Свойства: `Name*`, `Url*`, `UrlForSelfEmployed`, `HomeUrl*`, `IsDefault`, `IsSystem`, `Note`. Действия: `DeleteEntity`, `OpenWebsite`.
- `DueDiligenceStatus` (Результат проверки) — справочник. Свойства: `Name*`, `Color*`, `Sid`, `Status`, `Note`. Предустановлены два результата: «Approved» и «Rejected».
- `CounterpartyDueDiligenceHistory` (История проверки контрагента) — справочник. Свойства: `Counterparty*`, `Result*` (`DueDiligenceStatus`), `Reliability` (High/Medium/Low), `Created*`, `ValidTill`, `CreatedBy*`, `Name*`, `Note`, `IsActual`.
- `DueDiligenceQueueItem` (Элемент очереди загрузки отчёта) — справочник. Свойства: `CounterpartyId*`, `DocumentKindId*`, `Name`. Защищает от повторной загрузки отчёта.

## Маркеры надёжности и мониторинг

- `MarkersGroup` (Группа маркеров) — справочник. Свойства: `Name*`, `Description`, `Status`.
- `MarkerBase` (Маркер надёжности) — абстрактный справочник. Свойства: `Name*`, `Alias*`, `Priority*` (Low/Medium/High), `Group*` (`MarkersGroup`), `Description`, `Note`, `Status`.
- `BooleanMarker` — справочник, база `MarkerBase`. Маркер со значением «да/нет». Своих свойств нет.
- `MarkerValueBase` (Значение маркера) — абстрактный справочник. Свойства: `Marker*`, `Counterparty*`, `UpdateDate`, `Name`.
- `BooleanMarkerValue` — справочник, база `MarkerValueBase`. Свойство: `Value`.
- `CounterpartyMonitoringRule` (Правило мониторинга контрагентов) — справочник. Свойства: `Name*`, `IsSystem`, `Status`, `Note`. ТЧ: `Markers*` (`CounterpartyMonitoringRuleMarkers`, поле `Marker*`), `Responsibles*` (`CounterpartyMonitoringRuleResponsibles`, поле `Responsible*`). Действия: `DeleteEntity`, `AddMarkers`.
- `SyncMarkersQueueItem` (Элемент очереди синхронизации маркеров) — справочник. Свойства: `Name`. ТЧ: `Endpoints` (`SyncMarkersQueueItemEndpoints`, поле `Endpoint`), `Risks` (`SyncMarkersQueueItemRisks`, поле `Risk`: Low/Medium/High), `Counterparties` (`SyncMarkersQueueItemCounterparties`, поле `CounterpartyId`).
- `ChangedMarkerQueueItem` (Элемент очереди изменённых маркеров) — справочник. Свойства: `MarkerId`, `MarkerValueId`, `CounterpartyId`, `Name`. Служит источником для отчёта об изменённых маркерах.

## Перечисления

Отдельных перечислений уровня модуля нет. Перечисления заданы как свойства: `Counterparty.Reliability`, `CounterpartyDueDiligenceHistory.Reliability` (High/Medium/Low), `MarkerBase.Priority`, `SyncMarkersQueueItemRisks.Risk` (Low/Medium/High), `Person.Sex` (Male/Female), `CounterpartyExchangeBoxes.Status`.

## Структуры

Модульные (`ModuleStructures.cs`):
- `CounterpartyFromExchangeService` — найденный в сервисе обмена контрагент: `Name`, `TIN`, `TRRC`, `Box`, `Counterparty`, `ExchangeStatus`, `OrganizationId`.
- `ChangedMarkerInfo` — изменённый маркер: `Counterparty`, `Marker`, `MarkerValue`.
- `MarkersDataDto` (`[Public]`) — ответы Контур.Фокус по контрагенту: `Counterparty`, `SuccessfulEndpointResponses`.
- `MarkersRawData` (`[Public]`) — необработанный ответ: `RawData`.

Структуры сущностей (по одному файлу `*Structures.cs`):
- `CompanyBase`: `FoundCompanies`, `CompanyDisplayValue`, `FoundContact`, `BusinessEntity` — результат заполнения по сервису.
- `Counterparty`: `InvitationInfo`, `AllowedBoxes` — данные приглашения к обмену.
- `CounterpartyDueDiligenceHistory`: `HistoryRecordCreationInfo`, `CounterpartyDueDiligenceHistoryDto`, `AdditionalPropertyDto`, `EmployeeDto` (все `[Public]` по `Module.mtd` сущности).
- `DueDiligenceServiceConnection`: `ApiStatistics`. `DueDiligenceStatus`: `DueDiligenceStatusDto`.
- `Person`: `PhoneValidationAndFormatResult` (`[Public]`): `FormattedPhone`, `Errors`.

## Роли, создаваемые модулем

Идентификаторы лежат в `Constants.Module.RolesGuid` (`ModuleConstants.cs`). Публичные: `UsersWithAccessToCounterpartyRole`, `DueDiligenceResponsibleRole`, `UsersWithAccessToCounterpartyDueDiligenceResultsRole`, `ResponsibleForPersonsRole`, `EmployeesWithAccessToPersonsRole`. Роль `ExchangeServiceUsers` непубличная.

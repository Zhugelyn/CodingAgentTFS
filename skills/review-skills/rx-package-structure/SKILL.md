---
name: rx-package-structure
description: "Структура прикладного кода Directum RX на диске (src/Packages): проекты модуля, файлы и классы сущности, соответствие «файл → namespace → класс», что генерирует Directum Studio, а что пишет разработчик. Применяй, когда нужно найти или определить слой файла, понять, где лежит нужный код, или объяснить, почему нельзя создавать новые классы и файлы."
---

# Структура пакета Directum RX

Прикладное решение разрабатывается в Directum Studio (DS). Разработчик описывает типы сущностей, свойства, действия, обработчики, фоновые процессы и отчёты **в метаданных**, а DS генерирует C#-код (`*.g.cs`, `*.g.xml`). Руками пишется только тело функций и обработчиков в `partial`-классах рядом со сгенерированными файлами.

Следствия:
- новые классы и файлы вручную не создаются. Исключение: дополнительные файлы с `partial`-классами `ModuleFunctions` и `{Сущность}Functions`, например `{Тема}.ModuleServerFunctions.cs`;
- многие имена задаёт платформа, и менять их нельзя (см. skill `rx-platform-types`);
- нельзя создавать generic-методы с `[Public]`: платформа не поддерживает их публикацию.

## Проекты модуля

`src/Packages/Sungero.{Модуль}/`:

| Папка проекта | Namespace | Что внутри |
|---|---|---|
| `Sungero.{М}.Shared` | `Sungero.{М}.Shared` | Разделяемые функции и обработчики. Здесь же метаданные `*.mtd`, ресурсы `*.resx`, константы (`Sungero.{М}.Constants`), структуры (`Sungero.{М}.Structures.*`) |
| `Sungero.{М}.ClientBase` | `Sungero.{М}.Client` | UI-логика: действия, обработчики форм, клиентские функции |
| `Sungero.{М}.Server` | `Sungero.{М}.Server` | Бизнес-логика, серверные обработчики, ФП, АО, отчёты, предопределённые SQL |
| `Sungero.{М}.Isolated` | `Sungero.{М}.Isolated.{Область}` | Код изолированного процесса: конвертация, парсинг, сторонние библиотеки |
| `Sungero.{М}.Libraries` | — | Сторонние библиотеки |
| `Sungero.{М}.Settings` | — | JSON-настройки представлений (`FormView`, `ListView`, `ProcessKind`, `EntityStyle`) |
| `VersionData/{версия}/Script` | — | Скрипты конвертации Directum RX |

Папка проекта называется `ClientBase`, а namespace — `Client`.

## Файлы и классы одной сущности

Пример для `OfficialDocument` модуля `Docflow`:

```
Sungero.Docflow.Shared/OfficialDocument/
  OfficialDocument.mtd                    — метаданные, генерирует DS
  OfficialDocument.g.cs                   — IOfficialDocument, репозиторий OfficialDocuments; генерирует DS
  OfficialDocument.resx / .ru.resx        — ресурсы, пишет разработчик
  OfficialDocumentSystem.resx / .ru.resx  — системные ресурсы, генерирует DS
  OfficialDocumentConstants.cs            — класс Sungero.Docflow.Constants.OfficialDocument
  OfficialDocumentStructures.cs           — partial-классы прикладных структур
  OfficialDocumentStructures.g.cs         — интерфейсы структур и Create(); генерирует DS
  OfficialDocumentSharedFunctions.cs      — class OfficialDocumentFunctions, namespace Sungero.Docflow.Shared
  OfficialDocumentHandlers.cs             — разделяемые обработчики
Sungero.Docflow.Server/OfficialDocument/
  OfficialDocumentServerFunctions.cs      — class OfficialDocumentFunctions, namespace Sungero.Docflow.Server
  OfficialDocumentHandlers.cs             — серверные обработчики
  OfficialDocumentQueries.g.xml           — собирается из Queries/*.sql; генерирует DS
Sungero.Docflow.ClientBase/OfficialDocument/
  OfficialDocumentClientFunctions.cs      — class OfficialDocumentFunctions, namespace Sungero.Docflow.Client
  OfficialDocumentActions.cs              — действия карточки и списка
  OfficialDocumentHandlers.cs             — клиентские обработчики формы
```

**Слой определяется папкой проекта и namespace, а не именем класса.** Класс экземплярных функций во всех трёх слоях называется `{Сущность}Functions`; суффикс `Server`/`Shared`/`Client` есть только в имени файла. Класс модульных функций во всех слоях — `ModuleFunctions`.

## Модульный уровень

Файлы в корне проекта: `ModuleServerFunctions.cs` / `ModuleSharedFunctions.cs` / `ModuleClientFunctions.cs` (все содержат `ModuleFunctions`), `ModuleJobs.cs`, `ModuleAsyncHandlers.cs`, `ModuleHandlers.cs`, `ModuleInitializer.cs`, `ModuleConstants.cs`, `ModuleStructures.cs`, `Module.resx` / `Module.ru.resx`.

Крупные модульные функции разбиваются на partial-файлы в папке `ModuleServerFunctions/` с именами `{Тема}.ModuleServerFunctions.cs`. Устаревший код лежит в `ModuleServerFunctions/Obsolete/`.

## Отчёты

`Server/Reports/{Имя}Report/`: шаблон `*.frx`, предопределённые запросы `Queries/*.sql`, обработчики `{Имя}ReportHandlers.cs`.

## Что не правится руками

- Всё автогенерируемое: `*.g.cs`, `*.g.xml`, `*System.resx`, `*System.ru.resx`, папки `Generated/`.
- Метаданные `*.mtd` и схемы маршрутов задач `*Scheme*.xml`: их пишет DS по действиям в дизайнере. Изменение метаданных — это действие в Studio, а не правка текста файла.

## См. также

`rx-code-kinds` (виды кода и допустимые направления вызовов), `rx-call-facades` (как вызывать функции между слоями и модулями), `rx-architecture-layers` (уровни и слои платформы).

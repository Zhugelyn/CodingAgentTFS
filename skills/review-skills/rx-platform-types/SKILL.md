---
name: rx-platform-types
description: Платформенные идентификаторы, атрибуты и типы данных прикладного кода Directum RX (_obj, e, args, State, Info, Params, [Public], [Remote], nullable-свойства, Enumeration, Calendar, ресурсы). Применяй, когда нужно понять платформенное имя в коде, работать с датой и временем, enum-значениями, bool?, строками ресурсов или решить, можно ли переименовать идентификатор.
---

# Платформенные идентификаторы, атрибуты и типы

## Идентификаторы, заданные платформой

Их генерирует платформа: переименовывать нельзя, неиспользуемыми параметрами они не считаются, правила именования к ним не применяются.

- `_obj` — текущий экземпляр сущности в экземплярных функциях и обработчиках.
- `e` — аргументы обработчика (`Sungero.Domain.*EventArgs`). Всегда есть в сигнатуре, даже если не используется.
- `args` — аргументы асинхронного обработчика или блока схемы.
- `_added`, `_deleted`, `_created`, `_source` — элементы коллекций и исходный объект в обработчиках коллекций и копирования.
- `_obj.State` — состояние карточки: `State.IsInserted`, `State.IsCopied`, `State.Properties.{Свойство}.IsVisible / IsEnabled / IsRequired`.
- `_obj.Info` — метаданные типа: `Info.Properties.Note.Length`, локализованное имя типа.
- `((Sungero.Domain.Shared.IExtendedEntity)entity).Params` — временные параметры экземпляра, живут в рамках сессии карточки.

Статические точки входа без `using`: `Logger`, `Locks`, `Transactions`, `Calendar`, `Dialogs`, `Session`, `AccessRights`, `Reports`, `Hyperlinks`.

## Атрибуты

- `[Public]` — публикует метод, константу или структуру для других модулей и решений.
- `[Remote]`, `[Remote(IsPure = true)]` — разрешают вызов серверной функции с клиента. `IsPure = true`: функция не меняет состояние.
- `[ExpressionElement]` — метод доступен в no-code (конструкторе выражений).
- `[Obsolete]` — устаревший API.
- `public virtual` — точка расширения: заказчик может переопределить метод в своём решении через DS, после этого сигнатура заморожена. В старых модулях `virtual` стоит почти везде, но это исторически сложившаяся практика, а не конвенция; в новом коде модификатор выбирают осознанно (см. `rx-function-design`).

## Типы данных

- Сущность — интерфейс `I{Сущность}` (`IOfficialDocument`); репозиторий во множественном числе: `OfficialDocuments.GetAll(...)`, `.Get(id)`, `Employees.Create()`, `.Is()`, `.As()`.
- Идентификатор сущности — `long`.
- **Все свойства сущностей nullable**: `bool?`, `int?`, `long?`, `DateTime?`, `Enumeration?`. Проверка `if (_obj.IsStandard == true)` — штатная идиома для `bool?`. Для обычного `bool` сравнение с `true`/`false` не пишут.
- Перечисления — не C#-`enum`, а `Sungero.Core.Enumeration`. Значения: `{Сущность}.{Свойство}.{Значение}`, например `Docflow.ApprovalFunctionQueueItem.ProcessingStatus.Error`. `[Flags]`-перечислений в прикладном коде нет.
- LINQ-запросы по `GetAll()` транслируются в SQL через NHibernate.

## Дата и время

- Только `Calendar`: `Calendar.Now`, `Calendar.Today`, `Calendar.UserNow`, `Calendar.UserToday`, `Calendar.SqlMaxValue`. `Calendar` учитывает часовой пояс тенанта и пользователя; `DateTime.Now/Today/UtcNow` — нет. Исключение: поля внешней библиотеки в Isolated-области, значение которых уходит во внешнее API и не сравнивается с данными системы.
- Не смешивать пользовательское и серверное время в одном выражении: `Calendar.UserNow/UserToday` и `Calendar.Now/Today` сравнивать только после приведения к одной шкале через `FromUserTime()` / `ToUserTime()`. Свойства сущностей хранятся в серверном времени, поэтому сравнение с `Calendar.UserToday` без конвертации — ошибка.
- Строку в дату разбирать через `DateTime.TryParseExact` с явным шаблоном. `DateTime.Parse(s)` зависит от культуры.

## Числа и прочие ловушки

- Целочисленное деление не присваивать в `double`/`decimal` без приведения операнда: `(double)count / total`, а не `double share = count / total`.
- Коллекцию не передавать в собственный метод (`list.AddRange(list)`, `list.Contains(list)`): почти всегда опечатка.
- У цикла и рекурсии должно быть достижимое условие выхода.

## Ресурсы

- Сущность: `{Сущность}.resx` (английский) и `{Сущность}.ru.resx` (русский) пишет разработчик; `{Сущность}System.resx` / `.ru.resx` генерирует DS.
- Модуль: `Module.resx`, `Module.ru.resx`, при необходимости `Module.en.resx`; `ModuleSystem.*` генерирует DS.
- Ключ ресурса становится свойством `Resources.{Ключ}`. Если в значении есть `{0}`, DS генерирует `Resources.{Ключ}Format(...)`.
- Список ключей — поле `ResourcesKeys` в `*.mtd` сущности или модуля.

## См. также

`rx-call-facades`, `rx-function-design`, `rx-security-and-resources` (правила работы с ресурсами и константами), `rx-linq-and-entities`.

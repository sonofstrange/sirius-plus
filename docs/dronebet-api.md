# Sirius Plus x DroneBet API

Документ описывает двустороннюю интеграцию Sirius Plus и DroneBet. Секрет
передаётся только между серверами и никогда не отдаётся браузеру.

## Рабочий сценарий в Sirius Plus

Пользователь открывает `Сириус Коины -> DroneBet`, создаёт одноразовый код в
DroneBet и вводит его в Sirius Plus. Sirius Plus вызывает API DroneBet:

```text
https://dronebet.cloudpub.ru/api/partner/sirius
```

| Метод | Путь | Назначение |
| --- | --- | --- |
| `POST` | `/links/claim` | привязать DroneBet-аккаунт к Sirius UID |
| `GET` | `/accounts/{sirius_uid}/balance` | получить баланс печенек |
| `POST` | `/coins/credit` | начислить печеньки после списания коинов |
| `POST` | `/coins/debit` | списать печеньки перед начислением коинов |

Во всех запросах Sirius Plus отправляет:

```http
Authorization: Bearer <DRONEBET_PARTNER_TOKEN>
Content-Type: application/json
```

Курс фиксирован: **2 000 печенек DroneBet = 1 Сириус Коин**. В обмене
используются только целые коины, поэтому поле `amount` у DroneBet всегда
кратно `2000`.

### Контракт DroneBet

`POST /links/claim`:

```json
{"code":"AB12CD34","sirius_uid":"100119810111293745"}
```

Успешный ответ:

```json
{"ok":true,"status":"linked","user_id":42}
```

Баланс печенек:

```json
{"ok":true,"sirius_uid":"100119810111293745","balance":12500,"currency":"cookies"}
```

Начисление и списание используют одинаковое тело:

```json
{
  "sirius_uid":"100119810111293745",
  "amount":6000,
  "idempotency_key":"4b85d273-5d94-4f3e-b4bd-2d6e9d123456",
  "reason":"Обмен 3 Сириус Коинов"
}
```

DroneBet обязан сохранять результат операции по `idempotency_key`. При
повторном запросе с тем же ключом он возвращает прежний результат, а не меняет
баланс ещё раз. Sirius Plus аналогично защищает локальную часть обмена. При
таймауте пользователь повторяет обмен, а сервер использует тот же ключ.

## API Sirius Plus для DroneBet

Ниже указан обратный API: его вызывает **сервер DroneBet**, когда ему нужно
получить или изменить баланс Сириус Коинов. Базовый URL:

```text
https://sirius.rusanoff.ru/api/partner/dronebet
```

Курс обмена согласуется интерфейсами обеих сторон. Текущий ориентир:

```text
2000 печенек DroneBet = 1 Сириус Коин
```

## Безопасность

Все маршруты в этом документе вызываются **только сервером DroneBet**. Браузер
пользователя не знает секрет и не обращается к этим маршрутам.

Передавайте заголовок для каждого запроса:

```http
Authorization: Bearer <DRONEBET_PARTNER_TOKEN>
Content-Type: application/json
```

`DRONEBET_PARTNER_TOKEN` - общий случайный секрет длиной не менее 32 байт. Его
нельзя класть в репозиторий, JavaScript, APK или логи. Обмен идёт только по
HTTPS. Sirius Plus дополнительно ограничивает партнёрский API до 30 запросов в
минуту с одного IP; при превышении возвращается `429`.

Каждое изменение баланса требует уникального `idempotency_key` длиной 12-128
символов. При повторе того же запроса Sirius Plus вернёт исходный результат и
не изменит баланс второй раз. Повтор с тем же ключом, но другой суммой,
направлением или пользователем вернёт `409 idempotency_key_conflict`.

## Привязка аккаунта

1. Пользователь в Sirius Plus открывает `Сириус Коины -> DroneBet` и нажимает
   `Связать аккаунт`.
2. Sirius Plus показывает одноразовый код. Он действует 10 минут.
3. Пользователь вводит этот код в авторизованном DroneBet.
4. Сервер DroneBet вызывает `POST /links/claim`, указывая свой стабильный
   `external_user_id`.

Один Sirius UID можно связать только с одним DroneBet-аккаунтом и наоборот.
Это исключает подмену получателя при обмене. Sirius Plus не передаёт DroneBet
пароль, email, JWT, ФИО или токен Sirius.

### Подтвердить код

`POST /links/claim`

```json
{
  "code": "8SKJ45M2QZ",
  "external_user_id": "dronebet-user-4821"
}
```

Успех, `200`:

```json
{
  "ok": true,
  "status": "linked",
  "sirius_uid": "100119810111293745"
}
```

Ошибки:

| HTTP | `code` | Значение |
| --- | --- | --- |
| 400 | `invalid_link_code` | Код отсутствует, истёк или уже был использован. |
| 409 | `external_account_already_linked` | Этот DroneBet-аккаунт привязан к другому Sirius UID. |
| 409 | `sirius_account_already_linked` | Sirius UID уже привязан к другому DroneBet-аккаунту. |

## Просмотр баланса

`GET /accounts/{external_user_id}/balance`

Пример:

```text
GET /api/partner/dronebet/accounts/dronebet-user-4821/balance
```

Успех, `200`:

```json
{
  "ok": true,
  "external_user_id": "dronebet-user-4821",
  "coins": 56
}
```

Если аккаунты ещё не связаны: `404 {"ok": false, "code": "account_not_linked"}`.

## Начислить Сириус Коины

Вызывайте только после того, как DroneBet успешно и окончательно списал
печеньки у пользователя.

`POST /coins/credit`

```json
{
  "external_user_id": "dronebet-user-4821",
  "amount": 3,
  "idempotency_key": "4b85d273-5d94-4f3e-b4bd-2d6e9d123456",
  "reason": "Обмен 6000 печенек DroneBet"
}
```

Успех, `200`:

```json
{
  "ok": true,
  "external_user_id": "dronebet-user-4821",
  "direction": "credit",
  "amount": 3,
  "coins": 59,
  "replayed": false
}
```

`coins` - доступный баланс: зарезервированные для автозаписи коины в него не
входят. Пользователь Sirius Plus получает уведомление о начислении.

## Списать Сириус Коины

Этот маршрут используется перед начислением печенек в DroneBet. Сначала
успешно спишите коины в Sirius Plus, затем начислите печеньки на своей стороне.
При сетевом таймауте **не создавайте новый ключ**: повторите запрос с тем же
`idempotency_key` и получите его сохранённый результат.

`POST /coins/debit`

```json
{
  "external_user_id": "dronebet-user-4821",
  "amount": 2,
  "idempotency_key": "bf6c493c-40d0-4d9b-bb78-f55d91b0b95a",
  "reason": "Обмен 2 Сириус Коинов на печеньки"
}
```

Ответ имеет тот же формат, но `direction` равен `debit`.

Если доступных коинов недостаточно, ответ:

```json
{
  "ok": false,
  "code": "insufficient_coins",
  "balance": 1
}
```

HTTP-статус в этом случае `409`. Зарезервированные автозаписью коины списать
через API невозможно.

## Общие ошибки

| HTTP | `code` | Значение |
| --- | --- | --- |
| 400 | `invalid_json` | Тело запроса не JSON. |
| 400 | `invalid_amount` | Сумма не целое число от 1 до 1 000 000. |
| 400 | `invalid_idempotency_key` | Неверный или слишком короткий ключ операции. |
| 401 | `unauthorized` | Нет или неверен `Authorization: Bearer ...`. |
| 404 | `account_not_linked` | Аккаунты ещё не привязаны. |
| 409 | `idempotency_key_conflict` | Ключ уже использован для другой операции. |
| 429 | `rate_limited` | Превышен лимит запросов. Повторите позже. |
| 503 | `partner_api_disabled` | На Sirius Plus ещё не задан секрет интеграции. |

## Порядок операций

`Сириус Коины -> печеньки`: Sirius Plus сначала атомарно списывает доступные
коины, затем вызывает DroneBet `/coins/credit`. Если DroneBet явно отклонил
запрос, коиновая операция отменяется. При сетевом сбое обмен остаётся в
состоянии ожидания и безопасно повторяется.

`Печеньки -> Сириус Коины`: Sirius Plus сначала вызывает DroneBet
`/coins/debit`, затем начисляет коины у себя. Это исключает выпуск коинов без
успешного списания печенек.

Перед запуском обе стороны должны проверить: успешный обмен в каждую сторону,
недостаток средств, повтор после таймаута и повтор с конфликтным
`idempotency_key`.

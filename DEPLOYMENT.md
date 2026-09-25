# Закрытый Docker-стенд

Правила веток, Pull Request, CI и хранения секретов описаны в
[GitHub-инфраструктуре](docs/GITHUB_INFRASTRUCTURE.md).

Стенд запускает три независимых процесса из одного образа:

- `api` — FastAPI, доступен только внутри Docker-сети;
- `web` — Flask/Gunicorn, опубликован только на `127.0.0.1:5000` сервера;
  число процессов и потоков задаётся `WEB_WORKERS` (по умолчанию 2) и
  `WEB_THREADS` (по умолчанию 4), timeout — `WEB_TIMEOUT` (60 секунд);
- `bot` — polling-процесс MAX, запускается отдельным профилем после добавления токена.

Образ содержит официальный корневой сертификат Минцифры `Russian Trusted Root
CA`, необходимый для проверки TLS-сертификата `platform-api2.max.ru`. Исходный
файл загружен с `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`;
SHA-256 отпечаток сертификата:
`D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`.

## Назначение контуров

- `test` — рабочий контур разработчика Vovochka. Получает только коммиты из
  `Main_test`, успешно прошедшие Fast CI. База, MAX-бот и секреты отдельные;
- `prod` — внутренний контур владельца проекта. На нём проверяются объединённые
  изменения и выполняются регрессионные/нагрузочные тесты после слияния в
  `main` и успешного Full CI;
- `demo` — отдельный эталонный контур на сервере `gateway_sandbox` для показа и
  передачи заказчику. Он не участвует в автоматических Fast/Full-развёртываниях
  и обновляется вручную только до явно принятого SHA из `main`.

Такое разделение не позволяет разработке или внутреннему тестированию менять
данные и доступность клиентского demo-контура.

## Два изолированных контура на основном сервере

На одном сервере используются независимые Compose-проекты:

- `test`: `.env.test` + `.env.test.runtime`, данные в `runtime/test`, веб-порт `5001`;
- `prod`: `.env` + `.env.prod.runtime`, данные в `runtime/prod`, веб-порт `5000`.

Для каждого контура также используется собственный каталог квитанций:
`runtime/test/kv` или `runtime/prod/kv`. Он монтируется в контейнер как
`/app/KV` только для чтения. PDF-квитанции копируйте в нужный runtime-каталог;
их нельзя добавлять в Git.

Пользовательские `.env` не изменяются скриптами развёртывания. Одноразовые
bootstrap-пароли администраторов можно хранить в соответствующем
`.env.*.runtime`. Все эти файлы исключены из Git. Скрипт принудительно задаёт
`APP_ENV=test` для `test` и `APP_ENV=production` для `prod`, независимо от
локального содержимого `.env`.

## Первый запуск

```bash
cp .env.example .env.test
cp .env.runtime.example .env.test.runtime
cp .env.example .env
cp .env.runtime.example .env.prod.runtime
chmod 600 .env.test .env.test.runtime .env .env.prod.runtime
```

В `.env.test` и `.env` задайте разные `TOKEN`, `SECRET_KEY` и
`INTERNAL_API_TOKEN`. Настройки интеграции с 1С также задаются отдельно для
каждого контура и не удаляются скриптами. При первом запуске с пустой базой в
двух runtime-файлах задайте разные одноразовые значения
`BOOTSTRAP_ADMIN_PASSWORD` длиной не менее 12 символов. После первого входа приложение потребует сменить пароль. Сначала разверните
тестовый контур:

```bash
./scripts/deploy.sh test
```

Выполните health-check, smoke-тесты и приёмку. Затем продвиньте изменения из
`Main_test` в `main`; `prod` запускается только из SHA `main`, прошедшего Full CI:

```bash
./scripts/deploy.sh prod
```

Запускайте скрипты от непривилегированного владельца runtime-каталогов. При
сборке UID/GID пользователя контейнера автоматически берутся из `id -u` и
`id -g`, поэтому bind mounts не зависят от фиксированного UID 1000. При
необходимости значения можно явно передать через `APP_UID` и `APP_GID`.

Перед запуском `deploy.sh` проверяет выбранный токен официальным MAX `GET /me`.
Пустой или недействительный токен останавливает развёртывание. Если настроен
второй контур, скрипт также запрещает запуск одного MAX-бота одновременно в
test и prod. Значения токенов в вывод не попадают.

После первого входа и смены bootstrap-пароля очистите
`BOOTSTRAP_ADMIN_PASSWORD` в соответствующем `.env.*.runtime` и повторно
выполните `deploy.sh test` или `deploy.sh prod`. Скрипт пересоздаёт контейнеры,
чтобы одноразовый пароль исчез из их окружения.

Панель намеренно не доступна из интернета. Откройте SSH-туннель с рабочего
компьютера:

```powershell
ssh -L 5000:127.0.0.1:5000 botadmin@SERVER_IP
```

После этого откройте `http://127.0.0.1:5000`.

Для одновременного доступа к обеим панелям:

```powershell
ssh -L 5000:127.0.0.1:5000 -L 5001:127.0.0.1:5001 botadmin@SERVER_IP
```

Тестовая панель откроется по адресу `http://127.0.0.1:5001`.

Параметры Gunicorn задавайте в серверном файле `.env.runtime` конкретного
контура, например:

```dotenv
WEB_WORKERS=2
WEB_THREADS=4
WEB_TIMEOUT=60
```

`compose.yaml` не подставляет эти значения из корневого `.env`: контейнер
получает их из `env_file`, причём `.env.runtime` подключён после `.env` и имеет
приоритет. Некорректное или выходящее за допустимый диапазон значение
останавливает Gunicorn при старте.

Ограничитель входа принимает адрес клиента только от явно доверенного
ближайшего reverse proxy. По умолчанию forwarded-заголовки не доверяются:

```dotenv
TRUSTED_PROXY_CIDRS=
EXPECT_REVERSE_PROXY=true
```

Перед включением proxy временно выведите или измерьте именно app-side значение
Flask `request.remote_addr` (то есть адрес peer, который видит Gunicorn), а также
сверьте адреса контейнерной сети командой
`docker network inspect <project>_backend`. Значение Nginx `$remote_addr` — это
адрес клиента на стороне Nginx, а не peer Flask, и для allowlist не подходит.
Добавьте измеренный адрес proxy/gateway как `/32` (или IPv6 `/128`) отдельно для
каждого контура. Не предполагайте, что host Nginx виден как loopback:

```dotenv
TRUSTED_PROXY_CIDRS=172.31.44.5/32
```

Nginx должен **перезаписывать** заголовок одним hop, а не добавлять
недоверенную цепочку `$proxy_add_x_forwarded_for`:

```nginx
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header Host $host;
proxy_pass http://127.0.0.1:5000;
```

Если Nginx работает в Docker, выделите ему фиксированную отдельную подсеть и
добавьте только её в `TRUSTED_PROXY_CIDRS`. Пустой или некорректный allowlist
отключает доверие к forwarded-заголовку; тогда используется прямой peer.

Новые и переименованные логины ограничены 128 символами. Вход принимает не
больше 4096 символов: существующие legacy-логины длиной 129–4096 продолжают
работать при точном вводе (с учётом регистра). Перед обновлением обязательно
проверьте `SELECT id, username FROM users WHERE length(username) > 128;`.
Значения длиннее 4096 необходимо переименовать до обновления; остальные legacy-
значения рекомендуется безопасно сократить в разделе «Пользователи» панели.

## Запуск MAX-бота

Скрипт `deploy.sh` запускает бот выбранного контура вместе с API и веб-панелью.

## Проверки

Для уже развёрнутых контуров проверьте health endpoints:

```bash
curl --fail http://127.0.0.1:5001/healthz
curl --fail http://127.0.0.1:5000/healthz
```

Автоматический Fast-контур получает точный проверенный SHA из `Main_test`, а
Full-контур — точный проверенный SHA из `main`. Продвижение выполняется PR из
`Main_test` в `main`, поэтому production никогда не разворачивается из тестовой
ветки напрямую.

### Read-only smoke test

`scripts/smoke.sh test` только читает состояние test-контура. Другие аргументы
отклоняются. Скрипт не отправляет сообщения в MAX, не вызывает 1С и не выводит
окружение. Он проверяет:

- принадлежность контейнеров Compose-проекту, running/healthy и restart count;
- loopback `/healthz` с ограниченным timeout и свежесть heartbeat бота;
- SQLite `PRAGMA quick_check` через URI `mode=ro`;
- не менее 15% свободного места;
- commit и безопасный сокращённый image ID без конфигурации и секретов.

Ручной запуск из активного release:

```bash
./scripts/smoke.sh test
```

Для test-контура предусмотрен systemd timer раз в пять минут. Installer должен
запускаться от root и принимает абсолютный путь существующего release строго
внутри `/srv/bot-sandbox/releases`. Например, для текущего release:

```bash
sudo ./scripts/install-smoke-systemd.sh \
  /srv/bot-sandbox/releases/rso_max-2973d64
```

Installer проверяет `compose.yaml`, атомарно обновляет стабильную ссылку
`/srv/bot-sandbox/current/test`, копирует smoke в root-owned
`/usr/local/libexec/rso-max-smoke` и устанавливает только test unit/timer.
Smoke запускается от `botadmin` с `timeout`, `flock` и systemd hardening.
Результаты доступны без вывода секретов:

```bash
systemctl status rso-max-smoke-test.timer
journalctl -u rso-max-smoke-test.service --since today
```

Prod-контур намеренно не входит в область этого smoke-monitoring.

Для проверки непосредственно во время автоматического релиза используется
`scripts/release-smoke.sh fast|full`. В режиме `fast` он проверяет только
`rso-max-test`, порт `5001` и `runtime/test`; в режиме `full` — только
`rso-max-prod`, порт `5000` и `runtime/prod`. Full дополнительно выполняет
read-only `PRAGMA integrity_check` и более длинный замер restart count. Скрипт не
отправляет MAX-сообщения и не вызывает 1С.

## Резервная копия SQLite

```bash
./scripts/backup.sh test
./scripts/backup.sh prod
```

Скрипт использует SQLite Backup API, поэтому копия согласована даже при работающих
процессах. Сначала создаётся временный файл, затем выполняется
`PRAGMA integrity_check` и только после успешной проверки файл атомарно получает
итоговое имя. Копии старше 30 дней удаляются. Для автоматического запуска
добавьте отдельные задания cron для `backup.sh test` и `backup.sh prod`.

## Автоматические Fast и Full развёртывания

Серверные timers опрашивают GitHub каждые пять минут. GitHub не подключается к
серверу, SSH-ключи в Actions не используются:

- `fast` отслеживает **точное, регистрозависимое** имя `Main_test`, требует
  успешный завершённый push-run точного workflow `.github/workflows/ci.yml`
  именно для текущего SHA и обновляет
  только Compose-проект `rso-max-test`;
- `full` отслеживает `main`, требует тот же exact workflow run и обновляет
  только `rso-max-prod`;
- оба режима используют один lock, поэтому не выполняются одновременно;
- успешно применённый SHA записывается атомарно. Повторный poll становится
  no-op;
- после build/identity bot и web останавливаются, а старый API создаёт проверенный
  SQLite backup без конкурирующих writers; затем останавливается API. При ошибке,
  current symlink и контейнеры автоматически возвращаются на предыдущий SHA;
- smoke запускается только после состояния Docker `healthy` у всех трёх
  контейнеров. Ожидание учитывает start period бота.

При rollback неуспешная БД вместе с WAL/SHM сохраняется в закрытом каталоге
`runtime/<stack>/backups/failed-*`, проверенный pre-cutover backup возвращается
атомарно, stale WAL/SHM удаляются, и только затем запускается старый image.
Изменения пользователей между cutover и rollback могут потребовать ручной
сверки с сохранённой failed-БД. systemd оставляет до 1000 секунд после TERM для
rollback; `TimeoutStartSec`/`TimeoutStopSec` больше внутреннего deploy timeout.

Код релиза извлекается через `git archive` из exact SHA. Плавающие checkout,
локальные изменения, symlink/submodule из Git и непрошедший CI отклоняются.
Каждый image имеет отдельный тег контура и SHA.

Runtime и секреты должны быть подготовлены до установки:

```text
/srv/bot-sandbox/config/test/.env.test
/srv/bot-sandbox/config/test/.env.test.runtime
/srv/bot-sandbox/config/prod/.env
/srv/bot-sandbox/config/prod/.env.prod.runtime
/srv/bot-sandbox/state/test/data/database.sqlite
/srv/bot-sandbox/state/prod/data/database.sqlite
```

Env-файлы имеют режим `0600`, принадлежат `botadmin` и никогда не копируются в
Git/release/image. Для первого включения нужны существующие immutable release,
чьи `runtime/test` и `runtime/prod` разрешаются соответственно в
`/srv/bot-sandbox/state/test` и `/srv/bot-sandbox/state/prod`. Legacy production
runtime переносится в maintenance window с остановленными prod-контейнерами и
проверенным backup; installer намеренно не перемещает живую БД.

### Подготовка legacy production

Изменённый вручную `/srv/bot-sandbox/projects/rso_max` нельзя использовать как
baseline. В отдельное окно обслуживания подготовьте чистый immutable release из
exact SHA `main`, содержащий новые healthchecks и deployment scripts. Затем:

1. Запишите production container IDs/image IDs/restart counts и полный SHA.
2. Выполните из legacy-каталога `./scripts/backup.sh prod`, дождитесь проверки
   целостности и сохраните выведенный путь backup.
3. Остановите только `rso-max-prod`: `bot`, `web`, `api`. Test не останавливайте.
4. Убедитесь, что `/srv/bot-sandbox/state/prod` отсутствует. Только при
   остановленном prod атомарно переместите весь `runtime/prod` в `state/prod`;
   работающий `database.sqlite` копировать нельзя.
5. Создайте в legacy и immutable release ссылку `runtime/prod` на state-каталог,
   проверьте владельца `botadmin`, права и SQLite `PRAGMA integrity_check` через
   read-only URI.
6. Пересоздайте только `rso-max-prod` из baseline image, дождитесь `healthy` у
   всех трёх сервисов и выполните `release-smoke.sh full`. При сбое верните
   runtime и старые контейнеры по заранее записанному плану.

Только после этого release и `/state/prod` подходят для installer. Installer не
останавливает контейнеры и не перемещает данные автоматически.

Установка по умолчанию только размещает root-owned scripts/units и записывает
baseline SHA обоих уже работающих контуров — timers остаются выключенными:

```bash
sudo ./scripts/install-auto-deploy-systemd.sh \
  gptbossbest-ux/rso_max \
  /srv/bot-sandbox/releases/TEST_BASELINE TEST_FULL_SHA TEST_BOT_ID TEST_BOT_USERNAME \
  /srv/bot-sandbox/releases/PROD_BASELINE PROD_FULL_SHA PROD_BOT_ID PROD_BOT_USERNAME
```

Перед включением убедитесь, что heads `Main_test` и `main` всё ещё равны этим
baseline SHA и имеют зелёный gate. Повторите ту же команду с `--enable`:

```bash
sudo ./scripts/install-auto-deploy-systemd.sh \
  gptbossbest-ux/rso_max \
  /srv/bot-sandbox/releases/TEST_BASELINE TEST_FULL_SHA TEST_BOT_ID TEST_BOT_USERNAME \
  /srv/bot-sandbox/releases/PROD_BASELINE PROD_FULL_SHA PROD_BOT_ID PROD_BOT_USERNAME \
  --enable
```

Installer сам перепроверяет exact branch heads и CI перед активацией, поэтому
активация с baseline не вызывает немедленное повторное развёртывание. Для
приватного репозитория или повышенного API rate limit добавьте fine-grained
read-only token в `/etc/rso-max-deploy/github.conf`; файл root-owned `0600` и его
содержимое не выводится. Публичный GitHub API работает без токена.
При HTTP 403/429 poll завершается без deploy и повторяется timer; read-only token
рекомендуется для стабильного rate limit, но не обязателен для public repository.

Bot ID и username — публичные значения MAX `/me`, не токены. Installer требует
разные test/prod identities и закрепляет их в закрытой server config. Перед
каждым изменением контейнеров deploy сверяет выбранный env с закреплённой
identity. Ошибочная копия env или смена бота останавливает deploy; токены не
передаются аргументами и не выводятся.

Installer принимает только чистые git worktree baselines с exact HEAD, требует
один image у api/web/bot и OCI label `org.opencontainers.image.revision`, равный
baseline SHA. До записи applied SHA выполняются Fast и Full smoke. Для
существующей непустой БД `BOOTSTRAP_ADMIN_PASSWORD` обязан быть пустым.

```bash
systemctl status rso-max-auto-deploy-fast.timer
systemctl status rso-max-auto-deploy-full.timer
journalctl -u rso-max-auto-deploy-fast.service --since today
journalctl -u rso-max-auto-deploy-full.service --since today
```

## Ручное обновление и откат

Ручной аварийный процесс также использует exact SHA и строго один контур. Для
test кандидат берётся из `Main_test`, для prod — только из `main`. Не используйте
плавающую ссылку `latest`.

```bash
git fetch origin Main_test
RELEASE_SHA="$(git rev-parse --verify origin/Main_test^{commit})"
printf 'Release candidate: %s\n' "$RELEASE_SHA"

./scripts/backup.sh test
git switch --detach "$RELEASE_SHA"
./scripts/deploy.sh test
curl --fail http://127.0.0.1:5001/healthz
```

После приёмки Fast SHA продвигается Pull Request из `Main_test` в `main` и снова
проходит полный CI. Production получает новый SHA merge-коммита из `main`, а не
SHA ветки `Main_test`.

Только после успешного Full CI создайте production backup и разверните exact SHA
из `main`:

```bash
git fetch origin main
RELEASE_SHA="$(git rev-parse --verify origin/main^{commit})"
git switch --detach "$RELEASE_SHA"
./scripts/backup.sh prod
./scripts/deploy.sh prod
curl --fail http://127.0.0.1:5000/healthz
```

`git switch --detach` переводит сервер на уже полученный точный commit без
перезаписи локальных файлов через `git reset --hard`. Рабочее дерево перед
началом должно быть чистым; локальные изменения стенда храните вне Git.

Для отката выберите полный SHA последнего успешно проверенного релиза, создайте
актуальную резервную копию затронутого контура, переключитесь на этот SHA через
`git switch --detach <SHA>` и повторите `deploy.sh`. Если неуспешный релиз
изменил данные несовместимым образом, остановите сервисы и восстановите
проверенную копию SQLite согласно плану восстановления данных.

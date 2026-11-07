# T5600: forensic attribution первоначального Link Down

## Вывод на текущем наборе данных

**Не установлено, какие именно bytes writer и Python runtime были исполнены
при первоначальном исчезновении GPU0.** Наиболее конкретный source candidate —
`16f39946d95afe4c63e92404cb0e8bd89c39ac5e`; он точно совпадает с архивом
workspace. Но архив не снят с аварийной T5600, а crash JSONL отсутствует.
Применимость `struct.pack_into` к реальному аварийному запуску остаётся
**Unknown**. Она согласуется с известным семейством исходников и симптомом,
но нельзя исключить другую версию или локально изменённый primitive.

Создан [read-only collector](../tools/collect_t5600_forensics.py), чтобы
получить недостающие локальные признаки с T5600. Его результаты **ещё не
получены**. В разработке collector проверялся только на временных fixtures;
никакие synthetic history/kernel записи не являются evidence T5600.
Решение: **NO-GO** для SPEED/CHANGE_SPEED и любых новых аппаратных опытов.

## 1. Шкала доказанности и идентификация

| Утверждение | Степень | Источник / предел |
| --- | --- | --- |
| Три исходных Git версии используют `pack_into` прямо на mmap | **Confirmed** | Закреплённые ниже source versions; это свойства кода, не execution trace |
| `work/original.py` совпадает с `16f3994` | **Confirmed** | SHA256 и Git blob; `work/base.commit` имеет Git object SHA1 `16f3994…`. Это provenance workspace archive |
| `16f3994` — ведущий кандидат исходного writer | **Strongly supported** | Совпадение сохранённой копии и последнего состояния исходной ветки; без crash-file hash не уникальная атрибуция |
| Именно этот файл выполнялся при Link Down | **Unknown** | Нет contemporaneous SHA/argv/runtime; HEAD, reflog и mtime сами по себе недостаточны |
| На аварийной машине использовался именно `struct.pack_into`, а не другой primitive | **Unknown** | Все известные legacy versions содержат его; локальную правку/другой путь запуска нельзя исключить |
| CPython/Ubuntu reference pack_into предварительно очищает destination | **Confirmed** | Source + audited Ubuntu reference binary + RAM; не идентифицирует аварийный runtime |
| Предварительный clear дошёл до GPU и вызвал исчезновение | **Possible** | Согласуется с потенциально опасным промежуточным 8C040=0; bus trace отсутствует |
| GPU0 пропала после указанного SPEED intent и потребовала AC recovery | **Strongly supported** | Сообщение оператора; нет crash journal для независимого восстановления точного порядка/времени |
| Текущие GPU0 unbound/D0/MSE/BME описывают момент аварии | **Unknown** | Текущее состояние не переносится назад через cold boot и последующие эксперименты |

Это степени для **конкретных утверждений**, а не вероятность успеха будущей
записи. Даже Confirmed для записи в history означает присутствие записи,
а не доказательство выполнения команды и не её исходный file hash.

### Проверенные архивные fingerprints

| Версия | SHA256 `p104_gen2_linux.py` | Номинальный primitive |
| --- | --- | --- |
| [c2f4a75](https://github.com/AlexGL402/p104-unlock-v3/blob/c2f4a75bc34cbe8d0e6c3a9d3eeec920596a5861/p104_gen2_linux.py#L90-L106) | `34208c695b1a4c89d126950aa0219724f94f93ca7452aaeb09dcdd6b42372357` | `struct.pack_into`, line 98 |
| [e1bff32](https://github.com/AlexGL402/p104-unlock-v3/blob/e1bff32c92304a3b6fc1029e4d9c99d459ea8741/p104_gen2_linux.py#L90-L106) | `f5cca64aed395e1b53b0608f7851d6e7d1557b9412d4a3a9d37a417a63f0269b` | `struct.pack_into`, line 98 |
| [16f3994](https://github.com/AlexGL402/p104-unlock-v3/blob/16f39946d95afe4c63e92404cb0e8bd89c39ac5e/p104_gen2_linux.py#L87-L106) | `c5f61e73c47221ca0ccbf1c9a8e1504d9a42dbe3c8ef5f66a7c5008e10688f9d` | `struct.pack_into`, line 101 |

Последняя версия дополнительно имеет точный baseline и explicit BDF guard;
все три разделяют дефект writer. Их совпадения collector определяет по
полному SHA256, а не имени/mtime/тексту комментариев. Для неизвестной копии
он отдельно делает AST-анализ без исполнения: primitive, line numbers,
syntactic mmap argument, resource0/resource0_wc, flags O_RDWR/O_SYNC и
MAP_SHARED/PROT_WRITE. Наличие flags в AST не доказывает выбранную runtime
ветку или фактический memory type.

## 2. Timeline: что уже восстановлено и что ещё нет

Время ниже — Asia/Yekaterinburg (UTC+5), 2026 год. Git timestamp обозначает
создание записи Git, а не запуск программы. Полный разбор 13 JSONL не
повторён: используется [Evidence audit](linux-gen2-transition-decision.md#evidence-audit).

| Время / этап | Наблюдение | Степень / источник |
| --- | --- | --- |
| 06.11 05:28:37 | Commit c2f4a75: первый legacy Linux writer | **Confirmed**, Git metadata |
| 06.11 05:30:32 | Commit e1bff32: CAP2 gate | **Confirmed**, Git metadata |
| 06.11 05:31:43 | Commit 16f3994: strict baseline/BDF guard | **Confirmed**, Git metadata |
| Точное время неизвестно | Оператор сообщает первые три успешные BAR0-записи, затем исчезновение на intent `20085800→20045800`, до TLS/RL | **Strongly supported** как сообщение оператора; конкретные syscalls/stores/runtime **Unknown** |
| Между аварией и поздними опытами | Полное AC recovery по сообщению оператора | **Strongly supported**; соответствующие boot/kernel записи пока не представлены |
| 06.11 06:10:07 | Staged rewrite a902cc1, другой typed backend | **Confirmed** для кода; не доказывает, каким был ранее исполненный файл |
| 06.11 06:22:12 | Двухзаписный Stage1, STOP | **Confirmed** в [S1:1–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1/gpu0-1793928132780078956.jsonl#L1-L6); не crash trace |
| 06.11 06:39:11 | Override: третья запись дала CAP2=6, CAP остался Gen1 | **Confirmed** в [O1:50–54](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1-override/gpu0-stage1-override-1793929151155255357.jsonl#L50-L54) |
| 06.11 06:55:08 | Cap-speed: CAP Gen2, physical Gen1 x4 | **Confirmed** в [C:4–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L4-L6) |
| 06.11 07:06:36 | TLS + один root RL: Root LBMS появился, actual Gen1 x4 | **Confirmed** в [R:8–18](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L8-L18) |

Нельзя вывести время аварии из mtime сохранённой копии, «поместить» её
между двумя Git timestamps без execution evidence или совместить baseline
разных boot. Дальнейшая correlation должна учитывать UTC/локальную зону,
naive timestamps старого writer, boot ID и monotonic timestamp journal.

## 3. Collector: что собирается и как защищены данные

Collector работает обычным пользователем на Ubuntu 24.04; root/sudo режим
отвергается. Он не повышает привилегии и не пытается получить недоступные
root shell history или private configurations.

Сохраняется один JSON:

* Текущие Python version/implementation, путь и SHA256 executable и `_struct`;
  для built-in `_struct` используется interpreter. Dpkg package versions
  читаются через фиксированный `dpkg-query`. Это **current on-disk snapshot**,
  не доказательство runtime аварии или loaded image при обновлении файла.
* Найденные source copies: SHA256, размер, mtime/ctime с явным
  `timestamps_prove_execution=false`, redacted location и AST facts.
  Исходники не копируются целиком: в них могут находиться inline credentials.
* Git history file changes, commit timestamps, reflog entry timestamps,
  содержимое локальных Git source blobs в виде SHA/AST facts; raw source,
  авторы, emails, messages, remote URLs и private branch names не сохраняются.
  Если имеется `work/base.commit`, сохраняется его Git object hash без
  авторского header/message; это archive provenance, не execution evidence.
* Узкие совпадения в **сохранённых** `.bash_history`/`.zsh_history`: line,
  saved timestamp, тип команды, allowlisted flags, hashes pathname tokens
  script/interpreter. Не сохраняются команды целиком, URL, variable values,
  passwords, arbitrary argv или whole-history hashes. Absolute script token
  можно сопоставить с `absolute_path_sha256` current copy; относительный token
  не устанавливает рабочий каталог в момент запуска.
* Existing saved JSONL/log facts: write intents/readbacks, известные BAR0/
  link fields, provenance SHA при наличии, canonical STOP classes.
  Raw reason/messages и неизвестные поля отбрасываются. `before_mmio.new`
  явно помечен **intent_not_ack**.
* Доступные boot list и kernel journal за заданное окно: только события
  с явным GPU0/Root0 marker и class Xid/AER/Link Down/endpoint lost,
  timestamp/boot ID и числовой Xid. Полный MESSAGE не сохраняется;
  записи с GPU1/её Root BDF исключаются. Xid79 — сообщение драйвера,
  не спецификация того, какой MMIO store вызвал отказ.

По умолчанию проверяются repository, `work/original.py`, известный верхний
`$HOME/p104_gen2_linux.py` и ограниченные Downloads/Desktop/p104-unlock-v3
каталоги. **Нет рекурсивного сканирования всего HOME**. Имена source-файлов
ограничены `p104_gen2_linux.py` и обычными backup suffix; произвольное
переименование или копия в другом каталоге может остаться неизвестной.
`--search-root` позволяет явно расширить scope без автоматического поиска
по диску; указанная ниже команда использует только default scope.

Private/hidden directories, symlinks, FIFO и специальные файлы исключаются.
`/sys`, `/proc`, `/dev`, `/run` запрещены как исследуемые пути. Не открываются
resource0, PCI config, ROM, AER sysfs counters или driver controls.
Найденный source только хешируется и парсится AST; никогда не импортируется
и не исполняется. Никакие найденные shell commands не выполняются.

Единственные subprocess utilities — фиксированные `/usr/bin/git`,
`/usr/bin/journalctl`, `/usr/bin/dpkg-query`. Нет shell, network/fetch,
pager, textconv/external diff, signature-verifier или hooks. Git запускается с минимальным env,
disabled global/system config, optional locks и протоколами; stderr не
переносится в отчёт. Репозиторий не checkout/reset/fetch/status/update-index.
Его текущую metadata не следует «исправлять» перед сбором.

Ограничения: scan depth5 / 8192 entries / 48 sources / 48 logs; source512KiB,
log2MiB, saved history8MiB; max128 matches/input, 10000 log lines, 50000
history lines; Git history48 / reflog128 / objects51; 6 s и bounded stdout
на utility, report≤4MiB. Размер/timeout/неполный shallow clone/expired reflog
отмечаются как неполнота, не как отсутствие события. Отдельные ограничения
scan могут скрыть файл: нельзя считать найденные копии исчерпывающим набором.

Kernel query использует `_TRANSPORT=kernel` **без `-k`**, чтобы не ограничить
его неявно текущим boot. Окно ограничивает journal, а не датированные
history matches. Сохранённая history текущего пользователя не включает
несброшенный session buffer, удалённую history и отдельный root shell.
User journal visibility никогда не объявляется полной; отсутствие событий,
доступа или persistence не означает отсутствия Link Down.

Выход создаётся эксклюзивно с mode0600, без перезаписи существующего файла.
Иные явные file writes collector не выполняет; обычные filesystem reads
могут обновить atime, которое не используется в выводах. JSON содержит
хеши/метаданные, поэтому его следует просмотреть перед передачей.

## 4. Одна команда для T5600 и передача результата

Нужны именно данные T5600. Из корня актуальной ветки PR #1, обычным
пользователем, без sudo, выполнить **ровно эту одну команду**:

```bash
python3 -I -S tools/collect_t5600_forensics.py --repo . --since 2026-11-05T19:00:00Z --until 2026-11-06T03:00:00Z
```

Окно соответствует 06.11.2026 **00:00–08:00 Asia/Yekaterinburg** и выбрано
шире известной последовательности, поскольку точное время аварии неизвестно.
`-I -S` отключает local/PYTHONPATH/site подмены imports. Команда ничего не
проверяет на GPU и не запускает найденный writer, даже если history содержит
`--apply`. Compiler и native build для сбора не нужны.

Прикрепить созданный `t5600-linkdown-forensics-…Z.json` к разговору после
просмотра. **Не передавать полные shell history, private configs или исходники
с credentials.** Не удалять старые logs и не менять Git tree ради совпадения
hash. Если часть данных недоступна, передать JSON с такими gaps; sudo не нужен.
До получения файла этот отчёт не выдаёт forensic candidate за установленный
crash writer.

## 5. Как будет проверяться связь с аварией

1. Отделить source facts от execution facts. SHA current copy → архивная
   версия; совпадающий absolute path token history → кандидат места запуска.
   Ни одно из них отдельно не доказывает bytes файла в прошлом.
2. Сопоставить history timestamp/flags с reflog, но учитывать независимые
   часы и возможность edits. HEAD не равен working tree; detached запуск,
   backup copy и изменение файла после запуска возможны.
3. Искать contemporaneous saved log с script/runtime hash и GPU0 SPEED
   intent. Привязать его к boot/time kernel GPU loss. Если hash отсутствует,
   степень может быть Strongly supported, но не искусственно Confirmed.
4. Проверить фактический interpreter resolution. `sudo python3` может выбрать
   другой PATH/runtime, чем текущий collector; aliases/functions, shebang,
   venv и обновления пакетов остаются альтернативами. Shell configs ради
   этого не собираются и не выполняются.
5. Если provenance указывает на pack_into path, сопоставить точный historical
   binary/package с уже audited reference или разобрать отличающийся binary
   **offline**. Не подставлять current Python build автоматически.
6. Вручную обновить evidence grades и timeline. Collector всегда оставляет
   execution attribution Unknown и hardware NO-GO до такого review;
   он не выбирает следующий аппаратный trigger.

## 6. Механизм без GPU: что доказано и что нет

Ранее [root-cause investigation](linux-gen2-root-cause-investigation.md)
и [primitive audit](linux-gen2-mmio-primitive-audit.md) установили:
legacy source передаёт mmap destination в pack_into; CPython сначала
делает clear, затем packer; официальный Ubuntu reference ELF сохраняет
`memset` call в `0x62b239`, а uint packer имеет dword store в `0x629cf8`.
Адреса относятся только к указанному там interpreter SHA256.

Дополнительный тест выполнен **только на приватной RAM**: обычный `int=2**32`
передан в `<I`, без наблюдающего `__index__` callback. Возникает `struct.error`,
но destination остаётся нулевым; соседние sentinels сохранены. Это прошло
на development CPython3.12.14 и на распакованной Ubuntu reference3.12.3.
Таким образом, clear можно наблюдать и без callback. Вход намеренно
невалиден и **не является аварийным target**; результат не устанавливает,
какой path реально исполнялся на GPU. Native primitive для этого вывода
вообще не требуется.

Доказанная **программная** последовательность для reference packer содержит
clear до конечной упаковки; она не равна доказанной последовательности
**PCIe MMIO transactions**. Количество/ширина libc memset stores, memory
type, posting/coalescing, byte enables и внутреннее принятие GPU неизвестны.
RAM result или одна CPU instruction не заменяют bus trace. Не следует
называть это точно двумя dword TLP: `memset` может писать иначе.

## 7. Возможные причины исчезновения и оставшиеся Unknown

Ведущая **Possible** причина: скрытый clear промежуточно менял всё `8C040`,
а не только SPEED. Ноль сбрасывает bit29 при bit31=0 и содержит SPEED=0;
upstream сообщает риск такого width quadrant. Если ноль был принят GPU,
это могло нарушить controller/PHY state до позднего readback. Допустимость
конечного `20045800` и точный readback первых трёх writes этого не исключают.

Однако этот механизм нельзя приписать аварии, пока не связаны её writer
и runtime. Если фактически был typed/native store, эта версия объяснения
для него не действует. Возможны независимые опасные side effects конечного
слова, неподготовленный controller gate, ownership/PM или platform/VBIOS
training differences; см. три гипотезы предыдущего расследования.

Пока **Unknown**: actual crash file hash/argv, interpreter и libc build,
driver binding/D0/MSE/BME в тот момент, mapping attributes, полный
pre-SPEED baseline, конкретные bus stores, LTSSM sequence и причина
необходимости cold recovery. Отсутствие AER не исключает физического отказа;
Xid79, если найдётся, подтвердит driver-reported loss, а не конкретный store.

**NO-GO остаётся.** Сбор предназначен для объяснения прежнего отказа,
не для автоматического перехода к новому тесту. Никаких SPEED/CHANGE_SPEED,
retrain/reset, bind/unbind, VBIOS/EFI/driver изменений или GPU1 действий.
Даже успешная attribution pack_into не будет отдельным разрешением на
аппаратный повтор. Полное AC recovery T5600 затрагивает GPU1 и не является
автоматическим допустимым fallback.

## 8. Проверка реализации

[Тесты collector](../tests/test_t5600_forensics.py) используют временные
fixtures, canary secrets и mock journal; отдельный Git fixture проверяет,
что textconv/external diff не исполняются. Проверены secret redaction,
filtered GPU0 events across boots, root refusal, size/namespace/symlink/FIFO
gates, source parsing без исполнения, private exclusive output, архивные
fingerprints и Unknown при отсутствии runtime attribution. Дополнительный
RAM контроль не импортирует hardware tools и не обращается к устройствам.
CI запускает fixtures/RAM tests, **не collector против истории CI host**.
Локально прошли все **188 tests**, включая **25 новых**. Дополнительный
plain-int RAM control отдельно прошёл на Ubuntu reference interpreter.

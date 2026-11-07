# P104 / T5600: расследование причины Link Down

## Главный результат и решение

**Найдено принципиальное программное отличие Windows и исходного Linux-порта:
`struct.pack_into()` обнуляет целевую область перед упаковкой значения.**
Исходный порт вызывает её непосредственно на BAR0 mapping. Поэтому его
«SPEED-only write» на уровне CPython содержит промежуточное обнуление всего
`8C040`, а не только изменение SPEED. На обычной RAM это воспроизведено.

Это конкретная, исходниками подтверждённая ошибка MMIO-примитива и ведущая
гипотеза прежнего Link Down. Не требуется предполагать, что допустимое
конечное `20045800` само по себе всегда опасно: перед ним могла пройти
запись `00000000`, сбрасывающая оба width/commit-бита и остальные policy поля.
Но точная сборка Python аварийного запуска, hash исполнявшегося файла,
тип mapping и crash trace не сохранены. **Факт получения нуля GPU и точная
аппаратная причина Link Down пока не доказаны. Расследование не закрыто.**

**NO-GO для нового аппаратного SPEED/CHANGE_SPEED опыта. GO только для
offline-проверки найденного дефекта и provenance аварийного runtime.**
Доказательство скрытой записи не доказывает, что её устранение достаточно
для устойчивого Gen2 под NVIDIA/CUDA. Полезное новое отличие есть;
достаточных доказательств безопасности аппаратного повтора пока нет.

GPU0 `0000:03:00.0`, Root `0000:00:02.0`; GPU1 `0000:04:00.0` исключена.
Во время разработки не выполнялись PCI/MMIO чтения или записи, SPEED,
CHANGE_SPEED, retrain/reset, bind/unbind или CUDA. Рабочие скрипты,
evidence-ветка, NVIDIA, kernel, VBIOS/EFI не менялись. Из кода добавлен
только RAM-only reproducer и синтетические тесты, без hardware executor.

## 1. Доказательная база и версии

Ссылки на код ниже закреплены полными commit SHA и номерами строк.
Это разбор опубликованных исходников, не удостоверение того, что каждый
распространяемый exe собран именно из них.

| Обозначение | Зафиксированная версия / история | Смысл |
| --- | --- | --- |
| W-old | upstream `p104-gen2-unlock`, `0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15` | Старая линия v1.6; C# был добавлен в [038b200](https://github.com/skbskhud/p104-gen2-unlock/commit/038b200cddba2e068fb7421cf1a9957af8b28f05) и в доступной истории не переработан в v3 live path |
| W-3.2 | [4ce9658](https://github.com/skbskhud/p104-unlock-v3/commit/4ce9658299b814625dec2f987567ec6ecab783ca) | Новый tool v1.1: live group, SPEED без bit0, reset как fallback, 150 ms holder |
| W-3.3 | [7957cbf](https://github.com/skbskhud/p104-unlock-v3/commit/7957cbf7f26965aed7a49966cd64c4a0d778fcd5), текущее дерево `ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56` | Tool v1.2: live group сохранена, 20 ms holder, width retrain и переработанный CUDA test |
| L-original | target `linux-gen2`, [16f3994](https://github.com/AlexGL402/p104-unlock-v3/commit/16f39946d95afe4c63e92404cb0e8bd89c39ac5e) | Исходный `p104_gen2_linux.py` с `struct.pack_into` на mmap; не текущий staged backend |
| T-evidence | `ceb943228f6757fe25e114df59d5d3e8b3b1d5ad` | Уже проверенные 13 JSONL; аудит не повторён |
| CPython | `f6650f9ad73359051f3e558c2431a109bc016664` (v3.12.3) | Подтверждает скрытое memset; RAM-reproducer здесь запускался на CPython 3.12.14, не на T5600 |
| Nouveau | Linux v6.8 `e8f897f4afef0031fe618a8e94127a0934896aba` | Связанные GK104 controls; не доказанный GP104 unlock path |

Старый репозиторий также распространяет v3.3 — это не второй независимый
успех. Старые комментарии «обязательно нужен reset» нельзя переносить на
новый live path. Аналогично bit0 в W-old нельзя приписывать W-3.3.

История прежнего Linux-порта важна для отрицательных результатов поиска:
в [README до удаления](https://github.com/skbskhud/p104-gen2-unlock/blob/4f8dc9791f8b26432bed3d4d4eb6db10ee99e87a/linux/README-LINUX.md#L1-L16)
он прямо назван непроверенным на реальном Linux P104, вывод — ожидаемый,
а не измеренный. Его добавления/удаления в октябре 2026 не доказывают
существование успешного Linux Gen2 trace.

## 2. Критическое отличие примитива записи

### 2.1 Прямая цепочка исходников

1. L-original открывает `resource0` с O_SYNC, mmap MAP_SHARED и вызывает
   `struct.pack_into('<I', mm, off, new)` прямо в mapping; затем ждёт 20 ms
   и читает readback. Никакой временной RAM-копии перед записью нет:
   [L:87–106](https://github.com/AlexGL402/p104-unlock-v3/blob/16f39946d95afe4c63e92404cb0e8bd89c39ac5e/p104_gen2_linux.py#L87-L106). Guard проверяет только конечное `new`, а не все промежуточные
   stores библиотеки. `valid_linkcfg` использует XOR bit31/bit29: [L:58](https://github.com/AlexGL402/p104-unlock-v3/blob/16f39946d95afe4c63e92404cb0e8bd89c39ac5e/p104_gen2_linux.py#L58).
2. CPython `Struct.pack_into` получает writable buffer и передаёт **сам
   `buffer.buf + offset`** в `s_pack_internal`, без staging buffer:
   [PY:2053–2084](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L2053-L2084), [PY:2137–2144](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L2137-L2144).
3. Первая операция `s_pack_internal` — обнуление destination через `memset`;
   для `<I` size=4. Лишь затем вызывается packer каждого аргумента:
   [PY:1922–1937](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L1922-L1937), [PY:1985–1993](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L1985-L1993).
4. Native uint packer преобразует значение и копирует четыре байта в
   destination: [PY:631–647](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L631-L647). Варианты little-endian packer/оптимизации
   выбора таблицы не отменяют предшествующее memset. Это не только эффект
   нашего callback: memset расположен до выбора и вызова packer и обычного
   целочисленного аргумента.

Таким образом, на уровне изменяемого буфера исходный вызов имеет вид:

```text
20085800 → 00000000 → 20045800
              ^
       не попадает в before_mmio.new
       и не обязан попасть в поздний readback
```

Речь о **source-level memory mutations**. Число/ширина CPU stores, PCIe
TLP byte enables и факт раздельного применения GPU зависят от реальной
сборки, mapping и posting; RAM-снимок не является bus trace. Если mapping
допускал write combining/coalescing, нельзя априори считать, что каждый
source store виден устройству отдельной транзакцией. Это проверяется по
сохранённому runtime/binary и mapping provenance, не повтором на GPU.

### 2.2 Почему это соответствует симптомам

`00000000` сбрасывает bit29 при уже нулевом bit31. Upstream сообщает
потерю карты для такого сочетания (комментарий [W:627–634](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L627-L634)); само это
сообщение не заменяет аппаратную спецификацию. Кроме того, ноль содержит
SPEED=0 (8 GT/s в pre-TU102 карте), не Gen2, и обнуляет bit11 и все
прочие установленные policy bits. По данным другого Windows sweep,
очистка bit11 также теряла Gen2. Поэтому скрытый clear — существенно
более широкая операция, чем заявленный SPEED-only RMW.

Первые три BAR0-записи могли успешно завершаться, потому что временный ноль
имел иной эффект на этих регистрах; их поздний точный readback не опровергает
обнуление. Непосредственно в `8C040` оно затрагивает известные опасные поля.
Событие могло произойти внутри `pack_into` ещё до 20 ms ожидания/проверки.
Нет необходимости в отдельном TLS/RL, чтобы программная запись оказалась
не тем запросом, который указан в журнале. Конкретное внутреннее действие
нулевого слова на GP104 всё ещё неизвестно.

C# `Mwr` вызывает `Marshal.WriteInt32` по адресу mapping: [W:491–517](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L491-L517).
Windows Python использует `ctypes.c_uint32.from_address(...).value = ...`,
а не pack_into на BAR0: [WL:233–241](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/tools/lib_p104.py#L233-L241). Его `struct.pack` применяется к
обычной RAM для IOCTL аргументов — это другой случай. Нельзя делать вывод,
что всякая Python-сериализация опасна независимо от destination.

Текущий staged Linux backend уже использует typed uint32 assignment и
запрещает рассматриваемые `8C040` writes. Этот документ не меняет его и
не добавляет возможность выполнить SPEED. Отсутствие известного memset
в новом примитиве — необходимое устранение одного риска, не доказательство
безопасности MMIO/Gen2 на T5600.

### 2.3 RAM-only воспроизведение

Добавлен `design/p104_mmio_store_audit.py`. Он сам создаёт приватный
`bytearray` с sentinel-байтами; не принимает mapping, адрес, путь или
caller-owned buffer. `__index__`-callback снимает RAM во время преобразования
аргумента. Это делает промежуточное состояние видимым без polling GPU.
Вспомогательная функция не выдаёт этот снимок за аппаратную трассу.

Единственная команда reproducer, без sudo и без доступа к устройствам:

```bash
python3 -m design.p104_mmio_store_audit
```

На CPython 3.12.14 для targets `20045800`, `20045801`, `20085801`:
`conversion_snapshots=[0]`, final равен target, sentinels сохранены.
При искусственной ошибке преобразования приватная RAM остаётся обнулённой.
Семь синтетических тестов проверяют эти свойства, побитовые различия,
ограниченность входа и отсутствие hardware imports. Callback отличается
от обычного `int`; доказательство общего clear для обычного int даёт
исходный `s_pack_internal`, а не одно лишь наблюдение callback.

## 3. Windows/Linux timeline — по отдельным версиям

Это реконструкция **кода и опубликованных записей**, не единый реальный
timestamped trace. Где per-write driver state или baseline не опубликован,
ниже явно остаётся unknown.

| Фаза | W-3.3 C# live path | Windows Python toolchain | L-original / T5600 |
| --- | --- | --- | --- |
| Драйвер перед MMIO | WaitReady пытается дождаться `nvidia-smi -L`; после timeout всё равно продолжает. NVIDIA не отвязывается в live path; точного per-write binding/PM trace нет | Открывает BYOVD session, не выполняет автоматический unbind; точный NVIDIA state per write не журналируется | В текущих staged логах driver_bound=false. Состояние драйвера во время старой аварии нельзя доказать этим набором; нынешнее unbound D0 не исторический snapshot |
| Доступ к BAR0 | WinRing0 IOCTL для config; ThrottleStop IOCTL 8000645C отображает физические страницы BAR0; Marshal Read/WriteInt32 | Те же helpers; typed ctypes uint32 MMIO | sysfs resource0 mmap, read unpack_from, **write pack_into с предварительным clear** |
| До live group | Обнаружение GPU/root, config write self-test в RO identity offset00, snapshot, registry policy keys | Проба write channel / baseline, locks; не идентична C# CLI во всех деталях | Точный baseline guard и BDF, журнал; никаких сведений о скрытом RM preconditioning аварии |
| MMIO group | `8872C → 2241C → 8841C → 8C2C0 → 8C300 → 8C040 → 8C1C0 → 88610`; RMW, skip unchanged, 20 ms после каждой изменившейся записи перед readback | Та же группа и skip, 20 ms; точный full readback, mismatch может вернуть false | Такая же номинальная группа; на T5600 CYA0/1 no-op. По сообщению оператора исчезновение при 8C040, до cap-speed/VSEC/TLS/RL. Каждый pack_into может добавить незаявленный clear |
| CAP2 | Проверяется после всей группы, до 3 rounds × ~2 s, между rounds 300 ms | Аналогичный retry, может идти к TLS даже после неудачного latch | Поздние отдельные staged опыты: CAP2 появился после третьей prefix-записи, CAP Gen2 — только после cap-speed |
| TLS и retrain | TLS endpoint затем root; **RetrainBoth пишет RL root и endpoint**; до 3 rounds, polling J2 каждые 50 ms до 3 s, ещё live/retrain fallback | TLS оба; **root-only RL**, до 3 rounds с gap≥2 s и дополнительным fallback | В аварии эта фаза не достигнута. Отдельный безопасно ограниченный staged опыт: два TLS readback, один root RL, Gen1 x4 |
| Первый Gen2 | Код впервые ищет J2 после RL; published v1.1 report: первая попытка ~3 s, 0 resets. Полного per-write trace нет, нельзя исключить более ранний transient | Определяется sampled LNKSTA predicate после retrain | Физический Gen2 ни в одном доступном T5600 JSONL не зарегистрирован |
| Reset | В успешной live ветке отсутствует; при failure есть автоматический PnP fallback, 6 s disable, re-enable/remap | Утилита apply сама не повторяет весь C# reset path | В аварии не выполнялся по сообщению оператора. В этой разработке отсутствует |
| Holder | Main входит в HoldLoop **после** UnlockOnce, даже если unlock неуспешен; `-once` выходит до loop. Scheduled install может запустить отдельный экземпляр | Отдельная CLI apply, не тот же бесконечный C# HoldLoop | Автоматического holder нет и не добавляется |
| Поздние записи | 20 ms loop, SPEED исправляется при отклонении, RL может повторяться через 20 ms; width arm/retrain после Gen2 | Нельзя считать single-shot успех доказательством поведения C# holder | Новые попытки, bind и восстановительные записи запрещены |

Источники timeline: [W:828–854](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L828-L854), [W:2178–2226](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L2178-L2226),
[W:636–695](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L636-L695), [W:1251–1328](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L1251-L1328), [W:2230–2347](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L2230-L2347),
[WP:218–251](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/tools/apply_gen2_width.py#L218-L251), [WP:296–345](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/tools/apply_gen2_width.py#L296-L345), [WP:664–700](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/tools/apply_gen2_width.py#L664-L700).
Linux/evidence: [Evidence audit](linux-gen2-transition-decision.md#evidence-audit).

**Скрытые различия C# и Python важны:** C# PCI config helper передаёт uint32
payload и четырёхбайтный read через IOCTL; Linux staged TLS/RL — 16 bit.
Dword LNKCTL write захватывает соседний LNKSTA с W1C status bits; результат
зависит от реального helper. Копировать это как «более правильный retrain»
нельзя. Endpoint RL в C# не является подтверждённым вторым downstream-port
retrain. Ни то ни другое не объясняет прежнюю аварию, произошедшую **до RL**.

C# `ok &= RmwVerify` не short-circuit: последующие вызовы выполняются и после
false. `LiveLatch` не использует возвращаемое значение ApplyLiveGroup.
Это не строгая остановка после первого mismatch. Логи/комментарии успешного
пути нельзя трактовать как гарантию, что не было промежуточных неудач.
`Is8C040Legal` вообще отсутствует в этом RmwVerify call path; в отдельном
WriteSpeedGen2 он есть. Кроме того, реализация `arm31 || bit29` пропускает
11, хотя комментарий называет его опасным. Guard — не hardware safety proof.

### Старая W-old/v1.6 — не live группа v3.3

[O:427–465](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/P104-Gen2x4-组合包/src/P104Gen2x4.cs#L427-L465) сначала программирует XP3G override-регистры, другой pattern
`8841C` (reset-precondition), VSEC и 8872C, затем **одним Mwr** ставит
SPEED+bit0. Если CAP2 не Gen2 и allowReset=true, UnlockOnce вызывает
SetupAPI disable/enable, затем remap; после этого до шести циклов
WriteEnables → SPEED+bit0 → TLS → root+endpoint RL → sleep700ms:
[O:655–723](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/P104-Gen2x4-组合包/src/P104Gen2x4.cs#L655-L723). Начальное ожидание проверяет наличие устройства/BYOVD,
не тот же строгий nvidia-smi gate, что в новом коде.

Поэтому утверждение «Windows работает лишь потому, что всегда ставит
SPEED+CHANGE_SPEED одновременно» **опровергнуто кодом W-3.3**.
Утверждение «Windows всегда делает скрытый reset» также опровергнуто
наличием live path и опубликованным `-once / 0 resets` результатом
[V11:96–104](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/06-v1.1-更新说明.md#L96-L104). Исторические reset-based успехи нельзя смешивать с ним.

## 4. Регистровая матрица и semantic audit

### 4.1 Значения: не объединять разные Windows-снимки

W-old ниже — опубликованная fingerprint-таблица, а не последовательный raw
trace; W-sweep — отдельный baseline 2026-10-31 **после** Gen2/width arm,
holder остановлен. Это разные фазы, их нельзя подставлять как общий
pre-write baseline. Источники: [OF:209–228](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/docs/04-硬件事实表.md#L209-L228), [SW:20–55](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/evidence/P104-2.0x16-XVE位穷举-2026-10-31.md#L20-L55).

| BAR0 | W-old опубликованный Gen1 → Gen2 | W-sweep (Gen2) | T5600 factory → post-cap-speed |
| --- | --- | --- | --- |
| 8872C | `0→6` | `6` | `0→6` |
| 2241C | `0→81` | в этой baseline-таблице не указан | `0→81` |
| 8841C | разные reset/config состояния, нет единого полного before | `C0B40500` | `C0B46500→C0B40500` |
| 8C2C0 | не указан в fingerprint | `068711B3` | `068711B3` без изменения |
| 8C300 | не указан в fingerprint | `040A00C0` | `040A00C0` без изменения |
| 8C040 | `20585800→20545800` | `80045800` (width arm) | `20085800` сохраняется в baseline; аварийный intent `20045800` отдельно сообщён |
| 8C1C0 | `00110036→00120036` | `00120036` | `00010032→00020032` |
| 88610 | `0→1` | `1` | `0`, не изменялся |

Уже имеется Windows-описание **`20045800`**: 397 чтений с одинаковым словом
в другой фазе исследования [OR:138–165](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/P104-Gen2x4-组合包/研究过程记录.md#L138-L165). Поэтому различие 2054/2004 нельзя
превращать в доказательство «наше target слово всегда недопустимо».
Биты20/22, составляющие XOR=00500000 между этими fingerprints, не описаны
в envytools CONFIG_LINK; их состояние не даёт разрешения их менять.

### 4.2 Четыре candidate words и скрытый ноль

| Слово | SPEED[19:18] | directive[3:0] | ACTIVE[4] | bit31 / bit29 | XOR с 20085800 |
| --- | ---: | ---: | ---: | --- | --- |
| `20085800` | 2 (Gen1) | 0 | 0 | 0 / 1 | 0 |
| `20045800` | 1 (Gen2) | 0 | 0 | 0 / 1 | C0000 |
| `20045801` | 1 | 1 (CHANGE_SPEED) | 0 | 0 / 1 | C0001 |
| `20085801` | 2 | 1 | 0 | 0 / 1 | 1 |
| **`00000000`** | **0 (8 GT/s request encoding)** | 0 | 0 | **0 / 0** | **20085800** |

Обычный masked RMW — вычисление конечного dword CPU; устройство всё равно
видит полный store, не магическую masked transaction. RMW сохраняет
неизвестные поля из snapshot, но может вернуть RO/self-clearing bits и
устаревшее значение при concurrent RM writes. Полная константа к тому же
затирает все неучтённые поля. Ни один вариант не позволяет считать
общее число/семантику stores по одному конечному `new`.

Совмещённый `20045801` не доказан безопаснее `20045800`; при старом
pack_into он также предваряется clear. Даже с корректным typed store
внутренний порядок latch неизвестен. Самосброс directive — не ACK;
ACTIVE — sampled status, не счётчик выполненных команд. Отсутствие AER
не доказывает ни отсутствие попытки, ни успех.

### 4.3 Gates: доказанное отдельно от названий

| Регистр | Что подтверждено | Что не подтверждено / статус |
| --- | --- | --- |
| 8872C=6 | Низкий nibble сохраняется; T5600 override prefix достаточен для CAP2. Windows sweep после Gen2 не потерял линк при тестировании низких битов | Название `LTSSM trigger` старого автора и `FUSE_OVERRIDE / CHANGE_SPEED_WIDTH=3` нового — не независимая спецификация. Прямой запуск LTSSM этим store на GP104 **не установлен**; persistent policy/override возможен. Нельзя называть его доказанно только declaration или доказанным CHANGE_SPEED |
| 2241C=81 | Nouveau gf100 использует bit7 как cap-speed gate, bit0 для выбора PCIe version: `(reg & 1)+1`; T5600 значение проверено в prefix | Связь этих полей с физическим rate enforcement GP104 не полностью документирована; не менять другие bits |
| 8841C & ~6000 | Target empirically изменяет CAP2 при override; Windows описание подтверждает ту же mask | В envytools имя GEN2_DIS относится **[12:11]**, не [14:13]. Называть mask6000 «официальными GEN2_DIS bits» неверно: это наблюдённый gate без полной семантики |
| 8C2C0[2] | Уже 0 на обеих системах; номинальная no-op в live group | Не доказан дополнительный недостающий CYA0 gate |
| 8C300[4] | Pascal GP102 header называет BLOCK_HOST2XP_HOLD_LTSSM; уже 0 у Windows и T5600 | Другой Pascal chip, не полная GP104 state-machine specification; повтор no-op не оправдан |
| 8C1C0 | [17:16] cap-speed; T5600 изменение даёт LNKCAP Gen2. **Windows bit2=1, T5600 bit2=0**; Windows SYSTEM_MAX_SPEED[21:20]=1, T5600=0 | В GK104 `version_supported()` использует bit2 для PCIe version support. Это конкретное дополнительное отличие, но не доказанный GP104 физический Gen2 gate; выставлять bit2 нельзя. SYSTEM_MAX_SPEED=0 в той карте означает 8 GT/s, а не запрет Gen2 |
| 88610 | В Windows Gen2 baseline=1, T5600=0. Original live group ставит его **после** SPEED; CAP2/LNKCAP у нас появились без него | Гипотеза обязательного VSEC-before-SPEED не подтверждена успешной первой live группой; могла бы требовать остаточного VSEC=1, но полного pre-SPEED trace нет. Не доказаны enable-DMA/PHY зависимости |

Источники: [envytools pci.xml](https://github.com/envytools/envytools/blob/f102b82381f3f11cee113d16374c87091db039d9/rnndb/bus/pci.xml#L411-L427),
[GK:26–49](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c#L26-L49),
[gf100 version/cap-speed](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gf100.c#L32-L61),
[GP102 CYA1](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/02e64bf229e77873c9df22ebcfed6a3e0b813206/src/common/inc/swref/published/pascal/gp102/dev_nv_xp.h#L25-L29).

Здесь **не найдено документированного GP104 равенства**, связывающего все
эти gates с безопасным live SPEED transition. CAP2 и LNKCAP могут меняться
при сохранении Gen1 фактически — это прямо установлено на T5600;
«физический запрет» как отдельный единственный latch этим не доказан.
Если применить именно GK104/GF100 программные декодеры к T5600 prefix,
получится version=2 из 2241C[0], но version_supported=1 из 8C1C0[2].
Это конкретное несогласование related-family декодеров, заслуживающее
offline-проверки на GP104, а не доказательство необходимости выставить bit2.

## 5. Nouveau / Pascal: что источник действительно разрешает заключить

GK104 setter делает SPEED maskC0000 и **отдельный** CHANGE_SPEED bit0:
[GK:126–147](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c#L126-L147). Предварительно set_link ограничивает request cap-speed и
TLS; init проверяет version и корректирует capability/target. Эти RMW
используют MMIO accessor, а не buffer serializer с предварительным clear.
Это поддерживает разделение controls, но не гарантирует P104 Gen2.

Pascal `gp100.c` PCI function table не предоставляет GK104 pcie speed
callbacks: [GP:24–45](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gp100.c#L24-L45). Опубликованные в release 580.196.07
[GP102 XVE headers](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/02e64bf229e77873c9df22ebcfed6a3e0b813206/src/common/inc/swref/published/pascal/gp102/dev_nv_xve.h#L25-L39)
дают лишь SW_RESET fields; нет необходимого полного 8C040/8872C protocol.
[Open kernel modules того же релиза](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/02e64bf229e77873c9df22ebcfed6a3e0b813206/README.md#compatible-gpus)
поддерживают Turing и новее. Нельзя выдавать их RM за реализацию закрытого
Pascal driver 580.196.07.

Linux [device-I/O documentation](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/Documentation/driver-api/device-io.rst#L35-L110)
различает accessor ordering и posted write completion. Readback после
записи обычно нужен для завершения posted writes, но он не отменяет уже
прошедший destructive clear и не является ACK LTSSM-команды. `sleep`,
fsync JSONL и msync не превращают serializer в корректный MMIO accessor.
Тип cache/mapping ThrottleStop по одному IOCTL payload не установлен;
приписывать Windows особый «лечащий fence» без его реализации нельзя.

## 6. Bound/unbound, Root Port, VBIOS и независимые случаи

| Различие / утверждение | Классификация | Основание и недостающий наблюдаемый параметр |
| --- | --- | --- |
| Исходный Linux writer имеет скрытое memset, Windows typed writer — нет такого serializer step | **Подтверждено в исходниках и RAM** | Для привязки к аварии нужны hash скрипта и Python/_struct build; GPU trace отсутствует |
| Windows live работает только из строго bound NVIDIA state | **Неизвестно** как необходимое условие | Код предполагает RM readiness, но допускает timeout и не записывает per-write binding. Нужны сохранённые Windows PnP/PM snapshots у SPEED |
| Linux unbound D0 эквивалентен initialized RM state | **Опровергнуто как достаточное доказательство эквивалентности** | D0/MSE говорят о PCI доступности, не о PHY clock/policy/firmware. Конкретный недостающий RM register пока неизвестен |
| BME=0 само по себе запрещает physical link training | **Не подтверждено; неизвестно для данного внутреннего path** | Windows command=6, T5600=2 — реальная разница; BME контролирует bus-master transactions, не стандартное разрешение PCIe RL. Нельзя включать BME ради догадки |
| Стандартного CAP/TLS/RL достаточно | **Опровергнуто для испытанного T5600 baseline** | Единственный RL оставил Gen1; не равнозначно доказанному физическому Gen2 fuse lock |
| NVIDIA RM предварительно готовит другие controls | **Косвенно поддерживается** | Известны driver-инициированные drops; конкретные GP104 writes в Windows/Linux версиях не опубликованы. Нужны уже сохранённые бинарники/логи и offline-анализ их paths |
| Root платформы различается | **Подтверждено**, причинность неизвестна | Windows docs: 8086:9A01 / Gen4 x16; T5600 logs: 8086:3C04 / Gen3 x16. Оба объявляют Gen2. Нет сопоставленного TS1/TS2/LTSSM capture, чтобы доказать host training incompatibility |
| Windows disable равен нашему unbind D0 | **Опровергнуто для опубликованного disable опыта** | Там D3hot, MSE=0 и BAR0 недоступен, затем отдельные PM/command writes; это иная последовательность, не инструкция повторять её |
| .19 и .47 имеют разный DevInit, объясняющий отказ | **Неизвестно** | Версии различаются; проверенного образа .47 в ресурсах проекта не найдено. Версия BIOS не заменяет image/hash |

Windows disable evidence: [DIS:53–73](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/evidence/P104-2.0x16-停驱动宽度实验-2026-10-31.md#L53-L73). Это опыт ширины; нельзя выдавать
последующий Gen2 с запущенным holder за isolated unbound speed success.
В исходном Linux-порте нет строгого driver/PM guard нынешнего sampler;
текущее состояние T5600 после cold boot не восстанавливает состояние аварии.

### Поиск независимых случаев

Изучены оба upstream и история, их публичные Issues, индексируемые
Discussions/форумные/китайские результаты и технические публикации по P104,
`.47`, `8C040`, Linux 5 GT/s и bound/unbound. Полного независимого Linux
P104 .47 trace с регистровым baseline, реальным LNKSTA и без watchdog
в просмотренных источниках не найдено. Это результат ограниченного поиска,
не доказательство отсутствия таких систем вообще.

| Кейс / прямая ссылка | GPU ID, BIOS, OS, driver | Baseline / sequence | Actual LNKSTA, throughput, watchdog | Сила доказательства |
| --- | --- | --- | --- | --- |
| [Upstream live report](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/06-v1.1-更新说明.md#L96-L104) | P104 10DE:1B87; проект связывает карту с .19 / Windows11 / 572.75, но per-run manifest отсутствует | Live группа, -once, 0 resets, TLS/RL | Gen2; 1.570/1.673 GB/s; -once без собственного holder, поведение во время CUDA отдельно не изолировано | Исходный автор; не независимый от старого repo. Измерения опубликованы таблицей, raw status.txt не приложен |
| [Windows v3.3 user Issue #2](https://github.com/skbskhud/p104-unlock-v3/issues/2) | 10DE:1B87, BIOS unknown; Win11, 582.78; Gigabyte H77 + GTX950 | Installer + scheduled hold; точных MMIO baseline/sequence нет | status Gen2 x4, GPU-Z @x4 2.0 в игре; raw LNKSTA/throughput нет; holder=yes | Независимое пользовательское сообщение, подтверждает применимость шире одной платы, не Linux и не no-watchdog stability |
| [Windows freeze Issue #2 старого repo](https://github.com/skbskhud/p104-gen2-unlock/issues/2) | P104, ID/BIOS/driver unknown; Windows scripts, R7 240 display | status сначала Gen2; после перезапуска/ручного exe зависание; точный регистр неизвестен | Raw LNKSTA/throughput unknown; установленный bringup/hold | Не независимый register-level Link Down trace. Нельзя приписать сбой конкретно 8C040 |
| [Upstream hazard report](https://github.com/skbskhud/p104-gen2-unlock/blob/0bbfb290ce4ee1bd6da1bfc9008f8173ff560d15/docs/04-硬件事实表.md#L298-L306) | Карта автора P104 1B87, Win11; точный per-event BIOS/driver unknown | Bit walk 8C040; target неизвестен | Сообщается исчезновение GPU; LNKSTA/throughput не приложены | Поддерживает риск register-wide side effects, но не объясняет наш конкретный target |
| [Windows driver-disabled experiment](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/evidence/P104-2.0x16-停驱动宽度实验-2026-10-31.md#L53-L87) | P104 автора, .19 по общему проекту; Win11, per-run driver unknown | Disable→D3/MSE0→отдельное восстановление доступа; ширина arm, Root RL | Во время unbound-подобного опыта Gen1; финальный Gen2 с holder | Не независимый Linux-кейс; не доказательство физического запрета без NVIDIA |

Перепечатки, fork и dual-repo release одного автора исключены из числа
независимых подтверждений. Bilibili-ролики/агрегаторы с «2.0 x4» в названии
без доступного baseline и raw LNKSTA не превращены в регистровые кейсы.
P106/P102 и CMP на других контроллерах не используются для переноса
порядка writes: общность нужного GP104 control path не доказана.

### Offline-прошивка

В доступных деревьях обоих upstream, target checkout, evidence и локальных
library/shared ресурсах не найден сохранённый ROM .47 или .19, пригодный
для побайтового сравнения. Есть публикация расшифрованных .19 DevInit
offsets [SW:130–165](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/docs/evidence/P104-2.0x16-XVE位穷举-2026-10-31.md#L130-L165), но это текстовый отчёт, не сам проверяемый образ.
Она перечисляет SPEED/directives/bit11/14/15, не доказывает live-safe порядок
для .47. Поисковые каталоги .47 BIOS не удостоверяют совпадение конкретной
карты: один version string используется несколькими образами/модификациями.
Новые ROM dumps, скачивание случайного образа как «нашего», прошивка или
исполнение DevInit не выполнялись. Отличия .19/.47 не выдумываются.

## 7. Три ведущие гипотезы — максимум

### H1. Скрытый zero-store serializer, а не чистый SPEED RMW

**Статус:** дефект source path подтверждён; его участие в аварии сильно
поддерживается, но получение zero GPU пока не подтверждено.

* За: прямая цепочка original mmap→pack_into→memset; RAM reproduction;
  zero затрагивает известные опасные поля; первые три readback успешны,
  пропажа именно на чувствительном 8C040, до TLS/RL — соответствует механизму.
* Против/ограничения: нет runtime hash/crash trace; нет доказательства,
  что нулевой store дошёл отдельной транзакцией; опасность width quadrant
  взята из чужого опыта, не GP104 RTL specification. Если реальный crash
  writer был другим, H1 к нему не относится.
* Нужны: точный исполнявшийся файл, Python version/package build,
  `_struct` binary/hash и mapping path/cache provenance. Безопасный способ:
  анализ уже сохранённых файлов/пакетов; RAM-only reproducer без sudo,
  offline disassembly обычного `_struct`, сопоставление source и binary.
* Влияние: прежний опыт нельзя считать изолированным доказательством, что
  чистое изменение SPEED вызывает Link Down. Но исправление primitive
  не разрешает SPEED и не решает целевой no-watchdog Gen2 автоматически.
* Риск: offline нулевой для GPU0/GPU1; повтор старого writer способен снова
  потерять GPU0, а AC recovery прервёт GPU1. Повтор не предлагается.

### H2. Несогласованная controller policy / скрытый physical gate

**Статус:** различия зарегистрированы, причинность неизвестна.

* За: Windows 8C1C0 bit2=1 против T5600=0; Nouveau связывает его с version
  support. VSEC Windows1 против T5600=0. CAP/TLS согласованы, а Gen2 нет;
  исходная авария происходила до cap-speed/VSEC/TLS. Возможна раздельная
  декларация capability и rate-control state.
* Против: GK104 callback не доказывает GP104 hardware gate; SYSTEM_MAX_SPEED
  T5600=0 не Gen1 clamp; успешная Windows live группа сама пишет VSEC после
  SPEED. CAP2 и CAP уже переключались без VSEC; no-op CYA flags совпадают.
* Нужны: target GP104 register semantics/call path, реальный Windows полный
  pre-SPEED snapshot с bit2/VSEC и известным ownership. Безопасный способ:
  existing dumps/source/символы/статический анализ, без новых MMIO.
* Влияние: не вводить bit2/VSEC-before-SPEED как «исправление». После H1
  остаётся отдельный вопрос фактического Gen2 transition; A/E — гипотезы.
* Риск: сам анализ без риска; blind gate-write может потерять GPU0;
  восстановление всей T5600 затронет GPU1. Такие записи запрещены.

### H3. RM/PM или host/board/VBIOS контекст делает genuine live SPEED небезопасным

**Статус:** косвенно поддерживается различием систем, конкретный механизм неизвестен.

* За: RM участвует в Windows readiness и повторно меняет policy при CUDA;
  Windows command=6, T5600=2; VBIOS, Root Port и initialization history
  различаются. D0/MSE не доказывают готовность внутренних PHY clocks.
* Против: реальные Windows no-reset live успехи и одинаковый target
  20045800 в других фазах; нет matched trace, который связывает unbind,
  BME=0 или T5600 именно с Link Down. Host поддерживает Gen2; отсутствие
  AER не исключает, но и не подтверждает electrical training failure.
* Нужны: сохранённые PM/config/RM snapshots обоих запусков, точный Windows
  driver build, verified .47 image, для физической причины — уже имеющийся
  LTSSM/TS capture или документированный пассивный источник. Безопасный
  текущий путь — offline-анализ артефактов; не bind, reset или ROM dump.
* Влияние: не переносить Windows registry keys, driver version или .19 ROM
  на Linux как готовый рецепт. Даже исправленный access primitive не
  устраняет эту неопределённость.
* Риск: offline отсутствует; RM/PHY эксперименты могут потерять GPU0 или
  потребовать общей перезагрузки. CUDA init способен затронуть общую
  driver enumeration, поэтому изоляция GPU1 требует отдельной оценки.

## 8. Конкретные недостающие данные и следующий безопасный шаг

1. **Crash provenance:** сохранившийся hash/текст запускавшегося файла,
   command line, shell history timestamps, Python package version и `_struct`
   binary. Не запускать старую --apply ветку даже в целях диагностики.
   Без этого H1 остаётся причинной гипотезой, несмотря на установленный дефект.
2. **Primitive binary:** offline найти memset и конечный uint store в
   этой сборке; указать, могут ли быть дополнительные stores. RAM repro
   полезен, но не доказывает PCIe completion/cache policy.
3. **Mapping:** source/kernel путь resource0 vs resource0_wc, BAR flags,
   attribute/memory type из уже сохранённых данных; IOCTL/driver mapping
   реализации Windows. Не менять cache attributes и не делать hardware
   write test для их «проверки».
4. **Windows baseline:** полный snapshot **до первого SPEED**, driver/PM/
   command, 8C1C0 bit2 и SYSTEM_MAX_SPEED, VSEC, все unknown bits 8C040,
   TLS/STA/root identity; published post-unlock snapshot недостаточен.
5. **Target policy:** verified .47 image, если уже сохранён; точный closed
   RM GP104 path 580.196.07. Константы в Turing open RM не заменяют его.
6. **Crash timeline:** если исходный журнал найдётся — сопоставить его,
   не перезапускать отказ. Текущие 13 JSONL не содержат этого события;
   их повторный разбор не создаст недостающую трассу.

Первый шаг уже сделан здесь: source-level расследование и RAM-тесты.
Дальнейшая работа — привязать H1 к точной версии аварийного runtime.
Никакого нового аппаратного trigger или записи в 8872C/88610/8C040
из результатов не выводится. Условие будущего GO — отдельно проверенная
причинная модель и review точного MMIO доступа/предусловий, а не просто
успешный RAM тест или отсутствие AER. Требуется отдельное разрешение.

## 9. Будущая приёмка под CUDA без watchdog — пока только проект

Даже если unbound Gen2 будет достигнут позднее, нужен отдельный этап,
не автоматическая цепочка bind/CUDA. В этой работе его не выполняли.

1. Сначала реальная GPU/Root LNKSTA speed2,width4, idle Training и активный
   Root DLL; согласованные capability — лишь предусловие. Сохранить полное
   окно, отрицательные данные и ограничения sample gaps.
2. Только после отдельного разрешения проверить сохранение при NVIDIA bind,
   `cuInit`, создании живого контекста GPU0, H2D/D2H/compute, idle/load,
   уничтожении и повторном создании контекста. Выбор по UUID/BDF, не первому
   имени P104. Оценить глобальное влияние CUDA/driver init на GPU1 заранее.
3. При bound NVIDIA **никакого BAR0**. Наблюдать разрешённые config/Root
   status и поддерживаемую телеметрию; никакого holder, RL или recovery writes.
   Учитывать, что sampler не гарантирует capture каждого короткого импульса.
4. Оценивать все throughput измерения и ошибки, не лучший из retries.
   Целевой минимум — 30 минут Gen2 x4 в согласованном наборе нагрузок без
   Xid/AER/data errors и восстановительных записей. Любое observed Gen1,
   Link Down или снижение ширины — провал критерия непрерывности, STOP.
5. Не считать post-context Gen2 успешным CUDA результатом. Original
   DoBandwidth ждёт holder после создания контекста, выбирает лучшие
   измерения, затем уничтожает контекст **до** финального ожидания recovery:
   [W:1981–2069](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L1981-L2069). Это другая цель, чем постоянный Gen2 с живым контекстом.
6. При отказе не auto-bind/reset/restore. Full AC recovery T5600 влияет
   и на GPU1; пока её нельзя прерывать, такой способ восстановления
   недоступен и рискованный аппаратный опыт проводить нельзя.

Итог: установлен конкретный дефект исходного Linux MMIO writer, способный
объяснить прежнюю аварию. **GPU-level причинность и достаточный механизм
устойчивого GP104 Gen2/CUDA остаются недоказанными; аппаратный NO-GO сохранён.**

# T5600: provenance writer и аудит replacement primitive

## Решение и границы

**Подтверждён дефект семейства исходных Linux writers; не подтверждено,
какой конкретно файл и Python binary исполнялись в момент аварии.**
Сохранённый source snapshot точно соответствует `16f3994`, но это архив
исходной ветки, а не forensic-копия файла с T5600. Все три найденные ранние
версии используют тот же опасный `pack_into` на BAR0.

Дополнительно проверена реальная Ubuntu 24.04 reference-сборка
`python3.12 3.12.3-1ubuntu0.17`, а не только upstream CPython. Её compiler
**не устранил** предварительное обнуление. Выполнены source/binary audit
и RAM-only сравнения; пакеты распакованы в `/tmp`, не установлены.
Это не утверждение о версии, установленной на T5600 при Link Down.

Добавлен отдельный **RAM-owned read/write prototype**, не BAR0 backend.
Он имеет внутренние audited volatile uint32 accessors и не предоставляет
API для адреса, mmap, fd, внешнего буфера или указателя. Рабочие аппаратные
скрипты не изменены и не импортируют его. В этой работе не выполнялись
аппаратные обращения, SPEED/CHANGE_SPEED, retrain/reset, bind/unbind или
CUDA; GPU1, NVIDIA, kernel, EFI/VBIOS и evidence-ветка не изменялись.

**NO-GO для аппаратного SPEED/CHANGE_SPEED сохраняется до отдельного
разрешения.** Даже доказанный single-store primitive не подтверждает
безопасность конечного register state или устойчивость Gen2/CUDA.

## 1. Происхождение первоначального writer

История `p104_gen2_linux.py` в `linux-gen2` содержит три версии.
Время коммитов ниже — Asia/Yekaterinburg, 06.11.2026 (UTC+5),
не время выполнения на GPU.

| Commit / время | Writer / guards | SHA256 содержимого файла |
| --- | --- | --- |
| [c2f4a75](https://github.com/AlexGL402/p104-unlock-v3/blob/c2f4a75bc34cbe8d0e6c3a9d3eeec920596a5861/p104_gen2_linux.py#L90-L106), 05:28:37 | `pack_into` строка 98; начальная версия, без точного baseline/BDF confirmation поздней версии | `34208c695b1a4c89d126950aa0219724f94f93ca7452aaeb09dcdd6b42372357` |
| [e1bff32](https://github.com/AlexGL402/p104-unlock-v3/blob/e1bff32c92304a3b6fc1029e4d9c99d459ea8741/p104_gen2_linux.py#L90-L106), 05:30:32 | Тот же writer строка 98; CAP2 gate до TLS/RL | `f5cca64aed395e1b53b0608f7851d6e7d1557b9412d4a3a9d37a417a63f0269b` |
| [16f3994](https://github.com/AlexGL402/p104-unlock-v3/blob/16f39946d95afe4c63e92404cb0e8bd89c39ac5e/p104_gen2_linux.py#L87-L106), 05:31:43 | `pack_into` строка 101; точный BAR0 baseline и confirm-BDF | `c5f61e73c47221ca0ccbf1c9a8e1504d9a42dbe3c8ef5f66a7c5008e10688f9d` |

Сохранённая `work/original.py` побайтно совпадает с последней версией.
Её Git blob SHA1 — `09ae0b0ec6809274e18960d60b10f0e766a2f3a1`.
Сохранённый `work/base.commit`, хешированный как Git commit object,
даёт ровно `16f39946d95afe4c63e92404cb0e8bd89c39ac5e`.
Это подтверждает происхождение **workspace snapshot**, не факт запуска.
`work/` не добавляется в PR как новая аппаратная evidence.

Последующий staged rewrite
[a902cc1](https://github.com/AlexGL402/p104-unlock-v3/commit/a902cc192ebf7b640ebfd3e9178253a19f46171f)
заменил backend на typed ctypes assignment. Это более поздний код;
успешные staged readbacks не удостоверяют primitive первоначальной аварии.

Не найдено: hash/копия файла именно с аварийной машины, полная command line,
stdout/stderr того запуска, Python package version/build и crash JSONL.
В 13 уже разобранных журналах этого события нет; повторный аудит не нужен.
Поэтому `16f3994` — наиболее конкретный **source candidate**, а не
криптографически установленный исполнявшийся экземпляр. Общий дефект
сохраняется при выборе любой из трёх опубликованных версий; локальные
неизвестные правки аварийного файла исключить нельзя.

## 2. Конкретная reference-сборка Ubuntu 24.04

Использован официальный [Ubuntu source package 3.12.3-1ubuntu0.17](https://launchpad.net/ubuntu/+source/python3.12/3.12.3-1ubuntu0.17).
Файлы взяты из [официального pool](https://archive.ubuntu.com/ubuntu/pool/main/p/python3.12/).
Полный manifest с SHA256 пакетов, source archives, runtime files,
результатами RAM и адресами disassembly сохранён в
[offline evidence](evidence/linux-mmio-primitive-offline.json).

| Артефакт | Проверенное значение |
| --- | --- |
| Source version / серия | `3.12.3-1ubuntu0.17`, noble-security |
| Interpreter version | `3.12.3 (main, Aug 31 2026, 10:18:26) [GCC 13.3.0]` |
| `_struct` | **built-in**, внутри `usr/bin/python3.12`; отдельного `_struct.so` нет |
| SHA256 interpreter | `e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f` |
| ELF Build ID interpreter | `337d65cf00021797985cc9f77c0cc334a9fbeb38` |
| `_ctypes` | `usr/lib/python3.12/lib-dynload/_ctypes.cpython-312-x86_64-linux-gnu.so` |
| SHA256 `_ctypes` | `aad3e24f186e6c0a1c7efa8f16004f951f527db86f6451d30118657384a456c1` |

Source orig/debian tar checksums совпали с полем Checksums-Sha256
[DSC](https://archive.ubuntu.com/ubuntu/pool/main/p/python3.12/python3.12_3.12.3-1ubuntu0.17.dsc).
Ни один опубликованный Debian patch этого source package не ссылается
на `Modules/_struct.c` или `Modules/_ctypes/cfield.c`. Это checksum/source
сопоставление; отдельная проверка OpenPGP-подписи DSC здесь не выполнялась.

### Реальный machine-code path pack_into

Stripped ELF не сохраняет все локальные имена. Entry points установлены
через PyMethodDef и format tables, а не названы по ближайшему экспортному
символу, который objdump случайно печатает рядом.

* Module-level `pack_into`: entry `0x6cd4f0`, вызов Struct handler
  `0x62b180` в `0x6cd53a`.
* Handler получает destination buffer и offset, загружает format size,
  затем в `0x62b239` вызывает `memset@plt`: RDI — destination,
  RSI=0, RDX — format size (для `<I` = 4). Только после этого вызывает packer.
* Native table для `I`, size=4, alignment=4 указывает packer `0x629c90`.
  Его normal success path заканчивается `0x629cf8: mov DWORD PTR [r12],eax`.
  Matching native-endian optimization заменяет little-endian integer
  pack functions на native packer при совпадающем размере/endianness.

Для повторного **offline** просмотра распакованного reference ELF:

```bash
objdump -d -Mintel --start-address=0x62b180 --stop-address=0x62b300 python3.12
objdump -d -Mintel --start-address=0x629c90 --stop-address=0x629d0b python3.12
```

Эти адреса применимы только к указанному SHA256, не к любому Python 3.12.
Исходная связь buffer → memset → packer:
[CPython v3.12.3 pack_into](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L2053-L2144),
[clearing/dispatch](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L1922-L1995),
[native uint packer](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_struct.c#L631-L647).

**Не следует говорить «компилятор делает ровно два 32-bit stores».**
В этом ELF первый этап — вызов libc `memset`, не фиксированный zero-MOV.
Выбор libc implementation/IFUNC, длина, alignment и настройки компиляции
могут дать byte/word/dword/vector или перекрывающиеся stores. Из кода
`pack_into` не следует точный их счётчик/ширина. Reference `memset` не
устранён dead-store optimization; возможные stores нельзя игнорировать
на основании правильного конечного слова. Конкретная libc аварийной
T5600 неизвестна. Никакого наблюдения PCIe-транзакций здесь нет.

## 3. ctypes: проверен конкретный path, не общая безопасность

В Ubuntu reference `_ctypes`, `I_set` entry `0x15890` найден через fielddesc
для `I`. Для plain uint32, NUM_BITS(size)=0, success path содержит
`0x158b8: mov DWORD PTR [r12],eax`, без предварительного destination clear
или load. Bitfield branch — другой path, его нельзя подменять plain `.value`.

При этом в [cfield.c](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_ctypes/cfield.c#L689-L699)
**есть** source-level `memcpy(&x, ptr, sizeof(x))` перед SET и последующий
`memcpy(ptr, &x, sizeof(x))`. [SET macro](https://github.com/python/cpython/blob/f6650f9ad73359051f3e558c2431a109bc016664/Modules/_ctypes/cfield.c#L384-L404)
использует старое x только для bitfields. В проверенной optimized сборке
GCC убрал ненужный load с plain path; другая optimization/build может
его сохранить. Исходник не имеет volatile-MMIO контракта.

В development CPython 3.12.14 (Clang 22.1.3/BOLT) отдельно просмотрен
`I_set`: plain path также имеет один target dword store; это ещё одна
конкретная сборка, не доказательство для всех ctypes. Возврат правильного
значения в RAM не выявляет всех промежуточных accesses и ничего не говорит
о memory type, posting, driver ownership или внутренних latch GPU.

Кроме того, ctypes принимает произвольные адреса и допускает невыравненное
обращение; bitfield setters могут делать RMW, integer conversion может
маскировать/переполнять значение иначе, чем требует строгий register plan.
Поэтому **ctypes не выбран как автоматически безопасный MMIO replacement**.

## 4. RAM-only сравнение трёх методов

Сравнение выполнено в development runtime и в распакованном официальном
Ubuntu interpreter. Ubuntu запускался с `-S`, собственной распакованной
stdlib и приватным RAM extension; пакеты не устанавливались, GPU не открывалась.
Нативный extension собран GCC 14.2.0 в development environment, а не
приписан Ubuntu build toolchain GCC 13.3.0.

| Метод | Конечное слово при target 20045800 / 20045801 / 20085801 | Что действительно проверено |
| --- | --- | --- |
| `struct.pack_into('<I', RAM, ...)` | Точное для всех трёх | `__index__` probe видит промежуточный RAM=0; ordinary int имеет тот же final. Общий clear подтверждён source и Ubuntu binary, не только callback |
| `ctypes.c_uint32.from_address(private_RAM).value` | Точное для всех трёх | Final RAM + конкретный optimized I_set code path. RAM comparison не измеряет скрытые stores, отчёт явно ставит hidden_stores_measured=false |
| `RamWord.write` + отдельный `read` | Точное для всех трёх | Приватный aligned slot; volatile accessor assembly: один 32-bit store, отдельный 32-bit load; invalid input не меняет slot |

Правильный final RAM у всех трёх **не означает одинаковую семантику**.
RAM memory — WB, не device mapping UC; RAM readback не является PCIe
completion/ACK. `__index__` — наблюдающая подстановка, а не crash input;
про обычный int вывод берётся из общего source/binary path.

## 5. Новый изолированный primitive

Файлы:

* [Native RAM prototype](../design/p104_ram_word.c).
* [Build, disassembly gate и сравнение](../design/p104_ram_compare.py).
* [12 offline tests](../tests/test_ram_primitive.py).

У `RamWord` только `read()` / `write(exact_uint32_int)` и constructor
приватного слова. Нет внешнего pointer/address/buffer API или buffer protocol.
PyType не разрешает subclassing; wrapper валидирует значение до доступа,
отвергает bool, int subclasses, `__index__` callbacks, отрицательные и
слишком большие числа. После неверного аргумента старое RAM значение
сохраняется. Перед каждым access есть проверка alignment.

Native accessors ограничены x86-64 little-endian и проверенным build recipe:
C11, GCC-compatible cc, `-O2 -fno-lto -fno-ipa-cp`. `_Static_assert`
проверяет размер uint32_t и layout; slot явно aligned. До и после load/store
стоит compiler barrier с memory clobber. Он **не** выдаётся за CPU fence.

В проверенной сборке весь accessor, без окружающей Python machinery:

```asm
ram_store32:
    mov DWORD PTR [rdi], esi
    ret
ram_load32:
    mov eax, DWORD PTR [rdi]
    ret
```

Assembly gate требует именно эти инструкции (допускает CET endbr64),
иначе отказывает. Нет автоматического fallback на ctypes/memcpy/pack_into.
В тестах gate отклоняет отсутствующий symbol, дополнительный zero-store и
неправильную ширину. Создание/освобождение Python RAM objects естественно
имеет другие RAM writes; single-store утверждение относится к destination
access в accessor, а не ко всем инструкциям всего Python вызова.

**Это replacement design с audited RAM implementation, не разрешённый
hardware primitive.** В частности, он не имеет BAR0 mapping constructor,
не выполняет auto-readback/rollback, не знает BDF и не подключён к
`p104_gen2_linux.py` или sampler. Для будущего backend потребуется отдельный
review mapping/accessor/ownership слоя. Нельзя просто подставить внешний
адрес в эти внутренние функции и считать audit завершённым.

Безопасная самостоятельная команда — компиляция и обычная RAM, без sudo:

```bash
python3 -m design.p104_ram_compare
python3 -m unittest discover -s tests -p 'test_ram_primitive.py' -v
```

CLI использует temporary directory и печатает JSON с runtime и hashes
interpreter/_struct/_ctypes (для built-in — hash interpreter), compiler
command, source/binary hashes, disassembly и RAM results. Не принимает
device path, BDF, MMIO address или флаг apply. Для изменившегося compiler
проверка машинного кода обязательна; результат прошлой сборки не переносится.

## 6. Архитектурный контракт и его пределы

| Требование | Что необходимо / что установлено |
| --- | --- |
| Alignment/width | Адрес кратен 4, ровно uint32 little-endian, в пределах одной разрешённой области/страницы; никаких byte/unaligned/vector/bitfield/RMW/locked accesses. RAM prototype обеспечивает свой layout, но не валидирует BAR mapping |
| Single access | Один целевой scalar instruction в проверенном бинарнике; никаких serializer clears или implicit destination reads. Одна CPU-инструкция **не доказывает** число PCIe TLP или атомарность внутренних полей контроллера |
| Compiler ordering | volatile scalar access плюс explicit compiler barriers; проверка object code. Volatile сам по себе не упорядочивает обычную память и не является межпоточным lock |
| CPU/memory type | Нужен известный device-appropriate mapping; для рассматриваемого control register требуется отдельная проверка UC semantics. WB/WC не принимать как эквивалент. RAM WB тест не проверяет PAT/MTRR/PCI memory type |
| Posted writes | Store retirement/возврат Python не равен достижению endpoint. Нужен документированный completion/readback protocol, а не sleep, journal fsync или mmap.flush |
| Readback | Только после безопасного Root guard, из допустимого читаемого register, в нужной последовательности. Чтение того же слова после store не является автоматически безопасным и не доказывает выполнение CHANGE_SPEED |
| Ownership/lifecycle | Нет конкурирующих RM/kernel/userspace writers; unbound, D0, MSE/BME, topology, identity, root Training/DLLLA — отдельные guards. GIL не исключает driver/PCIe races |
| Failure | Исчезновение endpoint может дать SIGBUS/abort/in-flight timeout. Python exception handling и software deadline не гарантируют безопасное завершение такого load |

Источники: [GCC volatile semantics](https://gcc.gnu.org/onlinedocs/gcc/Volatiles.html),
[Linux device-I/O](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/Documentation/driver-api/device-io.rst),
[Linux PCI sysfs mapping](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/Documentation/PCI/sysfs-pci.rst#L40-L75),
[Intel SDM](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html),
Vol.3A, sections Guaranteed Atomic Operations и Memory Cache Control.
Intel описывает атомарность отдельных aligned memory accesses и поведение
memory types; это не спецификация latch GP104.

Kernel `readl/writel` обеспечивают архитектурно определённые I/O semantics;
plain userspace volatile pointer не является их универсальной заменой.
Для x86 UC нельзя механически добавлять mfence/sfence как «лечение»: CPU
fence не служит endpoint ACK и не исправляет неверный register state.
Для WC поведение и ordering иные; этот prototype WC **не поддерживает**.
Linux различает `resourceN` и `resourceN_wc` для prefetchable resources;
одного O_SYNC или имени файла недостаточно для заключения о полной цепочке
mapping attributes. Ни msync, ни fsync JSONL не заменяют MMIO access rules.

Assignment volatile uint32 не обязан reread destination; readback нужен
отдельным выражением. Atomic CAS/fetch-or или volatile C bitfields не
используются: они могут добавить MMIO reads/RMW и неподдерживаемые bus semantics.

## 7. Критерии допуска к возможному будущему тесту

Это checklist для отдельного разрешения, а не цепочка действий или новая
CLI-команда для GPU. Выполнение RAM-тестов само по себе не даёт GO.

1. **Provenance:** определить максимально возможный crash writer/runtime,
   сохранить package version, executable/_struct/_ctypes hash и build ID,
   command line и точную копию скрипта. Не воспроизводить Link Down ради hash.
   Для built-in `_struct` фиксировать interpreter, а не искать несуществующий .so.
2. **Compiled primitive:** зафиксировать compiler/flags/source/.so hashes,
   выполнить gate disassembly; исключить дополнительный clear/load/store,
   widening/splitting, LTO/instrumentation и неизвестные wrappers. Не полагаться
   только на RAM final/readback или чужую версию ctypes.
3. **Mapping/accessor review:** описать BAR identity/range/alignment/UC,
   mmap owner/lifetime и безопасное unmap; подтвердить register width/endian
   и completion ordering. Любой будущий API с внешним адресом требует нового
   review: сегодняшняя изоляция RAM к нему не переносится.
4. **Control-state review:** independently объяснить опасные bits/side effects
   8C040 и допустимый baseline. Исправление zero-store не доказывает, что
   конечные 20045800/20045801 безопасны или что Gen2 latch завершится.
5. **Обязательные guards:** GPU0/root точные BDF, отсутствие NVIDIA binding,
   D0/MSE/BME и idle Gen1 x4, Root Training=0/DLLLA=1 до endpoint access,
   полный разрешённый baseline. Не читать BAR0 bound GPU. После cold boot
   prefix отсутствует — отказ; его подготовка только отдельной операцией.
6. **STOP/наблюдение:** отдельный intent log до команды, ограниченные время/RAM,
   read/cycle/gap/skip/error metrics; no GPU access при Training/DLL inactive.
   Link Down, ошибка чтения, binding/PM change, непредвиденный register state,
   AER/Xid или ширина — STOP, без retries/rollback/reset/bind. Нет ACK —
   отрицательный sampled результат может оставаться неоднозначным.
7. **Разрешение и восстановление:** отдельно согласовать конкретную операцию,
   не «любой store этим primitive». В нынешнем PR SPEED/CHANGE_SPEED executor
   отсутствует. AC recovery всей T5600 затрагивает GPU1; пока её нельзя
   прерывать, такой recovery неприемлем и рискованный опыт остаётся NO-GO.
8. **Цель после unlock:** отдельная будущая приёмка actual 5 GT/s x4 при
   NVIDIA bind и живом CUDA context без BAR0 доступа, holder/watchdog или
   recovery writes. Сегодня ни bind, ни CUDA тест не выполняются.

Остаются неизвестными фактические CPU/PCIe stores аварии, memory type,
достижение промежуточного нуля GPU, latch/PHY реакция и возможные RM/firmware
ограничения. Reference Ubuntu binary и RAM audit значительно усиливают
source-level объяснение, но не закрывают эти аппаратные вопросы.

## 8. Offline validation

Локально прошли все **163 unit tests**, включая 12 новых native/RAM tests;
assembly gate и Python compilation checks прошли. Официальный Ubuntu
reference interpreter отдельно воспроизвёл три RAM сравнения. Никакие
пакеты не устанавливались в систему. Отдельный CI workflow
`ram-primitive.yml` собирает prototype на Ubuntu 24.04 / Python 3.12,
проверяет машинный код и публикует RAM report как artifact. CI не скачивает
Ubuntu reference packages и не является аппаратной валидацией.

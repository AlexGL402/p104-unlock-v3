# T5600: короткое инженерное заключение

**Решение: NO-GO для аппаратного запуска имеющейся реализации сейчас.**
Native accessor исправен; проверен RAM-прототип, а не полный путь доступа
к BAR0. Единственная необходимая следующая программная проверка указана ниже.
Дальнейшее расширение forensic-поиска не является условием допуска.

## Проверка primitive

Повторная сборка GCC 14.2, `-O2 -fno-lto -fno-ipa-cp`, дала:

```asm
ram_store32:  mov DWORD PTR [rdi], esi
              ret
ram_load32:   mov eax, DWORD PTR [rdi]
              ret
```

**Один 32-битный store, без предварительного clear и implicit read.**
Alignment не следует из `mov`: его обеспечивают `_Alignas(4)`, static assertions
и `require_alignment()` перед доступом. Проверен [этот код](../design/p104_ram_word.c),
source SHA256 `e8ab8a5e9b6bcdd7f673766cdc969cce5402c637971a3550e4d03ccac5101992`.
Одна CPU-инструкция не доказывает число PCIe TLP или atomic latch GPU.

`RamWord` владеет обычной RAM и инициализирует её в конструкторе. Он не имеет
BAR0 adapter. **Нельзя переносить constructor initialization на MMIO**:
там до явной команды должно быть ноль stores. Поэтому disassembly accessor
пока не удостоверяет весь будущий hardware write path.

## Сравнение с Windows

[ApplyLiveGroup](https://github.com/skbskhud/p104-unlock-v3/blob/ef45a8d3dd4cdbcde6a4611dd9ecda75ac4efb56/src/P104Gen2x4.cs#L636-L674):
`8872C → 2241C → 8841C → 8C2C0 → 8C300 → 8C040 → 8C1C0 → 88610`.
Каждый изменённый dword пишется typed store, затем 20 ms и readback;
неизменённые значения пропускаются. SPEED меняется **без bit0 CHANGE_SPEED**.
Native scalar store соответствует этому способу записи; serializer clear
исключён. Windows live ожидает готовый NVIDIA; наш Linux-доступ остаётся только unbound.
Группа Windows не является разрешением повторить все её операции на T5600.

Для будущего ограниченного опыта фиксируется уже проверенная Linux-подготовка:
префикс `8872C=6 → 2241C=81 → 8841C=C0B40500`, затем cap-speed
`8C1C0=20032`; согласованные capability и TLS=2 должны быть проверены отдельно.
Это отличается от исходного порядка Windows и не доказывает безопасность
перехода, но не воспроизводит SPEED до подготовки capability. Никаких новых
VSEC/CYA/width writes или совмещённого SPEED+CHANGE_SPEED по догадке.

## Реальные условия допуска GPU0

| Условие | Почему существенно |
| --- | --- |
| Полный adapter не инициализирует mapped word; выполняет только явный aligned uint32 access через обычный resource0 mapping, не WC | Пока проверен только внутренний RAM accessor; скрытые stores обвязки должны быть исключены |
| GPU0 `03:00.0` unbound, D0, MSE=1/BME=0; точный подготовленный BAR0 baseline | Bound NVIDIA может конкурировать за policy. При сообщённом factory baseline после cold boot опыт обязан отказать; prefix не восстанавливается автоматически |
| Root `00:02.0`, оба конца Gen1 x4, согласованные Gen2 capability/TLS=2; Root Training=0/DLLLA=1 перед MMIO | Нельзя обращаться к endpoint при Recovery/Link Down. Во время Training/DLL inactive дальнейший GPU MMIO прекращается; никакого автоматического RL/bind/reset |

Отсутствие ACK, неизвестная длительность ACTIVE и неоконченная атрибуция
старой аварии **не добавляются как отдельные блокеры** первого ограниченного
опыта. Остаточный риск потери GPU0 реален: прошлое восстановление требовало
AC. Автоматического восстановления нет; окно восстановления должно учитывать
работу GPU1, поскольку полное выключение T5600 её прервёт.

## Одна минимальная проверка, снимающая программный блокер

Проверить **полный будущий adapter на обычном RAM/file-backed mmap**, с тем же
offset `8C040`: открытие/закрытие не пишет destination; явный write даёт ровно
один 32-bit store, без clear/RMW, с отдельным readback и сохранением соседних
слов. Нужен контроль всех target stores (RAM watchpoint или эквивалентная
instrumentation), плюс disassembly конечной сборки; одного final RAM значения
недостаточно. Никаких устройств и аппаратного executor для этой проверки.

После её успеха, review adapter и отдельно разрешённой подготовки baseline
можно рассматривать **GO на одну изолированную SPEED-only операцию** с наблюдением
Root/GPU и STOP, без retrain/CHANGE_SPEED/повторов. Критерий первого шага —
точный readback и сохранение доступности/ширины; это ещё не Gen2-разблокировка.
Сейчас такой GO и разрешение
на запись не выданы. Native primitive/рабочие аппаратные инструменты не изменены.

## Что новый forensic JSON реально изменил

Evidence: `04298d6f2339f343fb8e4b5e1c02cd922f1adf5f`; номера ниже — строки JSON,
а `history:1602` — номер строки исходной history.

* **Confirmed:** текущий Python3.12.3 / Ubuntu0.17 имеет audited interpreter
  SHA `e50d468e…` [F:12–52](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L12-L52); `.old` точно равен `16f3994` и содержит pack_into
  на mmap [F:92–140](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L92-L140). Current copy SHA `10f3003d…` совпадает с ctypes-версией
  `1699707` [F:2450–2463](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L2450-L2463). Это snapshots, не execution evidence.
* **Strongly supported:** history:1602 — лучший кандидат первоначального
  аварийного запуска: bare token `p104_gen2_linux.py` (SHA `612f0a82…`),
  `--apply --confirm-bdf --confirm-risk` [F:621–638](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L621-L638). Среди известных версий
  этот набор принимает `16f3994`; staged CLI отвергает `--confirm-risk` до
  hardware access. History:1635/1913/1969 имеют `--mode`, без confirm-risk,
  и absolute-token hash, совпадающий с current pathname [F:670–687](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L670-L687),
  [F:936–952](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L936-L952), [F:1110–1127](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L1110-L1127). History:1979 также содержит apply с `--mode`, но относительный token.
  Конкретные mode values collector удалил.
* Reflog: `16f3994` — 06.11 **05:32:08**, `a902cc1` — **06:18:08**
  Asia/Yekaterinburg [F:2268–2280](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L2268-L2280). `.old` mtime/ctime согласуются с этими
  секундами; это поддерживает смену версии, **не датирует history:1602**.
  Boot records показывают следующую доступную загрузку с 05:33:46 [F:2984–3000](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L2984-L3000),
  но не доказывают время команды или AC recovery.
* Применение pack_into в аварии — **Strongly supported гипотеза**, не Confirmed;
  точные исполненные bytes/runtime и получение clear самой GPU — **Unknown**.
  Если history:1602 вводилась уже при staged файле, она могла просто завершиться
  argparse error. Bare Python token также не удостоверяет historical executable.
* Ограничения: ровно **128** history matches, все без timestamps, обрезка на
  history:1992 [F:1269–1285](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L1269-L1285); история не полна, order не является временем
  исполнения. Journal events=[] [F:2603–2605](https://github.com/AlexGL402/p104-unlock-v3/blob/04298d6f2339f343fb8e4b5e1c02cd922f1adf5f/evidence/t5600/forensics/t5600-linkdown-forensics-20261107T012250839310Z.json#L2603-L2605) — только заданное окно, BDF/class
  фильтр и доступные записи; сообщения без BDF, continuation или с обоими GPU
  могли быть исключены. Отсутствие events не означает отсутствия аварии.

Подробности прежнего collector design сохранены в [истории документа](https://github.com/AlexGL402/p104-unlock-v3/blob/905129260424b61e735ade23db9946a9f9ae1eac/docs/t5600-linkdown-forensics.md).
Дополнительный forensic-сбор не предлагается. Аппаратных операций не выполнялось.

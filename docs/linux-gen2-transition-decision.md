# P104 / T5600: решение о переходе с Gen1 на Gen2

Новое source-level расследование: [Root cause investigation](linux-gen2-root-cause-investigation.md).
В исходном Linux-порте найден скрытый clear destination внутри CPython
`struct.pack_into`, вызываемой прямо на MMIO. RAM-only тест подтверждает
промежуточный ноль. Это ведущее объяснение аварии, ранее не учитывавшееся
в сравнении A–E; связь с точным crash runtime и применение нуля GPU ещё
не доказаны. Журнальный Evidence audit ниже сохраняет силу. Hardware NO-GO
остаётся: устранение serializer-дефекта не подтверждает устойчивый Gen2/CUDA.

## Решение

**Новый аппаратный эксперимент с SPEED или CHANGE_SPEED пока не выбран.**
Подготовленные capability и TLS устраняют два известных ограничения запроса,
но ещё не объясняют Link Down при `20085800 → 20045800`. Не найдено
подтверждения, что совмещённая запись `20045801` устраняет его причину.
Самый обоснованный кандидат для дальнейшего исследования — A, с подготовкой
capability/TLS **до** SPEED, но это пока гипотеза, а не рекомендация повторить
аварийную запись. Вариант C полезен лишь при наблюдаемом положительном событии;
при имеющейся наблюдаемости отрицательный результат может ничего не разделить.

Документ заменяет прежнее предпочтение same-speed control как следующего
аппаратного шага в [предыдущем проекте](linux-speed-transition-design.md).
Существующие инструменты и offline-планировщики не изменены; их старое
предложение не является текущим разрешением на эксперимент. Исполнитель
записей не добавлен. Разработка не обращалась к PCI config, BAR0 или GPU.

Цель приёмки — **реальные устойчивые 5 GT/s x4 под NVIDIA/CUDA**, без
восстановительного watchdog. Изменение LNKCAP/LNKCAP2 не является достижением цели.

## Что известно именно о целевой системе

GPU0 `0000:03:00.0`, Root Port `0000:00:02.0`, NVIDIA 580.196.07,
Ubuntu 24.04, kernel 6.8.0-142-generic, VBIOS `86.04.7A.00.47`.
GPU1 `0000:04:00.0` и её Root Port исключены из всех предложений.

Первоначально это были результаты, сообщённые пользователем. Теперь 13 JSONL
проверены программно: см. [Evidence audit](#evidence-audit). Журнал аварийной
SPEED-записи и образ .47 в этом наборе отсутствуют. Полный baseline аварии
и её промежуточные состояния по-прежнему проверить нельзя.

| Наблюдение | Что оно устанавливает | Чего не устанавливает |
| --- | --- | --- |
| Первые три записи исходного unlock успешны; `8C040=20045800` немедленно обрушила линк до TLS/retrain | На этой системе изменение SPEED без директивы уже опасно; восстановление потребовало обесточивания | Какой внутренний переход вызвал отказ; исправляет ли его иной порядок подготовки |
| Два declaration switches без FUSE_OVERRIDE не дали Gen2 capability | Минимальная двухзаписная гипотеза недостаточна в проверенном baseline | Что эти switches вообще не нужны |
| `8872C=6 → 2241C=81 → 8841C=C0B40500` дали LNKCAP2 `2→6` после третьей записи | Префикс достаточен для speed vector Gen2 в этом опыте | Что физическая скорость изменилась |
| `8C1C0: 10032→20032` дало LNKCAP `00453C42`, CAP2=6 | Поле LNK_CAP_SPEED управляет объявленным максимумом | Что оно заменяет внутренний SPEED |
| TLS=2 с двух сторон + один root retrain оставили Gen1 x4 | Эта стандартная последовательность в проверенном состоянии недостаточна | Что физическая попытка 5 GT/s отсутствовала; нулевой AER этого не доказывает |
| После холодного старта GPU0 восстановилась; сейчас unbound, D0, MSE=1, BME=0, префикс утрачен | Сейчас нельзя считать подготовку capability/TLS сохранённой | Что прежний post-cap-speed baseline всё ещё действителен |

Числа BAR0 далее — hex. Известный заводской набор:

```text
8872C=00000000  2241C=00000000  8841C=C0B46500
8C2C0=068711B3  8C300=040A00C0  8C040=20085800
8C1C0=00010032  88610=00000000
```

Историческое десятичное `4537409` равно `0x00453C41`. Его нельзя трактовать
как hex `0x04537409`. После cap-speed ожидается `00453C42`, а не заводское
`00453C41`; фактическая LNKSTA при этом оставалась Gen1 x4.

## Источники и пределы переноса на GP104

1. [Linux v6.8 gk104.c](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gk104.c):
   `gk104_pcie_set_link()` ограничивает запрошенную скорость значениями
   cap-speed и LNKCTL speed **в программном коде**. Setter затем делает
   `nvkm_mask(8C040, C0000, 40000)` для 5 GT/s и отдельно
   `nvkm_mask(8C040, 1, 1)`. Это две read-modify-write операции, не одна.
   Из этого не следует аппаратное равенство «физическая скорость = минимум
   трёх полей» и не следует безопасность первого RMW на P104.
2. [Linux v6.8 gp100.c](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/nouveau/nvkm/subdev/pci/gp100.c):
   Pascal PCI function table не содержит этих GK104 speed callbacks.
   Последовательность из пункта 1 нельзя выдавать за проверенный Nouveau
   unlock на GP104/P104 или за последовательность закрытого NVIDIA RM.
3. [envytools pci.xml, pinned revision](https://github.com/envytools/envytools/blob/f102b82381f3f11cee113d16374c87091db039d9/rnndb/bus/pci.xml):
   PPCI2 `40` описывает directive [3:0], ACTIVE [4], SPEED [19:18];
   для pre-TU102 SPEED: 0=8 GT/s, 1=5 GT/s, 2=2.5 GT/s.
   В `1C0` LNK_CAP_SPEED [17:16] использует **другую** кодировку:
   1=Gen1, 2=Gen2, 3=Gen3. SYSTEM_MAX_SPEED [21:20] — отдельное поле.
   Reverse-engineered карта имеет оговорки; не задаёт ACK, длительность
   ACTIVE, порядок внутренних latch или гарантию composite write.
4. [NVIDIA Pascal GP102 header, release 580.196.07](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/580.196.07/src/common/inc/swref/published/pascal/gp102/dev_nv_xp.h):
   `8C300[4] = BLOCK_HOST2XP_HOLD_LTSSM`. В нашем baseline бит уже 0.
   Этот короткий header другого Pascal-чипа не описывает протокол SPEED /
   CHANGE_SPEED. [README того же релиза](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/580.196.07/README.md#compatible-gpus)
   ограничивает поддерживаемые open-module GPU поколением Turing и новее.
   Наличие Pascal headers не открывает реализацию закрытого GP104 RM.
5. Исходный Windows unlock:
   [C#](https://github.com/skbskhud/p104-unlock-v3/blob/main/src/P104Gen2x4.cs),
   [Python](https://github.com/skbskhud/p104-unlock-v3/blob/main/tools/apply_gen2_width.py),
   [README](https://github.com/skbskhud/p104-unlock-v3/blob/main/README.md).
   Для воспроизводимого сравнения использована также
   [копия C# в этом PR до данного исследования](https://github.com/AlexGL402/p104-unlock-v3/blob/d6510ff2b72481327cfbc6a59083f12fc0e5f6d2/src/P104Gen2x4.cs#L556).
   Целевой репозиторий — fork этого проекта: это **один источник**, не два
   независимых подтверждения. `ApplyLiveGroup` сначала пишет override/switches,
   затем CYA0/CYA1, SPEED, cap-speed, VSEC; TLS/retrain следуют позже.
   `WriteSpeedGen2` специально не включает bit0, с комментарием о нежелательном
   периодическом CHANGE_SPEED. Python повторяет группу, не изолирует механизм.
   Исходная группа использует для `8C1C0` маску `F0000`, а проверенный Linux
   cap-speed — `30000`: при `10032` обе дают `20032`, но эквивалентность
   нельзя переносить на произвольное исходное слово.
6. [Отчёт upstream о побитовом sweep / DevInit](https://github.com/skbskhud/p104-unlock-v3/blob/main/docs/evidence/P104-2.0x16-XVE位穷举-2026-10-31.md):
   сведения относятся к другой P104 с VBIOS .19. Очистка bit11 теряла Gen2;
   у нас bit11 уже 1. Сообщается о самосбросе bit0/1 и найденных в DevInit
   директивах; это не ACK и не трасса выполнения на .47. Однобитный sweep из
   SPEED=1 даёт SPEED=0 или 3, **не 2**: он не доказывает Gen2 при SPEED=2.
7. [Linux v6.8 PCI definitions](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/pci_regs.h)
   задают стандартные поля LNKCAP, TLS, RL, Training, DLLLA и LBMS.
   Стандартный retrain и proprietary directive — разные точки управления;
   равнозначность их внутренней реализации не установлена.

Upstream сообщает реальную LNKSTA `1042` и около 1.55/1.69 GB/s на P104
с VBIOS .19 под Windows/NVIDIA 572.75. Это свидетельство физической
возможности Gen2 на другой P104, но опыт включает группу настроек и holder.
В изученных источниках не найден независимый изолированный тест A/B/C
на P104 .47 + Linux 580.196.07. Примеры на Ampere/CMP и копии одного проекта
не восполняют этот пробел. Отсутствие найденного отчёта не доказывает, что
такого опыта нигде нет.

## Точные различия и сравнение вариантов

Ниже описаны **гипотетические транзакции**, не разрешение их выполнять.
Полные слова применимы только к точному исходному `8C040=20085800`.
ACTIVE=0, directive=0, SPEED=2; bit11 и bit29 установлены, bit31 сброшен.
Не следует переписывать иные поля ради совпадения с другой картой.

| Вариант | Последовательность и XOR | Подтверждение и отличие от аварии | Интерпретируемость / возможный отказ | Риск и решение |
| --- | --- | --- | --- | --- |
| A: SPEED, затем CHANGE_SPEED | После отдельно проверенной подготовки E: `20085800→20045800` (XOR `000C0000`: bit19 1→0, bit18 0→1); затем, только если линк доступен, `20045800→20045801` (XOR 1) | Есть GK104 setter; оригинальный P104 unlock подтверждает SPEED-only в группе. Изолированного GP104 Linux подтверждения нет. Новое относительно аварии — capability/TLS подготовлены раньше; первая SPEED-запись идентична аварийной | Устойчивые 5 GT/s хорошо различимы; Link Down информативен лишь как отказ нового baseline. Gen1 после попытки не отделяет clamp, пропуск команды, краткий Gen2 с fallback и физическую неудачу | Высокая практическая настороженность: потеря GPU от первой записи уже наблюдалась. Нет основания обещать холодное восстановление во всех случаях. Пока не выбирать |
| B: SPEED+directive вместе | После E: `20085800→20045801`, XOR `000C0001` | Нет изолированного подтверждения на целевой P104. Убирает промежуточную CPU-транзакцию, но это **не** порядок Nouveau | Неизвестно, какое поле внутренний автомат увидит первым. Самосброс может оставить ровно `20045800`. Отрицательный результат многозначен; устойчивый Gen2 был бы положительным физическим результатом | Не доказано безопаснее A; Link Down и обесточивание возможны. Пока не выбирать |
| C: CHANGE_SPEED при SPEED=Gen1 | После E: `20085800→20085801`, XOR `00000001`; SPEED остаётся 2 | Directive известна по карте и related-family коду, но отдельного целевого ACK-теста нет. Не повторяет опасную смену SPEED | Наблюдаемый ACTIVE/Training даст положительное свидетельство реакции, не Gen2-разблокировки. Самосброс или неизменный Gen1 — не доказательство выполнения/игнорирования. Даже same-speed команда может запустить Recovery | Меньше изменяемых полей, но нет подтверждённой границы риска; возможен Link Down. При нынешних blind windows не выбран как следующий опыт |
| D: без записи 8C040 | Подготовка E, затем оба TLS `(old & ~F) | 2`; root LNKCTL `old | 20` один раз. В стандартном RL меняется bit5, не ширина | Эта последовательность уже проверена на T5600 и не дала Gen2. Никакого автоматического повтора. Другой возможный агент — NVIDIA RM при bind/PM/CUDA — не изолированный механизм и может сам писать 8C040 | Негативный результат исключает достаточность только испытанной стандартной комбинации. Не исключает внутренний запрос/другие gates или короткий fallback | Повтор не даёт нового технического преимущества. Bind не считать безрисковым альтернативным unlock; сегодня запрещён |
| E: порядок подготовки | `8872C:0→6` XOR6; `2241C:0→81` XOR81; `8841C:C0B46500→C0B40500` XOR6000; `8C1C0:10032→20032` XOR30000; затем TLS=2 на обоих концах | Первые три шага и cap-speed отдельно подтверждены на целевой карте. Это отличие от исходной группы, где SPEED предшествовал cap-speed/TLS | Подготовка даёт согласованный advertised максимум, но не физический переход. Nouveau software clamp обосновывает порядок; неизвестно, действует ли аналогичный аппаратный clamp и предотвращает ли это Link Down | Не самостоятельный следующий unlock-опыт: результат capability уже известен. Префикс утрачен при cold boot; его нельзя автоматически восстановить ради A/B/C |

Для TLS XOR зависит от фактического исходного младшего nibble: 1→2 даёт 3,
3→2 даёт 1, 2→2 даёт 0. Остальные биты LNKCTL2 должны сохраняться;
никаких фиксированных полных config-слов. Все маски таблицы — hex.
Изменение cap-speed и SPEED имеет разные маски и кодировки.

Численные вероятности успеха/Link Down из этих единичных опытов оценить
нельзя. Для A/B положительный устойчивый Gen2 был бы информативен, но
шанс его получения неизвестен. Для C ожидаемая диагностическая ценность
отрицательного исхода низка. Для повторения D/E новизна результата низка.

### Может ли GP104 физически работать на Gen2 при SPEED=2?

Утверждение «никогда не может» пока не доказано. Nouveau использует SPEED
как запрос перед директивой, а не измеритель фактической скорости. Нет
подтверждённой для GP104 модели, в которой это поле непрерывно запрещает
более высокую скорость независимо от того, какой агент запустил переход.
Задержанное применение, другой источник управления, состояние автомата
или драйверная перезапись не исключены.

Но нет и проверенного целевого примера устойчивых 5 GT/s с неизменным
SPEED=2. Наш стандартный retrain с этим значением дал Gen1. Комментарий
`WidthArm` о fallback на `20085800` «без влияния на Gen2» не содержит
изолированной временной трассы с отключённым holder и не является таким
доказательством. Поэтому SPEED — сильная оставшаяся гипотеза запроса,
**не доказанная абсолютная блокировка**. Для её подтверждения нужен
согласованный по времени фактический Gen2 и SPEED=2 без скрытых writers,
либо документированная GP104 семантика. Под NVIDIA читать BAR0 для этого
запрещено; такой контрпример нельзя добывать обходом guard.

### Другие gates: не добавлять переменные без доказательств

`8C1C0[21:20]=0` в related-family карте — не код Gen1; его не надо
«исправлять». `8C300[4]` и `8C2C0[2]` уже сброшены, а `8C040[11]` уже
установлен. `88610=0` не помешал разблокировать capability; это не доказывает
ненужность VSEC для физического перехода, но verified GP104 зависимости
не найдено. Менять VSEC, SYSTEM_MAX_SPEED, ширину, bit29/31 или прошивку
ради перебора нельзя. Original `Is8C040Legal` — программный фильтр, а не
доказательство безопасности: аварийное слово проходило подобные guards.

## Наблюдаемость: что позволяет нынешний sampler

Последний fresh-fast, **без профилирования**, 50 мс: 266 GPU samples,
GPU cycle p50 187.0 мкс, Root cycle p50 30.1 мкс, skew p50 17.7 мкс,
максимальный консервативный GPU gap 232.8 мкс; ошибок и изменений нет,
266 postchecks безопасны. Это достаточная основа для наблюдения
**сохраняющегося** результата, но не полный capture перехода.

* Краткий ACTIVE/Training может целиком попасть между чтениями. Ни максимум
  прошлой выборки, ни p99 не являются гарантированной будущей границей.
* Root и GPU читаются последовательно; это не атомарный снимок.
* Во время Root Training или DLL inactive MMIO намеренно пропускается.
  Именно тогда может быть наиболее интересный внутренний transient.
* Driver/D0/MSE/BME и link guards обязательны, но между check и load остаётся
  race. Исчезновение устройства может привести к ошибке/SIGBUS, которую
  обычная Python-обработка исключений не обязана пережить.
* Удлинение окна помогает увидеть долгий fallback, но не сокращает blind gap.
  Более быстрый опрос не создаёт аппаратный ACK.

Наблюдение ACTIVE или Training после команды свидетельствовало бы о реакции
некоторой логики при исключённых других writers, но не доказывало бы
завершение именно запрошенного перехода. Самосброс directive не является
ACK. Отсутствие ACTIVE, AER или сообщений ядра не доказывает игнорирование
либо отсутствие попытки. Полный LTSSM/сигнальный capture потребовал бы
документированного пассивного state/history источника или внешнего PCIe
анализатора; проверенного такого источника для Intel Root T5600 здесь нет.

В kernel 6.8 AER trace отражает ошибки, а не всю историю LTSSM. Нельзя
подменять её событиями из более новых ядер/других host controllers.
Kernel MMIO trace не превращает Python mmap load в trace закрытого RM.
Подробности и источники: [observability review](linux-observability-review.md).

`Root=7041`, `GPU=1041`: XOR6000 — Root DLLLA (bit13) и LBMS (bit14).
Оба speed/width `(LNKSTA & 03FF)` равны `0041`, Training=0.
GPU LNKCAP `00453C42` имеет DLL active reporting bit20=0: GPU DLLLA=0
здесь не означает Link Down. Root LBMS — sticky management status,
не AER и не доказательство перехода на Gen2. Без предшествующего baseline
его нельзя приписать конкретной команде. Не очищать W1C ради маркера.
Root `3041` в sampler согласуется с Gen1 x4, DLL active, без LBMS.

## NVIDIA bind и CUDA: отдельная незакрытая часть задачи

Ранее успешный unbind/bind GPU0 не подтверждает сохранение новых volatile
настроек при следующем bind. Закрытый GP104 RM 580.196.07 может
переинициализировать policy/capability при probe, PM или создании контекста;
точные записи в этой версии не установлены опубликованным Pascal header.

В оригинальном C# `DoBandwidth` после `cuCtxCreate_v2` ждёт holder,
при его наличии пробует до четырёх измерений и выбирает лучший throughput.
После уничтожения контекста снова ждёт восстановления holder.
Комментарии отдельно описывают возврат SPEED к Gen1 при инициализации
контекста. Это сильное предупреждение о driver ownership, но не трасса
Linux 580.196.07. Лучший throughput, post-exit Gen2 и watchdog recovery
не доказывают непрерывный Gen2 при живом CUDA-контексте.

Будущая приёмка должна разделять: Gen2 unbound; Gen2 после отдельно
разрешённого bind; Gen2 во время создания/жизни/уничтожения контекста,
H2D/D2H, compute, idle/load. Под привязанным NVIDIA — **никакого BAR0**;
только согласованные config-наблюдения и поддерживаемая телеметрия,
которая дополняет, но не заменяет LNKSTA. Не вводить holder и повторы,
маскирующие падения. Нельзя импортировать Windows registry/watchdog policy
как решение для Ubuntu.

Для последующего CUDA-теста выбирать GPU0 по UUID/BDF, не по первому имени
P104. Даже `cuInit` может затронуть общую инициализацию драйвера/перечисление
GPU: обещать абсолютную изоляцию GPU1 на основании выбора одного контекста
нельзя. До такой фазы нужна отдельная оценка воздействия; сейчас bind/CUDA
не выполняются и не входят в предлагаемый следующий шаг.

## Следующий шаг: получить недостающие данные без опасных записей

Следующий выбранный эксперимент — **offline-сопоставление уже полученных
трасс и исходников**, а не новый аппаратный trigger. Он проверит, имеется
ли реальное отличие A/E от аварии, вместо предположения об этом отличии.
Разбор предоставленного набора теперь выполнен в Evidence audit ниже;
пункты о crash log, .47 и точном driver path остаются открытыми.

1. Сохранить исходные JSONL прошлой аварии, override, cap-speed, TLS/retrain
   и fresh-fast; добавить версии файлов/commit, boot ID, монотонные границы,
   единицы времени и stderr. Использовать уже имеющиеся файлы, не повторять
   диагностику и не читать устройства в процессе разработки.
2. Сопоставить полный **pre-write** baseline аварии с post-cap-speed опытом:
   все известные BAR0, LNKCAP/2, LNKCTL/2, LNKSTA обеих сторон, PM/command,
   driver binding. Отсутствующее поле отметить unknown, не восстанавливать
   его из позднего снимка. Не объединять prefix из одного boot с TLS другого.
3. Проверить последовательность/ширину транзакций и задержки оригинальных
   логов. Сохранить AER до/после, ошибки чтения, kernel monotonic window,
   dropped samples и пропуски MMIO. Не объявлять отсутствие ошибок ACK.
4. Offline исследовать **уже сохранённый**, если доступен, образ .47:
   сравнить DevInit paths с .19, условия SPEED/directive, ширину доступа,
   питание/сброс/задержки. Не делать свежий ROM dump, не запускать DevInit,
   не прошивать. Статический сценарий после reset не равен live-переходу.
5. Искать независимый GP104/P104 trace с полным baseline, holder выключен,
   конкретным драйвером, последовательностью SPEED/directive и непрерывной
   LNKSTA. Для закрытого 580 RM полезен offline-анализ уже скопированных
   бинарников с версиями/hash; открытый Turing RM не заменяет его. Нужна
   связь с конкретным GP104 path, не просто найденная константа `8C040`.
6. Если изоляции даёт только внешний пассивный анализатор, заранее оценить
   его собственное влияние на signal integrity. Не предлагать debug writes,
   новый kernel/driver или undocumented LTSSM history-enable ради трассы.

Искомые недостающие сведения: подтверждённая семантика latch/commit GP104;
условия, из-за которых SPEED-only немедленно обрушает линк; доказательство,
что подготовка E устраняет эти условия; target-matched bind/CUDA policy.
ACK не обязателен для доказательства устойчивых 5 GT/s, но нужен иной
положительный маркер, чтобы **отрицательный same-speed опыт C** разделял
исполненную и проигнорированную команду. Сейчас такого маркера нет.

## Условия возможного будущего опыта и восстановления

Это review checklist, не исполнитель и не разрешение на подготовку.
До появления нового объяснения отказа A/B остаются no-go. Любой опыт
с записями потребует отдельного разрешения на точную последовательность.

1. **До подготовки:** отдельно согласованы GPU0-only scope, окно наблюдения,
   риск потери устройства и способ восстановления. Cold power cycle всей
   T5600 затронет работающую GPU1; при требовании не прерывать GPU1 такой
   способ восстановления недоступен и рискованный опыт проводить нельзя.
2. **Baseline:** точные BDF/topology/PCI ID, driver absent, D0, MSE=1/BME=0,
   Root idle/DLL active, оба конца Gen1 x4; все полные известные BAR0 слова
   и config до подготовки. Никаких GPU MMIO при Training/inactive DLL.
   После cold boot отказ из factory baseline: префикс разрешается только
   отдельной операцией, без auto-chain. Затем отдельная проверка согласованных
   CAP Gen2/CAP2=6 и TLS=2. Несовпадение любого gate — отказ, не исправление.
3. **Логирование:** ограниченные время/RAM, последовательные номера,
   monotonic start/end каждого чтения, root→GPU skew, полные слова + поля,
   guard/read/cycle costs, skip/error/drop counts, отдельный JSONL результата.
   Durable intent до возможной команды; никакого fsync в горячем цикле.
   Утрата процесса/хвоста журнала классифицируется как unknown, не success.
4. **STOP:** Link Down/inaccessible config, `FFFF/FFFFFFFF`, read error,
   неожиданный driver/PM/command, ширина не x4, неожиданные MMIO изменения,
   AER delta/Xid, потерянные samples сверх заранее принятой границы.
   Training — немедленно прекратить GPU MMIO, продолжать лишь безопасный
   Root monitor до заранее ограниченного timeout; persistent Training или
   DLL inactive — окончательный STOP. Максимум одна заранее выбранная
   попытка; никакого RL, reset, rollback, bind, fallback или второй GPU.
5. **Успех промежуточной фазы:** обе LNKSTA speed=2,width=4, Training=0,
   Root DLL active, согласованная capability; отсутствие ошибок и устойчивое
   наблюдение в заданном окне (например 2 с) — provisional unbound Gen2,
   не доказательство отсутствия коротких переходов между samples.
6. **Итоговая приёмка отдельно:** после разрешённого bind контекст CUDA
   остаётся живым; фиксируются все H2D/D2H результаты и ошибки, не best-of.
   Предлагаемый критерий — не менее 30 минут actual Gen2 x4 через load/idle
   и lifecycle контекста без восстановительных записей, Xid/AER и потери
   данных. Это критерий будущей проверки, не уже достигнутый результат.
7. **При отказе:** завершить попытку, не обращаться к исчезнувшему GPU,
   сохранить Root/host журналы без recovery writes. Если разрешённое
   восстановление требует полного выключения/снятия питания, его делает
   оператор в согласованное окно после остановки затрагиваемых задач.
   Исторически это помогло, но гарантии нет. После включения не auto-apply:
   новый factory baseline и отдельный review. Не переносить опыт на GPU1.

Такой план даст интерпретируемый **положительный** физический результат и
зафиксирует отказ без автоматических повторов. Он не устраняет текущую
неопределённость отрицательных A/B/C и потому не служит основанием
реализовать или запускать hardware write executor сейчас.


## Evidence audit

### Состав и метод проверки

Аудит привязан к неизменяемому коммиту evidence-ветки
[`ceb943228f6757fe25e114df59d5d3e8b3b1d5ad`](https://github.com/AlexGL402/p104-unlock-v3/tree/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work).
Все **13 JSONL / 75 285 строк** загружены целиком и разобраны, включая
22 772 331-байтный Root sampler. Размер и Git blob SHA-1 каждого файла
сверены с Git tree; ошибок JSON нет. Evidence-ветка не изменялась.

Программная проверка независимо пересчитала из sample-записей p50 (median),
p99 (nearest rank), max для read/cycle/no-read/conservative gap всех
каналов, сверила summary и порядок времён внутри канала. Для всех MMIO
`before_mmio` найдена следующая `after_mmio` с тем же offset и точным
readback; для retrain проверены все 164 port snapshots, AER status и
каждое числовое значение kernel AER counters. Вместе с проверками размера,
hash, JSON/event, длительностей чтения и ts выполнено 68 732 assertions;
все прошли. Это проверки файлов и согласованности, не аппаратная валидация.

Словарь доказательств:

* `old`, `readback`, `actual`, `link/links`, sample `raw` — зарегистрированные
  чтения. Их происхождение — сам скрипт, не независимый bus trace.
* `new`, `expected`, `plan`, `before_*` — ожидаемое значение/намерение;
  одиночное `before_*` не является аппаратным подтверждением записи.
* `capability_gen2`, `gen2_x4`, `retrains`, STOP reason и manifest flags —
  выводы/счётчики/декларации программы; проверяем их по raw fields, где они есть.
* Не записанное поле — **unknown**, а не ноль и не перенесённый из другого
  опыта baseline. Промежутки между файлами не заполнены догадками.

### Хронология всех 13 файлов

Время начала переведено из записанного `ts` / `started_utc` в
**Asia/Yekaterinburg (UTC+5), 2026 год**. ID ниже обозначает конкретный
JSONL; ссылка содержит исходный путь и строки. Внутри staged-журналов `ts`
не убывает. Межфайловой доказанной boot/monotonic continuity нет.

| ID, начало | Строк | Содержимое и завершение |
| --- | ---: | --- |
| [S1:1–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1/gpu0-1793928132780078956.jsonl#L1-L6), 06.11 06:22:12.781 | 6 | Два switches, два точных readback; STOP без fallback/retrain |
| [O1:1–54](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1-override/gpu0-stage1-override-1793929151155255357.jsonl#L1-L54), 06.11 06:39:11.157 | 54 | Три override-записи; CAP2=6 только после третьей; inconsistent capability, STOP |
| [O2:1–54](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/override-520/gpu0-stage1-override-1793930007183451370.jsonl#L1-L54), 06.11 06:53:27.185 | 54 | Второй самостоятельный override-опыт, тот же результат; каталог `override-520` |
| [CP:1–3](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-plan/gpu0-stage1-cap-speed-1793930069627383795.jsonl#L1-L3), 06.11 06:54:29.629 | 3 | Cap-speed **dry-run**, hardware_writes=false; не реальная запись |
| [C:1–26](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L1-L26), 06.11 06:55:08.246 | 26 | Одна cap-speed запись; CAP Gen2, link Gen1; обычный результат capability_consistent |
| [RP:1–5](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-plan/gpu0-stage2-cap-retrain-1793930734987569069.jsonl#L1-L5), 06.11 07:05:34.989 | 5 | TLS/retrain **dry-run**, hardware_writes=false |
| [R:1–18](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L1-L18), 06.11 07:06:36.887 | 174 | Два TLS readback, один retrain intent; отрицательный результат и STOP в строках 173–174 |
| [Q:1–4](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-root-sampler/root0-lnksta-1794002799062628713.jsonl#L1-L4), 07.11 03:06:39.062 | 63 654 | 63 650 Root samples за ~250 мс; stop_reason=duration |
| [D0:1–2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-dual-first/gpu0-dual-1794004110508195856.jsonl#L1-L2), 07.11 03:28:30.508 | 2 | Отказ: driver bound; нет preflight/capture/sample |
| [D:1–9](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-dual-first/gpu0-dual-1794004274508572209.jsonl#L1-L9), 07.11 03:31:14.508 | 875 | 747 Root + 124 GPU samples за ~50 мс; duration |
| [P1:1–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-reference/gpu0-dual-profile-1794005123518674721.jsonl#L1-L6), 07.11 03:45:23.520 | 2 866 | Reference + profiling: 592 Root, 98 GPU, 2 172 profile spans; duration |
| [P2:1–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-fresh-fast/gpu0-dual-profile-1794005316984632966.jsonl#L1-L6), 07.11 03:48:36.986 | 5 695 | Fresh-fast + profiling: 1 177 Root, 196 GPU, 4 318 spans; duration |
| [F:1–9](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-fresh-fast-coverage/gpu0-dual-profile-1794005450984539954.jsonl#L1-L9), 07.11 03:50:50.984 | 1 871 | Fresh-fast без profiling: 1 601 Root + 266 GPU; duration |

### Baseline: что действительно различается

Полные восемь BAR0 слов непосредственно записаны в O1:2, O2:2,
CP:2, C:2–3, RP:2, R:2. `expected` и `actual` там совпадают.
В S1 полного BAR0 snapshot нет. В sampler-файлах есть только `8C040`,
поэтому отсутствие префикса в день sampler **из этих JSONL не доказано**.

| Baseline | 8872C / 2241C / 8841C | 8C1C0 | GPU CAP / CAP2 | GPU CTL2 / Root CTL2 |
| --- | --- | --- | --- | --- |
| [O1:1–3](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1-override/gpu0-stage1-override-1793929151155255357.jsonl#L1-L3), [O2:1–3](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/override-520/gpu0-stage1-override-1793930007183451370.jsonl#L1-L3) | `0 / 0 / C0B46500` | `10032` | `00453C41 / 2` | `0000 / 0043` |
| [CP:1–2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-plan/gpu0-stage1-cap-speed-1793930069627383795.jsonl#L1-L2), [C:1–3](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L1-L3) | `6 / 81 / C0B40500` | `10032` | `00453C41 / 6` | `0000 / 0043` |
| [RP:1–2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-plan/gpu0-stage2-cap-retrain-1793930734987569069.jsonl#L1-L2), [R:1–2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L1-L2) | `6 / 81 / C0B40500` | `20032` | `00453C42 / 6` | `0000 / 0043` |

Во всех этих **полных baseline** одинаковы `8C2C0=068711B3`,
`8C300=040A00C0`, `8C040=20085800`, `88610=0`.
GPU CTL=0140, STA=1041; Root CAP=007A7903, CAP2=E, CTL=0040, STA=3041.
В исходном S1:1 те же config-поля с GPU CAP2=2, но про остальные BAR0
слова оттуда заключать нельзя. Staged baseline содержит driver_bound=false,
но не raw D-state/MSE/BME: прохождение проверок исходного скрипта нельзя
выдавать за отдельно записанные PMCSR/command измерения.

**Разрывы состояния:** S1 заканчивается изменёнными двумя switches,
но O1 начинается с обоими заводскими значениями. O1 заканчивается
префиксом, но O2 снова начинается полностью заводским. Значит, между
этими запусками состояние менялось вне записанной последовательности;
почему именно — power cycle, reinit или отдельная операция — неизвестно.
Имя `override-520` само по себе не доказывает версию NVIDIA 520, задержку
или иной hardware baseline. Оба override-файла имеют одинаковые raw baseline
и последовательность, но это разные записи по времени, а не дубль файла.

Напротив, O2→CP/C и C→RP/R согласованы по видимым BAR0/config значениям.
Это совместимость snapshots, **не** доказательство непрерывного boot или
отсутствия сторонних операций. Boot ID, executable hash/commit и точная
driver/VBIOS версия в staged-файлах отсутствуют; их нельзя получить из
имени файла. Kernel 6.8.0-142-generic явно указан в manifests samplers.

### Записи, capability и STOP

* **S1:** [S1:2–5](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1/gpu0-1793928132780078956.jsonl#L2-L5) содержит `2241C:0→81`, затем
  `8841C:C0B46500→C0B40500`, оба readback точны. STOP в [S1:6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1/gpu0-1793928132780078956.jsonl#L6) спустя
  около 2.04 с после второго `after_mmio`. Важно: конечного raw CAP2
  в этом коротком журнале **нет**. «Не защёлкнул Gen2» — вывод скрипта,
  согласующийся с сообщением оператора, а не сохранённая временная серия CAP2.
* **O1 и O2:** before/readback пары — строки 4–5, 27–28, 50–51;
  offsets строго `8872C → 2241C → 8841C`. Между первой и второй,
  второй и третьей записями около 2 с наблюдения. Все 44 override samples
  в каждом файле разобраны: CAP2=2 до третьего шага, CAP2=6 в строке 52,
  GPU CAP всё время `00453C41`, оба линка Gen1 x4. Ссылки:
  [O1:50–54](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/stage1-override/gpu0-stage1-override-1793929151155255357.jsonl#L50-L54), [O2:50–54](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/override-520/gpu0-stage1-override-1793930007183451370.jsonl#L50-L54). Первый post-write config snapshot следует
  за third before_mmio примерно на 15.05 / 13.16 мс соответственно;
  это не измеренная аппаратная latency latch. `capability_gen2=false`
  обозначает **согласованность CAP и CAP2**, не отсутствие бита Gen2 в CAP2.
  Оба файла прекращаются после третьей записи; recovery_writes=false.
* **C:** [C:4–6](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L4-L6) непосредственно сохраняет mask=30000,
  `8C1C0:10032→20032`, полный точный readback и первый CAP `00453C42`.
  Все 20 samples ([C:6–25](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L6-L25)) дают CAP2=6, GPU STA=1041, Root STA=3041.
  Наблюдение длится около 1.994 с от первого до последнего sample;
  [C:26](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-speed-result/gpu0-stage1-cap-speed-1793930108244936087.jsonl#L26) корректно разделяет capability_gen2=true и link_gen2_x4=false.
  Итог нормальный, не abort. Полного BAR0 post-snapshot после записи нет;
  неизменность остальных слов во всём интервале не измерялась.

Во всём наборе: **9 MMIO intents + 9 совпадающих readback** (2+3+3+1),
только offsets 2241C, 8841C, 8872C, 8C1C0. Нет intent/readback записи
в 8C040, VSEC, дополнительные MMIO, bind/reset или автоматическое
восстановление. Отсутствие записей в журнале не является независимым
доказательством отсутствия любых сторонних writers.

### TLS/retrain, LBMS и AER

В реальном R (не RP dry-run) последовательность однозначна:

1. [R:8–9](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L8-L9): GPU offset A8, `LNKCTL2:0000→0002`, readback=0002.
2. [R:12–13](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L12-L13): Root offset C0, `0043→0042`, readback=0042.
   Сохранён bit6; HASD bit5=0 до и после с обеих сторон. Нельзя говорить,
   что Root исходно имел TLS=1: он имел TLS=3, GPU — TLS=0.
3. [R:14–16](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L14-L16): оба TLS=2 подтверждены port snapshots; затем один
   `before_retrain`, Root offset A0, `LNKCTL:0040→0060` (RL bit5).
4. [R:17–18](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L17-L18): первый Root snapshot имеет CTL=0040 (RL уже не установлен),
   STA=7041; GPU STA=1041. Повторных before_retrain нет.
   [R:173–174](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L173-L174): retrains=1, gen2_x4=false, STOP no retry.

Точность утверждения о выполнении: TLS имеет явные readback. Для RL
отдельного `after_retrain`/bus ACK нет; есть намерение, счётчик и изменение
Root status. Совокупность согласуется с одной выполненной записью из кода,
но журнал не является аппаратной трассой каждого обращения.

Теперь имеется **до/после baseline LBMS именно этого опыта**:
последний Root STA=3041 в R:14, первый 7041 в R:17. XOR=4000 — изменился
только LBMS, не DLLLA (она была 1), не скорость и не ширина. Прежнее
сравнение Root7041 с GPU1041 даёт XOR6000 и отвечает на другой вопрос.
LBMS появился в этом интервале вокруг запроса RL; без concurrent-writer
trace нельзя строго доказать единственную причину, но связь по времени
теперь зарегистрирована. LBMS не доказывает достижение 5 GT/s.

Все **82 Root port snapshots**: четыре 3041 до RL, 78 значений 7041 после;
все **82 GPU snapshots**: 1041. Training=0 во всех сохранённых статусах,
скорость/ширина всегда Gen1 x4. Первая post-RL запись журнала отстоит от
`before_retrain` на ~6.738 мс; это разность logger wall timestamps, не
точная длительность RL или LTSSM. Максимальные интервалы между port log
records: ~68.908 мс Root, ~69.025 мс GPU. Пять секунд наблюдения после
RL не исключают невидимые краткие попытки/возвраты между этими чтениями.

Во всех 164 snapshots raw AER uncor/cor/device_status, Root root_status
и **все** сохранённые kernel AER counters равны нулю, включая последние
[R:171–172](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/cap-retrain-result/gpu0-stage2-cap-retrain-1793930796885371367.jsonl#L171-L172). Это сильнее одного финального AER snapshot, но не доказывает
отсутствие физического training/fallback. Полного kernel journal/Xid stream
в этих JSONL нет. `8C040=20085800` доказан в R:2 **до** TLS/retrain;
финального BAR0 snapshot на отрицательном пути R нет. Нельзя утверждать,
что этот опыт непосредственно измерил SPEED после RL.

### Read-only наблюдения и дополнительные ограничения

[D0:2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-dual-first/gpu0-dual-1794004110508195856.jsonl#L2) фиксирует отказ при bound GPU без samples: это ожидаемый STOP,
не сбой MMIO. В успешных D/P1/P2/F preflight: identity=1B8710DE,
command=2 (MSE=1/BME=0), PMCSR=8 (**D-state по bits1:0=0**, не требуется
полное слово 0), power_state=D0, mapping=PROT_READ; см. [D:2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-dual-first/gpu0-dual-1794004274508572209.jsonl#L2), [F:2](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-fresh-fast-coverage/gpu0-dual-profile-1794005450984539954.jsonl#L2).
Root identity=3C048086, PCIe cap90, LNKSTA offset A2.

Проверены все **67 767 Root samples** в Q/D/P1/P2/F: только 3041;
все **684 GPU samples** D/P1/P2/F: только 20085800, ACTIVE=0,
directive=0, SPEED=2. Summary counts, timing quantiles и max согласуются
с raw sample timestamps. Финальные записи:
[Q:63654](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-root-sampler/root0-lnksta-1794002799062628713.jsonl#L63654), [D:875](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-dual-first/gpu0-dual-1794004274508572209.jsonl#L875), [P1:2866](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-reference/gpu0-dual-profile-1794005123518674721.jsonl#L2866), [P2:5695](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-fresh-fast/gpu0-dual-profile-1794005316984632966.jsonl#L5695), [F:1871](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-fresh-fast-coverage/gpu0-dual-profile-1794005450984539954.jsonl#L1871).
Ошибок чтения, overwrites и Training/DLL skips в этих captures нет;
все GPU samples имеют безопасный postcheck. No-transition — только
наблюдение этих коротких окон, не опыт с CHANGE_SPEED.

Дополнительные нюансы, которые нельзя скрыть словом «ошибок нет»:

* В Q capture_elapsed=250003300 нс: последний read завершился после
  deadline, overrun=3300 нс, оба соответствующих flags=true. Потерь нет,
  но длительность не ровно 250 мс. Read max=31.595 мкс; conservative
  max gap=36.966 мкс — разные величины ([Q:63654](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-root-sampler/root0-lnksta-1794002799062628713.jsonl#L63654)).
* В P1 есть три `profile_span failed=true` в последнем цикле 99:
  [P1:2160](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-reference/gpu0-dual-profile-1794005123518674721.jsonl#L2160), [P1:2174–2175](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-reference/gpu0-dual-profile-1794005123518674721.jsonl#L2174-L2175). В P2 — три в цикле 197:
  [P2:4316–4317](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-fresh-fast/gpu0-dual-profile-1794005316984632966.jsonl#L4316-L4317), [P2:4321](https://github.com/AlexGL402/p104-unlock-v3/blob/ceb943228f6757fe25e114df59d5d3e8b3b1d5ad/evidence/t5600/work/t5600-profile-fresh-fast/gpu0-dual-profile-1794005316984632966.jsonl#L4321). Capture summary при этом duration/error=null;
  spans заканчиваются у границы 50 мс, число MMIO complete cycles 98/196.
  Это согласуется с выходом по deadline через exception в текущем коде,
  а не с аппаратной read error. Сам span reason не записан, поэтому
  интерпретация причины — **вывод**, не отдельное измерение. `failed=true`
  нельзя ни игнорировать, ни объявлять доказательством потери GPU.
* Profile spans выгружены раньше sample rows и вложены по фазам; порядок
  строк всего JSONL не является глобальным порядком событий capture.
  Для сопоставления нужны start/end + clock_origin_ns, с учётом вложенности.
* Root после R имел LBMS=1, а в Q/D/P1/P2/F он снова 0. Между файлами
  около 20 часов и нет непрерывной трассы/boot ID/записи очистки. Почему
  sticky bit исчез — неизвестно; это не повод молча связать их в один run.
* Profiling reference/fresh-fast сохраняет шесть driver guards на complete
  cycle; ускорение не достигнуто уменьшением их частоты. Независимые p50
  из P1/P2: state_checks 359.446/175.136 мкс, driver_binding
  41.558/13.927 мкс на вызов, MMIO bracket 3.383/1.646 мкс,
  GPU sample cycle 504.272/253.538 мкс. В F без profiling cycle=187.023 мкс,
  conservative max с границами=232.822 мкс. Вложенные медианы нельзя
  складывать; эти различия не являются гарантией максимальной задержки.

### Что изменил аудит в решении

Подготовленные capability действительно отличались от заводских; перед
единственным RL оба TLS были согласованно 2, HASD не являлся установленным
ограничителем. LBMS получил доказанный baseline и интервал изменения.
Но финальный SPEED после RL не измерен, промежуточные Training/LTSSM
могли быть пропущены, а восстановление baseline между некоторыми запусками
не документировано. Нового доказанного внутреннего gate в этих логах нет.

**Журнала аварийной `20085800→20045800` среди всех 13 файлов нет.**
Причина прежнего Link Down остаётся неизвестной. Нельзя сравнить с ним
полный pre-write baseline, точную ширину доступа, питание/ownership или
порядок внутренних событий. Кандидаты A/B/C не получили нового основания
для аппаратного запуска. Следующий пробел для offline-анализа — исходный
crash log (если сохранился), происхождение межзапусковых восстановлений,
точная версия исполнявшегося кода и target-matched .47/580 RM сведения.
Ни повтор аварийного SPEED, ни подготовка prefix, ни аппаратные действия
для заполнения этих пробелов этим аудитом не предлагаются.

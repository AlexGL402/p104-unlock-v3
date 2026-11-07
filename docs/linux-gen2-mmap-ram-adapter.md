# RAM-backed mapping adapter: admission check

Scope: `design/p104_mmap_ram.py` / `.c`. Full open–access–close lifecycle,
**ordinary anonymous RAM only**. No path, fd, BAR0 mapping, external-address
constructor, hardware executor, or changes to working hardware tools.

The mapping starts with kernel-provided zero pages. Opening does not initialize
the mapped words in user space; closing only unmaps them. Metadata allocation is
separate. Explicit writes validate bounds/alignment first, then use one volatile
uint32 store with compiler barriers. Readback is a separate method. Python exact
integer validation rejects coercion callbacks, bool, negative and overflowing
values before native access. Native validation repeats mapping/bounds/alignment
checks. The Python wrapper holds the GIL through native calls (PyDLL), preventing
concurrent close/free through this API. Reinitialization of an open object fails.

## Verification

```
python3 -m unittest discover -s tests -p 'test_mmap_ram_adapter.py' -v
python3 -m design.p104_mmap_ram
```

The final shared-library assembly gate accepts only:

```asm
ram_store32: mov DWORD PTR [rdi], esi
             ret
ram_load32:  mov eax, DWORD PTR [rdi]
             ret
```

Alignment is checked independently; x86 `mov` alone does not enforce it.
Unexpected generated code fails closed.

An isolated native harness compiles the same adapter source and watches the
entire RAM mapping, including offset `0x8C040` and adjacent DWORDs. Its mmap
interceptor makes pages read-only **before returning them to mapping_open**.
Every attempted write faults; the Linux/x86-64 SIGSEGV handler permits exactly
one instruction using TF single-step, then SIGTRAP re-protects the pages.
Checks require zero stores during open, reads, invalid operations and close;
one store per explicit write (including same-value writes); unchanged neighbors.
A deliberate zero-then-restore negative control must produce two traps despite
an unchanged final value. The harness binary is separately disassembled. A
signal/observer failure terminates the subprocess and fails the test; it is not
a skip. The observer is test-only and is never linked into the shared library.

Tests also cover first/last DWORD, overrun, unaligned/huge/negative offsets,
closed/null handles, bad values, allocation failure, repeated close and rejected
reinitialization. CI runs these tests on Ubuntu 24.04 / Python 3.12 and retains
the shared-library assembly/compiler command/hashes as an artifact.

## Remaining risks

This removes the RAM-path pre-clear/hidden-store blocker for this build.
It does **not** certify device mapping attributes, PCIe posted-write completion,
TLP count, GPU register latching, or safe SPEED semantics. No real BAR0 adapter
is exposed. Future resource0 integration needs review for zero initialization,
non-WC mapping, safe lifetime and unchanged integer/alignment/bounds checks.
Compiler/architecture changes require a new assembly audit. The page-trap harness
is Linux/x86-64-specific, single-threaded and uses synchronous mprotect in signal
handlers; it is laboratory instrumentation, not production synchronization.
Driver/D0/MSE/BME, exact baseline and Root Link guards remain separate admission
conditions. GPU loss/cold recovery remains possible. SPEED/CHANGE_SPEED hardware
execution remains NO-GO until separate review and explicit authorization.

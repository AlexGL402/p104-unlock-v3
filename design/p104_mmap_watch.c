/* Isolated Linux/x86-64 RAM-only write observer. Never linked into adapter.
 * Read-only pages trap every attempted write; TF reprotects after one instruction.
 */
#define _GNU_SOURCE
#include <signal.h>
#include <ucontext.h>
#include <unistd.h>
#include <stdio.h>
#include <stdint.h>
#include <sys/mman.h>
static void *watched;
static size_t watched_length;
static volatile sig_atomic_t faults, steps, pending;
static void observer(int signal, siginfo_t *info, void *context) {
    ucontext_t *u = context;
    if (signal == SIGSEGV) {
        uintptr_t a = (uintptr_t)info->si_addr;
        if (pending || a < (uintptr_t)watched ||
            a >= (uintptr_t)watched + watched_length ||
            !(u->uc_mcontext.gregs[REG_ERR] & 2)) _exit(90);
        if (mprotect(watched, watched_length, PROT_READ | PROT_WRITE)) _exit(91);
        faults++; pending = 1;
        u->uc_mcontext.gregs[REG_EFL] |= 0x100;
    } else {
        if (!pending || info->si_code != TRAP_TRACE) _exit(92);
        if (mprotect(watched, watched_length, PROT_READ)) _exit(93);
        steps++; pending = 0;
        u->uc_mcontext.gregs[REG_EFL] &= ~0x100;
    }
}
static void *observed_mmap(void *a, size_t n, int p, int f, int fd, off_t o) {
    void *result = mmap(a, n, p, f, fd, o);
    if (result != MAP_FAILED) {
        watched = result; watched_length = n;
        if (mprotect(result, n, PROT_READ)) _exit(94);
    }
    return result;
}
#define mmap observed_mmap
#include "p104_mmap_ram.c"
#undef mmap
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"check failed: %s\n", #x); return 1; } } while (0)
/* Negative control: observer must detect zero then restore, even final equality. */
static __attribute__((noinline)) void bad_store(volatile uint32_t *p, uint32_t v) {
    *p = 0; *p = v;
}
int main(void) {
    struct sigaction action = {0};
    action.sa_sigaction = observer; action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    CHECK(!sigaction(SIGSEGV, &action, NULL));
    CHECK(!sigaction(SIGTRAP, &action, NULL));
    Mapping *m = mapping_open(0x90000);
    CHECK(m && faults == 0 && steps == 0);
    const uint64_t offset = 0x8c040;
    CHECK(!mapping_write(m, offset-4, 0xaabbccdd));
    CHECK(!mapping_write(m, offset+4, 0x11223344));
    CHECK(faults == 2 && steps == 2);
    faults = steps = 0;
    uint32_t value;
    CHECK(!mapping_read(m, offset, &value) && value == 0 && faults == 0);
    CHECK(!mapping_write(m, offset, 0)); /* same-value store must be counted */
    CHECK(faults == 1 && steps == 1);
    for (unsigned i=0; i<4; i++) {
        uint32_t values[] = {0x20045800, 0xffffffff, 0x20085801, 0};
        faults = steps = 0;
        CHECK(!mapping_write(m, offset, values[i]));
        CHECK(faults == 1 && steps == 1);
        CHECK(!mapping_read(m, offset, &value) && value == values[i]);
        CHECK(!mapping_read(m, offset-4, &value) && value == 0xaabbccdd);
        CHECK(!mapping_read(m, offset+4, &value) && value == 0x11223344);
        CHECK(faults == 1 && steps == 1);
    }
    faults = steps = 0;
    CHECK(mapping_write(m, offset+1, 3) == -1);
    CHECK(mapping_write(m, UINT64_MAX, 3) == -1);
    CHECK(mapping_write(m, m->length-3, 3) == -1);
    CHECK(mapping_read(m, 1, &value) == -1);
    CHECK(mapping_read(m, 0, NULL) == -1);
    CHECK(faults == 0 && steps == 0);
    bad_store((volatile uint32_t *)((char *)m->base + offset), 0);
    CHECK(faults == 2 && steps == 2);
    faults = steps = 0;
    CHECK(!mapping_close(m));
    CHECK(faults == 0 && steps == 0);
    CHECK(mapping_close(m) == -1);
    CHECK(mapping_read(m, 0, &value) == -1);
    CHECK(mapping_write(m, 0, 1) == -1);
    mapping_destroy(m);
    CHECK(!mapping_open(3));
    CHECK(!mapping_open(16u*1024u*1024u+1));
    puts("PASS: open/close=0 stores; write=1; readback=0; neighbors intact; negative control=2");
    return 0;
}

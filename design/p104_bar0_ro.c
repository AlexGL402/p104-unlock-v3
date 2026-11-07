/* Read-only BAR0 mapping backend. No device store primitive exists. */
#define _GNU_SOURCE
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#if !defined(__x86_64__) || !defined(__GNUC__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Audited backend requires GNU-compatible little-endian x86-64"
#endif
_Static_assert(sizeof(uint32_t) == 4, "DWORD required");
typedef struct { void *base; size_t length; } ReadMapping;
static __attribute__((noinline)) uint32_t ro_load32(const volatile uint32_t *p) {
    __asm__ __volatile__("" ::: "memory");
    uint32_t v = *p;
    __asm__ __volatile__("" ::: "memory");
    return v;
}
static int allowed(uint64_t o) {
    switch (o) {
    case 0x2241c: case 0x8841c: case 0x8872c: case 0x8c040:
    case 0x8c1c0: case 0x8c2c0: case 0x8c300: case 0x88610: return 1;
    default: return 0;
    }
}
ReadMapping *ro_map(int fd) {
    int flags = fcntl(fd, F_GETFL);
    long page = sysconf(_SC_PAGESIZE);
    if (flags < 0 || (flags & O_ACCMODE) != O_RDONLY || page <= 0) return NULL;
    size_t length = ((0x8c304u + (size_t)page - 1) / (size_t)page) * (size_t)page;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
        (uint64_t)st.st_size < length) return NULL;
    ReadMapping *m = calloc(1, sizeof(*m));
    if (!m) return NULL;
    m->base = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    if (m->base == MAP_FAILED) { free(m); return NULL; }
    m->length = length;
    /* No dereference, initialization or prefault of mapped memory. */
    return m;
}
uint64_t ro_read(ReadMapping *m, uint64_t offset) {
    if (!m || !m->base || !allowed(offset) || offset > m->length - 4) return UINT64_MAX;
    uintptr_t p = (uintptr_t)m->base + (size_t)offset;
    if (p & 3u) return UINT64_MAX;
    return ro_load32((const volatile uint32_t *)p);
}
int ro_close(ReadMapping *m) {
    if (!m) return -1;
    if (munmap(m->base, m->length)) return -1;
    m->base = NULL; free(m);
    return 0;
}

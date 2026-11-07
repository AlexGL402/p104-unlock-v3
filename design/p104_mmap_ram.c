/* RAM-only complete mapping lifecycle. No fd/path/address input exists.
 * Compiler barriers order compilation, not PCIe posted writes.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <sys/mman.h>
#if !defined(__x86_64__) || !defined(__GNUC__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Only audited GNU-compatible little-endian x86-64 supported"
#endif
_Static_assert(sizeof(uint32_t) == 4, "dword required");
typedef struct { void *base; size_t length; } Mapping;
#define BARRIER() __asm__ __volatile__("" ::: "memory")
static __attribute__((noinline)) void ram_store32(volatile uint32_t *p, uint32_t v) {
    BARRIER(); *p = v; BARRIER();
}
static __attribute__((noinline)) uint32_t ram_load32(const volatile uint32_t *p) {
    BARRIER(); uint32_t v = *p; BARRIER(); return v;
}
Mapping *mapping_open(size_t length) {
    if (length < 4 || length > 16u * 1024u * 1024u) return NULL;
    Mapping *m = calloc(1, sizeof(*m));
    if (!m) return NULL;
    m->base = mmap(NULL, length, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (m->base == MAP_FAILED) { free(m); return NULL; }
    m->length = length;
    /* Deliberately no access to mapped destination, including initialization. */
    return m;
}
int mapping_close(Mapping *m) {
    if (!m || !m->base) return -1;
    if (munmap(m->base, m->length)) return -1;
    m->base = NULL; m->length = 0;
    return 0;
}
void mapping_destroy(Mapping *m) {
    if (!m) return;
    if (m->base) (void)mapping_close(m);
    free(m);
}
static volatile uint32_t *checked(Mapping *m, uint64_t offset) {
    if (!m || !m->base || m->length < 4 || offset > m->length - 4) return NULL;
    uintptr_t p = (uintptr_t)m->base + (size_t)offset;
    if (p & 3u) return NULL;
    return (volatile uint32_t *)p;
}
int mapping_write(Mapping *m, uint64_t offset, uint32_t value) {
    volatile uint32_t *p = checked(m, offset);
    if (!p) return -1;
    ram_store32(p, value);
    return 0;
}
int mapping_read(Mapping *m, uint64_t offset, uint32_t *out) {
    volatile uint32_t *p = checked(m, offset);
    if (!p || !out) return -1;
    *out = ram_load32(p);
    return 0;
}

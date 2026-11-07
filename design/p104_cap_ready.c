/* Fixed four-step preparation only. No SPEED/config/reset/retrain API. */
#define ro_load32 ram_load32
#include "p104_bar0_ro.c"
#undef ro_load32
typedef struct { ReadMapping read; unsigned next, blocked; } PrepMapping;
static const uint32_t offsets[] = {0x8872c, 0x2241c, 0x8841c, 0x8c1c0};
static const uint32_t before[] = {0, 0, 0xc0b46500, 0x00010032};
static const uint32_t after[] = {6, 0x81, 0xc0b40500, 0x00020032};
static __attribute__((noinline)) void ram_store32(volatile uint32_t *p, uint32_t v) {
    __asm__ __volatile__("" ::: "memory");
    *p = v;
    __asm__ __volatile__("" ::: "memory");
}
PrepMapping *prep_map(int fd) {
    int flags = fcntl(fd, F_GETFL);
    long page = sysconf(_SC_PAGESIZE);
    if (flags < 0 || (flags & O_ACCMODE) != O_RDWR || page <= 0) return NULL;
    size_t length = ((0x8c304u + (size_t)page - 1) / (size_t)page) * (size_t)page;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
        (uint64_t)st.st_size < length) return NULL;
    PrepMapping *m = malloc(sizeof(*m));
    if (!m) return NULL;
    m->read.base = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m->read.base == MAP_FAILED) { free(m); return NULL; }
    m->read.length = length;
    m->next = m->blocked = 0; /* RAM owner metadata, no device initialization. */
    return m;
}
int prep_step(PrepMapping *m, unsigned index) {
    if (!m || !m->read.base || m->blocked) return -1;
    if (index != m->next || index >= 4) { m->blocked = 1; return -1; }
    m->blocked = 1; /* Consume before access; a failure cannot be retried. */
    uintptr_t address = (uintptr_t)m->read.base + offsets[index];
    if ((address & 3u) || offsets[index] > m->read.length - 4) return -1;
    if (ram_load32((const volatile uint32_t *)address) != before[index]) return -2;
    ram_store32((volatile uint32_t *)address, after[index]);
    m->next++; m->blocked = 0;
    return 0;
}
uint64_t prep_read(PrepMapping *m, uint64_t offset) {
    if (!m) return UINT64_MAX;
    return ro_read(&m->read, offset);
}
int prep_close(PrepMapping *m) {
    if (!m) return -1;
    if (munmap(m->read.base, m->read.length)) return -1;
    m->read.base = NULL; free(m);
    return 0;
}

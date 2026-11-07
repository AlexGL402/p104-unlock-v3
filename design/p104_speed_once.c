/* One fixed SPEED-only operation. No preparation/config/reset/retrain API.
 * Reuse the unchanged read-only mapping/load implementation for preflight.
 */
#include "p104_bar0_ro.c"
typedef struct { ReadMapping read; unsigned spent; } SpeedMapping;
/* Same audited scalar store as p104_mmap_ram.c; no destination initialization. */
static __attribute__((noinline)) void ram_store32(volatile uint32_t *p, uint32_t v) {
    __asm__ __volatile__("" ::: "memory");
    *p = v;
    __asm__ __volatile__("" ::: "memory");
}
SpeedMapping *speed_map(int fd) {
    int flags = fcntl(fd, F_GETFL);
    long page = sysconf(_SC_PAGESIZE);
    if (flags < 0 || (flags & O_ACCMODE) != O_RDWR || page <= 0) return NULL;
    size_t length = ((0x8c304u + (size_t)page - 1) / (size_t)page) * (size_t)page;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
        (uint64_t)st.st_size < length) return NULL;
    SpeedMapping *m = malloc(sizeof(*m));
    if (!m) return NULL;
    m->read.base = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m->read.base == MAP_FAILED) { free(m); return NULL; }
    m->read.length = length;
    m->spent = 0; /* Owner metadata, never a device word. */
    return m;
}
int speed_once(SpeedMapping *m) {
    if (!m || !m->read.base || m->spent) return -1;
    m->spent = 1; /* Consume BEFORE the load/store; every outcome is terminal. */
    uintptr_t address = (uintptr_t)m->read.base + 0x8c040u;
    if (0x8c044u > m->read.length || (address & 3u)) return -1;
    if (ro_load32((const volatile uint32_t *)address) != 0x20085800u) return -2;
    ram_store32((volatile uint32_t *)address, 0x20045800u);
    m->spent = 2;
    return 0;
}
uint64_t speed_readback(SpeedMapping *m) {
    if (!m || m->spent != 2) return UINT64_MAX;
    return ro_read(&m->read, 0x8c040u); /* Separate explicit load. */
}
int speed_close(SpeedMapping *m) {
    if (!m) return -1;
    if (munmap(m->read.base, m->read.length)) return -1;
    m->read.base = NULL; free(m);
    return 0;
}

/* ApplyLiveGroup only. Fixed recipe; no config, reset, retrain or directive API. */
#define ro_load32 ram_load32
#include "p104_bar0_ro.c"
#undef ro_load32
struct Recipe { uint32_t offset, mask, value; };
static const struct Recipe recipe[] = {
#include P104_LIVE_RECIPE_HEADER
};
_Static_assert(sizeof(recipe)/sizeof(recipe[0]) == 8, "eight Windows entries required");
typedef struct { ReadMapping read; unsigned next, blocked; } LiveMapping;
static __attribute__((noinline)) void ram_store32(volatile uint32_t *p, uint32_t v) {
    __asm__ __volatile__("" ::: "memory");
    *p = v;
    __asm__ __volatile__("" ::: "memory");
}
LiveMapping *live_map(int fd) {
    int flags = fcntl(fd, F_GETFL);
    long page = sysconf(_SC_PAGESIZE);
    if (flags < 0 || (flags & O_ACCMODE) != O_RDWR || page <= 0) return NULL;
    size_t length = ((0x8c304u + (size_t)page - 1)/(size_t)page)*(size_t)page;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
        (uint64_t)st.st_size < length) return NULL;
    LiveMapping *m = malloc(sizeof(*m));
    if (!m) return NULL;
    m->read.base = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m->read.base == MAP_FAILED) { free(m); return NULL; }
    m->read.length = length;
    m->next = m->blocked = 0; /* RAM metadata only; no mapping initialization. */
    return m;
}
int live_step(LiveMapping *m, unsigned index, uint32_t expected_old) {
    if (!m || !m->read.base || m->blocked) return -1;
    m->blocked = 1;
    if (index != m->next || index >= 8) return -1;
    struct Recipe r = recipe[index];
    uintptr_t address = (uintptr_t)m->read.base+r.offset;
    if ((address & 3u) || r.offset > m->read.length-4) return -1;
    uint32_t old = ram_load32((const volatile uint32_t *)address);
    if (old == UINT32_MAX || (old >> 16) == 0xbadf) return -3;
    if (old != expected_old) return -2; /* Stale overwrite protection. */
    if (r.offset == 0x8c040 && (old & 0xa000001fu) != 0x20000000u) return -4;
    uint32_t nv = (old & ~r.mask) | (r.value & r.mask);
    if (nv != old) ram_store32((volatile uint32_t *)address, nv);
    m->next++; m->blocked = 0;
    return nv == old ? 1 : 0; /* 1 means skip: no store, sleep or readback. */
}
uint64_t live_read(LiveMapping *m, uint64_t offset) {
    if (!m) return UINT64_MAX;
    return ro_read(&m->read, offset);
}
int live_close(LiveMapping *m) {
    if (!m) return -1;
    if (munmap(m->read.base, m->read.length)) return -1;
    m->read.base = NULL; free(m);
    return 0;
}

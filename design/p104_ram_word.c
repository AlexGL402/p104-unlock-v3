/* RAM-only audited access prototype. No mapping, fd, external buffer or address API.
 * This is NOT a BAR0 backend. The only accessible word is owned by RamWord.
 * Compiler barriers do not flush PCIe posted writes or make WC mappings safe.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stddef.h>
#include <stdint.h>
#include <limits.h>

#if !defined(__x86_64__) || !defined(__GNUC__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Audited prototype requires GNU-compatible x86-64 little-endian compilation"
#endif
_Static_assert(CHAR_BIT == 8 && sizeof(uint32_t) == 4, "32-bit word required");

typedef struct {
    PyObject_HEAD
    _Alignas(4) volatile uint32_t value;
} RamWord;
_Static_assert(offsetof(RamWord, value) % 4 == 0, "word must be aligned");
_Static_assert(_Alignof(RamWord) >= 4, "allocation alignment required");

#define COMPILER_BARRIER() __asm__ __volatile__("" ::: "memory")

/* Keep these internal functions separate for disassembly auditing. */
static __attribute__((noinline)) uint32_t
ram_load32(const volatile uint32_t *address)
{
    COMPILER_BARRIER();
    uint32_t value = *address;
    COMPILER_BARRIER();
    return value;
}

static __attribute__((noinline)) void
ram_store32(volatile uint32_t *address, uint32_t value)
{
    COMPILER_BARRIER();
    *address = value;
    COMPILER_BARRIER();
}

/* Complete conversion before touching even the private RAM destination. */
static int
parse_word(PyObject *argument, uint32_t *out)
{
    if (!PyLong_CheckExact(argument)) {
        PyErr_SetString(PyExc_TypeError, "an exact int is required; no address/buffer/index callback");
        return -1;
    }
    unsigned long long value = PyLong_AsUnsignedLongLong(argument);
    if (PyErr_Occurred()) return -1;
    if (value > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "word outside uint32 range");
        return -1;
    }
    *out = (uint32_t)value;
    return 0;
}

static int
require_alignment(RamWord *self)
{
    if (((uintptr_t)&self->value & 3u) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "unaligned owned word; no access performed");
        return -1;
    }
    return 0;
}

static int
word_init(RamWord *self, PyObject *args, PyObject *keywords)
{
    static char *names[] = {"initial", NULL};
    PyObject *argument = NULL;
    uint32_t initial = 0;
    if (!PyArg_ParseTupleAndKeywords(args, keywords, "|O", names, &argument)) return -1;
    if (argument && parse_word(argument, &initial) < 0) return -1;
    if (require_alignment(self) < 0) return -1;
    ram_store32(&self->value, initial);
    return 0;
}

static PyObject *
word_read(RamWord *self, PyObject *unused)
{
    (void)unused;
    if (require_alignment(self) < 0) return NULL;
    return PyLong_FromUnsignedLong(ram_load32(&self->value));
}

static PyObject *
word_write(RamWord *self, PyObject *argument)
{
    uint32_t value;
    if (parse_word(argument, &value) < 0) return NULL;
    if (require_alignment(self) < 0) return NULL;
    ram_store32(&self->value, value);
    Py_RETURN_NONE;
}

static PyMethodDef word_methods[] = {
    {"read", (PyCFunction)word_read, METH_NOARGS, "One volatile load from owned RAM."},
    {"write", (PyCFunction)word_write, METH_O, "One volatile store to owned RAM; no implicit readback."},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot word_slots[] = {
    {Py_tp_new, PyType_GenericNew},
    {Py_tp_init, word_init},
    {Py_tp_methods, word_methods},
    {0, NULL}
};
static PyType_Spec word_spec = {
    .name = "_p104_ram_word.RamWord",
    .basicsize = sizeof(RamWord),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = word_slots
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_p104_ram_word",
    .m_doc = "RAM-only access prototype. Hardware integration is prohibited.",
    .m_size = -1
};

PyMODINIT_FUNC
PyInit__p104_ram_word(void)
{
    PyObject *m = PyModule_Create(&module);
    if (!m) return NULL;
    PyObject *type = PyType_FromSpec(&word_spec);
    if (!type || PyModule_AddObject(m, "RamWord", type) < 0) {
        Py_XDECREF(type);
        Py_DECREF(m);
        return NULL;
    }
    return m;
}

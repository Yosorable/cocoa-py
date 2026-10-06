/* Compact sprite storage. Included after the collector's math helpers. */

struct BatchSpriteRecord {
    double matrix[6] = {1, 0, 0, 1, 0, 0};
    double size[2] = {0, 0}, anchor[2] = {.5, .5};
    double uv[4] = {0, 0, 1, 1}, tint[4] = {1, 1, 1, 1};
    double opacity = 1, z = 0;
    bool visible = true, additive = false, flip_x = false, flip_y = false;
    Py_ssize_t parent = -1;
    PyObject *texture = nullptr, *clip = nullptr;
};

struct SpriteBatchData {
    std::vector<BatchSpriteRecord> items;
    std::vector<size_t> draw_order;
    bool order_dirty = true;
    unsigned long long revision = 0;
    size_t clip_count = 0;
    bool collecting = false;
};

struct BatchDataObject {
    PyObject_HEAD
    SpriteBatchData *data;
};

static PyTypeObject BatchDataType = { PyVarObject_HEAD_INIT(NULL, 0) };

static int batch_storage_traverse(BatchDataObject *self, visitproc visit, void *arg) {
    for (const auto &item : self->data->items) {
        Py_VISIT(item.texture);
        Py_VISIT(item.clip);
    }
    return 0;
}

static int batch_storage_clear(BatchDataObject *self) {
    std::vector<BatchSpriteRecord> detached;
    detached.swap(self->data->items);
    self->data->draw_order.clear();
    self->data->order_dirty = true;
    self->data->clip_count = 0;
    ++self->data->revision;
    for (const auto &item : detached) {
        Py_XDECREF(item.texture);
        Py_XDECREF(item.clip);
    }
    return 0;
}

static void batch_storage_dealloc(BatchDataObject *self) {
    PyObject_GC_UnTrack(self);
    batch_storage_clear(self);
    delete self->data;
    PyObject_GC_Del(self);
}

static int batch_init_type() {
    BatchDataType.tp_name = "_cocoa._scene_accel._BatchData";
    BatchDataType.tp_basicsize = sizeof(BatchDataObject);
    BatchDataType.tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC;
    BatchDataType.tp_traverse = (traverseproc)batch_storage_traverse;
    BatchDataType.tp_clear = (inquiry)batch_storage_clear;
    BatchDataType.tp_dealloc = (destructor)batch_storage_dealloc;
    return PyType_Ready(&BatchDataType);
}

static int batch_build_order(SpriteBatchData *data) {
    if (!data->order_dirty) return 0;
    const size_t count = data->items.size();
    try {
        std::vector<size_t> first(count + 1, count), next(count, count), pending, order;
        order.reserve(count);
        for (size_t i = count; i-- > 0;) {
            size_t parent = data->items[i].parent + 1;
            next[i] = first[parent];
            first[parent] = i;
        }
        if (first[0] != count) pending.push_back(first[0]);
        while (!pending.empty()) {
            size_t i = pending.back();
            pending.pop_back();
            order.push_back(i);
            if (next[i] != count) pending.push_back(next[i]);
            if (first[i + 1] != count) pending.push_back(first[i + 1]);
        }
        data->draw_order.swap(order);
        data->order_dirty = false;
    } catch (const std::bad_alloc &) { PyErr_NoMemory(); return -1; }
    return 0;
}

static bool batch_writable(SpriteBatchData *data) {
    if (!data->collecting) return true;
    PyErr_SetString(PyExc_RuntimeError, "cannot change batch sprites during collection");
    return false;
}

static SpriteBatchData *batch_data(PyObject *storage) {
    if (!Py_IS_TYPE(storage, &BatchDataType)) {
        PyErr_SetString(PyExc_TypeError, "expected SpriteBatch storage");
        return nullptr;
    }
    return ((BatchDataObject *)storage)->data;
}

static PyObject *batch_new(PyObject *, PyObject *) {
    auto *data = new (std::nothrow) SpriteBatchData;
    if (!data) return PyErr_NoMemory();
    auto *storage = PyObject_GC_New(BatchDataObject, &BatchDataType);
    if (!storage) { delete data; return nullptr; }
    storage->data = data;
    PyObject_GC_Track(storage);
    return (PyObject *)storage;
}

static bool batch_start_collecting(SpriteBatchData *data) {
    if (data->collecting) {
        PyErr_SetString(PyExc_RuntimeError, "cannot collect the same batch recursively during collection");
        return false;
    }
    data->collecting = true;
    return true;
}

static int batch_begin_collection(PyObject *node, PyObject *cache, CollectState *st) {
    PyObject *storage = PyObject_GetAttrString(node, "_batch_data");
    auto *data = storage ? batch_data(storage) : nullptr;
    if (!data || !batch_start_collecting(data)) { Py_XDECREF(storage); return -1; }
    PyObject *entry = PyTuple_Pack(3, node, cache, storage);
    int status = entry ? PyList_Append(st->batch_collections, entry) : -1;
    if (status < 0) data->collecting = false;
    Py_XDECREF(entry);
    Py_DECREF(storage);
    return status;
}

static void batch_end_collections(PyObject *collections) {
    if (!collections) return;
    // Unlock all storage before releasing any owners that can run finalizers.
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(collections); ++i) {
        PyObject *storage = PyTuple_GET_ITEM(PyList_GET_ITEM(collections, i), 2);
        ((BatchDataObject *)storage)->data->collecting = false;
    }
}

static PyObject *batch_begin_collect(PyObject *, PyObject *storage) {
    auto *data = batch_data(storage);
    if (!data || !batch_start_collecting(data)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *batch_end_collect(PyObject *, PyObject *storage) {
    auto *data = batch_data(storage);
    if (!data) return nullptr;
    data->collecting = false;
    Py_RETURN_NONE;
}

static int batch_numbers(PyObject *value, double *out, Py_ssize_t n) {
    // Numeric conversion may call Python and mutate the original sequence.
    PyObject *seq = PySequence_Tuple(value);
    if (!seq) return -1;
    if (PySequence_Fast_GET_SIZE(seq) != n) {
        Py_DECREF(seq);
        PyErr_Format(PyExc_ValueError, "expected %zd sprite values", n);
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; ++i) {
        out[i] = PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i));
        if (PyErr_Occurred() || !isfinite(out[i])) {
            Py_DECREF(seq);
            if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "sprite values must be finite");
            return -1;
        }
    }
    Py_DECREF(seq);
    return 0;
}

static bool batch_index(SpriteBatchData *data, Py_ssize_t index) {
    if (index >= 0 && (size_t)index < data->items.size()) return true;
    PyErr_SetString(PyExc_IndexError, "sprite index out of range");
    return false;
}

/* Field numbers are private to _sprite_batch.py. Parsing completes before any
   record is touched, so failed conversions cannot partially update a sprite. */
static PyObject *batch_set(PyObject *, PyObject *args) {
    PyObject *capsule, *value;
    Py_ssize_t index;
    int field;
    if (!PyArg_ParseTuple(args, "OniO", &capsule, &index, &field, &value)) return nullptr;
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data) || !batch_index(data, index)) return nullptr;
    double parsed[6] = {};
    int count = 0, flag = 0;
    Py_ssize_t parent = -1;
    if (field <= 4 && field >= 0) {
        count = field == 0 ? 6 : (field <= 2 ? 2 : 4);
        if (batch_numbers(value, parsed, count) < 0) return nullptr;
    } else if (field == 5 || field == 6) {
        parsed[0] = PyFloat_AsDouble(value);
        if (PyErr_Occurred()) return nullptr;
        if (!isfinite(parsed[0])) {
            PyErr_SetString(PyExc_ValueError, "sprite values must be finite");
            return nullptr;
        }
    } else if (field >= 7 && field <= 10) {
        flag = PyObject_IsTrue(value);
        if (flag < 0) return nullptr;
    } else if (field == 13) {
        parent = PyLong_AsSsize_t(value);
        if (PyErr_Occurred()) return nullptr;
        if (parent < -1 || parent >= index) {
            PyErr_SetString(PyExc_ValueError, "sprite parent must precede its child");
            return nullptr;
        }
    } else if (field != 11 && field != 12) {
        PyErr_SetString(PyExc_ValueError, "invalid sprite field");
        return nullptr;
    }
    /* Conversion may run Python callbacks that append more records. */
    if (!batch_index(data, index)) return nullptr;
    auto &item = data->items[index];
    if (count) {
        double *dst = field == 0 ? item.matrix : field == 1 ? item.size :
                      field == 2 ? item.anchor : field == 3 ? item.uv : item.tint;
        if (memcmp(dst, parsed, count * sizeof(double))) {
            memcpy(dst, parsed, count * sizeof(double));
            ++data->revision;
        }
    } else if (field == 5 || field == 6) {
        double &dst = field == 5 ? item.opacity : item.z;
        if (dst != parsed[0]) { dst = parsed[0]; ++data->revision; }
    } else if (field >= 7 && field <= 10) {
        bool &dst = field == 7 ? item.visible : field == 8 ? item.additive :
                    field == 9 ? item.flip_x : item.flip_y;
        if (dst != (bool)flag) { dst = flag; ++data->revision; }
    } else if (field == 13) {
        if (item.parent != parent) {
            item.parent = parent;
            data->order_dirty = true;
            ++data->revision;
        }
    } else {
        PyObject *&dst = field == 11 ? item.texture : item.clip;
        if (dst != value) {
            if (field == 12) {
                if (dst && dst != Py_None) --data->clip_count;
                if (value != Py_None) ++data->clip_count;
            }
            PyObject *old = dst;
            dst = Py_NewRef(value);
            ++data->revision;
            Py_XDECREF(old);
        }
    }
    Py_RETURN_NONE;
}

static PyObject *batch_get(PyObject *, PyObject *args) {
    PyObject *capsule;
    Py_ssize_t index;
    int field;
    if (!PyArg_ParseTuple(args, "Oni", &capsule, &index, &field)) return nullptr;
    auto *data = batch_data(capsule);
    if (!data || !batch_index(data, index)) return nullptr;
    const auto &item = data->items[index];
    if (field >= 0 && field <= 4) {
        int count = field == 0 ? 6 : (field <= 2 ? 2 : 4);
        const double *src = field == 0 ? item.matrix : field == 1 ? item.size :
                            field == 2 ? item.anchor : field == 3 ? item.uv : item.tint;
        PyObject *result = PyTuple_New(count);
        if (!result) return nullptr;
        for (int i = 0; i < count; ++i) {
            PyObject *number = PyFloat_FromDouble(src[i]);
            if (!number) { Py_DECREF(result); return nullptr; }
            PyTuple_SET_ITEM(result, i, number);
        }
        return result;
    }
    if (field == 5 || field == 6) return PyFloat_FromDouble(field == 5 ? item.opacity : item.z);
    if (field >= 7 && field <= 10) return PyBool_FromLong(field == 7 ? item.visible :
        field == 8 ? item.additive : field == 9 ? item.flip_x : item.flip_y);
    if (field == 11 || field == 12) {
        PyObject *value = field == 11 ? item.texture : item.clip;
        return Py_NewRef(value ? value : Py_None);
    }
    if (field == 13) return PyLong_FromSsize_t(item.parent);
    PyErr_SetString(PyExc_ValueError, "invalid sprite field");
    return nullptr;
}

static PyObject *batch_append(PyObject *, PyObject *capsule) {
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data)) return nullptr;
    PyObject *index = PyLong_FromSize_t(data->items.size());
    if (!index) return nullptr;
    try { data->items.emplace_back(); }
    catch (const std::bad_alloc &) { Py_DECREF(index); return PyErr_NoMemory(); }
    ++data->revision;
    data->order_dirty = true;
    return index;
}

static PyObject *batch_copy(PyObject *, PyObject *args) {
    PyObject *capsule, *source;
    Py_ssize_t parent;
    if (!PyArg_ParseTuple(args, "OOn", &capsule, &source, &parent)) return nullptr;
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data)) return nullptr;
    auto *pending = batch_data(source);
    if (!pending) return nullptr;
    if (pending->items.size() != 1 || parent < -1 ||
        (parent >= 0 && (size_t)parent >= data->items.size())) {
        PyErr_SetString(PyExc_ValueError, "invalid sprite append");
        return nullptr;
    }
    PyObject *index = PyLong_FromSize_t(data->items.size());
    if (!index) return nullptr;
    auto item = pending->items[0];
    item.parent = parent;
    try { data->items.push_back(item); }
    catch (const std::bad_alloc &) { Py_DECREF(index); return PyErr_NoMemory(); }
    Py_XINCREF(item.texture);
    Py_XINCREF(item.clip);
    if (item.clip && item.clip != Py_None) ++data->clip_count;
    ++data->revision;
    data->order_dirty = true;
    return index;
}

static PyObject *batch_order(PyObject *, PyObject *capsule) {
    auto *data = batch_data(capsule);
    if (!data || batch_build_order(data) < 0) return nullptr;
    PyObject *result = PyTuple_New(data->draw_order.size());
    if (!result) return nullptr;
    for (size_t i = 0; i < data->draw_order.size(); ++i) {
        PyObject *index = PyLong_FromSize_t(data->draw_order[i]);
        if (!index) { Py_DECREF(result); return nullptr; }
        PyTuple_SET_ITEM(result, i, index);
    }
    return result;
}

static PyObject *batch_revision(PyObject *, PyObject *capsule) {
    auto *data = batch_data(capsule);
    return data ? PyLong_FromUnsignedLongLong(data->revision) : nullptr;
}

static PyObject *batch_check_writable(PyObject *, PyObject *capsule) {
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *batch_transforms(PyObject *, PyObject *args) {
    PyObject *capsule, *indices, *matrices, *transform = Py_None;
    int components = 0;
    if (!PyArg_ParseTuple(args, "OOO|Op", &capsule, &indices, &matrices, &transform, &components)) return nullptr;
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data)) return nullptr;
    double prepend[6];
    if (transform != Py_None && batch_numbers(transform, prepend, 6) < 0) return nullptr;
    PyObject *ids = PySequence_Tuple(indices);
    if (!ids) return nullptr;
    PyObject *poses = PySequence_Tuple(matrices);
    if (!poses) { Py_DECREF(ids); return nullptr; }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(ids);
    std::vector<std::array<double, 6>> parsed;
    std::vector<Py_ssize_t> positions;
    bool ok = PySequence_Fast_GET_SIZE(poses) == count;
    if (!ok) PyErr_SetString(PyExc_ValueError, "indices and transforms must have the same length");
    try { if (ok) { parsed.resize(count); positions.resize(count); } }
    catch (const std::bad_alloc &) { PyErr_NoMemory(); ok = false; }
    for (Py_ssize_t i = 0; ok && i < count; ++i) {
        positions[i] = PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(ids, i));
        ok = !PyErr_Occurred() && batch_index(data, positions[i]) &&
             batch_numbers(PySequence_Fast_GET_ITEM(poses, i), parsed[i].data(), components ? 5 : 6) == 0;
        if (ok && components) {
            auto values = parsed[i];
            c_matrix(values[0], values[1], values[2], values[3], values[4], parsed[i].data());
            for (double value : parsed[i]) if (!isfinite(value)) {
                PyErr_SetString(PyExc_ValueError, "sprite pose must produce a finite transform");
                ok = false;
                break;
            }
        }
        if (ok && transform != Py_None) {
            // Match separately rounded multiplication and addition, including
            // callers that prepare their transforms with Python floats.
            #pragma clang fp contract(off)
            auto b = parsed[i];
            auto &out = parsed[i];
            out[0] = prepend[0]*b[0] + prepend[2]*b[1];
            out[1] = prepend[1]*b[0] + prepend[3]*b[1];
            out[2] = prepend[0]*b[2] + prepend[2]*b[3];
            out[3] = prepend[1]*b[2] + prepend[3]*b[3];
            out[4] = prepend[0]*b[4] + prepend[2]*b[5] + prepend[4];
            out[5] = prepend[1]*b[4] + prepend[3]*b[5] + prepend[5];
            for (double value : out) if (!isfinite(value)) {
                PyErr_SetString(PyExc_ValueError, "composed sprite transform must be finite");
                ok = false;
                break;
            }
        }
    }
    if (ok) {
        bool changed = false;
        for (Py_ssize_t i = 0; i < count; ++i) {
            auto &dst = data->items[positions[i]];
            if (memcmp(dst.matrix, parsed[i].data(), sizeof(dst.matrix))) {
                memcpy(dst.matrix, parsed[i].data(), sizeof(dst.matrix));
                changed = true;
            }
        }
        if (changed) ++data->revision;
    }
    Py_DECREF(ids);
    Py_DECREF(poses);
    if (!ok) return nullptr;
    Py_RETURN_NONE;
}

struct BatchSpriteUpdate {
    double matrix[6], size[2], tint[4], opacity, z;
    bool visible;
};

static PyObject *batch_update(PyObject *, PyObject *args) {
    PyObject *capsule, *indices, *matrices, *poses, *sizes, *tints, *opacities, *depths, *visible, *transform;
    if (!PyArg_ParseTuple(args, "OOOOOOOOOO", &capsule, &indices, &matrices, &poses,
                          &sizes, &tints, &opacities, &depths, &visible, &transform)) return nullptr;
    auto *data = batch_data(capsule);
    if (!data || !batch_writable(data)) return nullptr;
    if (matrices != Py_None && poses != Py_None) {
        PyErr_SetString(PyExc_ValueError, "supply transforms or poses, not both");
        return nullptr;
    }
    bool components = poses != Py_None;
    bool geometry = matrices != Py_None || components;
    if (transform != Py_None && !geometry) {
        PyErr_SetString(PyExc_ValueError, "a shared transform requires transforms or poses");
        return nullptr;
    }
    PyObject *ids = PySequence_Tuple(indices);
    if (!ids) return nullptr;
    Py_ssize_t count = PyTuple_GET_SIZE(ids);
    PyObject *inputs[] = {components ? poses : matrices, sizes, tints, opacities, depths, visible};
    PyObject *columns[6] = {};
    std::vector<BatchSpriteUpdate> parsed;
    std::vector<Py_ssize_t> positions;
    double prepend[6];
    bool ok = true;
    // Freeze every outer sequence before invoking any element conversion.
    for (int field = 0; ok && field < 6; ++field) {
        if (inputs[field] == Py_None) continue;
        columns[field] = PySequence_Tuple(inputs[field]);
        ok = columns[field] && PyTuple_GET_SIZE(columns[field]) == count;
        if (!ok && !PyErr_Occurred())
            PyErr_SetString(PyExc_ValueError, "indices and sprite values must have the same length");
    }
    if (ok && transform != Py_None) ok = batch_numbers(transform, prepend, 6) == 0;
    try { if (ok) { parsed.resize(count); positions.resize(count); } }
    catch (const std::bad_alloc &) { PyErr_NoMemory(); ok = false; }
    for (Py_ssize_t i = 0; ok && i < count; ++i) {
        positions[i] = PyLong_AsSsize_t(PyTuple_GET_ITEM(ids, i));
        ok = !PyErr_Occurred() && batch_index(data, positions[i]);
        auto &item = parsed[i];
        for (int field = 0; ok && field < 6; ++field) {
            if (!columns[field]) continue;
            PyObject *value = PyTuple_GET_ITEM(columns[field], i);
            if (field <= 2) {
                double *out = field == 0 ? item.matrix : field == 1 ? item.size : item.tint;
                int width = field == 0 ? (components ? 5 : 6) : field == 1 ? 2 : 4;
                ok = batch_numbers(value, out, width) == 0;
                if (ok && field == 0) {
                    if (components) {
                        double values[5];
                        memcpy(values, out, sizeof(values));
                        c_matrix(values[0], values[1], values[2], values[3], values[4], out);
                    }
                    if (transform != Py_None) {
                        // Preserve the same separate rounding as Python floats.
                        #pragma clang fp contract(off)
                        double b[6];
                        memcpy(b, out, sizeof(b));
                        out[0] = prepend[0]*b[0] + prepend[2]*b[1];
                        out[1] = prepend[1]*b[0] + prepend[3]*b[1];
                        out[2] = prepend[0]*b[2] + prepend[2]*b[3];
                        out[3] = prepend[1]*b[2] + prepend[3]*b[3];
                        out[4] = prepend[0]*b[4] + prepend[2]*b[5] + prepend[4];
                        out[5] = prepend[1]*b[4] + prepend[3]*b[5] + prepend[5];
                    }
                    for (int j = 0; j < 6; ++j) if (!isfinite(out[j])) {
                        PyErr_SetString(PyExc_ValueError, "sprite pose must produce a finite transform");
                        ok = false;
                        break;
                    }
                }
            } else if (field <= 4) {
                double number = PyFloat_AsDouble(value);
                ok = !PyErr_Occurred() && isfinite(number);
                if (!ok && !PyErr_Occurred())
                    PyErr_SetString(PyExc_ValueError, "sprite values must be finite");
                (field == 3 ? item.opacity : item.z) = number;
            } else {
                int flag = PyObject_IsTrue(value);
                ok = flag >= 0;
                item.visible = flag;
            }
        }
    }
    if (ok) {
        bool changed = false;
        for (Py_ssize_t i = 0; i < count; ++i) {
            auto &dst = data->items[positions[i]];
            const auto &src = parsed[i];
            for (int field = 0; field < 3; ++field) {
                if (!columns[field]) continue;
                double *out = field == 0 ? dst.matrix : field == 1 ? dst.size : dst.tint;
                const double *value = field == 0 ? src.matrix : field == 1 ? src.size : src.tint;
                size_t bytes = (field == 0 ? 6 : field == 1 ? 2 : 4) * sizeof(double);
                if (memcmp(out, value, bytes)) {
                    memcpy(out, value, bytes);
                    changed = true;
                }
            }
            if (columns[3] && dst.opacity != src.opacity) { dst.opacity = src.opacity; changed = true; }
            if (columns[4] && dst.z != src.z) { dst.z = src.z; changed = true; }
            if (columns[5] && dst.visible != src.visible) { dst.visible = src.visible; changed = true; }
        }
        if (changed) ++data->revision;
    }
    Py_DECREF(ids);
    for (auto column : columns) Py_XDECREF(column);
    if (!ok) return nullptr;
    Py_RETURN_NONE;
}

static int emit_sprite_batch(PyObject *node, CNodeCache *cache, CollectState *st,
                             const double world[6], double opacity) {
    PyObject *capsule = PyObject_GetAttrString(node, "_batch_data");
    auto *data = capsule ? batch_data(capsule) : nullptr;
    if (!data) { Py_XDECREF(capsule); return -1; }
    if (batch_build_order(data) < 0) { Py_DECREF(capsule); return -1; }
    const size_t count = data->items.size();
    if (count > INT_MAX) {
        Py_DECREF(capsule);
        PyErr_SetString(PyExc_OverflowError, "too many batch sprites");
        return -1;
    }
    std::vector<std::array<double, 6>> worlds;
    std::vector<double> opacities;
    std::vector<bool> shown;
    try { worlds.resize(count); opacities.resize(count); shown.resize(count); }
    catch (const std::bad_alloc &) { Py_DECREF(capsule); PyErr_NoMemory(); return -1; }
    if (cache->dyn_capacity < (int)count) {
        void *grown = realloc(cache->dyn_cmds, sizeof(CCmd) * count);
        if (!grown) { Py_DECREF(capsule); PyErr_NoMemory(); return -1; }
        cache->dyn_cmds = (CCmd *)grown;
        cache->dyn_capacity = (int)count;
    }
    for (int i = 0; i < cache->dyn_count; ++i) Py_CLEAR(cache->dyn_cmds[i].texture);
    cache->dyn_count = cache->cmd_count = 0;
    for (size_t i : data->draw_order) {
        const auto &item = data->items[i];
        double pop = item.parent < 0 ? opacity : opacities[item.parent];
        shown[i] = item.visible && pop > .001 && (item.parent < 0 || shown[item.parent]);
        opacities[i] = pop * item.opacity;
        if (!shown[i]) continue;
        c_mul(item.parent < 0 ? world : worlds[item.parent].data(), item.matrix, worlds[i].data());
        if (!item.texture || item.texture == Py_None) continue;
        CCmd *cmd = &cache->dyn_cmds[cache->dyn_count++];
        memset(cmd, 0, sizeof(*cmd));
        cmd->z = item.z;
        cmd->order = ++st->order;
        cmd->kind = item.additive ? KIND_TEX_ADDITIVE : KIND_TEX;
        cmd->texture = Py_NewRef(item.texture);
        cmd->batch_index = (int)i;
        cmd->mesh_batch_idx = cmd->particle_idx = -1;
        double x0 = -item.anchor[0] * item.size[0], y0 = -item.anchor[1] * item.size[1];
        double x1 = x0 + item.size[0], y1 = y0 + item.size[1];
        float u0 = item.uv[item.flip_x ? 2 : 0], u1 = item.uv[item.flip_x ? 0 : 2];
        float v0 = item.uv[item.flip_y ? 3 : 1], v1 = item.uv[item.flip_y ? 1 : 3];
        const double corners[6][4] = {
            {x0,y0,u0,v0}, {x1,y0,u1,v0}, {x0,y1,u0,v1},
            {x1,y0,u1,v0}, {x1,y1,u1,v1}, {x0,y1,u0,v1}};
        float vertices[24];
        for (int j = 0; j < 6; ++j) {
            double x, y;
            c_apply(worlds[i].data(), corners[j][0], corners[j][1], &x, &y);
            vertices[4*j] = x; vertices[4*j+1] = y;
            vertices[4*j+2] = corners[j][2]; vertices[4*j+3] = corners[j][3];
        }
        memcpy(cmd->vb, vertices, sizeof(vertices));
        float p[4] = {(float)KIND_TEX, 0, 0, 0}, fill[4] = {};
        float style[4] = {0, 0, (float)opacities[i], 0};
        float tint[4] = {(float)item.tint[0], (float)item.tint[1], (float)item.tint[2], (float)item.tint[3]};
        pack_quad(p, style, fill, tint, cmd->qb);
    }
    Py_XSETREF(cache->snap_cache, capsule);
    cache->batch_revision = data->revision;
    return 0;
}

/* Per-instance clips live in batch coordinates, before the instance pose.
   Cache commands contain no borrowed clip pointers from earlier frames. */
static int batch_clip_commands(CNodeCache *cache, CollectState *st, int start, PyObject *parents) {
    auto *data = batch_data(cache->snap_cache);
    if (!data) return -1;
    if (!data->clip_count || start == st->count) return 0;
    PyObject *states = PyDict_New();
    PyObject *world = Py_BuildValue("(dddddd)", cache->world[0], cache->world[1], cache->world[2],
                                   cache->world[3], cache->world[4], cache->world[5]);
    if (!states || !world) { Py_XDECREF(states); Py_XDECREF(world); return -1; }
    std::vector<PyObject *> contexts;
    std::vector<bool> needed;
    try {
        contexts.resize(data->items.size());
        needed.resize(data->items.size());
    }
    catch (const std::bad_alloc &) { Py_DECREF(states); Py_DECREF(world); PyErr_NoMemory(); return -1; }
    for (int i = start; i < st->count; ++i) {
        Py_ssize_t index = st->cmds[i].batch_index;
        while (index >= 0 && !needed[index]) {
            needed[index] = true;
            index = data->items[index].parent;
        }
    }
    int status = 0;
    for (size_t i = 0; i < data->items.size(); ++i) {
        if (!needed[i]) continue;
        const auto &item = data->items[i];
        PyObject *context = item.parent < 0 ? parents : contexts[item.parent];
        if (item.clip && item.clip != Py_None) {
            PyObject *key = PyTuple_Pack(2, item.clip, context);
            PyObject *merged = key ? PyDict_GetItemWithError(states, key) : nullptr;
            if (!merged && !PyErr_Occurred()) {
                PyObject *region = PyObject_CallMethod(item.clip, "_state", "(O)", world);
                PyObject *suffix = region ? PyTuple_Pack(1, region) : nullptr;
                Py_XDECREF(region);
                merged = suffix ? PySequence_Concat(context, suffix) : nullptr;
                Py_XDECREF(suffix);
                if (merged && (PyDict_SetItem(states, key, merged) < 0 || PyList_Append(st->clip_contexts, merged) < 0)) {
                    Py_DECREF(merged); merged = nullptr;
                } else if (merged) Py_DECREF(merged); /* state owns it */
            }
            Py_XDECREF(key);
            if (!merged) { status = -1; break; }
            context = merged;
        }
        contexts[i] = context;
    }
    if (!status) for (int i = start; i < st->count; ++i) {
        st->cmds[i].clips = contexts[st->cmds[i].batch_index];
        Py_hash_t hash = PyObject_Hash(st->cmds[i].clips);
        if (hash == -1) { status = -1; break; }
        st->fingerprint = fingerprint_mix(st->fingerprint, (unsigned long long)hash);
    }
    Py_DECREF(world);
    Py_DECREF(states);
    return status;
}

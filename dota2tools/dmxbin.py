"""Reader for binary DMX (encoding binary 9), which is what Hammer saves.

The text path we use for building maps goes through dmxconvert, but a painted
map is far too large for that -- a painted copy of the alpha map was 989 MB, and its text
form does not fit on the disk. This reads the binary directly.

Layout, established by probing template_map.vmap:

    "<!-- dmx encoding binary 9 format vmap 20 -->\\n\\0"
    int32   prefix element count
      int32 attribute count, then attributes with inline names
    int32   string dictionary count, then null-terminated strings
    int32   element count
      int32 type index, int32 name index, 16 byte guid   (headers first)
      int32 attribute count, then attributes             (bodies after)

Attribute names and string values are dictionary indices; strings inside string
arrays are inline, which is the one asymmetry in the format.
"""
import struct


# Source 2 widened the type enum: an element array is 33 against an element's
# 1, so arrays sit 32 above their base type rather than the 14 older DMX used.
ARRAY = 32

SINGLE = {
    2: ("<i", 4), 3: ("<f", 4), 4: ("<B", 1), 7: ("<i", 4),
    8: ("<4B", 4), 9: ("<2f", 8), 10: ("<3f", 12), 11: ("<4f", 16),
    12: ("<3f", 12), 13: ("<4f", 16), 14: ("<16f", 64),
    # Source 2 additions past the classic enum.
    15: ("<Q", 8), 16: ("<B", 1),
}
TYPE_NAMES = {15: "uint64", 16: "uint8", 1: "element", 2: "int", 3: "float", 4: "bool", 5: "string",
              6: "binary", 7: "time", 8: "color", 9: "vector2", 10: "vector3",
              11: "vector4", 12: "qangle", 13: "quaternion", 14: "matrix"}


class Element:
    __slots__ = ("type", "name", "guid", "attrs", "offsets")

    def __init__(self, type_, name, guid):
        self.type, self.name, self.guid, self.attrs = type_, name, guid, {}
        # Where each array attribute's values start in the file, to patch
        # fixed-size values in place.
        self.offsets = {}

    def __repr__(self):
        return f"<{self.type} {self.name!r} {len(self.attrs)} attrs>"


class Dmx:
    def __init__(self, data, want=None):
        """Parse `data`. `want` limits which attribute names are kept, which
        matters on a map this size: keeping everything costs gigabytes."""
        self.d = data
        self.want = want
        self.o = data.find(b"\0") + 1
        self.header = data[:self.o - 1].decode("ascii", "replace")
        self._prefix()
        self.strings = self._strings()
        self.elements = self._elements()

    def _i32(self):
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def _cstr(self):
        j = self.d.find(b"\0", self.o)
        s = self.d[self.o:j].decode("utf-8", "replace")
        self.o = j + 1
        return s

    def _prefix(self):
        """The prefix block carries the map's thumbnail and asset metadata.

        Its attribute names are inline strings, and so are its string values --
        the dictionary has not been read yet at this point in the file.
        """
        for _ in range(self._i32()):
            for _ in range(self._i32()):
                self._cstr()
                t = self.d[self.o]
                self.o += 1
                if t < ARRAY:
                    self._skip_one(t)
                else:
                    for _ in range(self._i32()):
                        self._skip_one(t - ARRAY)

    def _skip_one(self, t):
        if t == 5:
            self._cstr()
        elif t == 6:
            self.o += 4 + self._i32()
        elif t == 1:
            self.o += 4
        else:
            self.o += SINGLE[t][1]

    def _strings(self):
        return [self._cstr() for _ in range(self._i32())]

    def _value(self, t):
        d, o = self.d, self.o
        if t == 1:
            self.o += 4
            return struct.unpack_from("<i", d, o)[0]
        if t == 5:
            self.o += 4
            return self.strings[struct.unpack_from("<i", d, o)[0]]
        if t == 6:
            n = struct.unpack_from("<i", d, o)[0]
            self.o = o + 4 + n
            return d[o + 4:o + 4 + n]
        fmt, size = SINGLE[t]
        self.o = o + size
        v = struct.unpack_from(fmt, d, o)
        return v[0] if len(v) == 1 else v

    def _array(self, t, keep):
        n = self._i32()
        if t == 5:
            out = [self._cstr() for _ in range(n)]
            return out if keep else None
        if t in (1, 2, 3, 4, 6, 7):
            if t in (1, 2, 7):
                fmt, size = "<i", 4
            elif t == 3:
                fmt, size = "<f", 4
            elif t == 4:
                fmt, size = "<B", 1
            else:
                out = [self._value(6) for _ in range(n)]
                return out if keep else None
            o = self.o
            self.o = o + size * n
            return list(struct.unpack_from(f"<{n}{fmt[1]}", self.d, o)) if keep else None
        fmt, size = SINGLE[t]
        o = self.o
        self._last = o
        self.o = o + size * n
        if not keep:
            return None
        w = size // 4
        flat = struct.unpack_from(f"<{n * w}f" if t != 8 else f"<{n * 4}B",
                                  self.d, o)
        return [flat[i * w:(i + 1) * w] for i in range(n)]

    def _elements(self):
        n = self._i32()
        els = []
        for _ in range(n):
            ti, ni = struct.unpack_from("<ii", self.d, self.o)
            self.o += 8 + 16
            els.append(Element(self.strings[ti], self.strings[ni],
                               self.d[self.o - 16:self.o]))
        for el in els:
            for _ in range(self._i32()):
                name = self.strings[self._i32()]
                t = self.d[self.o]
                self.o += 1
                keep = self.want is None or name in self.want
                if t < ARRAY:
                    v = self._value(t)
                else:
                    self._last = None
                    v = self._array(t - ARRAY, keep)
                    if self._last is not None:
                        el.offsets[name] = self._last
                # Unwanted values are dropped but their names are kept, so a
                # first pass can map the file without holding it in memory.
                el.attrs[name] = v if keep else (TYPE_NAMES.get(
                    t if t < ARRAY else t - ARRAY, t), None)
        return els

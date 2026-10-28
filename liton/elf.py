"""Pure-python ELF reader.

Liton must analyse a foreign binary without executing it, and without trusting
that ``ldd`` (which runs the loader) is safe or present.  This module parses the
ELF structures directly so the analysis is a pure function of the file bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .models import ElfInfo
from .util import machine_name

# e_type
ET_REL, ET_EXEC, ET_DYN, ET_CORE = 1, 2, 3, 4

# p_type
PT_LOAD, PT_DYNAMIC, PT_INTERP = 1, 2, 3
PT_GNU_STACK = 0x6474E551

# d_tag
DT_NULL, DT_NEEDED, DT_STRTAB, DT_SYMTAB, DT_STRSZ = 0, 1, 5, 6, 10
DT_SONAME, DT_RPATH, DT_RUNPATH = 14, 15, 29
DT_SYMENT = 11
DT_GNU_HASH = 0x6FFFFEF5
DT_HASH = 4
DT_VERNEED, DT_VERNEEDNUM = 0x6FFFFFFE, 0x6FFFFFFF
DT_VERDEF, DT_VERDEFNUM = 0x6FFFFFFC, 0x6FFFFFFD

# sh_type
SHT_STRTAB, SHT_DYNAMIC, SHT_DYNSYM = 3, 6, 11

# st_shndx
SHN_UNDEF = 0

PF_X = 0x1

_TYPE_NAMES = {ET_REL: "REL", ET_EXEC: "EXEC", ET_DYN: "DYN", ET_CORE: "CORE"}


class NotElfError(ValueError):
    pass


@dataclass
class _Segment:
    vaddr: int
    offset: int
    filesz: int
    memsz: int
    flags: int


class Elf:
    """A parsed ELF object.  Cheap to construct; work happens in :meth:`parse`."""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.segments: list[_Segment] = []
        self._dynamic: dict[int, list[int]] = {}
        self.info = ElfInfo(path=path)

    # -- primitive readers -------------------------------------------------
    def _fmt(self, size: int) -> str:
        endian = "<" if self.little else ">"
        return endian + {1: "B", 2: "H", 4: "I", 8: "Q"}[size]

    def _u(self, offset: int, size: int) -> int:
        fmt = self._fmt(size)
        return struct.unpack_from(fmt, self.data, offset)[0]

    def _cstr(self, offset: int, limit: int = 1 << 20) -> str:
        end = self.data.find(b"\x00", offset, min(len(self.data), offset + limit))
        if end < 0:
            end = min(len(self.data), offset + limit)
        return self.data[offset:end].decode("utf-8", "replace")

    def _vaddr_to_offset(self, vaddr: int) -> int | None:
        for seg in self.segments:
            if seg.vaddr <= vaddr < seg.vaddr + seg.filesz:
                return seg.offset + (vaddr - seg.vaddr)
        return None

    def _str_at_vaddr(self, vaddr: int) -> str:
        off = self._vaddr_to_offset(vaddr)
        if off is None:
            return ""
        return self._cstr(off)

    # -- parsing -----------------------------------------------------------
    def parse(self) -> ElfInfo:
        info = self.info
        if len(self.data) < 16 or self.data[:4] != b"\x7fELF":
            raise NotElfError(f"{self.path}: not an ELF file")

        elf_class = self.data[4]
        data_enc = self.data[5]
        if elf_class not in (1, 2):
            raise NotElfError(f"{self.path}: unknown ELF class {elf_class}")

        self.little = data_enc != 2
        info.elf_class = elf_class
        info.little_endian = self.little
        w = 8 if elf_class == 2 else 4  # address width

        if elf_class == 2:
            e_type = self._u(16, 2)
            e_machine = self._u(18, 2)
            e_phoff = self._u(32, 8)
            e_phentsize = self._u(54, 2)
            e_phnum = self._u(56, 2)
        else:
            e_type = self._u(16, 2)
            e_machine = self._u(18, 2)
            e_phoff = self._u(28, 4)
            e_phentsize = self._u(42, 2)
            e_phnum = self._u(44, 2)

        info.e_type = _TYPE_NAMES.get(e_type, str(e_type))
        info.machine = machine_name(e_machine)

        self._read_phdrs(e_phoff, e_phentsize, e_phnum, w)
        self._read_dynamic(w)

        info.needed = self._needed()
        info.soname = self._soname()
        info.rpath = self._rpath(DT_RPATH)
        info.runpath = self._rpath(DT_RUNPATH)
        info.is_pie = e_type == ET_DYN and bool(info.interpreter)
        info.has_exec_stack = self._exec_stack()
        info.valid = True
        return info

    def _read_phdrs(self, phoff: int, entsize: int, phnum: int, w: int) -> None:
        if not phoff or not entsize:
            return
        for i in range(phnum):
            base = phoff + i * entsize
            if base + entsize > len(self.data):
                break
            p_type = self._u(base, 4)
            if w == 8:
                p_flags = self._u(base + 4, 4)
                p_offset = self._u(base + 8, 8)
                p_vaddr = self._u(base + 16, 8)
                p_filesz = self._u(base + 32, 8)
                p_memsz = self._u(base + 40, 8)
            else:
                p_offset = self._u(base + 4, 4)
                p_vaddr = self._u(base + 8, 4)
                p_filesz = self._u(base + 16, 4)
                p_memsz = self._u(base + 20, 4)
                p_flags = self._u(base + 24, 4)

            if p_type == PT_LOAD:
                self.segments.append(_Segment(p_vaddr, p_offset, p_filesz, p_memsz, p_flags))
            elif p_type == PT_INTERP:
                self.info.interpreter = self._cstr(p_offset).strip()
            elif p_type == PT_DYNAMIC:
                self._ph_dynamic = (p_offset, p_filesz)
            elif p_type == PT_GNU_STACK:
                self._gnu_stack_flags = p_flags

    def _read_dynamic(self, w: int) -> None:
        ph = getattr(self, "_ph_dynamic", None)
        if not ph:
            return
        p_offset, p_filesz = ph
        entsize = 16 if w == 8 else 8
        count = p_filesz // entsize if entsize else 0
        for i in range(count):
            base = p_offset + i * entsize
            if base + entsize > len(self.data):
                break
            if w == 8:
                d_tag = self._u(base, 8)
                d_val = self._u(base + 8, 8)
            else:
                d_tag = self._u(base, 4)
                d_val = self._u(base + 4, 4)
            if d_tag == DT_NULL:
                break
            self._dynamic.setdefault(d_tag, []).append(d_val)

    def _exec_stack(self) -> bool:
        return bool(getattr(self, "_gnu_stack_flags", 0) & PF_X)

    # -- dynamic table accessors -------------------------------------------
    def _dt(self, tag: int) -> int:
        vals = self._dynamic.get(tag)
        return vals[0] if vals else 0

    def _dynstr(self, offset: int) -> str:
        strtab = self._dt(DT_STRTAB)
        if not strtab:
            return ""
        off = self._vaddr_to_offset(strtab + offset)
        if off is None:
            return ""
        return self._cstr(off)

    def _needed(self) -> list[str]:
        out: list[str] = []
        for off in self._dynamic.get(DT_NEEDED, []):
            name = self._dynstr(off)
            if name and name not in out:
                out.append(name)
        return out

    def _soname(self) -> str:
        offs = self._dynamic.get(DT_SONAME)
        return self._dynstr(offs[0]) if offs else ""

    def _rpath(self, tag: int) -> list[str]:
        out: list[str] = []
        for off in self._dynamic.get(tag, []):
            raw = self._dynstr(off)
            for part in raw.split(":"):
                part = part.replace("$ORIGIN", ".").strip()
                if part and part not in out:
                    out.append(part)
        return out

    def _sym_count_from_hash(self) -> int:
        """Number of dynamic symbols, derived from DT_GNU_HASH / DT_HASH."""
        gnu = self._dt(DT_GNU_HASH)
        if gnu:
            base = self._vaddr_to_offset(gnu)
            if base is not None:
                nbuckets = self._u(base, 4)
                symoffset = self._u(base + 4, 4)
                bloom_size = self._u(base + 8, 4)
                word = 8 if self.info.elf_class == 2 else 4
                buckets_off = base + 16 + bloom_size * word
                chains_off = buckets_off + nbuckets * 4
                max_sym = symoffset
                for b in range(nbuckets):
                    bucket = self._u(buckets_off + b * 4, 4)
                    if bucket < symoffset:
                        continue
                    idx = bucket - symoffset
                    while True:
                        pos = chains_off + idx * 4
                        if pos + 4 > len(self.data):
                            break
                        if self._u(pos, 4) & 1:
                            break
                        idx += 1
                    max_sym = max(max_sym, bucket + (idx - (bucket - symoffset)) + 1)
                if max_sym > symoffset:
                    return max_sym

        legacy = self._dt(DT_HASH)
        if legacy:
            base = self._vaddr_to_offset(legacy)
            if base is not None:
                return self._u(base + 4, 4)  # nchain == number of symbols
        return 0

    def version_definitions(self) -> list[str]:
        """Symbol versions this object *provides*, from DT_VERDEF."""
        verdef_vaddr = self._dt(DT_VERDEF)
        count = self._dt(DT_VERDEFNUM)
        if not verdef_vaddr or not count:
            return []
        base = self._vaddr_to_offset(verdef_vaddr)
        if base is None:
            return []

        out: list[str] = []
        off = base
        for _ in range(count):
            if off + 20 > len(self.data):
                break
            vd_cnt = self._u(off + 4, 2)
            vd_aux = self._u(off + 12, 4)
            vd_next = self._u(off + 16, 4)
            aux = off + vd_aux
            for _ in range(vd_cnt):
                if aux + 8 > len(self.data):
                    break
                vda_name = self._u(aux, 4)
                vda_next = self._u(aux + 4, 4)
                name = self._dynstr(vda_name)
                if name and name not in out:
                    out.append(name)
                if not vda_next:
                    break
                aux += vda_next
            if not vd_next:
                break
            off += vd_next
        return out

    def version_needs(self) -> dict[str, list[str]]:
        """Required symbol versions per library, from the DT_VERNEED table.

        This is what makes a binary fail with ``GLIBC_2.38 not found`` on an
        older distribution: the *libraries* are present, but they do not export
        the required *version*.
        """
        verneed_vaddr = self._dt(DT_VERNEED)
        count = self._dt(DT_VERNEEDNUM)
        if not verneed_vaddr or not count:
            return {}
        base = self._vaddr_to_offset(verneed_vaddr)
        if base is None:
            return {}

        out: dict[str, list[str]] = {}
        off = base
        for _ in range(count):
            if off + 16 > len(self.data):
                break
            vn_cnt = self._u(off + 2, 2)
            vn_file = self._u(off + 4, 4)
            vn_aux = self._u(off + 8, 4)
            vn_next = self._u(off + 12, 4)
            lib = self._dynstr(vn_file)

            aux = off + vn_aux
            versions: list[str] = []
            for _ in range(vn_cnt):
                if aux + 16 > len(self.data):
                    break
                vna_name = self._u(aux + 8, 4)
                vna_next = self._u(aux + 12, 4)
                name = self._dynstr(vna_name)
                if name:
                    versions.append(name)
                if not vna_next:
                    break
                aux += vna_next
            if lib and versions:
                out.setdefault(lib, [])
                for v in versions:
                    if v not in out[lib]:
                        out[lib].append(v)
            if not vn_next:
                break
            off += vn_next
        return out

    def imported_symbols(self) -> list[str]:
        """Undefined dynamic symbols (the API surface the app needs)."""
        symtab_vaddr = self._dt(DT_SYMTAB)
        if not symtab_vaddr:
            return []
        symtab_off = self._vaddr_to_offset(symtab_vaddr)
        if symtab_off is None:
            return []

        count = self._sym_count_from_hash()
        if not count:
            count = self._sym_count_from_sections()
        if not count:
            return []

        entsize = 24 if self.info.elf_class == 2 else 16
        out: list[str] = []
        for i in range(count):
            base = symtab_off + i * entsize
            if base + entsize > len(self.data):
                break
            st_name = self._u(base, 4)
            if self.info.elf_class == 2:
                st_shndx = self._u(base + 6, 2)
            else:
                st_shndx = self._u(base + 14, 2)
            if st_shndx != SHN_UNDEF or not st_name:
                continue
            name = self._dynstr(st_name)
            if name:
                out.append(name)
        return out

    def _sym_count_from_sections(self) -> int:
        """Fallback: count entries in an SHT_DYNSYM section."""
        try:
            if self.info.elf_class == 2:
                e_shoff = self._u(40, 8)
                e_shentsize = self._u(58, 2)
                e_shnum = self._u(60, 2)
                sh_type_at, sh_size_at, sh_entsize_at = 4, 32, 56
            else:
                e_shoff = self._u(32, 4)
                e_shentsize = self._u(46, 2)
                e_shnum = self._u(48, 2)
                sh_type_at, sh_size_at, sh_entsize_at = 4, 20, 36
        except Exception:
            return 0
        if not e_shoff:
            return 0
        for i in range(e_shnum):
            base = e_shoff + i * e_shentsize
            if base + e_shentsize > len(self.data):
                break
            if self._u(base + sh_type_at, 4) != SHT_DYNSYM:
                continue
            entsize = self._u(base + sh_entsize_at, 8 if self.info.elf_class == 2 else 4)
            size = self._u(base + sh_size_at, 8 if self.info.elf_class == 2 else 4)
            if entsize:
                return size // entsize
        return 0


def parse_elf(path: str) -> Elf:
    """Parse *path* and return the :class:`Elf` with ``.info`` populated."""
    elf = Elf(path)
    elf.parse()
    return elf


def looks_like_elf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False

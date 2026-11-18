"""
CASC/TVFS/BLTE & MPQ Storage Library for Warcraft III: Reforged (v3.0.x)
Provides read-only extraction from CASC and clean in-place MPQ patching.
"""
import os, glob, struct, zlib, binascii, hashlib

DEFAULT_GAME_DIR = r'J:\Games\Warcraft III'
DEFAULT_WORKSPACE = r'p:\WC3Japanese'

# Initialize StormLib MPQ cryptographic table
STORM_BUF = [0] * 0x500
_seed = 0x00100001
for _i1 in range(256):
    _i2 = _i1
    for _ in range(5):
        _seed = (_seed * 125 + 3) % 0x2AAAAB
        _t1 = (_seed & 0xFFFF) << 16
        _seed = (_seed * 125 + 3) % 0x2AAAAB
        _t2 = _seed & 0xFFFF
        STORM_BUF[_i2] = _t1 | _t2
        _i2 += 0x100


def mpq_hash_string(s: str, hash_type: int) -> int:
    seed1 = 0x7FED7FED
    seed2 = 0xEEEEEEEE
    for ch in s.upper().encode('ascii', errors='replace'):
        val = STORM_BUF[(hash_type << 8) + ch]
        seed1 = (val ^ (seed1 + seed2)) & 0xFFFFFFFF
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def mpq_decrypt_block(data: bytes, key: int) -> bytes:
    seed2 = 0xEEEEEEEE
    out = bytearray(data)
    for i in range(0, len(data) - 3, 4):
        seed2 = (seed2 + STORM_BUF[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        val = struct.unpack_from('<I', data, i)[0]
        ch = (val ^ ((key + seed2) & 0xFFFFFFFF)) & 0xFFFFFFFF
        struct.pack_into('<I', out, i, ch)
        key = (((~key << 0x15) + 0x11111111) | (key >> 0x0B)) & 0xFFFFFFFF
        seed2 = (ch + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return bytes(out)


def mpq_encrypt_block(data: bytes, key: int) -> bytes:
    seed2 = 0xEEEEEEEE
    out = bytearray(data)
    for i in range(0, len(data) - 3, 4):
        seed2 = (seed2 + STORM_BUF[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        ch = struct.unpack_from('<I', data, i)[0]
        val = (ch ^ ((key + seed2) & 0xFFFFFFFF)) & 0xFFFFFFFF
        struct.pack_into('<I', out, i, val)
        key = (((~key << 0x15) + 0x11111111) | (key >> 0x0B)) & 0xFFFFFFFF
        seed2 = (ch + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return bytes(out)


def read_mpq_file(mpq: bytes, filename: str) -> bytes | None:
    if not mpq or mpq[:4] != b'MPQ\x1a':
        return None
    hdr_sz, arc_sz, fmt_ver, sec_sz, ht_off, bt_off, ht_cnt, bt_cnt = struct.unpack_from('<IIHHIIII', mpq, 4)
    ht_raw = mpq_decrypt_block(mpq[ht_off:ht_off + ht_cnt * 16], mpq_hash_string('(hash table)', 3))
    bt_raw = mpq_decrypt_block(mpq[bt_off:bt_off + bt_cnt * 16], mpq_hash_string('(block table)', 3))

    h_idx = mpq_hash_string(filename, 0) & (ht_cnt - 1)
    h_a = mpq_hash_string(filename, 1)
    h_b = mpq_hash_string(filename, 2)

    for i in range(ht_cnt):
        pos = ((h_idx + i) % ht_cnt) * 16
        na, nb, lc, plat, b_idx = struct.unpack_from('<IIHHI', ht_raw, pos)
        if b_idx == 0xFFFFFFFF:
            return None
        if na == h_a and nb == h_b and b_idx < bt_cnt:
            f_pos, c_sz, f_sz, flags = struct.unpack_from('<IIII', bt_raw, b_idx * 16)
            break
    else:
        return None

    if not (flags & 0x80000000):
        return None

    raw_file = mpq[f_pos:f_pos + c_sz]
    base_name = filename.split('\\')[-1].split('/')[-1]
    file_key = mpq_hash_string(base_name, 3)
    if flags & 0x00020000:
        file_key = ((file_key + f_pos) ^ f_sz) & 0xFFFFFFFF

    if not (flags & 0x00000200):
        if flags & 0x00010000:
            raw_file = mpq_decrypt_block(raw_file, file_key)
        return raw_file
    if flags & 0x01000000:
        if flags & 0x00010000:
            raw_file = mpq_decrypt_block(raw_file, file_key)
        return zlib.decompress(raw_file[1:]) if raw_file[0] == 2 else raw_file

    sector_size = 512 << sec_sz
    num_sectors = (f_sz + sector_size - 1) // sector_size
    num_offsets = num_sectors + 1 + (1 if (flags & 0x04000000) else 0)
    sot_raw = raw_file[:num_offsets * 4]
    if flags & 0x00010000:
        sot_raw = mpq_decrypt_block(sot_raw, (file_key - 1) & 0xFFFFFFFF)
    offsets = struct.unpack_from(f'<{num_offsets}I', sot_raw, 0)
    out = bytearray()
    for s in range(num_sectors):
        s_data = raw_file[offsets[s]:offsets[s+1]]
        if flags & 0x00010000:
            s_data = mpq_decrypt_block(s_data, (file_key + s) & 0xFFFFFFFF)
        expected = min(sector_size, f_sz - len(out))
        if len(s_data) < expected:
            if s_data[0] == 2:
                out.extend(zlib.decompress(s_data[1:]))
            else:
                out.extend(s_data)
        else:
            out.extend(s_data)
    return bytes(out)


def patch_mpq_file(mpq_bytes: bytes, filename: str, new_file_bytes: bytes) -> bytes:
    hdr_sz, arc_sz, fmt_ver, sec_sz, ht_off, bt_off, ht_cnt, bt_cnt = struct.unpack_from('<IIHHIIII', mpq_bytes, 4)
    ngis_footer = mpq_bytes[arc_sz:]
    ht_raw = bytearray(mpq_decrypt_block(mpq_bytes[ht_off:ht_off + ht_cnt * 16], mpq_hash_string('(hash table)', 3)))
    bt_raw = bytearray(mpq_decrypt_block(mpq_bytes[bt_off:bt_off + bt_cnt * 16], mpq_hash_string('(block table)', 3)))

    h_idx = mpq_hash_string(filename, 0) & (ht_cnt - 1)
    h_a = mpq_hash_string(filename, 1)
    h_b = mpq_hash_string(filename, 2)

    b_idx = None
    for i in range(ht_cnt):
        pos = ((h_idx + i) % ht_cnt) * 16
        na, nb, lc, plat, bi = struct.unpack_from('<IIHHI', ht_raw, pos)
        if bi == 0xFFFFFFFF:
            raise KeyError(f'{filename} not found in MPQ')
        if na == h_a and nb == h_b:
            b_idx = bi
            break
    if b_idx is None:
        raise KeyError(f'{filename} not found in MPQ')

    sector_size = 512 << sec_sz
    f_sz = len(new_file_bytes)
    num_sectors = (f_sz + sector_size - 1) // sector_size
    sot_size = (num_sectors + 1) * 4
    sectors_buf = bytearray()
    offsets = [sot_size]
    for s in range(num_sectors):
        chunk = new_file_bytes[s * sector_size : (s + 1) * sector_size]
        comp = b'\x02' + zlib.compress(chunk)
        if len(comp) < len(chunk):
            sectors_buf.extend(comp)
        else:
            sectors_buf.extend(chunk)
        offsets.append(sot_size + len(sectors_buf))
    sot_buf = struct.pack(f'<{num_sectors + 1}I', *offsets)
    packed_data = sot_buf + bytes(sectors_buf)
    c_sz = len(packed_data)

    new_f_pos = ht_off
    new_flags = 0x80000200
    struct.pack_into('<IIII', bt_raw, b_idx * 16, new_f_pos, c_sz, f_sz, new_flags)

    out = bytearray(mpq_bytes[:ht_off])
    attr_h_idx = mpq_hash_string('(attributes)', 0) & (ht_cnt - 1)
    attr_a = mpq_hash_string('(attributes)', 1)
    attr_b = mpq_hash_string('(attributes)', 2)
    for i in range(ht_cnt):
        pos = ((attr_h_idx + i) % ht_cnt) * 16
        na, nb, lc, plat, bi = struct.unpack_from('<IIHHI', ht_raw, pos)
        if bi == 0xFFFFFFFF:
            break
        if na == attr_a and nb == attr_b:
            af_pos, ac_sz, af_sz, aflags = struct.unpack_from('<IIII', bt_raw, bi * 16)
            attr_raw = out[af_pos:af_pos + ac_sz]
            akey = ((mpq_hash_string('(attributes)', 3) + af_pos) ^ af_sz) & 0xFFFFFFFF
            sot_enc = attr_raw[:8]
            sec0_dec = bytearray(mpq_decrypt_block(attr_raw[8:], akey))
            new_crc = binascii.crc32(new_file_bytes) & 0xFFFFFFFF
            struct.pack_into('<I', sec0_dec, 8 + b_idx * 4, new_crc)
            sec0_enc = mpq_encrypt_block(bytes(sec0_dec), akey)
            out[af_pos:af_pos + ac_sz] = sot_enc + sec0_enc
            break

    new_ht_off = ht_off + len(packed_data)
    new_bt_off = new_ht_off + ht_cnt * 16
    new_arc_sz = new_bt_off + bt_cnt * 16

    ht_enc = mpq_encrypt_block(ht_raw, mpq_hash_string('(hash table)', 3))
    bt_enc = mpq_encrypt_block(bt_raw, mpq_hash_string('(block table)', 3))

    out.extend(packed_data)
    out.extend(ht_enc)
    out.extend(bt_enc)
    out.extend(ngis_footer)
    struct.pack_into('<IIHHIIII', out, 4, hdr_sz, new_arc_sz, fmt_ver, sec_sz, new_ht_off, new_bt_off, ht_cnt, bt_cnt)
    return bytes(out)


class CascReader:
    def __init__(self, game_dir: str = DEFAULT_GAME_DIR):
        self.game_dir = game_dir
        self.data_dir = os.path.join(game_dir, 'Data', 'data')
        self.config_dir = os.path.join(game_dir, 'Data', 'config')
        self.index = {}
        self.build_info = {}
        self.vfs_ekeys = {}
        self._load_index()
        self._load_build_config()

    def _load_index(self):
        idx_files = {}
        for p in glob.glob(os.path.join(self.data_dir, '*.idx')):
            name = os.path.basename(p)
            bucket = name[:2]
            ver = int(name[2:10], 16)
            if bucket not in idx_files or ver > idx_files[bucket][0]:
                idx_files[bucket] = (ver, p)

        for bucket, (ver, p) in sorted(idx_files.items()):
            with open(p, 'rb') as f:
                buf = f.read()
            entries_size = struct.unpack_from('<I', buf, 32)[0]
            pos = 40
            end = 40 + entries_size
            while pos + 18 <= end:
                key = buf[pos:pos+9]
                loc = int.from_bytes(buf[pos+9:pos+14], 'big')
                size = int.from_bytes(buf[pos+14:pos+18], 'little')
                archive = loc >> 30
                offset = loc & 0x3FFFFFFF
                if size > 30:
                    self.index[key] = (archive, offset, size)
                pos += 18

    def _load_build_config(self):
        # Find Build Configuration in Data/config
        for p in glob.glob(os.path.join(self.config_dir, '*', '*', '*')):
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                txt = f.read()
            if txt.startswith('# Build Configuration'):
                self.build_config_path = p
                for line in txt.splitlines():
                    if '=' in line and not line.startswith('#'):
                        k, v = [x.strip() for x in line.split('=', 1)]
                        self.build_info[k] = v
                        if k.startswith('vfs-') and not k.endswith('-size'):
                            parts = v.split()
                            if len(parts) == 2:
                                self.vfs_ekeys[k] = bytes.fromhex(parts[1])[:9]
                break

    def read_blte(self, ekey9: bytes) -> bytes | None:
        if ekey9 not in self.index:
            return None
        arc, off, sz = self.index[ekey9]
        with open(os.path.join(self.data_dir, f'data.{arc:03d}'), 'rb') as f:
            f.seek(off + 30)
            blte = f.read(sz - 30)
        if blte[:4] != b'BLTE':
            return None
        hdr_sz = struct.unpack_from('>I', blte, 4)[0]
        if hdr_sz == 0:
            mode = blte[8:9]
            return zlib.decompress(blte[9:]) if mode == b'Z' else blte[9:]
        num_chunks = int.from_bytes(blte[9:12], 'big')
        pos = 12
        chunks = []
        for _ in range(num_chunks):
            c_sz, u_sz = struct.unpack_from('>II', blte, pos)
            chunks.append((c_sz, u_sz))
            pos += 24
        out = bytearray()
        for c_sz, u_sz in chunks:
            chunk = blte[pos:pos+c_sz]
            pos += c_sz
            if chunk[0:1] == b'N':
                out.extend(chunk[1:])
            elif chunk[0:1] == b'Z':
                out.extend(zlib.decompress(chunk[1:]))
        return bytes(out)

    def parse_tvfs(self, vfs_name: str) -> dict[str, tuple[bytes, int]]:
        ekey = self.vfs_ekeys.get(vfs_name)
        data = self.read_blte(ekey) if ekey else None
        if not data or data[:4] != b'TVFS':
            return {}
        ver, hdr_size, ekey_size, pkey_size, flags = struct.unpack_from('>BBBBI', data, 4)
        pt_off, pt_sz, vfs_off, vfs_sz, cft_off, cft_sz = struct.unpack_from('>IIIIII', data, 12)
        if cft_sz <= 0xFF: cft_osz = 1
        elif cft_sz <= 0xFFFF: cft_osz = 2
        elif cft_sz <= 0xFFFFFF: cft_osz = 3
        else: cft_osz = 4
        pt = data[pt_off:pt_off+pt_sz]
        vfs = data[vfs_off:vfs_off+vfs_sz]
        cft = data[cft_off:cft_off+cft_sz]

        def _parse_nodes(buf, pos, end, prefix=''):
            results = {}
            cur_prefix = prefix
            while pos < end:
                pre = False
                post = False
                if buf[pos] == 0:
                    pre = True
                    pos += 1
                name_len = buf[pos]
                pos += 1
                if name_len == 0xFF:
                    val = struct.unpack_from('>I', buf, pos)[0]
                    pos += 4
                    name = ''
                else:
                    name = buf[pos:pos+name_len].decode('utf-8', errors='replace')
                    pos += name_len
                    if pos < end and buf[pos] == 0:
                        post = True
                        pos += 1
                    if pos < end and buf[pos] == 0xFF:
                        pos += 1
                        val = struct.unpack_from('>I', buf, pos)[0]
                        pos += 4
                    else:
                        val = None
                node_str = ('/' if pre else '') + name + ('/' if post else '')
                cur = cur_prefix + node_str
                if val is not None:
                    cur_prefix = prefix
                    if val & 0x80000000:
                        folder_len = val & 0x7FFFFFFF
                        sub_end = pos + folder_len - 4
                        results.update(_parse_nodes(buf, pos, sub_end, cur))
                        pos = sub_end
                    else:
                        span_count = vfs[val]
                        file_off, span_len = struct.unpack_from('>II', vfs, val + 1)
                        cft_entry_off = int.from_bytes(vfs[val+9:val+9+cft_osz], 'big')
                        f_ekey = cft[cft_entry_off:cft_entry_off+ekey_size]
                        results[cur.lstrip('/')] = (f_ekey, span_len)
                else:
                    cur_prefix = cur + '/'
            return results

        return _parse_nodes(pt, 0, len(pt))

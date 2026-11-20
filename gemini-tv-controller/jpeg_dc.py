"""
Tiny pure-Python JPEG reader that only recovers the DC coefficient of every 8x8 block,
i.e. the average colour of each block. That is all a "dominant colour" needs, and it avoids
the IDCT entirely. The TV has no Pillow, so this is what spotify_ambient.py uses.

Supports baseline (SOF0/SOF1) and progressive (SOF2) Huffman JPEGs, any chroma subsampling,
restart markers. For progressive files only the first DC scan is read (successive approximation
is applied as a shift, which is close enough for a colour average).

    dc_pixels(data) -> list of (r, g, b) floats, one per 8x8 block of the first component
"""

from typing import Dict, List, Tuple

ZIGZAG_LEN = 64


class JpegError(ValueError):
    pass


class _Bits:
    """MSB-first bit reader over entropy-coded data; stops at markers (byte stuffing 0xFF00 removed)."""

    __slots__ = ("d", "p", "acc", "n", "marker")

    def __init__(self, data: bytes, pos: int):
        self.d, self.p, self.acc, self.n, self.marker = data, pos, 0, 0, None

    def _fill(self):
        while self.n <= 24:
            if self.marker is not None or self.p >= len(self.d):
                self.acc <<= 8          # feed zeros past the end of the segment
                self.n += 8
                continue
            b = self.d[self.p]
            if b == 0xFF:
                nxt = self.d[self.p + 1] if self.p + 1 < len(self.d) else 0
                if nxt == 0x00:
                    self.p += 2
                elif nxt == 0xFF:       # fill bytes
                    self.p += 1
                    continue
                else:
                    self.marker = nxt
                    continue
            else:
                self.p += 1
            self.acc = (self.acc << 8) | b
            self.n += 8

    def bits(self, k: int) -> int:
        if k == 0:
            return 0
        if self.n < k:
            self._fill()
        self.n -= k
        return (self.acc >> self.n) & ((1 << k) - 1)

    def bit(self) -> int:
        return self.bits(1)

    def reset(self):
        """After an RSTn marker: drop leftover bits and skip the marker."""
        self.acc = self.n = 0
        if self.marker is not None and 0xD0 <= self.marker <= 0xD7:
            # p points at the 0xFF of the marker
            self.p += 2
        else:
            # marker not reached yet (bits fed from buffer); search for it
            while self.p + 1 < len(self.d):
                if self.d[self.p] == 0xFF and 0xD0 <= self.d[self.p + 1] <= 0xD7:
                    self.p += 2
                    break
                self.p += 1
        self.marker = None


def _huff_table(counts: bytes, symbols: bytes) -> Dict[Tuple[int, int], int]:
    table, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[(length, code)] = symbols[k]
            code += 1
            k += 1
        code <<= 1
    return table


def _decode(bits: _Bits, table) -> int:
    code = 0
    for length in range(1, 17):
        code = (code << 1) | bits.bit()
        sym = table.get((length, code))
        if sym is not None:
            return sym
    raise JpegError("bad huffman code")


def _extend(v: int, t: int) -> int:
    return v - (1 << t) + 1 if t and v < (1 << (t - 1)) else v


def dc_blocks(data: bytes):
    """Returns (components, width, height): per component a dict with h, v, bw, bh and a 2-D list
    of dequantised DC values (block averages * 8, level-shifted by -1024)."""
    if data[:2] != b"\xff\xd8":
        raise JpegError("not a JPEG")
    qt: Dict[int, List[int]] = {}
    hts: Dict[Tuple[int, int], dict] = {}
    comps: List[dict] = []
    progressive, restart, width, height = False, 0, 0, 0
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise JpegError("marker expected")
        m = data[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if m == 0xD9:
            break
        seg_len = (data[pos + 2] << 8) | data[pos + 3]
        seg = data[pos + 4:pos + 2 + seg_len]
        pos += 2 + seg_len
        if m == 0xDB:                                   # quantisation tables
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                if pq:
                    qt[tq] = [(seg[i + 1 + 2 * k] << 8) | seg[i + 2 + 2 * k] for k in range(64)]
                    i += 129
                else:
                    qt[tq] = list(seg[i + 1:i + 65])
                    i += 65
        elif m == 0xC4:                                 # huffman tables
            i = 0
            while i < len(seg):
                tc, th = seg[i] >> 4, seg[i] & 15
                counts = seg[i + 1:i + 17]
                n = sum(counts)
                hts[(tc, th)] = _huff_table(counts, seg[i + 17:i + 17 + n])
                i += 17 + n
        elif m in (0xC0, 0xC1, 0xC2):                   # frame header
            progressive = m == 0xC2
            height, width = (seg[1] << 8) | seg[2], (seg[3] << 8) | seg[4]
            for k in range(seg[5]):
                cid, hv, tq = seg[6 + 3 * k], seg[7 + 3 * k], seg[8 + 3 * k]
                comps.append({"id": cid, "h": hv >> 4, "v": hv & 15, "tq": tq})
        elif 0xC3 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            raise JpegError("unsupported JPEG type (lossless/arithmetic)")
        elif m == 0xDD:
            restart = (seg[0] << 8) | seg[1]
        elif m == 0xDA:                                 # start of scan
            if not comps or not width or not height:
                raise JpegError("scan before frame")
            done = _scan(data, pos, seg, comps, hts, qt, width, height, progressive, restart)
            if done:
                return comps, width, height
            # progressive AC / refinement scan: skip its entropy data up to the next real marker
            while pos + 1 < len(data):
                if data[pos] == 0xFF and data[pos + 1] != 0x00 and not 0xD0 <= data[pos + 1] <= 0xD7:
                    break
                pos += 1
    raise JpegError("no DC scan found")


def _scan(data, pos, seg, comps, hts, qt, width, height, progressive, restart) -> bool:
    ns = seg[0]
    by_id = {c["id"]: c for c in comps}
    scomps = []
    for k in range(ns):
        c = by_id.get(seg[1 + 2 * k])
        if c is None:
            raise JpegError("unknown component in scan")
        c["td"], c["ta"] = seg[2 + 2 * k] >> 4, seg[2 + 2 * k] & 15
        scomps.append(c)
    ss, se, ah, al = seg[1 + 2 * ns], seg[2 + 2 * ns], seg[3 + 2 * ns] >> 4, seg[3 + 2 * ns] & 15
    if progressive and (ss != 0 or ah != 0):
        return False                                    # not the first DC scan: skip
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    mcux = (width + 8 * hmax - 1) // (8 * hmax)
    mcuy = (height + 8 * vmax - 1) // (8 * vmax)
    for c in comps:
        if "dc" not in c:
            c["bw"], c["bh"] = mcux * c["h"], mcuy * c["v"]
            c["dc"] = [[0] * c["bw"] for _ in range(c["bh"])]
    bits = _Bits(data, pos)
    pred = {c["id"]: 0 for c in scomps}
    single = ns == 1
    if single:
        c = scomps[0]
        # non-interleaved scan: blocks of just this component, in raster order over its own size
        cw = (width * c["h"] + 8 * hmax - 1) // (8 * hmax)
        ch = (height * c["v"] + 8 * vmax - 1) // (8 * vmax)
        units = [(c, x, y) for y in range(ch) for x in range(cw)]
        groups = [[u] for u in units]
    else:
        groups = []
        for my in range(mcuy):
            for mx in range(mcux):
                g = []
                for c in scomps:
                    for v in range(c["v"]):
                        for h in range(c["h"]):
                            g.append((c, mx * c["h"] + h, my * c["v"] + v))
                groups.append(g)
    full = not progressive and se > 0
    for n, g in enumerate(groups):
        if restart and n and n % restart == 0:
            bits.reset()
            for k in pred:
                pred[k] = 0
        for c, x, y in g:
            dct = hts.get((0, c["td"]))
            if dct is None:
                raise JpegError("missing DC table")
            t = _decode(bits, dct)
            diff = _extend(bits.bits(t), t) if t else 0
            pred[c["id"]] += diff
            c["dc"][y][x] = (pred[c["id"]] << al) * qt.get(c["tq"], [1])[0]
            if full:                                    # baseline: AC coefficients follow, skip them
                act = hts.get((1, c["ta"]))
                if act is None:
                    raise JpegError("missing AC table")
                k = 1
                while k < 64:
                    rs = _decode(bits, act)
                    r, s = rs >> 4, rs & 15
                    if s == 0:
                        if r != 15:
                            break                       # end of block
                        k += 16
                        continue
                    bits.bits(s)
                    k += r + 1
    if progressive:                                     # DC may come in one scan per component
        for c in scomps:
            c["seen"] = True
        return all(c.get("seen") for c in comps)
    return True


def dc_pixels(data: bytes) -> List[Tuple[float, float, float]]:
    """Average colour of every 8x8 block (in the first component's block grid), as RGB 0..255."""
    comps, width, height = dc_blocks(data)
    y = comps[0]
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    # visible blocks of the first component only (padding blocks at the edge are skipped)
    yw = max(1, (width * y["h"] + 8 * hmax - 1) // (8 * hmax))
    yh = max(1, (height * y["v"] + 8 * vmax - 1) // (8 * vmax))
    out = []
    for by in range(yh):
        for bx in range(yw):
            vals = []
            for c in comps:
                cx = min(c["bw"] - 1, bx * c["h"] // y["h"])
                cy = min(c["bh"] - 1, by * c["v"] // y["v"])
                vals.append(c["dc"][cy][cx] / 8.0)       # DC = 8 * mean (level-shifted)
            if len(comps) >= 3:
                Y, cb, cr = vals[0] + 128, vals[1], vals[2]
                r = Y + 1.402 * cr
                g = Y - 0.344136 * cb - 0.714136 * cr
                b = Y + 1.772 * cb
            else:
                r = g = b = vals[0] + 128
            out.append(tuple(max(0.0, min(255.0, v)) for v in (r, g, b)))
    return out


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        px = dc_pixels(f.read())
    n = len(px)
    print(n, "blocks; mean", tuple(round(sum(p[i] for p in px) / n) for i in range(3)))

"""
CivicMind — lays the FICTIONAL demo utility networks along REAL street centre-lines.

The streets come from an OpenStreetMap extract bundled in data/osm/kothrud_streets.json
(© OpenStreetMap contributors, ODbL). Every utility record built here is synthetic:
the networks follow the streets because almost all buried urban utilities run
under the road reservation, each at its own typical position across the road:

    storm drain | electricity | water | sewer (centre) | gas | telecom
     road edge                                                 other edge

Everything is deterministic: the same extract always yields the same records, IDs
and connections. Water/gas/electricity are grown as supply trees from their
sources (loops become "unknown direction" links), sewer/storm as gravity trees
towards an outfall, telecom as undirected links.
"""
import heapq
import json
import math
import os
import zlib
from collections import defaultdict

import config

EXTRACT_PATH = os.path.join(config.PROJECT_ROOT, "data", "osm", "kothrud_streets.json")
RANK = {"trunk": 4, "primary": 4, "secondary": 3, "tertiary": 2, "unclassified": 1, "residential": 1,
        "living_street": 1}
WIDTH = {1: 0.85, 2: 1.0, 3: 1.3, 4: 1.6}           # wider roads -> utilities further from the centre-line
CLIP = (18.4975, 73.8015, 18.5105, 73.8185)        # keep street segments whose middle lies inside
OFFSET = {"storm": 6.5, "electric": 4.5, "water": 2.5, "sewer": 0.0, "gas": -2.5, "telecom": -4.5}

_LAT0, _LNG0, _K = 18.504, 73.81, 111_320.0
_KX = _K * math.cos(math.radians(_LAT0))


def to_xy(lat, lng):
    return ((lng - _LNG0) * _KX, (lat - _LAT0) * _K)


def to_ll(x, y):
    return (round(y / _K + _LAT0, 7), round(x / _KX + _LNG0, 7))


def h(*parts):
    """Stable pseudo-random number for deterministic choices."""
    return zlib.crc32("|".join(map(str, parts)).encode())


def length(xy):
    return sum(math.dist(a, b) for a, b in zip(xy, xy[1:]))


def point_at(xy, d):
    d = max(0.0, d)
    for a, b in zip(xy, xy[1:]):
        seg = math.dist(a, b)
        if d <= seg and seg > 0:
            t = d / seg
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        d -= seg
    return xy[-1]


def cut(xy, d0, d1):
    """Sub-polyline between distances d0 < d1 along xy."""
    out, run = [point_at(xy, d0)], 0.0
    for a, b in zip(xy, xy[1:]):
        run += math.dist(a, b)
        if d0 < run < d1:
            out.append(b)
    out.append(point_at(xy, d1))
    return [p for i, p in enumerate(out) if i == 0 or math.dist(p, out[i - 1]) > 0.05]


def offset_line(xy, d):
    """Offset a polyline `d` metres to its left (mitred, limited at sharp bends)."""
    if abs(d) < 1e-9:
        return list(xy)
    norms = []
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        L = math.hypot(x2 - x1, y2 - y1) or 1.0
        norms.append((-(y2 - y1) / L, (x2 - x1) / L))
    out = []
    for i, (x, y) in enumerate(xy):
        if i == 0:
            nx, ny, s = *norms[0], 1.0
        elif i == len(xy) - 1:
            nx, ny, s = *norms[-1], 1.0
        else:
            ax, ay = norms[i - 1]
            bx, by = norms[i]
            mx, my = ax + bx, ay + by
            m = math.hypot(mx, my)
            if m < 1e-6:
                nx, ny, s = bx, by, 1.0
            else:
                nx, ny = mx / m, my / m
                s = 1.0 / max(nx * bx + ny * by, 0.5)
        out.append((x + nx * d * s, y + ny * d * s))
    return out


def seg_distance(p, xy):
    """(distance, along) of point p (x, y) to polyline xy."""
    best, along, run = 1e18, 0.0, 0.0
    for a, b in zip(xy, xy[1:]):
        bx, by = b[0] - a[0], b[1] - a[1]
        L2 = bx * bx + by * by
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * bx + (p[1] - a[1]) * by) / L2))
        q = (a[0] + bx * t, a[1] + by * t)
        dd = math.dist(p, q)
        if dd < best:
            best, along = dd, run + math.sqrt(L2) * t
        run += math.sqrt(L2)
    return best, along


class Streets:
    """Intersections (nodes) and the street segments between them."""

    def __init__(self, path=EXTRACT_PATH):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.attribution = d["attribution"]
        pts = {int(k): to_xy(*v) for k, v in d["nodes"].items()}
        adj = defaultdict(dict)
        for w in d["ways"]:
            r, name = RANK.get(w["class"], 1), w.get("name")
            for u, v in zip(w["nodes"], w["nodes"][1:]):
                if u == v or u not in pts or v not in pts:
                    continue
                old = adj[u].get(v)
                adj[u][v] = adj[v][u] = (max(r, old[0]) if old else r, name or (old[1] if old else None))
        ends = {n for n in adj if len(adj[n]) != 2}
        segs, seen = [], set()
        for s in sorted(ends):
            for nb in sorted(adj[s]):
                if (s, nb) in seen:
                    continue
                path, prev, cur = [s, nb], s, nb
                seen.update({(s, nb), (nb, s)})
                while cur not in ends:
                    nxt = next(x for x in sorted(adj[cur]) if x != prev)
                    seen.update({(cur, nxt), (nxt, cur)})
                    path.append(nxt)
                    prev, cur = cur, nxt
                if path[0] == path[-1]:
                    continue
                xy = [pts[n] for n in path]
                mid = point_at(xy, length(xy) / 2)
                lat, lng = to_ll(*mid)
                if not (CLIP[0] <= lat <= CLIP[2] and CLIP[1] <= lng <= CLIP[3]) or length(xy) < 3:
                    continue
                names = sorted(adj[u][v][1] for u, v in zip(path, path[1:]) if adj[u][v][1])
                segs.append({"a": path[0], "b": path[-1], "xy": xy, "len": length(xy),
                             "rank": max(adj[u][v][0] for u, v in zip(path, path[1:])),
                             "name": max(names, key=names.count) if names else None})
        self.segs = segs
        self.node_xy = {}
        self.node_segs = defaultdict(list)
        for i, s in enumerate(segs):
            for n in (s["a"], s["b"]):
                self.node_segs[n].append(i)
                self.node_xy[n] = pts[n]
            dx, dy = s["xy"][-1][0] - s["xy"][0][0], s["xy"][-1][1] - s["xy"][0][1]
            s["side"] = 1 if dx > 1e-9 or (abs(dx) <= 1e-9 and dy > 0) else -1
        self.node_rank = {n: max(segs[i]["rank"] for i in ix) for n, ix in self.node_segs.items()}
        self._anchor = {}

    # ---------------------------------------------------------------- helpers
    def other(self, i, n):
        s = self.segs[i]
        return s["b"] if s["a"] == n else s["a"]

    def oriented(self, i, start):
        """Segment points ordered from node `start`."""
        s = self.segs[i]
        return s["xy"] if s["a"] == start else s["xy"][::-1]

    def degree(self, n):
        return len(self.node_segs[n])

    def _end_normal(self, i, n):
        s = self.segs[i]
        xy = s["xy"]
        (x1, y1), (x2, y2) = (xy[0], xy[1]) if s["a"] == n else (xy[-2], xy[-1])
        L = math.hypot(x2 - x1, y2 - y1) or 1.0
        return (-(y2 - y1) / L * s["side"], (x2 - x1) / L * s["side"])

    def anchor(self, n, ut):
        """Where utility `ut` crosses intersection `n` (all its lines meet here)."""
        key = (n, ut)
        if key not in self._anchor:
            d = OFFSET[ut] * WIDTH[self.node_rank[n]]
            nx = sum(self._end_normal(i, n)[0] for i in self.node_segs[n])
            ny = sum(self._end_normal(i, n)[1] for i in self.node_segs[n])
            m = math.hypot(nx, ny)
            if m < 0.25:
                nx, ny = self._end_normal(self.node_segs[n][0], n)
                m = 1.0
            x, y = self.node_xy[n]
            self._anchor[key] = (x + nx / m * d, y + ny / m * d)
        return self._anchor[key]

    def line(self, i, ut, start=None, extra=0.0):
        """Offset geometry (x, y) of segment i for utility ut, optionally ordered from node `start`."""
        s = self.segs[i]
        d = (OFFSET[ut] * WIDTH[s["rank"]] + extra) * s["side"]
        xy = offset_line(s["xy"], d)
        if not extra:
            xy = [self.anchor(s["a"], ut)] + xy[1:-1] + [self.anchor(s["b"], ut)]
        if start is not None and start != s["a"]:
            xy = xy[::-1]
        return xy

    def nearest_node(self, lat, lng, ok=lambda n: True):
        p = to_xy(lat, lng)
        return min((n for n in sorted(self.node_xy) if ok(n)), key=lambda n: math.dist(p, self.node_xy[n]))

    def nearest_seg(self, lat, lng, ok=lambda i: True):
        p = to_xy(lat, lng)
        best = min((seg_distance(p, s["xy"])[0], i) for i, s in enumerate(self.segs) if ok(i))
        return best[1]

    def grow(self, roots, include, weight=None):
        """Multi-source Dijkstra over included segments -> (dist, parent segment)."""
        weight = weight or (lambda i: self.segs[i]["len"])
        dist, parent = {r: 0.0 for r in roots}, {r: None for r in roots}
        pq = [(0.0, r) for r in sorted(roots)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for i in self.node_segs[u]:
                if not include(i):
                    continue
                v, nd = self.other(i, u), d + weight(i)
                if nd < dist.get(v, 1e18) - 1e-9:
                    dist[v], parent[v] = nd, i
                    heapq.heappush(pq, (nd, v))
        return dist, parent

    def path(self, parent, n):
        """Segments from the root to node n (root first) and the nodes passed."""
        segs, nodes = [], [n]
        while parent.get(n) is not None:
            i = parent[n]
            segs.append(i)
            n = self.other(i, n)
            nodes.append(n)
        return segs[::-1], nodes[::-1]

    def label(self, i):
        s = self.segs[i]
        if s["name"]:
            return s["name"]
        for n in (s["a"], s["b"]):
            for j in self.node_segs[n]:
                if self.segs[j]["name"]:
                    return f"lane off {self.segs[j]['name']}"
        return "local lane"


def geo_line(xy):
    return {"type": "LineString", "coordinates": [[ll[1], ll[0]] for ll in (to_ll(*p) for p in xy)]}


def geo_point(p):
    lat, lng = to_ll(*p)
    return {"type": "Point", "coordinates": [lng, lat]}


def join(parts):
    out = []
    for xy in parts:
        for p in xy:
            if not out or math.dist(p, out[-1]) > 0.05:
                out.append(p)
    return out


# ============================================================================ generic network builder
class Net:
    """Collects assets and connections for one utility."""

    def __init__(self, ut, legacy_conn):
        self.ut, self.assets, self.conns, self.legacy = ut, [], [], legacy_conn
        self.count = defaultdict(int)
        self._pairs = set()

    def new_id(self, prefix):
        self.count[prefix] += 1
        return f"{prefix}-{self.count[prefix]:04d}"

    def asset(self, aid, at, name, geom, **extra):
        self.assets.append((aid, self.ut, at, name, geom, extra))
        return aid

    def link(self, f, t, ctype, direction, **extra):
        """Record one connection once. Core-scenario pairs keep their original connection IDs."""
        key = frozenset((f, t))
        if f == t or key in self._pairs:
            return
        self._pairs.add(key)
        cid = self.legacy.get((f, t))
        if cid is None and direction != "downstream" and (t, f) in self.legacy:
            cid, f, t = self.legacy[(t, f)], t, f     # keep the recorded orientation of an undirected link
        cid = cid or f"UC-{self.ut[:2].upper()}-{len(self.conns) + 1:05d}"
        self.conns.append((cid, f, t, ctype, direction, extra))


def _quality(ut, key):
    """Deterministic data-quality mix: most records verified, some not, a few approximate."""
    r = h(ut, key) % 100
    if r < 4:
        return {"verification_status": "unverified", "provenance": "approximate_unverified"}
    if r < 13:
        return {"verification_status": "unverified"}
    return {}


def grow_network(st, net, *, prefix, include, roots, root_devices, flow, line_spec, node_device=None,
                 start_device=None, spur=None, split=None, overrides=None, weight=None):
    """Lay one network along the included street segments.

    flow: 'supply' (away from the roots), 'gravity' (towards the roots) or 'none' (undirected links).
    line_spec(i) -> (asset_type, name, extra)
    node_device(n) -> (id, type, name, extra) | None    — device at an intersection (manhole, RMU, cabinet…)
    start_device(i) -> (id, type, name, extra) | None   — device at the upstream start of a supply line (valve)
    spur(i) -> (id, type, name, extra) | None           — device hanging off a line (hydrant, transformer, inlet)
    split(i) -> bool                                    — put a manhole in the middle of a long gravity line
    overrides: {segment: (id, name)} fixed IDs for the core demo scenario (never split, no start device)
    """
    overrides = overrides or {}
    ut = net.ut
    ctype = {"supply": "feeds", "gravity": "drains_to", "none": "links"}[flow]
    direction = "bidirectional" if flow == "none" else "downstream"
    dist, parent = st.grow(roots, include, weight)
    tree = {i for i in parent.values() if i is not None}

    devices = {}
    for r in roots:
        rid, rtype, rname, rextra = root_devices[r]
        devices[r] = net.asset(rid, rtype, rname, geo_point(st.anchor(r, ut)), **rextra)
    if node_device:
        for n in sorted(dist):
            if n not in devices:
                dev = node_device(n)
                if dev:
                    devices[n] = net.asset(dev[0], dev[1], dev[2], geo_point(st.anchor(n, ut)), **dev[3])

    first, last, start_of = {}, {}, {}
    for i in range(len(st.segs)):
        s = st.segs[i]
        if not include(i) or s["a"] not in dist or s["b"] not in dist:
            continue
        near, far = (s["a"], s["b"]) if dist[s["a"]] <= dist[s["b"]] else (s["b"], s["a"])
        start = far if flow == "gravity" else near
        xy = st.line(i, ut, start=start)
        atype, name, extra = line_spec(i)
        extra = {**_quality(ut, i), **extra}
        chain = []
        if i in overrides:
            chain.append(net.asset(overrides[i][0], atype, overrides[i][1], geo_line(xy), **extra))
        else:
            pre = start_device(i) if (start_device and flow == "supply" and i in tree) else None
            if pre:
                at = min(6.0, s["len"] / 3)
                chain.append(net.asset(pre[0], pre[1], pre[2], geo_point(point_at(xy, at)), **pre[3]))
                xy = cut(xy, at, length(xy))
            if split and split(i) and s["len"] > 40:
                mid = length(xy) / 2
                chain += [net.asset(net.new_id(prefix), atype, name, geo_line(cut(xy, 0, mid)), **extra),
                          net.asset(net.new_id("MH"), "manhole", f"Manhole, {st.label(i)} (fictional)",
                                    geo_point(point_at(xy, mid)), **_quality(ut, ("mh", i))),
                          net.asset(net.new_id(prefix), atype, name, geo_line(cut(xy, mid, length(xy))), **extra)]
            else:
                chain.append(net.asset(net.new_id(prefix), atype, name, geo_line(xy), **extra))
        for x, y in zip(chain, chain[1:]):
            net.link(x, y, ctype, direction)
        first[i], last[i], start_of[i] = chain[0], chain[-1], start
        sp = spur(i) if (spur and i not in overrides) else None
        if sp:
            spot = point_at(xy, length(xy) / 2)
            net.asset(sp[0], sp[1], sp[2], geo_point(spot), **sp[3])
            if flow == "gravity":
                net.link(sp[0], chain[-1], ctype, direction)      # an inlet drains into the line
            else:
                net.link(chain[-1], sp[0], ctype, direction)

    def end_at(i, n):
        return first[i] if start_of[i] == n else last[i]

    for n in sorted(dist):
        dev, tin = devices.get(n), parent.get(n)
        tin = tin if tin in first else None
        segs = [i for i in st.node_segs[n] if i in first]
        if flow == "supply":
            if dev and tin is not None:
                net.link(last[tin], dev, ctype, direction)
            up = dev or (last[tin] if tin is not None else None)
            if not up:
                continue
            for i in segs:
                if i == tin:
                    continue
                if i in tree and parent.get(st.other(i, n)) == i:
                    net.link(up, first[i], ctype, direction)
                else:
                    net.link(up, end_at(i, n), "links", "unknown", notes="Loop; flow direction not recorded")
        elif flow == "gravity":
            if dev and tin is not None:
                net.link(dev, first[tin], ctype, direction)
            down = dev or (first[tin] if tin is not None else None)
            if not down:
                continue
            for i in segs:
                if i != tin and start_of[i] != n:          # the line ends here, so it drains into this node
                    net.link(last[i], down, ctype, direction)
        else:
            hub = dev or (end_at(tin, n) if tin is not None else None)
            if not hub:
                continue
            for i in segs:
                if dev or i != tin:
                    net.link(hub, end_at(i, n), ctype, direction)
    return dist, parent


# ============================================================================ the demo city
def _pick(r, *options):
    return options[r % len(options)]


def build():
    """Return (assets, connections, info) for the whole synthetic demo network."""
    st = Streets()
    S = st.segs
    nseg = len(S)
    assets, conns = [], []
    info = {"attribution": st.attribution, "segments": nseg}

    def connected(root, wanted):
        """`wanted` segments plus the street paths that join them to `root` (keeps a network in one piece)."""
        _, par = st.grow([root], lambda i: True)
        keep = set()
        for i in range(nseg):
            if wanted(i):
                keep.add(i)
                for n in (S[i]["a"], S[i]["b"]):
                    keep.update(st.path(par, n)[0])
        return keep

    # ------------------------------------------------------------- core water scenario (fixed IDs)
    old_leak = to_xy(18.50128, 73.81309)
    leak = st.nearest_seg(18.50128, 73.81309, lambda i: S[i]["rank"] == 1)
    best = None
    for j2, j3 in ((S[leak]["a"], S[leak]["b"]), (S[leak]["b"], S[leak]["a"])):
        nbrs = [i for i in st.node_segs[j3] if i != leak and st.other(i, j3) not in (j2, j3)]
        if len(nbrs) < 2:
            continue
        for s6 in nbrs:
            j4 = st.other(s6, j3)
            dist, parent = st.grow([j2], lambda i: i not in (leak, s6) and j3 not in (S[i]["a"], S[i]["b"]))
            if j4 not in dist or dist[j4] > 700:
                continue
            branch, bnodes = st.path(parent, j4)
            rest = [i for i in nbrs if i != s6 and i not in branch]
            if not rest:
                continue
            s7 = max(rest, key=lambda i: (st.degree(st.other(i, j3)) > 1, S[i]["len"]))
            score = dist[j4] + (0 if S[s7]["len"] > 30 else 500)
            if best is None or score < best[0]:
                best = (score, j2, j3, s6, j4, branch, s7)
    _, J2, J3, s6, J4, branch, s7 = best
    END = st.other(s7, J3)
    s102 = next((i for i in st.node_segs[END] if i != s7 and i not in branch and st.other(i, END) not in (J3, J4)), None)
    core = {leak, s6, s7, *branch} | ({s102} if s102 is not None else set())
    src_node = st.nearest_node(18.5048, 73.8098, lambda n: n not in (J2, J3, J4, END))
    rw = {1: 1.0, 2: 0.8, 3: 0.7, 4: 0.6}
    blocked = {J3, J4, END} | {n for i in branch for n in (S[i]["a"], S[i]["b"]) if n != J2}
    dist, parent = st.grow([src_node], lambda i: i not in core and not ({S[i]["a"], S[i]["b"]} & blocked),
                           weight=lambda i: S[i]["len"] / rw[S[i]["rank"]])
    trunk, tnodes = st.path(parent, J2)
    core |= set(trunk)
    info["core_water_segments"] = sorted(core)

    W = "water"

    def oline(i, start, extra=0.0):
        return st.line(i, W, start=start, extra=extra)

    def path_xy(seg_list, start):
        parts, n = [], start
        for i in seg_list:
            parts.append(oline(i, n))
            n = st.other(i, n)
        return join(parts)

    trunk_xy = path_xy(trunk, src_node)
    if len(trunk) >= 2:
        j1 = tnodes[-2]
        wm01 = path_xy(trunk[:-1], src_node)
        wm02_full = oline(trunk[-1], j1)
        J1 = st.anchor(j1, W)
    else:
        L_ = length(trunk_xy)
        wm01, wm02_full = cut(trunk_xy, 0, L_ * 0.5), cut(trunk_xy, L_ * 0.5, L_)
        J1 = wm01[-1]
    v1 = point_at(wm02_full, 6)
    wm02 = cut(wm02_full, 6, length(wm02_full))
    leak_xy = oline(leak, J2)
    v2 = point_at(leak_xy, 6)
    wm03 = cut(leak_xy, 6, length(leak_xy))
    _, along = seg_distance(old_leak, st.oriented(leak, J2))
    along = min(max(along, 25.0), S[leak]["len"] - 15.0)
    branch_xy = path_xy(branch, J2)
    bl = length(branch_xy)
    v3 = point_at(branch_xy, bl / 2)
    wm04, wm05 = cut(branch_xy, 0, bl / 2), cut(branch_xy, bl / 2, bl)
    wm06 = oline(s6, J3)
    s7_xy = oline(s7, J3)
    v4 = point_at(s7_xy, 6)
    wm07 = cut(s7_xy, 6, length(s7_xy))
    old_xy = st.line(s7, W, start=J3, extra=-3.5 * S[s7]["side"])
    wm101 = cut(old_xy, 10, max(length(old_xy) - 5, 12))
    if s102 is not None:
        x102 = st.line(s102, W, start=END, extra=-3.5 * S[s102]["side"])
        wm102 = cut(x102, 0, min(80.0, length(x102)))
        wm102 = [wm101[-1]] + wm102[1:] if wm102 else wm102
    else:
        a, b = wm101[-2], wm101[-1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L_ = math.hypot(dx, dy) or 1
        wm102 = [b, (b[0] - dy / L_ * 40, b[1] + dx / L_ * 40)]

    P, Ln = geo_point, geo_line
    core_assets = [
        ("SRC-DEMO-01", W, "reservoir", "Hill reservoir R-1 (fictional)", P(st.anchor(src_node, W)), {"criticality": "critical"}),
        ("WM-DEMO-01", W, "main", f"Trunk main from reservoir, {st.label(trunk[0])} (fictional)", Ln(wm01),
         {"criticality": "critical", "attributes": {"diameter_mm": 450, "material": "ductile iron"}}),
        ("J-DEMO-01", W, "junction", "Junction J-1 (fictional)", P(J1), {}),
        ("V-DEMO-01", W, "valve", "Sluice valve V-1 (fictional)", P(v1), {}),
        ("WM-DEMO-02", W, "main", f"Distribution main, {st.label(trunk[-1])} (fictional)", Ln(wm02),
         {"criticality": "high", "attributes": {"diameter_mm": 300}}),
        ("J-DEMO-02", W, "junction", "Junction J-2 (fictional)", P(st.anchor(J2, W)), {}),
        ("V-DEMO-02", W, "valve", "Sluice valve V-2 (fictional)", P(v2), {}),
        ("WM-DEMO-03", W, "main", f"Distribution main, Lane 7 — {st.label(leak)} (fictional)", Ln(wm03),
         {"criticality": "high", "attributes": {"diameter_mm": 200, "material": "cast iron", "install_year": 1994}}),
        ("J-DEMO-03", W, "junction", "Junction J-3 (fictional)", P(st.anchor(J3, W)), {}),
        ("WM-DEMO-04", W, "branch", "Branch to east colony (fictional)", Ln(wm04), {}),
        ("V-DEMO-03", W, "valve", "Branch valve V-3 (fictional)", P(v3), {"verification_status": "unverified"}),
        ("WM-DEMO-05", W, "branch", "East colony branch (fictional)", Ln(wm05), {}),
        ("J-DEMO-04", W, "junction", "Junction J-4 (fictional)", P(st.anchor(J4, W)), {}),
        ("WM-DEMO-06", W, "branch", "Loop link J-3 to J-4 (fictional)", Ln(wm06),
         {"verification_status": "unverified", "provenance": "approximate_unverified"}),
        ("V-DEMO-04", W, "valve", "Sluice valve V-4 (fictional)", P(v4), {}),
        ("WM-DEMO-07", W, "branch", f"Branch along {st.label(s7)} (fictional)", Ln(wm07), {}),
        ("END-DEMO-01", W, "endpoint", "Service area endpoint (fictional)", P(st.anchor(END, W)), {}),
        ("WM-DEMO-101", W, "main", "Old main, unrecorded feed (fictional)", Ln(wm101),
         {"verification_status": "unverified", "provenance": "approximate_unverified"}),
        ("WM-DEMO-102", W, "branch", "Branch off old main (fictional)", Ln(wm102),
         {"verification_status": "unverified", "provenance": "approximate_unverified"}),
    ]
    core_conns = [
        ("UC-DEMO-01", "SRC-DEMO-01", "WM-DEMO-01", "feeds", "downstream", {}),
        ("UC-DEMO-02", "WM-DEMO-01", "J-DEMO-01", "feeds", "downstream", {}),
        ("UC-DEMO-03", "J-DEMO-01", "V-DEMO-01", "feeds", "downstream", {}),
        ("UC-DEMO-04", "V-DEMO-01", "WM-DEMO-02", "feeds", "downstream", {}),
        ("UC-DEMO-05", "WM-DEMO-02", "J-DEMO-02", "feeds", "downstream", {"verification_status": "unverified"}),
        ("UC-DEMO-06", "J-DEMO-02", "V-DEMO-02", "feeds", "downstream", {}),
        ("UC-DEMO-07", "V-DEMO-02", "WM-DEMO-03", "feeds", "downstream", {}),
        ("UC-DEMO-08", "WM-DEMO-03", "J-DEMO-03", "feeds", "downstream", {}),
        ("UC-DEMO-09", "J-DEMO-02", "WM-DEMO-04", "feeds", "downstream", {}),
        ("UC-DEMO-10", "WM-DEMO-04", "V-DEMO-03", "feeds", "downstream", {}),
        ("UC-DEMO-11", "V-DEMO-03", "WM-DEMO-05", "feeds", "downstream", {}),
        ("UC-DEMO-12", "WM-DEMO-05", "J-DEMO-04", "feeds", "downstream", {}),
        ("UC-DEMO-13", "J-DEMO-03", "WM-DEMO-06", "links", "unknown", {"verification_status": "unverified",
                                                                         "notes": "Loop; flow direction not recorded"}),
        ("UC-DEMO-14", "WM-DEMO-06", "J-DEMO-04", "links", "unknown", {"verification_status": "unverified"}),
        ("UC-DEMO-15", "J-DEMO-03", "V-DEMO-04", "feeds", "downstream", {}),
        ("UC-DEMO-16", "V-DEMO-04", "WM-DEMO-07", "feeds", "downstream", {}),
        ("UC-DEMO-17", "WM-DEMO-07", "END-DEMO-01", "feeds", "downstream", {}),
        ("UC-DEMO-18", "WM-DEMO-101", "WM-DEMO-102", "feeds", "downstream", {"verification_status": "unverified"}),
    ]
    assets += core_assets
    conns += core_conns
    leak_line = st.oriented(leak, J2)
    info["leak_point"] = to_ll(*point_at(leak_line, along))
    info["leak_reports"] = [to_ll(*point_at(leak_line, along + d)) for d in (-6.0, 5.0, -1.0)]

    legacy = {("SUB-DEMO-01", "CB-DEMO-01"): "UC-DEMO-21", ("CB-DEMO-01", "SW-DEMO-01"): "UC-DEMO-22",
              ("SW-DEMO-01", "CB-DEMO-02"): "UC-DEMO-23", ("CB-DEMO-02", "TR-DEMO-01"): "UC-DEMO-24",
              ("SWR-DEMO-01", "MH-DEMO-01"): "UC-DEMO-31", ("MH-DEMO-01", "SWR-DEMO-02"): "UC-DEMO-32",
              ("SWR-DEMO-02", "OUT-DEMO-01"): "UC-DEMO-33", ("GV-DEMO-01", "GM-DEMO-01"): "UC-DEMO-41",
              ("FB-DEMO-01", "CAB-DEMO-01"): "UC-DEMO-51"}

    def fic(text):
        return f"{text} (fictional)"

    # ------------------------------------------------------------- water: rest of the area (separate zones)
    net = Net(W, legacy)
    w_inc = lambda i: i not in core   # noqa: E731
    esr_targets = [(18.5095, 73.8160), (18.4990, 73.8040), (18.4985, 73.8165), (18.5095, 73.8040)]
    roots = []
    for k, (la, lo) in enumerate(esr_targets):
        n = st.nearest_node(la, lo, lambda n: any(w_inc(i) for i in st.node_segs[n]) and n not in roots
                            and n not in (src_node, J2, J3, J4, END))
        roots.append(n)
    root_dev = {n: (f"ESR-D{k + 2}", "reservoir", fic(f"Elevated service reservoir ESR-{k + 2}, {st.label(st.node_segs[n][0])}"),
                    {"criticality": "critical", "attributes": {"capacity": f"{_pick(h('esr', k), 9, 12, 15)} lakh litres"}})
                for k, n in enumerate(roots)}

    def water_line(i):
        s, r = S[i], h("w", i)
        if s["rank"] >= 3:
            return "main", fic(f"Water main, {st.label(i)}"), {"criticality": "high", "attributes": {
                "diameter_mm": _pick(r, 300, 350, 400), "material": "ductile iron", "install_year": 1990 + r % 25}}
        if s["rank"] == 2:
            return "main", fic(f"Distribution main, {st.label(i)}"), {"criticality": "medium", "attributes": {
                "diameter_mm": _pick(r, 200, 250), "material": _pick(r // 7, "ductile iron", "cast iron"),
                "install_year": 1982 + r % 34}}
        return "branch", fic(f"Distribution pipe, {st.label(i)}"), {"criticality": "low", "attributes": {
            "diameter_mm": _pick(r, 100, 150, 100), "material": _pick(r // 5, "HDPE", "cast iron", "DI", "HDPE"),
            "install_year": 1978 + r % 44}}

    def water_valve(i):
        s = S[i]
        if s["rank"] >= 2 or h("wv", i) % 2 == 0:
            return net.new_id("WV"), "valve", fic(f"Sluice valve, {st.label(i)}"), _quality(W, ("wv", i))
        return None

    def hydrant(i):
        if S[i]["len"] > 90 and h("hy", i) % 3 == 0:
            return net.new_id("HY"), "hydrant", fic(f"Fire hydrant, {st.label(i)}"), _quality(W, ("hy", i))
        return None

    grow_network(st, net, prefix="WP", include=w_inc, roots=roots, root_devices=root_dev, flow="supply",
                 line_spec=water_line, start_device=water_valve, spur=hydrant)
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- sewer: gravity towards one outfall
    net = Net("sewer", legacy)
    out_node = st.nearest_node(18.5015, 73.8150)
    mh_seg = min(st.node_segs[out_node], key=lambda i: math.dist(st.node_xy[st.other(i, out_node)], to_xy(18.503, 73.8125)))
    mh_node = st.other(mh_seg, out_node)
    s01 = min((i for i in st.node_segs[mh_node] if i != mh_seg),
              key=lambda i: math.dist(st.node_xy[st.other(i, mh_node)], to_xy(18.504, 73.811)))
    forced = {mh_seg, s01}

    def sewer_line(i):
        r = h("s", i)
        if S[i]["rank"] >= 3 or i == mh_seg:
            return "trunk_sewer", fic(f"Trunk sewer, {st.label(i)}"), {"criticality": "high", "attributes": {
                "diameter_mm": _pick(r, 600, 750, 900), "material": "RCC", "depth_m": round(2.5 + r % 20 / 10, 1)}}
        return "sewer_line", fic(f"Sewer line, {st.label(i)}"), {"attributes": {
            "diameter_mm": _pick(r, 200, 250, 300), "material": _pick(r // 3, "stoneware", "PVC", "RCC"),
            "depth_m": round(1.4 + r % 15 / 10, 1)}}

    def manhole(n):
        if n == mh_node:
            return "MH-DEMO-01", "manhole", "Manhole MH-1 (fictional)", {}
        return net.new_id("MH"), "manhole", fic(f"Manhole, {st.label(st.node_segs[n][0])}"), _quality("sewer", ("n", n))

    grow_network(st, net, prefix="SEW", include=lambda i: True, roots=[out_node], flow="gravity",
                 root_devices={out_node: ("OUT-DEMO-01", "outfall", "Outfall to treatment (fictional)",
                                          {"criticality": "critical"})},
                 line_spec=sewer_line, node_device=manhole, split=lambda i: S[i]["len"] > 110,
                 overrides={s01: ("SWR-DEMO-01", "Sewer line, Lane 4 (fictional)"),
                            mh_seg: ("SWR-DEMO-02", "Trunk sewer to outfall (fictional)")},
                 weight=lambda i: 0.01 if i in forced else S[i]["len"])
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- stormwater: main roads + some lanes
    net = Net("storm", legacy)
    sto_root = st.nearest_node(18.4985, 73.8175, lambda n: st.node_rank[n] >= 2)
    storm_set = connected(sto_root, lambda i: S[i]["rank"] >= 2 or h("sd", i) % 3 == 0)
    storm_inc = storm_set.__contains__

    def storm_line(i):
        r = h("sd", i)
        return "storm_drain", fic(f"Roadside storm drain, {st.label(i)}"), {"attributes": {
            "diameter_mm": _pick(r, 450, 600, 900) if S[i]["rank"] >= 2 else _pick(r, 300, 450),
            "material": _pick(r // 3, "RCC", "masonry")}}

    def inlet(i):
        if h("si", i) % 2 == 0:
            return net.new_id("SI"), "inlet", fic(f"Storm inlet, {st.label(i)}"), _quality("storm", ("si", i))
        return None

    grow_network(st, net, prefix="SD", include=storm_inc, roots=[sto_root], flow="gravity",
                 root_devices={sto_root: ("SO-D1", "outfall", fic("Storm outfall to nallah"), {"criticality": "high"})},
                 line_spec=storm_line, spur=inlet)
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- electricity: substation, RMUs, transformers
    net = Net("electric", legacy)
    tr_node = st.nearest_node(18.5062, 73.8081, lambda n: st.degree(n) >= 2)
    best = None
    for cb2 in st.node_segs[tr_node]:
        sw = st.other(cb2, tr_node)
        for cb1 in st.node_segs[sw]:
            sub = st.other(cb1, sw)
            if cb1 == cb2 or sub == tr_node:
                continue
            score = -S[cb1]["rank"] * 100 + S[cb2]["len"]
            if best is None or score < best[0]:
                best = (score, cb1, sw, cb2, sub)
    _, cb1, sw_node, cb2, sub_node = best
    sub2 = st.nearest_node(18.4990, 73.8160, lambda n: st.node_rank[n] >= 2)
    e_forced = {cb1, cb2}

    def cable(i):
        r = h("e", i)
        if S[i]["rank"] >= 2:
            return "cable", fic(f"11 kV cable, {st.label(i)}"), {"criticality": "high" if S[i]["rank"] >= 3 else "medium",
                                                                  "attributes": {"voltage_kv": 11, "cable_type": "XLPE 3-core"}}
        return "cable", fic(f"LT cable, {st.label(i)}"), {"attributes": {"voltage_kv": 0.433,
                                                                         "cable_type": _pick(r, "XLPE 4-core", "PVC 4-core")}}

    def e_node(n):
        if n == sw_node:
            return "SW-DEMO-01", "ring_main_unit", "Ring main unit RMU-1 (fictional)", {}
        if n == tr_node:
            return "TR-DEMO-01", "transformer", "Feeder pillar FP-1 (fictional)", {}
        if st.node_rank[n] >= 2 and st.degree(n) >= 3 and h("rmu", n) % 3 == 0:
            return net.new_id("RMU"), "ring_main_unit", fic(f"Ring main unit, {st.label(st.node_segs[n][0])}"), {}
        return None

    def transformer(i):
        if S[i]["rank"] == 1 and S[i]["len"] > 60 and h("dt", i) % 3 == 0:
            return (net.new_id("DT"), "transformer", fic(f"Distribution transformer, {st.label(i)}"),
                    {"attributes": {"capacity": f"{_pick(h('kva', i), 250, 315, 400, 630)} kVA"}})
        return None

    grow_network(st, net, prefix="EC", include=lambda i: True, roots=[sub_node, sub2], flow="supply",
                 root_devices={sub_node: ("SUB-DEMO-01", "substation", "Substation S-1 (fictional)", {"criticality": "critical"}),
                               sub2: ("SUB-D2", "substation", fic(f"22/11 kV substation S-2, {st.label(st.node_segs[sub2][0])}"),
                                      {"criticality": "critical"})},
                 line_spec=cable, node_device=e_node, spur=transformer,
                 overrides={cb1: ("CB-DEMO-01", "11 kV cable S-1 to switch (fictional)"),
                            cb2: ("CB-DEMO-02", "LT feeder to streetlights (fictional)")},
                 weight=lambda i: 0.01 if i in e_forced else S[i]["len"])
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- gas: city gate, main roads + some lanes
    net = Net("gas", legacy)
    gate = st.nearest_node(18.5098, 73.8030, lambda n: st.node_rank[n] >= 2)
    gas_set = connected(gate, lambda i: S[i]["rank"] >= 2 or h("g", i) % 2 == 0)
    gas_inc = gas_set.__contains__
    gd, gp = st.grow([gate], gas_inc)
    gv_node = st.nearest_node(18.5000, 73.8090, lambda n: n in gd and any(
        gp.get(st.other(i, n)) == i for i in st.node_segs[n] if gas_inc(i)))
    gm01 = next(i for i in sorted(st.node_segs[gv_node]) if gas_inc(i) and gp.get(st.other(i, gv_node)) == i)

    def gas_line(i):
        r = h("gl", i)
        if S[i]["rank"] >= 2:
            return "gas_main", fic(f"Medium-pressure gas main, {st.label(i)}"), {"criticality": "high", "attributes": {
                "diameter_mm": _pick(r, 125, 180), "material": "PE100", "pressure_class": "MP 4 bar"}}
        return "gas_service", fic(f"Low-pressure gas line, {st.label(i)}"), {"attributes": {
            "diameter_mm": _pick(r, 63, 90), "material": "PE100", "pressure_class": "LP 100 mbar"}}

    def gas_node(n):
        if n == gv_node:
            return "GV-DEMO-01", "valve", "Gas isolation valve GV-1 (fictional)", {}
        if st.degree(n) >= 3 and h("gv", n) % 3 == 0:
            return net.new_id("GV"), "valve", fic(f"Gas isolation valve, {st.label(st.node_segs[n][0])}"), {}
        if st.node_rank[n] >= 2 and h("drs", n) % 11 == 0:
            return net.new_id("DRS"), "regulator", fic(f"District regulating station, {st.label(st.node_segs[n][0])}"), {}
        return None

    grow_network(st, net, prefix="GP", include=gas_inc, roots=[gate], flow="supply",
                 root_devices={gate: ("CGS-D1", "city_gate", fic("City gate station CGS-1"), {"criticality": "critical"})},
                 line_spec=gas_line, node_device=gas_node,
                 overrides={gm01: ("GM-DEMO-01", "Medium-pressure gas main (fictional)")})
    for a in net.assets:      # gas is restricted: mark every record so the map shows it as sensitive
        a[5].setdefault("sensitivity", "restricted")
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- telecom: exchange, ducts, cabinets
    net = Net("telecom", legacy)
    fb01 = st.nearest_seg(18.5038, 73.8122, lambda i: S[i]["rank"] >= 2)
    cab_node = S[fb01]["b"]
    exch = st.nearest_node(18.5075, 73.8145, lambda n: st.node_rank[n] >= 2)
    tel_set = connected(exch, lambda i: S[i]["rank"] >= 2 or h("t", i) % 10 < 3 or i == fb01)
    tel_inc = tel_set.__contains__

    def fibre(i):
        return "fibre", fic(f"Fibre duct, {st.label(i)}"), {"attributes": {"cable_type": _pick(h("fo", i), "96F", "48F", "24F")}}

    def t_node(n):
        if n == cab_node:
            return "CAB-DEMO-01", "cabinet", "Street cabinet C-1 (fictional)", {}
        if st.degree(n) >= 3 and h("cab", n) % 4 == 0:
            return net.new_id("CAB"), "cabinet", fic(f"Street cabinet, {st.label(st.node_segs[n][0])}"), {}
        if h("ch", n) % 3 == 0:
            return net.new_id("CH"), "chamber", fic(f"Joint chamber, {st.label(st.node_segs[n][0])}"), {}
        return None

    grow_network(st, net, prefix="FO", include=tel_inc, roots=[exch], flow="none",
                 root_devices={exch: ("EXC-D1", "exchange", fic("Telephone exchange / PoP"), {"criticality": "high"})},
                 line_spec=fibre, node_device=t_node, overrides={fb01: ("FB-DEMO-01", "Fibre route F-1 (fictional)")})
    assets += net.assets
    conns += net.conns

    # ------------------------------------------------------------- sanity: unique IDs, every connection resolvable
    ids = [a[0] for a in assets]
    assert len(ids) == len(set(ids)), "duplicate asset IDs"
    known = set(ids)
    conns = [c for c in conns if c[1] in known and c[2] in known]
    cids = [c[0] for c in conns]
    assert len(cids) == len(set(cids)), "duplicate connection IDs"
    return assets, conns, info

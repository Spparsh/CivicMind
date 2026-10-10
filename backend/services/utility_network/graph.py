"""
CivicMind — deterministic network tracing over RECORDED connections only.

  * Traverses `utility_connections`; geometry is never consulted, so two lines
    that merely touch or cross on the map are not connected.
  * Direction semantics: `downstream` means flow from -> to; `bidirectional`
    links are walked both ways; `unknown` links are walked only in a "both"
    exploration and are always reported as gaps in directional traces.
  * Bounded by max_hops and max_nodes; cycles are detected and reported.
  * Candidate isolation devices are the NEAREST recorded control device on
    each traced branch (a device behind another device is not a candidate).
    They are candidates only: an authorized operator confirms the real
    control point. Nothing here operates or recommends operating equipment.
  * The result says what is missing: no reachable source, dead ends,
    unverified links, cross-utility links, truncation.
"""
from collections import deque

from services.utility_network import model, registry

MAX_HOPS_LIMIT = 25
DEFAULT_MAX_HOPS = 10
MAX_NODES = 400
DIRECTIONS = ("upstream", "downstream", "both")


class TraceError(ValueError):
    pass


def _relation(edge, pk):
    """How the OTHER end of `edge` relates to asset `pk`."""
    outgoing = edge["from_asset"] == pk
    if edge["direction"] == "downstream":
        return "downstream" if outgoing else "upstream"
    return edge["direction"]


def trace(asset_id, direction="both", max_hops=DEFAULT_MAX_HOPS, max_nodes=MAX_NODES):
    if direction not in DIRECTIONS:
        raise TraceError("direction must be upstream, downstream or both")
    try:
        max_hops = int(max_hops)
    except (TypeError, ValueError):
        raise TraceError("max_hops must be a whole number") from None
    if not 1 <= max_hops <= MAX_HOPS_LIMIT:
        raise TraceError(f"max_hops must be between 1 and {MAX_HOPS_LIMIT}")
    start = registry.get_row(asset_id)
    if not start:
        return None

    utility = start["utility_type"]
    controls_apply = model.has_isolation(utility)
    rows = {start["id"]: start}
    hop = {start["id"]: 0}
    parent = {start["id"]: None}          # pk -> (parent pk, edge)
    behind_control = {start["id"]: False}  # a control device already lies between start and this node
    edges, unknown_links, cross_links, cycles, warnings = {}, [], [], [], []
    seen_edges = set()
    frontier_open = set()                 # nodes whose expansion stopped at max_hops
    q = deque([start["id"]])
    truncated = False

    while q:
        u = q.popleft()
        if hop[u] >= max_hops:
            frontier_open.add(u)
            continue
        for e in registry.edges_of(u):
            if e["id"] in seen_edges:
                continue
            rel = _relation(e, u)
            v = e["to_asset"] if e["from_asset"] == u else e["from_asset"]
            if rel == "unknown" and direction != "both":
                seen_edges.add(e["id"])
                unknown_links.append(registry.edge_view(e))
                continue
            if direction != "both" and rel not in (direction, "bidirectional"):
                continue
            seen_edges.add(e["id"])
            if v not in rows:
                vrow = registry.get_row_by_pk(v)
                if vrow["utility_type"] != utility:
                    cross_links.append(registry.edge_view(e))
                    continue
                if len(rows) >= max_nodes:
                    truncated = True
                    continue
                rows[v] = vrow
                hop[v] = hop[u] + 1
                parent[v] = (u, e)
                behind_control[v] = behind_control[u] or bool(rows[u]["is_control_device"] and u != start["id"])
                q.append(v)
            else:
                if parent.get(u) and parent[u][0] == v:
                    pass  # the edge we arrived by, already counted
                else:
                    cycles.append(e["connection_id"])
            edges[e["id"]] = registry.edge_view(e)
            if e["direction"] == "unknown":
                unknown_links.append(registry.edge_view(e))

    def path_to(pk):
        nodes, links = [rows[pk]["asset_id"]], []
        while parent.get(pk):
            p, e = parent[pk]
            links.append(e)
            nodes.append(rows[p]["asset_id"])
            pk = p
        return list(reversed(nodes)), list(reversed(links))

    visited, sources, candidates = [], [], []
    for pk, r in sorted(rows.items(), key=lambda kv: (hop[kv[0]], kv[1]["asset_id"])):
        nodes, links = path_to(pk)
        item = registry.summary(r) | {"hop": hop[pk],
                                      "via": links[-1]["connection_id"] if links else None}
        visited.append(item)
        if r["is_source"] and pk != start["id"]:
            sources.append(item | {"path": nodes})
        if controls_apply and r["is_control_device"] and pk != start["id"] and not behind_control[pk]:
            unverified = [e["connection_id"] for e in links if e["verification_status"] != "verified"]
            if r["verification_status"] != "verified":
                unverified.append(r["asset_id"])
            rel = "upstream" if direction == "upstream" else "downstream" if direction == "downstream" \
                else ("unknown" if any(e["direction"] == "unknown" for e in links) else "connected")
            candidates.append(item | {
                "path": nodes, "side": rel, "path_verified": not unverified, "unverified_records": unverified,
                "basis": "Nearest recorded control device on this traced branch: " + " → ".join(nodes),
                "status": "candidate — requires operator confirmation",
            })

    # Dead ends: nodes that could not be expanded further in the requested direction.
    dead_ends = []
    if direction in ("upstream", "downstream"):
        for pk, r in rows.items():
            if pk in frontier_open:
                continue
            arrived_by = parent[pk][1]["id"] if parent.get(pk) else None
            onward = [e for e in registry.edges_of(pk)
                      if e["id"] != arrived_by and _relation(e, pk) in (direction, "bidirectional", "unknown")]
            if not onward and not (direction == "upstream" and r["is_source"]):
                dead_ends.append(r["asset_id"])
    if direction == "upstream" and model.UTILITY_TYPES.get(utility, {}).get("source_types") and not sources:
        warnings.append("No recorded source is reachable upstream. The network record is incomplete here — "
                        "a connection may be missing or unrecorded.")
    if unknown_links:
        warnings.append(f"{len(unknown_links)} recorded link(s) have unknown flow direction; the operator must "
                        "confirm how they behave.")
    if cycles:
        warnings.append("The recorded network contains a loop; supply may arrive from more than one side.")
    if frontier_open:
        warnings.append(f"Trace stopped at the {max_hops}-hop limit; the network continues beyond it.")
    if truncated:
        warnings.append(f"Trace stopped at {max_nodes} assets.")
    if not controls_apply:
        warnings.append(f"No isolation devices are modelled for {model.UTILITY_TYPES.get(utility, {}).get('label', utility)}.")
    unverified_edges = [e["connection_id"] for e in edges.values() if e["verification_status"] != "verified"]
    if unverified_edges:
        warnings.append(f"{len(unverified_edges)} traced connection(s) are not field-verified.")

    prov = {}
    for r in rows.values():
        prov[r["provenance_class"]] = prov.get(r["provenance_class"], 0) + 1
    for e in edges.values():
        prov[e["provenance_class"]] = prov.get(e["provenance_class"], 0) + 1
    return {
        "selected_asset_id": start["asset_id"], "utility_type": utility, "direction": direction,
        "visited_assets": visited, "edges": list(edges.values()), "possible_sources": sources,
        "candidate_isolation_assets": candidates, "unknown_links": unknown_links,
        "cross_utility_links": cross_links, "dead_ends": dead_ends, "cycles_detected": sorted(set(cycles)),
        "unverified_connections": unverified_edges, "warnings": warnings,
        "supporting_record_ids": sorted({v["asset_id"] for v in visited} | {e["connection_id"] for e in edges.values()}),
        "trace_limits": {"max_hops": max_hops, "max_nodes": max_nodes, "hop_limit_reached": bool(frontier_open),
                         "node_limit_reached": truncated},
        "provenance_summary": prov,
        "contains_synthetic": any(r["is_synthetic"] for r in rows.values()),
        "note": "Recorded connections only. Candidate devices need confirmation by the authorized operator.",
    }


def isolation(asset_id, max_hops=DEFAULT_MAX_HOPS):
    """Upstream and downstream candidate control devices for one asset (e.g. a leaking main)."""
    up = trace(asset_id, "upstream", max_hops)
    if up is None:
        return None
    down = trace(asset_id, "downstream", max_hops)
    return {
        "selected_asset_id": asset_id, "utility_type": up["utility_type"],
        "applicable": model.has_isolation(up["utility_type"]),
        "upstream_candidates": up["candidate_isolation_assets"],
        "downstream_candidates": down["candidate_isolation_assets"],
        "unknown_links": up["unknown_links"] + [u for u in down["unknown_links"]
                                                if u["connection_id"] not in {x["connection_id"] for x in up["unknown_links"]}],
        "sources": up["possible_sources"],
        "warnings": list(dict.fromkeys(up["warnings"] + down["warnings"])),
        "note": ("Candidates come from recorded connections. They are not an instruction to operate anything: "
                 "the authorized operator identifies and operates the actual control point."),
    }

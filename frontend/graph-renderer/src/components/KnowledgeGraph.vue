<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { Graph, GraphNode, LayoutMode } from '../types'
import type { StyleResolver } from '../graph'
import { nodeFilterKey } from '../graph'

const props = defineProps<{
  graph: Graph
  types: string[]
  activeKeys: Set<string>
  pinnedTypes: Set<string>
  styleFor: StyleResolver
  layout: LayoutMode
  selectedId: string | null
}>()

const emit = defineEmits<{
  'node-selected': [node: GraphNode]
  background: []
  // The filter keys (type or type::subtype) of every node in the current
  // selection's focus, so the sidebar can highlight which types are involved.
  'focus-keys': [keys: string[]]
}>()

// D3 mutates simulation data (adds x/y/vx/vy and swaps source/target ids for
// node objects), so we always operate on local clones — never on the props.
interface SimNode extends GraphNode, d3.SimulationNodeDatum {
  depth: number
}
interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  rel: string
  // Endpoints trimmed to the node boundaries, recomputed each tick so the
  // arrowhead always sits just outside the target circle (whatever its size).
  x1?: number
  y1?: number
  x2?: number
  y2?: number
}

const svgRef = ref<SVGSVGElement | null>(null)

let simulation: d3.Simulation<SimNode, SimLink> | null = null
let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown> | null = null
let linkSel: d3.Selection<SVGLineElement, SimLink, SVGGElement, unknown> | null = null
let linkLabelSel: d3.Selection<SVGTextElement, SimLink, SVGGElement, unknown> | null = null
let nodeSel: d3.Selection<SVGGElement, SimNode, SVGGElement, unknown> | null = null
// Bridge edges: dashed connectors drawn between two *visible* nodes that are only
// linked through a chain of currently-hidden nodes, so a filtered view still shows
// how the surviving nodes relate (e.g. a work item back to the aircraft) instead
// of collapsing into disconnected islands. Recomputed from the active filter.
let bridgeLayer: d3.Selection<SVGGElement, unknown, null, undefined> | null = null
let bridgeSel: d3.Selection<SVGLineElement, SimLink, SVGGElement, unknown> | null = null
let currentBridges: SimLink[] = []
// Undirected adjacency over every edge and a lookup of the live simulation nodes,
// both used to discover bridges across hidden-only paths.
let fullAdj = new Map<string, string[]>()
let simNodeById = new Map<string, SimNode>()

// Relationship types that form the containment hierarchy (the tree spine).
const HIERARCHY = ['HAS_SYSTEM', 'HAS_COMPONENT', 'HAS_PART', 'HAS_PHASE', 'HAS_RUNWAY']
const isHierarchy = (rel: string): boolean => HIERARCHY.includes(rel)

// SDLC lifecycle/provenance edges. Unlike the aircraft containment tree (which is
// strictly parent → child, so a selection drills *downward*), an SDLC thread has no
// single direction: a release is a sink that implementation points *into*, a
// requirement is a source everything derives *from*. So when a node is selected we
// trace these edges in BOTH directions to reveal its whole lineage — the
// implementation, tests, evidence and requirements that went into it as well as
// what it feeds. Actor, compliance-objective and cross-domain seams are deliberately
// excluded so the trace stays inside one thread and doesn't bleed into the aircraft.
const LINEAGE = new Set([
  'DERIVES_FROM',
  'SATISFIES',
  'IMPLEMENTS',
  'VERIFIES',
  'PRODUCES',
  'USES_EVIDENCE',
  'INCLUDES_CLAIM',
  'MITIGATES',
  'TRACES_TO_HAZARD',
  'IMPLEMENTED_BY',
  'IMPLEMENTS_WORK',
  'AFFECTS',
  'RESOLVES',
  'EXECUTES',
  'INCLUDED_IN_BASELINE',
  'HAS_CONFIGURATION_ITEM',
  'RAISES',
  'MERGES',
  'TRIGGERS',
  'FOUND_BY',
  'ANALYSES',
  'REVIEWS',
  'ASSURES',
])
const isLineage = (rel: string): boolean => LINEAGE.has(rel)

// Cross-domain seams (edges whose endpoints sit in different domains, e.g. an
// SDLC artefact bound to an aircraft system) are drawn as dashed accent lines so
// the link between the two graphs stands out from intra-domain edges.
const CROSS_COLOR = '#ffd84d'
const isCross = (d: SimLink): boolean =>
  (d.source as SimNode).domain !== (d.target as SimNode).domain

// Dashed bridge connectors (a chain of hidden nodes collapsed to a single line).
const BRIDGE_COLOR = '#8b97a6'

// Above this size the renderer drops the per-edge text labels (thousands of
// mostly-hidden <text> nodes repositioned every tick) to stay responsive.
const LINK_LABEL_MAX_LINKS = 1200

// Remembered node positions so layout switches aren't jarring.
const posCache = new Map<string, { x: number; y: number }>()
// The live zoom transform, preserved across rebuilds.
let savedTransform: d3.ZoomTransform = d3.zoomIdentity

// Per-build derived structure, recomputed on every build().
let parentMap = new Map<string, string>()
// Directed adjacency used for selection tracing. Each entry records the relationship
// class so the traversal can recurse through containment (down only) and lineage
// (both directions) while treating everything else as an immediate leaf.
let outAdj = new Map<string, { t: string; hier: boolean; lineage: boolean }[]>()
// Incoming lineage edges (target → sources), so selecting a sink node (e.g. a
// release everything is included in) still reaches its provenance.
let inLineageAdj = new Map<string, string[]>()
// Node filter key by id, the containment tree (parent → children), the
// association adjacency (undirected, non-containment edges), and the set of
// contained node ids. A node stays visible only if it is connected back to a
// pinned anchor (the aircraft) — and a *contained* node may only be reached
// through its containment parent, never via an association edge, so e.g. an
// electrical component can never appear unless the electrical system it hangs
// off is shown.
let nodeKeyById = new Map<string, string>()
let hierChildren = new Map<string, string[]>()
let assocAdj = new Map<string, string[]>()
let hasHierParent = new Set<string>()
let anchorIds: string[] = []
// Memoised downstream set for the current selection (cleared on rebuild).
let focusCache: { id: string | null; set: Set<string> } = { id: null, set: new Set() }

function teardown(): void {
  simulation?.stop()
  simulation = null
  if (svgRef.value) {
    d3.select(svgRef.value).on('.zoom', null).on('click', null)
    svgRef.value.replaceChildren()
  }
  linkSel = null
  linkLabelSel = null
  nodeSel = null
  bridgeLayer = null
  bridgeSel = null
  currentBridges = []
  zoomBehavior = null
}

// Depth of every node measured from the hierarchy roots; nodes that aren't part
// of any containment tree (people, documents, ATC…) are pushed to the outer ring.
function computeDepths(nodes: GraphNode[], links: Graph['links']): Map<string, number> {
  const childOf = new Map<string, string[]>()
  const hasParent = new Set<string>()
  parentMap = new Map()
  for (const l of links) {
    if (!isHierarchy(l.rel)) continue
    if (!childOf.has(l.source)) childOf.set(l.source, [])
    childOf.get(l.source)!.push(l.target)
    hasParent.add(l.target)
    parentMap.set(l.target, l.source)
  }
  const depth = new Map<string, number>()
  const queue: string[] = []
  for (const n of nodes) {
    if (!hasParent.has(n.id)) {
      depth.set(n.id, 0)
      queue.push(n.id)
    }
  }
  while (queue.length) {
    const id = queue.shift()!
    const d = depth.get(id)!
    for (const c of childOf.get(id) ?? []) {
      if (!depth.has(c)) {
        depth.set(c, d + 1)
        queue.push(c)
      }
    }
  }
  let maxDepth = 0
  for (const d of depth.values()) maxDepth = Math.max(maxDepth, d)
  for (const n of nodes) if (!depth.has(n.id)) depth.set(n.id, maxDepth + 1)
  return depth
}

function build(): void {
  const svgEl = svgRef.value
  if (!svgEl) return

  teardown()

  // Forget cached positions for nodes that are no longer present (e.g. after
  // loading a different/larger export) so the cache can't grow unbounded.
  const presentIds = new Set(props.graph.nodes.map((n) => n.id))
  for (const key of posCache.keys()) if (!presentIds.has(key)) posCache.delete(key)

  const W = svgEl.clientWidth
  const H = svgEl.clientHeight
  const root = d3.select(svgEl)

  const depths = computeDepths(props.graph.nodes, props.graph.links)

  // Directed adjacency (source → targets) drives downstream focus highlighting.
  // The `hier` flag marks containment edges, which alone are followed transitively.
  // The containment tree and association adjacency drive the anchoring below.
  outAdj = new Map()
  inLineageAdj = new Map()
  hierChildren = new Map()
  assocAdj = new Map()
  hasHierParent = new Set()
  fullAdj = new Map()
  focusCache = { id: null, set: new Set() }
  const addAssoc = (a: string, b: string): void => {
    const list = assocAdj.get(a)
    if (list) list.push(b)
    else assocAdj.set(a, [b])
  }
  const addFull = (a: string, b: string): void => {
    const list = fullAdj.get(a)
    if (list) list.push(b)
    else fullAdj.set(a, [b])
  }
  const addInLineage = (a: string, b: string): void => {
    const list = inLineageAdj.get(a)
    if (list) list.push(b)
    else inLineageAdj.set(a, [b])
  }
  for (const l of props.graph.links) {
    const hier = isHierarchy(l.rel)
    const lineage = isLineage(l.rel)
    addFull(l.source, l.target)
    addFull(l.target, l.source)
    if (!outAdj.has(l.source)) outAdj.set(l.source, [])
    outAdj.get(l.source)!.push({ t: l.target, hier, lineage })
    if (lineage) addInLineage(l.target, l.source)
    if (hier) {
      const children = hierChildren.get(l.source)
      if (children) children.push(l.target)
      else hierChildren.set(l.source, [l.target])
      hasHierParent.add(l.target)
    } else {
      addAssoc(l.source, l.target)
      addAssoc(l.target, l.source)
    }
  }

  // Reachability inputs: a lookup of every node's filter key and the pinned
  // anchor node ids the anchoring fans out from.
  nodeKeyById = new Map(props.graph.nodes.map((n) => [n.id, nodeFilterKey(n)]))
  anchorIds = props.graph.nodes.filter((n) => props.pinnedTypes.has(n.type)).map((n) => n.id)

  // Single <g> container that the zoom behaviour transforms.
  const container = root.append('g')

  zoomBehavior = d3
    .zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.2, 4])
    .on('zoom', (e) => {
      // Edges and labels depend only on the type filter and selection (never on
      // zoom/pan), so a zoom/pan is purely a transform update — nothing to restyle.
      savedTransform = e.transform
      container.attr('transform', e.transform.toString())
    })
  root.call(zoomBehavior)
  // Clicking empty canvas clears the current selection.
  root.on('click', () => emit('background'))
  // Re-apply the preserved transform after a rebuild.
  root.call(zoomBehavior.transform, savedTransform)

  // Arrow markers, one per node type (coloured by target type). Sized in user
  // space (not stroke-width) and with the tip at the line end, so trimming the
  // line to the node boundary places the arrowhead just outside any node — even
  // large ones like the aircraft.
  const defs = root.append('defs')
  props.types.forEach((type) => {
    const cfg = props.styleFor(type)
    defs
      .append('marker')
      .attr('id', `arrow-${type}`)
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 8)
      .attr('refY', 0)
      .attr('markerWidth', 7)
      .attr('markerHeight', 7)
      .attr('markerUnits', 'userSpaceOnUse')
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', cfg.color)
      .attr('opacity', 0.7)
  })

  // Clone nodes/links so the force simulation never mutates props. Seed each
  // node's position from the cache (or near its parent) for stable rebuilds.
  const nodes: SimNode[] = props.graph.nodes.map((d) => {
    const cached = posCache.get(d.id)
    const seed = cached ?? seedFromParent(d.id, W, H)
    return { ...d, depth: depths.get(d.id) ?? 1, x: seed.x, y: seed.y }
  })
  const nodeMap = new Map(nodes.map((d) => [d.id, d]))
  simNodeById = nodeMap
  const links: SimLink[] = props.graph.links
    .map((d) => ({ source: nodeMap.get(d.source)!, target: nodeMap.get(d.target)!, rel: d.rel }))
    .filter((d) => d.source && d.target)

  const radial = props.layout === 'radial'
  let maxDepth = 0
  for (const n of nodes) maxDepth = Math.max(maxDepth, n.depth)
  const ringGap = Math.max(70, Math.min(160, Math.min(W, H) / 2 / (maxDepth + 1.5)))

  // Node degree drives the de-clumping: hubs (aircraft, flights) repel harder and
  // hold their children further out, so dense neighbourhoods fan open instead of
  // piling into a hairball.
  const degree = new Map<string, number>()
  for (const l of props.graph.links) {
    degree.set(l.source, (degree.get(l.source) ?? 0) + 1)
    degree.set(l.target, (degree.get(l.target) ?? 0) + 1)
  }
  const degreeOf = (id: string): number => degree.get(id) ?? 0

  simulation = d3
    .forceSimulation<SimNode>(nodes)
    .force(
      'link',
      d3
        .forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance((d) => {
          const base =
            d.rel === 'HAS_SYSTEM'
              ? 120
              : d.rel === 'HAS_COMPONENT'
                ? 80
                : d.rel === 'FOLLOWS'
                  ? 60
                  : 90
          // Extend links hanging off a hub so its many children spread out.
          const hub = Math.max(
            degreeOf((d.source as SimNode).id),
            degreeOf((d.target as SimNode).id),
          )
          return base + Math.min(140, Math.sqrt(hub) * 16)
        })
        .strength(radial ? 0.15 : 0.6),
    )
    // Stronger base repulsion than before, scaled up further for hubs and capped
    // by distanceMax so it stays fast and far-apart clusters don't fly off-screen.
    .force(
      'charge',
      d3
        .forceManyBody<SimNode>()
        .strength((d) => (radial ? -160 : -300) - Math.min(420, degreeOf(d.id) * 14))
        .distanceMax(radial ? 650 : 520)
        .theta(0.9),
    )
    // Collision radius tracks each node's drawn size (plus padding) so circles and
    // their labels stop overlapping on busy graphs.
    .force(
      'collision',
      d3
        .forceCollide<SimNode>()
        .radius((d) => props.styleFor(d.type).size + 22)
        .strength(0.9),
    )

  if (radial) {
    simulation
      .force(
        'radial',
        d3.forceRadial<SimNode>((d) => d.depth * ringGap, W / 2, H / 2).strength(0.35),
      )
      .force('x', d3.forceX(W / 2).strength(0.02))
      .force('y', d3.forceY(H / 2).strength(0.02))
  } else {
    // Default layout is purely connectivity-driven: only a gentle pull toward the
    // centre keeps the graph on-screen, so the link force decides positions and
    // every node visibly hangs off its neighbours (and ultimately the aircraft).
    // No type-clustering force — that pulled leaf nodes out to per-type anchors,
    // leaving them tethered only by a faint edge and looking disconnected.
    simulation
      .force('x', d3.forceX(W / 2).strength(0.03))
      .force('y', d3.forceY(H / 2).strength(0.03))
  }

  const typeOf = (n: SimNode | string | number): string => (typeof n === 'object' ? n.type : '')

  // Links
  linkSel = container
    .append('g')
    .selectAll<SVGLineElement, SimLink>('line')
    .data(links)
    .enter()
    .append('line')
    .attr('class', 'link')
    .attr('stroke', (d) => (isCross(d) ? CROSS_COLOR : props.styleFor(typeOf(d.target)).color))
    .attr('stroke-dasharray', (d) => (isCross(d) ? '5 4' : null))
    .attr('stroke-width', (d) => (isCross(d) ? 1.7 : null))
    .attr('marker-end', (d) => `url(#arrow-${typeOf(d.target)})`)

  // Link labels for non-structural edges. Skipped entirely on large graphs, where
  // thousands of (mostly hidden) <text> nodes — each repositioned every tick —
  // dominate render and tick time.
  linkLabelSel =
    links.length <= LINK_LABEL_MAX_LINKS
      ? container
          .append('g')
          .selectAll<SVGTextElement, SimLink>('text')
          .data(links.filter((d) => !isHierarchy(d.rel)))
          .enter()
          .append('text')
          .attr('class', 'link-label')
          .attr('fill', (d) => (isCross(d) ? CROSS_COLOR : null))
          .attr('font-size', (d) => (isCross(d) ? 8 : null))
          .attr('font-weight', (d) => (isCross(d) ? 600 : null))
          .text((d) => d.rel)
      : null

  // Bridge layer sits above the real edges but below the nodes. Populated lazily
  // by applyVisualState whenever the filter hides intermediate nodes.
  bridgeLayer = container.append('g').attr('class', 'bridges')
  bridgeSel = null
  currentBridges = []

  // Track whether a gesture was a drag so the trailing click doesn't select.
  let dragMoved = false

  // Nodes
  nodeSel = container
    .append('g')
    .selectAll<SVGGElement, SimNode>('g')
    .data(nodes)
    .enter()
    .append('g')
    .attr('class', 'node')
    .call(
      d3
        .drag<SVGGElement, SimNode>()
        .on('start', (e, d) => {
          dragMoved = false
          if (!e.active) simulation!.alphaTarget(0.3).restart()
          d.fx = d.x
          d.fy = d.y
        })
        .on('drag', (e, d) => {
          dragMoved = true
          d.fx = e.x
          d.fy = e.y
        })
        .on('end', (e, d) => {
          if (!e.active) simulation!.alphaTarget(0)
          d.fx = null
          d.fy = null
        }),
    )
    .on('click', (e, d) => {
      // Keep the background handler from also firing and clearing selection.
      e.stopPropagation()
      if (dragMoved) return
      emit('node-selected', d)
    })

  nodeSel
    .append('circle')
    .attr('r', (d) => props.styleFor(d.type).size)
    .attr('fill', (d) => props.styleFor(d.type).dimColor)
    .attr('stroke', (d) => props.styleFor(d.type).color)

  nodeSel
    .append('text')
    .attr('class', 'node-label')
    .attr('dy', (d) => props.styleFor(d.type).size + 12)
    .text((d) => d.label)

  simulation.on('tick', () => {
    // Trim each link to the node boundaries: start at the source edge and stop a
    // hair outside the target circle, leaving room for the arrowhead so it always
    // shows regardless of node size (the aircraft is much larger than a leaf).
    const ARROW = 4
    for (const d of links) {
      const s = d.source as SimNode
      const t = d.target as SimNode
      const sx = s.x ?? 0
      const sy = s.y ?? 0
      const tx = t.x ?? 0
      const ty = t.y ?? 0
      const dist = Math.hypot(tx - sx, ty - sy) || 1
      const ux = (tx - sx) / dist
      const uy = (ty - sy) / dist
      const sr = props.styleFor(s.type).size
      const tr = props.styleFor(t.type).size + ARROW
      d.x1 = sx + ux * sr
      d.y1 = sy + uy * sr
      d.x2 = tx - ux * tr
      d.y2 = ty - uy * tr
    }

    linkSel!
      .attr('x1', (d) => d.x1 ?? 0)
      .attr('y1', (d) => d.y1 ?? 0)
      .attr('x2', (d) => d.x2 ?? 0)
      .attr('y2', (d) => d.y2 ?? 0)

    positionBridges()

    if (linkLabelSel) {
      linkLabelSel
        .attr('x', (d) => (((d.source as SimNode).x ?? 0) + ((d.target as SimNode).x ?? 0)) / 2)
        .attr('y', (d) => (((d.source as SimNode).y ?? 0) + ((d.target as SimNode).y ?? 0)) / 2)
    }

    nodeSel!.attr('transform', (d) => {
      // Mutate the cached position in place to avoid allocating an object per node
      // on every tick (heavy GC churn on large graphs).
      const cached = posCache.get(d.id)
      if (cached) {
        cached.x = d.x ?? 0
        cached.y = d.y ?? 0
      } else {
        posCache.set(d.id, { x: d.x ?? 0, y: d.y ?? 0 })
      }
      return `translate(${d.x ?? 0},${d.y ?? 0})`
    })
  })

  applyVisualState()
  emitFocusKeys()
}

function seedFromParent(id: string, W: number, H: number): { x: number; y: number } {
  const parent = parentMap.get(id)
  const p = parent ? posCache.get(parent) : undefined
  if (p) return { x: p.x + (Math.random() - 0.5) * 30, y: p.y + (Math.random() - 0.5) * 30 }
  return { x: W / 2 + (Math.random() - 0.5) * 60, y: H / 2 + (Math.random() - 0.5) * 60 }
}

// The clicked node's focus set, memoised for the active selection. Three traversal
// rules combine:
//   • containment (hierarchy) edges expand transitively *downward* — selecting an
//     aircraft system reveals its whole component subtree (unchanged behaviour);
//   • SDLC lineage edges expand transitively in *both* directions — selecting a
//     release reveals everything included in it, and the implementation, tests,
//     evidence and requirements behind those, while selecting a requirement reveals
//     the implementation and verification it drives;
//   • every other outgoing edge contributes its immediate target only (actors,
//     compliance objectives, cross-domain seams), so the trace stays scoped to the
//     selected node's own thread instead of leaking across the whole graph.
function downstreamSet(id: string): Set<string> {
  if (focusCache.id === id) return focusCache.set
  const set = new Set<string>([id])
  const expanded = new Set<string>([id])
  const queue = [id]
  const enqueue = (t: string): void => {
    set.add(t)
    if (!expanded.has(t)) {
      expanded.add(t)
      queue.push(t)
    }
  }
  while (queue.length) {
    const cur = queue.shift()!
    for (const { t, hier, lineage } of outAdj.get(cur) ?? []) {
      set.add(t)
      if (hier || lineage) enqueue(t)
    }
    // Incoming lineage edges: reach the artefacts that feed into this node.
    for (const s of inLineageAdj.get(cur) ?? []) enqueue(s)
  }
  focusCache = { id, set }
  return set
}

// Nodes that remain anchored under the current filter: starting from the pinned
// anchors (the aircraft), fan out so that
//   • a containment child is reached only through its (visible) parent, and
//   • a non-contained node is reached through any visible association edge.
// So hiding a system hides all of its components (they can only re-connect
// through that system), while hiding flights cascades to the airports/phases
// that only hung off them. Returns null when there is no visible anchor, so the
// plain filter applies and nothing is anchored away.
function anchoredNodes(active: Set<string>): Set<string> | null {
  const isActive = (id: string): boolean => {
    const k = nodeKeyById.get(id)
    return k != null && active.has(k)
  }
  const seeds = anchorIds.filter(isActive)
  if (!seeds.length) return null

  const reach = new Set<string>(seeds)
  const queue = [...seeds]
  while (queue.length) {
    const cur = queue.shift()!
    for (const child of hierChildren.get(cur) ?? []) {
      if (!reach.has(child) && isActive(child)) {
        reach.add(child)
        queue.push(child)
      }
    }
    for (const neighbour of assocAdj.get(cur) ?? []) {
      if (!reach.has(neighbour) && !hasHierParent.has(neighbour) && isActive(neighbour)) {
        reach.add(neighbour)
        queue.push(neighbour)
      }
    }
  }
  return reach
}

// Single source of truth for all visual emphasis: the type filter governs what is
// visible (selecting a node first turns on its lineage's types in the sidebar, so
// the lineage is visible through the normal filter and stays user-deselectable),
// and the click-selected focus then emphasises that node's lineage within the
// visible set. Computed from state — never patched incrementally.
function applyVisualState(): void {
  if (!nodeSel || !linkSel) return
  const active = props.activeKeys
  const focusId = props.selectedId
  const focus = focusId ? downstreamSet(focusId) : null
  const inFocus = (id: string): boolean => !focus || focus.has(id)

  // Anchoring (hide a node when it can't trace a path back to the aircraft root
  // through visible nodes) governs the operational aircraft hierarchy only — it
  // is what makes hiding a System cascade to its Components. The SDLC overlay is a
  // federated graph, not part of that containment tree, so its nodes are shown
  // whenever their type is active, regardless of which neighbours are hidden.
  const anchored = anchoredNodes(active)
  const typeVisible = (n: SimNode): boolean =>
    active.has(nodeFilterKey(n)) && (n.domain === 'sdlc' || !anchored || anchored.has(n.id))
  const labelVisible = (n: SimNode): boolean => typeVisible(n)

  nodeSel.style('opacity', (d) => {
    // Visibility always respects the filter so the user can deselect any type,
    // even while a node is selected. Within the visible set, a selection brightens
    // its own lineage and fades the rest back so the clicked thread stands out.
    if (!typeVisible(d)) return focusId ? 0.05 : 0.08
    if (focusId) return inFocus(d.id) ? 1 : 0.18
    return 1
  })

  nodeSel.select<SVGTextElement>('text.node-label').style('opacity', (d) => {
    if (!labelVisible(d)) return 0
    return !focusId || inFocus(d.id) ? 1 : 0
  })

  // An edge is in focus when a node is selected and both endpoints fall inside
  // that selection's focus subtree — those edges brighten and reveal their labels.
  const edgeInFocus = (d: SimLink): boolean => {
    if (!focus) return false
    const sn = d.source as SimNode
    const tn = d.target as SimNode
    return focus.has(sn.id) && focus.has(tn.id)
  }

  linkSel.style('opacity', (d) => {
    const sn = d.source as SimNode
    const tn = d.target as SimNode
    // The filter wins first: an edge with a hidden endpoint is faded right back.
    if (!typeVisible(sn) || !typeVisible(tn)) return 0.04
    if (edgeInFocus(d)) return isCross(d) ? 0.95 : 0.6
    // A selection is active but this edge is outside it — fade it well back so the
    // focused subtree stands out; otherwise keep the structural line clearly visible
    // so every node reads as tethered to its neighbours. Cross-domain seams stay
    // more prominent in both states so the link between the two graphs is visible.
    if (focusId) return isCross(d) ? 0.3 : 0.05
    return isCross(d) ? 0.9 : 0.4
  })

  if (linkLabelSel) {
    linkLabelSel.style('opacity', (d) => {
      const sn = d.source as SimNode
      const tn = d.target as SimNode
      if (!typeVisible(sn) || !typeVisible(tn)) return 0
      if (edgeInFocus(d)) return 0.95
      // Cross-domain seam labels are always shown (there are few of them) so the
      // relationship type between the two graphs is readable at a glance.
      if (isCross(d)) return 0.95
      return 0
    })
  }

  // While a node is selected the revealed lineage carries the story; fade the
  // filter-derived bridge connectors back so they don't compete with it.
  updateBridges(typeVisible, focusId != null)
}

// Discover and draw the dashed bridge connectors for the current filter. A bridge
// links two *visible* nodes whose only connection runs through a connected blob of
// hidden nodes, so the filtered graph never fragments into islands. We add the
// minimum set needed: visible nodes already joined through other visible edges (or
// an earlier bridge) are left alone via a union-find, so bridges appear only where
// the hidden nodes are genuinely the sole link.
function updateBridges(typeVisible: (n: SimNode) => boolean, dim = false): void {
  if (!bridgeLayer) return

  const visible = new Set<string>()
  for (const [id, n] of simNodeById) if (typeVisible(n)) visible.add(id)

  // Union-find over visible nodes, pre-merged on every visible-to-visible edge.
  const parent = new Map<string, string>()
  for (const id of visible) parent.set(id, id)
  const find = (x: string): string => {
    let r = x
    while (parent.get(r) !== r) r = parent.get(r)!
    while (parent.get(x) !== r) {
      const nx = parent.get(x)!
      parent.set(x, r)
      x = nx
    }
    return r
  }
  const union = (a: string, b: string): void => {
    const ra = find(a)
    const rb = find(b)
    if (ra !== rb) parent.set(ra, rb)
  }
  for (const l of props.graph.links) {
    if (visible.has(l.source) && visible.has(l.target)) union(l.source, l.target)
  }

  // Walk each connected component of hidden nodes, collect the visible nodes on its
  // border, and bridge any pair not already joined.
  const seen = new Set<string>()
  const bridges: SimLink[] = []
  for (const id of simNodeById.keys()) {
    if (visible.has(id) || seen.has(id)) continue
    const border = new Set<string>()
    const queue = [id]
    seen.add(id)
    while (queue.length) {
      const cur = queue.shift()!
      for (const nb of fullAdj.get(cur) ?? []) {
        if (visible.has(nb)) border.add(nb)
        else if (!seen.has(nb)) {
          seen.add(nb)
          queue.push(nb)
        }
      }
    }
    const b = [...border]
    if (b.length < 2) continue
    const anchor = b[0]
    for (let i = 1; i < b.length; i++) {
      if (find(anchor) !== find(b[i])) {
        union(anchor, b[i])
        bridges.push({
          source: simNodeById.get(anchor)!,
          target: simNodeById.get(b[i])!,
          rel: 'BRIDGE',
        })
      }
    }
  }

  currentBridges = bridges
  const join = bridgeLayer.selectAll<SVGLineElement, SimLink>('line').data(bridges)
  join.exit().remove()
  bridgeSel = join
    .enter()
    .append('line')
    .attr('class', 'bridge')
    .attr('stroke', BRIDGE_COLOR)
    .attr('stroke-dasharray', '2 6')
    .attr('stroke-width', 1.3)
    .attr('stroke-linecap', 'round')
    .merge(join)
  bridgeSel.attr('opacity', dim ? 0.18 : 0.55)
  positionBridges()
}

// Position bridge lines, trimmed to each endpoint's circle (no arrowhead).
function positionBridges(): void {
  if (!bridgeSel) return
  for (const d of currentBridges) {
    const s = d.source as SimNode
    const t = d.target as SimNode
    const sx = s.x ?? 0
    const sy = s.y ?? 0
    const tx = t.x ?? 0
    const ty = t.y ?? 0
    const dist = Math.hypot(tx - sx, ty - sy) || 1
    const ux = (tx - sx) / dist
    const uy = (ty - sy) / dist
    d.x1 = sx + ux * props.styleFor(s.type).size
    d.y1 = sy + uy * props.styleFor(s.type).size
    d.x2 = tx - ux * props.styleFor(t.type).size
    d.y2 = ty - uy * props.styleFor(t.type).size
  }
  bridgeSel
    .attr('x1', (d) => d.x1 ?? 0)
    .attr('y1', (d) => d.y1 ?? 0)
    .attr('x2', (d) => d.x2 ?? 0)
    .attr('y2', (d) => d.y2 ?? 0)
}

function resetView(): void {
  const svgEl = svgRef.value
  if (!svgEl || !zoomBehavior) return
  savedTransform = d3.zoomIdentity
  d3.select(svgEl).transition().duration(400).call(zoomBehavior.transform, d3.zoomIdentity)
  simulation?.alpha(0.5).restart()
}

// When a node is selected, smoothly scale/pan so its whole focus subtree fits the
// visible canvas. The fit target excludes the fixed side panels (left filter rail,
// right info panel) and the top header so the selection lands in clear space.
// These insets mirror the panel sizes in SidebarFilters.vue / InfoPanel.vue.
const PANEL_LEFT = 280
const PANEL_RIGHT = 280
const PANEL_TOP = 56
function zoomToFocus(id: string): void {
  const svgEl = svgRef.value
  if (!svgEl || !zoomBehavior) return

  const focus = downstreamSet(id)
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  let count = 0
  for (const nid of focus) {
    const p = posCache.get(nid)
    if (!p) continue
    minX = Math.min(minX, p.x)
    maxX = Math.max(maxX, p.x)
    minY = Math.min(minY, p.y)
    maxY = Math.max(maxY, p.y)
    count++
  }
  if (count === 0) return

  const fullW = svgEl.clientWidth
  const fullH = svgEl.clientHeight
  const availW = Math.max(100, fullW - PANEL_LEFT - PANEL_RIGHT)
  const availH = Math.max(100, fullH - PANEL_TOP)
  const midX = (minX + maxX) / 2
  const midY = (minY + maxY) / 2
  // Floor the span so a single node (or tight cluster) isn't magnified excessively.
  const spanX = Math.max(maxX - minX, 200)
  const spanY = Math.max(maxY - minY, 200)
  const margin = 1.2 // ~20% breathing room around the focus subtree

  let scale = Math.min(availW / (spanX * margin), availH / (spanY * margin))
  scale = Math.max(0.2, Math.min(2.5, scale))

  // Centre the focus within the visible region between the panels.
  const cx = PANEL_LEFT + availW / 2
  const cy = PANEL_TOP + availH / 2
  const transform = d3.zoomIdentity.translate(cx - scale * midX, cy - scale * midY).scale(scale)
  d3.select(svgEl).transition().duration(500).call(zoomBehavior.transform, transform)
}

let resizeTimer: ReturnType<typeof setTimeout> | null = null
function onResize(): void {
  if (resizeTimer !== null) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    resizeTimer = null
    if (props.graph.nodes.length) build()
  }, 180)
}

// Tell the parent which filter keys the current selection's focus spans, so the
// sidebar can highlight the node types that took part. Empty when nothing is
// selected.
function emitFocusKeys(): void {
  const id = props.selectedId
  if (!id) {
    emit('focus-keys', [])
    return
  }
  const keys = new Set<string>()
  for (const nid of downstreamSet(id)) {
    const k = nodeKeyById.get(nid)
    if (k != null) keys.add(k)
  }
  emit('focus-keys', [...keys])
}

defineExpose({ resetView })

onMounted(() => {
  build()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (resizeTimer !== null) clearTimeout(resizeTimer)
  teardown()
})

// Rebuild when the data or layout changes.
watch(
  () => props.graph,
  () => build(),
)
watch(
  () => props.layout,
  () => build(),
)

// Filter toggles and selection only need a cheap visual update.
watch(
  () => props.activeKeys,
  () => applyVisualState(),
  { deep: true },
)
watch(
  () => props.selectedId,
  (id) => {
    applyVisualState()
    emitFocusKeys()
    if (id) zoomToFocus(id)
  },
)
</script>

<template>
  <svg id="graph" ref="svgRef"></svg>
</template>

<style scoped>
#graph {
  position: fixed;
  left: 0;
  top: 0;
  right: 0;
  bottom: 0;
  width: 100vw;
  height: 100vh;
  z-index: 1;
}
</style>

<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { Graph, GraphNode, LayoutMode } from '../types'
import type { StyleResolver } from '../graph'
import { humanizeType } from '../graph'

const props = defineProps<{
  graph: Graph
  types: string[]
  activeTypes: Set<string>
  styleFor: StyleResolver
  layout: LayoutMode
  selectedId: string | null
}>()

const emit = defineEmits<{
  'node-selected': [node: GraphNode]
  background: []
}>()

// D3 mutates simulation data (adds x/y/vx/vy and swaps source/target ids for
// node objects), so we always operate on local clones — never on the props.
interface SimNode extends GraphNode, d3.SimulationNodeDatum {
  depth: number
}
interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  rel: string
}

const svgRef = ref<SVGSVGElement | null>(null)

let simulation: d3.Simulation<SimNode, SimLink> | null = null
let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown> | null = null
let linkSel: d3.Selection<SVGLineElement, SimLink, SVGGElement, unknown> | null = null
let linkLabelSel: d3.Selection<SVGTextElement, SimLink, SVGGElement, unknown> | null = null
let nodeSel: d3.Selection<SVGGElement, SimNode, SVGGElement, unknown> | null = null
// Cluster labels: a small heading per node type, positioned above each type's
// cluster. Only populated in the clustered (default) layout.
let hullLabelSel: d3.Selection<SVGTextElement, string, SVGGElement, unknown> | null = null
// Per-build: nodes grouped by primary type, used to position cluster labels.
let nodesByType = new Map<string, SimNode[]>()

// Relationship types that form the containment hierarchy (the tree spine).
const HIERARCHY = ['HAS_SYSTEM', 'HAS_COMPONENT', 'HAS_PART', 'HAS_PHASE', 'HAS_RUNWAY']
const isHierarchy = (rel: string): boolean => HIERARCHY.includes(rel)

// Above these sizes the renderer drops expensive per-element extras (the per-node
// blur filter, the per-edge text labels) to stay responsive on large graphs.
// Smaller graphs render exactly as before.
const SHADOW_MAX_NODES = 600
const LINK_LABEL_MAX_LINKS = 1200
// Cluster labels iterate every node of a type each tick to find their heading
// position, so they are only drawn while the graph is small enough to stay cheap.
const CLUSTER_LABEL_MAX_NODES = 1500
// Vertical gap (px) between a cluster's topmost node and its heading label.
const CLUSTER_LABEL_GAP = 32

// Remembered node positions so layout switches aren't jarring.
const posCache = new Map<string, { x: number; y: number }>()
// The live zoom transform, preserved across rebuilds.
let savedTransform: d3.ZoomTransform = d3.zoomIdentity

// Per-build derived structure, recomputed on every build().
let parentMap = new Map<string, string>()
let outAdj = new Map<string, { t: string; hier: boolean }[]>()
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
  hullLabelSel = null
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
  outAdj = new Map()
  focusCache = { id: null, set: new Set() }
  for (const l of props.graph.links) {
    if (!outAdj.has(l.source)) outAdj.set(l.source, [])
    outAdj.get(l.source)!.push({ t: l.target, hier: isHierarchy(l.rel) })
  }

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

  // Arrow markers, one per node type (coloured by target type).
  const defs = root.append('defs')
  props.types.forEach((type) => {
    const cfg = props.styleFor(type)
    defs
      .append('marker')
      .attr('id', `arrow-${type}`)
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 18)
      .attr('refY', 0)
      .attr('markerWidth', 6)
      .attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', cfg.color)
      .attr('opacity', 0.6)
  })

  // Clone nodes/links so the force simulation never mutates props. Seed each
  // node's position from the cache (or near its parent) for stable rebuilds.
  const nodes: SimNode[] = props.graph.nodes.map((d) => {
    const cached = posCache.get(d.id)
    const seed = cached ?? seedFromParent(d.id, W, H)
    return { ...d, depth: depths.get(d.id) ?? 1, x: seed.x, y: seed.y }
  })
  const nodeMap = new Map(nodes.map((d) => [d.id, d]))
  const links: SimLink[] = props.graph.links
    .map((d) => ({ source: nodeMap.get(d.source)!, target: nodeMap.get(d.target)!, rel: d.rel }))
    .filter((d) => d.source && d.target)

  const radial = props.layout === 'radial'
  let maxDepth = 0
  for (const n of nodes) maxDepth = Math.max(maxDepth, n.depth)
  const ringGap = Math.max(70, Math.min(160, Math.min(W, H) / 2 / (maxDepth + 1.5)))

  // Group nodes by primary type and give each type a stable anchor on a ring
  // around the centre. In the default layout a gentle force pulls each node toward
  // its type anchor so same-type nodes settle into their own readable cluster.
  nodesByType = d3.group(nodes, (d) => d.type)
  const clusterTypes = props.types.filter((t) => nodesByType.has(t))
  const typeAnchors = new Map<string, { x: number; y: number }>()
  const clusterR = Math.min(W, H) / 3.2
  clusterTypes.forEach((t, i) => {
    const a = (i / Math.max(1, clusterTypes.length)) * 2 * Math.PI - Math.PI / 2
    typeAnchors.set(t, { x: W / 2 + clusterR * Math.cos(a), y: H / 2 + clusterR * Math.sin(a) })
  })
  const anchorX = (d: SimNode): number => typeAnchors.get(d.type)?.x ?? W / 2
  const anchorY = (d: SimNode): number => typeAnchors.get(d.type)?.y ?? H / 2

  simulation = d3
    .forceSimulation<SimNode>(nodes)
    .force(
      'link',
      d3
        .forceLink<SimNode, SimLink>(links)
        .id((d) => d.id)
        .distance((d) => {
          if (d.rel === 'HAS_SYSTEM') return 120
          if (d.rel === 'HAS_COMPONENT') return 80
          if (d.rel === 'FOLLOWS') return 60
          return 90
        })
        .strength(radial ? 0.15 : 0.5),
    )
    .force('charge', d3.forceManyBody().strength(radial ? -140 : -220))
    .force('collision', d3.forceCollide(22))

  if (radial) {
    simulation
      .force(
        'radial',
        d3.forceRadial<SimNode>((d) => d.depth * ringGap, W / 2, H / 2).strength(0.35),
      )
      .force('x', d3.forceX(W / 2).strength(0.02))
      .force('y', d3.forceY(H / 2).strength(0.02))
  } else {
    simulation
      .force('x', d3.forceX<SimNode>(anchorX).strength(0.18))
      .force('y', d3.forceY<SimNode>(anchorY).strength(0.18))
  }

  // Cluster headings: one label per type, drawn above each type's cluster in the
  // default (clustered) layout only. Created here (before nodes) so positions
  // update each tick.
  if (!radial && nodes.length <= CLUSTER_LABEL_MAX_NODES) {
    const hullLabelGroup = container.append('g').attr('class', 'hull-labels')
    hullLabelSel = hullLabelGroup
      .selectAll<SVGTextElement, string>('text')
      .data(clusterTypes)
      .enter()
      .append('text')
      .attr('class', 'hull-label')
      .attr('fill', (t) => props.styleFor(t).color)
      .text((t) => humanizeType(t))
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
    .attr('stroke', (d) => props.styleFor(typeOf(d.target)).color)
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
          .text((d) => d.rel)
      : null

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

  const circleSel = nodeSel
    .append('circle')
    .attr('r', (d) => props.styleFor(d.type).size)
    .attr('fill', (d) => props.styleFor(d.type).dimColor)
    .attr('stroke', (d) => props.styleFor(d.type).color)

  // The per-node drop-shadow blur is the most expensive SVG effect at scale, so
  // it is only applied while the graph is small enough to stay smooth.
  if (nodes.length <= SHADOW_MAX_NODES) {
    circleSel.style('filter', (d) => `drop-shadow(0 0 6px ${props.styleFor(d.type).color}66)`)
  }

  nodeSel
    .append('text')
    .attr('class', 'node-label')
    .attr('dy', (d) => props.styleFor(d.type).size + 12)
    .text((d) => d.label)

  simulation.on('tick', () => {
    if (hullLabelSel) updateClusterLabels()

    linkSel!
      .attr('x1', (d) => (d.source as SimNode).x ?? 0)
      .attr('y1', (d) => (d.source as SimNode).y ?? 0)
      .attr('x2', (d) => (d.target as SimNode).x ?? 0)
      .attr('y2', (d) => (d.target as SimNode).y ?? 0)

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
}

// Reposition each cluster heading above its type's topmost node, centred on the
// cluster. Cheap at PoC scale; gated by CLUSTER_LABEL_MAX_NODES on big graphs.
function updateClusterLabels(): void {
  if (!hullLabelSel) return
  hullLabelSel.attr('transform', (type) => {
    const ns = nodesByType.get(type)
    if (!ns || ns.length === 0) return 'translate(-9999,-9999)'
    let minY = Infinity
    let sumX = 0
    for (const n of ns) {
      sumX += n.x ?? 0
      minY = Math.min(minY, n.y ?? 0)
    }
    return `translate(${sumX / ns.length},${minY - CLUSTER_LABEL_GAP})`
  })
}

function seedFromParent(id: string, W: number, H: number): { x: number; y: number } {
  const parent = parentMap.get(id)
  const p = parent ? posCache.get(parent) : undefined
  if (p) return { x: p.x + (Math.random() - 0.5) * 30, y: p.y + (Math.random() - 0.5) * 30 }
  return { x: W / 2 + (Math.random() - 0.5) * 60, y: H / 2 + (Math.random() - 0.5) * 60 }
}

// The clicked node's downstream focus set, memoised for the active selection.
// Containment (hierarchy) edges are followed transitively so a subtree fully
// expands; association edges (USES_AIRCRAFT, FEEDS, OCCURS_AT…) include their
// immediate target but are not drilled into, keeping the focus scoped.
function downstreamSet(id: string): Set<string> {
  if (focusCache.id === id) return focusCache.set
  const set = new Set<string>([id])
  const expanded = new Set<string>([id])
  const queue = [id]
  while (queue.length) {
    const cur = queue.shift()!
    for (const { t, hier } of outAdj.get(cur) ?? []) {
      set.add(t)
      if (hier && !expanded.has(t)) {
        expanded.add(t)
        queue.push(t)
      }
    }
  }
  focusCache = { id, set }
  return set
}

// Single source of truth for all visual emphasis: type filter (fade), the
// click-selected focus (reveal the node and its downstream subtree). Edges are
// always drawn but kept faint; selecting a node brightens the edges of its focus
// subtree and reveals their labels, so the graph stays readable when unselected.
// Computed from state — never patched incrementally.
function applyVisualState(): void {
  if (!nodeSel || !linkSel) return
  const active = props.activeTypes
  const focusId = props.selectedId
  const focus = focusId ? downstreamSet(focusId) : null
  const inFocus = (id: string): boolean => !focus || focus.has(id)

  const typeVisible = (n: SimNode): boolean => active.has(n.type)
  const labelVisible = (n: SimNode): boolean => typeVisible(n)

  nodeSel.style('opacity', (d) => {
    if (!typeVisible(d)) return 0.08
    if (focusId && !inFocus(d.id)) return 0.08
    return 1
  })

  nodeSel.select<SVGTextElement>('text.node-label').style('opacity', (d) => {
    if (!labelVisible(d)) return 0
    if (focusId && !inFocus(d.id)) return 0.15
    return 1
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
    if (!active.has(sn.type) || !active.has(tn.type)) return 0.04
    if (edgeInFocus(d)) return 0.6
    // A selection is active but this edge is outside it — fade it well back so the
    // focused subtree stands out; otherwise keep the default faint structural line.
    return focusId ? 0.05 : 0.22
  })

  if (linkLabelSel) {
    linkLabelSel.style('opacity', (d) => {
      const sn = d.source as SimNode
      const tn = d.target as SimNode
      if (!active.has(sn.type) || !active.has(tn.type)) return 0
      return edgeInFocus(d) ? 0.95 : 0
    })
  }

  // Cluster labels track the type filter, and recede while a node is selected so
  // the focused subtree isn't competing with the group headings for attention.
  if (hullLabelSel) {
    const labelFade = focusId ? 0.4 : 1
    hullLabelSel.style('opacity', (type) => (active.has(type) ? 0.9 * labelFade : 0))
  }
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
const PANEL_LEFT = 220
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
  () => props.activeTypes,
  () => applyVisualState(),
  { deep: true },
)
watch(
  () => props.selectedId,
  (id) => {
    applyVisualState()
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

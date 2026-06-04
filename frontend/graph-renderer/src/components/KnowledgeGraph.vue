<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { Graph, GraphNode, LayoutMode } from '../types'
import type { StyleResolver } from '../graph'

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

// Relationship types that form the containment hierarchy (the tree spine).
const HIERARCHY = ['HAS_SYSTEM', 'HAS_COMPONENT', 'HAS_PART', 'HAS_PHASE', 'HAS_RUNWAY']
const isHierarchy = (rel: string): boolean => HIERARCHY.includes(rel)

// Remembered node positions so layout switches aren't jarring.
const posCache = new Map<string, { x: number; y: number }>()
// The live zoom transform, preserved across rebuilds.
let savedTransform: d3.ZoomTransform = d3.zoomIdentity
let zoomK = 1

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
      savedTransform = e.transform
      zoomK = e.transform.k
      container.attr('transform', e.transform.toString())
      applyVisualState()
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
        .strength(radial ? 0.15 : 0.6),
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
    simulation.force('center', d3.forceCenter(W / 2, H / 2))
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

  // Link labels for non-structural edges.
  linkLabelSel = container
    .append('g')
    .selectAll<SVGTextElement, SimLink>('text')
    .data(links.filter((d) => !isHierarchy(d.rel)))
    .enter()
    .append('text')
    .attr('class', 'link-label')
    .text((d) => d.rel)

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
    .style('filter', (d) => `drop-shadow(0 0 6px ${props.styleFor(d.type).color}66)`)

  nodeSel
    .append('text')
    .attr('class', 'node-label')
    .attr('dy', (d) => props.styleFor(d.type).size + 12)
    .text((d) => d.label)

  simulation.on('tick', () => {
    linkSel!
      .attr('x1', (d) => (d.source as SimNode).x ?? 0)
      .attr('y1', (d) => (d.source as SimNode).y ?? 0)
      .attr('x2', (d) => (d.target as SimNode).x ?? 0)
      .attr('y2', (d) => (d.target as SimNode).y ?? 0)

    linkLabelSel!
      .attr('x', (d) => (((d.source as SimNode).x ?? 0) + ((d.target as SimNode).x ?? 0)) / 2)
      .attr('y', (d) => (((d.source as SimNode).y ?? 0) + ((d.target as SimNode).y ?? 0)) / 2)

    nodeSel!.attr('transform', (d) => {
      posCache.set(d.id, { x: d.x ?? 0, y: d.y ?? 0 })
      return `translate(${d.x ?? 0},${d.y ?? 0})`
    })
  })

  applyVisualState()
}

// Nodes with no cached position spawn near their parent so the layout grows
// outward instead of flinging nodes across the canvas.
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
// click-selected focus (fade everything but the node and its downstream subtree)
// and label level-of-detail. Computed from state — never patched incrementally.
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
    if (focusId && !inFocus(d.id)) return 0.12
    return 1
  })

  nodeSel
    .select<SVGTextElement>('text.node-label')
    .style('opacity', (d) => (labelVisible(d) ? 1 : 0))

  linkSel.style('opacity', (d) => {
    const sn = d.source as SimNode
    const tn = d.target as SimNode
    if (!active.has(sn.type) || !active.has(tn.type)) return 0.06
    if (focus && !(focus.has(sn.id) && focus.has(tn.id))) return 0.1
    return 0.45
  })

  if (linkLabelSel) {
    linkLabelSel.style('opacity', (d) => {
      const sn = d.source as SimNode
      const tn = d.target as SimNode
      if (!active.has(sn.type) || !active.has(tn.type)) return 0
      const inFocusEdge = !!focus && focus.has(sn.id) && focus.has(tn.id)
      return zoomK > 1.4 || inFocusEdge ? 0.75 : 0
    })
  }
}

function resetView(): void {
  const svgEl = svgRef.value
  if (!svgEl || !zoomBehavior) return
  savedTransform = d3.zoomIdentity
  zoomK = 1
  d3.select(svgEl).transition().duration(400).call(zoomBehavior.transform, d3.zoomIdentity)
  simulation?.alpha(0.5).restart()
}

// Resizes fire rapidly; debounce so the canvas rebuilds once the gesture settles
// instead of tearing down and re-running the simulation on every event.
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
  () => applyVisualState(),
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

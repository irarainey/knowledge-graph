<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as d3 from 'd3'
import type { Graph, GraphNode } from '../types'
import type { StyleResolver } from '../graph'

const props = defineProps<{
  graph: Graph
  types: string[]
  activeTypes: Set<string>
  styleFor: StyleResolver
}>()

const emit = defineEmits<{
  'node-selected': [node: GraphNode]
}>()

// D3 mutates simulation data (adds x/y/vx/vy and swaps source/target ids for
// node objects), so we always operate on local clones — never on the props.
interface SimNode extends GraphNode, d3.SimulationNodeDatum {}
interface SimLink extends d3.SimulationLinkDatum<SimNode> {
  rel: string
}

const svgRef = ref<SVGSVGElement | null>(null)

let simulation: d3.Simulation<SimNode, SimLink> | null = null
let zoomBehavior: d3.ZoomBehavior<SVGSVGElement, unknown> | null = null
let linkSel: d3.Selection<SVGLineElement, SimLink, SVGGElement, unknown> | null = null
let nodeSel: d3.Selection<SVGGElement, SimNode, SVGGElement, unknown> | null = null

const STRUCTURAL = ['HAS_SYSTEM', 'HAS_COMPONENT', 'HAS_PART', 'HAS_PHASE']

function teardown(): void {
  simulation?.stop()
  simulation = null
  if (svgRef.value) {
    d3.select(svgRef.value).on('.zoom', null)
    svgRef.value.replaceChildren()
  }
  linkSel = null
  nodeSel = null
  zoomBehavior = null
}

function build(): void {
  const svgEl = svgRef.value
  if (!svgEl) return

  teardown()

  const W = svgEl.clientWidth
  const H = svgEl.clientHeight

  const root = d3.select(svgEl)

  // Single <g> container that the zoom behaviour transforms.
  const container = root.append('g')

  zoomBehavior = d3
    .zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.2, 4])
    .on('zoom', (e) => container.attr('transform', e.transform.toString()))
  root.call(zoomBehavior)

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

  // Clone nodes/links so the force simulation never mutates props.
  const nodes: SimNode[] = props.graph.nodes.map((d) => ({ ...d }))
  const nodeMap = new Map(nodes.map((d) => [d.id, d]))
  const links: SimLink[] = props.graph.links
    .map((d) => ({ source: nodeMap.get(d.source)!, target: nodeMap.get(d.target)!, rel: d.rel }))
    .filter((d) => d.source && d.target)

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
        .strength(0.6),
    )
    .force('charge', d3.forceManyBody().strength(-220))
    .force('center', d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(22))

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
  const linkLabelSel = container
    .append('g')
    .selectAll<SVGTextElement, SimLink>('text')
    .data(links.filter((d) => !STRUCTURAL.includes(d.rel)))
    .enter()
    .append('text')
    .attr('class', 'link-label')
    .text((d) => d.rel)

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
          if (!e.active) simulation!.alphaTarget(0.3).restart()
          d.fx = d.x
          d.fy = d.y
        })
        .on('drag', (e, d) => {
          d.fx = e.x
          d.fy = e.y
        })
        .on('end', (e, d) => {
          if (!e.active) simulation!.alphaTarget(0)
          d.fx = null
          d.fy = null
        }),
    )
    .on('click', (_e, d) => emit('node-selected', d))

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

    linkLabelSel
      .attr('x', (d) => (((d.source as SimNode).x ?? 0) + ((d.target as SimNode).x ?? 0)) / 2)
      .attr('y', (d) => (((d.source as SimNode).y ?? 0) + ((d.target as SimNode).y ?? 0)) / 2)

    nodeSel!.attr('transform', (d) => `translate(${d.x ?? 0},${d.y ?? 0})`)
  })

  updateVisibility()
}

function updateVisibility(): void {
  if (!nodeSel || !linkSel) return
  const active = props.activeTypes
  nodeSel.classed('faded', (d) => !active.has(d.type))
  linkSel.style('opacity', (d) => {
    const sn = d.source as SimNode
    const tn = d.target as SimNode
    return active.has(sn.type) && active.has(tn.type) ? 0.35 : 0.04
  })
}

function resetView(): void {
  const svgEl = svgRef.value
  if (!svgEl || !zoomBehavior) return
  d3.select(svgEl).transition().duration(400).call(zoomBehavior.transform, d3.zoomIdentity)
  simulation?.alpha(0.5).restart()
}

function onResize(): void {
  if (props.graph.nodes.length) build()
}

defineExpose({ resetView })

onMounted(() => {
  build()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  teardown()
})

// Rebuild when the underlying data changes.
watch(
  () => props.graph,
  () => build(),
)

// Filter toggles only need a cheap visibility update.
watch(
  () => props.activeTypes,
  () => updateVisibility(),
  { deep: true },
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

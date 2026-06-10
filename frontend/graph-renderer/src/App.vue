<script setup lang="ts">
import { computed, onMounted, ref, useTemplateRef } from 'vue'
import GraphHeader from './components/GraphHeader.vue'
import SidebarFilters from './components/SidebarFilters.vue'
import InfoPanel from './components/InfoPanel.vue'
import KnowledgeGraph from './components/KnowledgeGraph.vue'
import {
  adaptNeo4j,
  buildTypeTree,
  createStyleResolver,
  nodeFilterKey,
  presentTypes,
} from './graph'
import type { Graph, GraphNode, LayoutMode, NodeStyles, RawGraph } from './types'
import { filterGraphByVersion } from './version'
import type { GraphViewMode } from './version'

const graph = ref<Graph>({ nodes: [], links: [] })
const styles = ref<NodeStyles>({})
const activeKeys = ref<Set<string>>(new Set())
const selectedNode = ref<GraphNode | null>(null)
const layout = ref<LayoutMode>('force')
const viewKind = ref<GraphViewMode['kind']>('current')
const asOfDate = ref(new Date().toISOString().slice(0, 10))
const error = ref<string | null>(null)

// The aircraft is the root the whole graph hangs off, so it is always shown and
// is excluded from the sidebar — hiding it would orphan everything.
const PINNED_TYPES = new Set(['Aircraft'])

const graphRef = useTemplateRef<InstanceType<typeof KnowledgeGraph>>('graphRef')

const styleFor = computed(() => createStyleResolver(styles.value))
const viewMode = computed<GraphViewMode>(() =>
  viewKind.value === 'as-of' ? { kind: 'as-of', date: asOfDate.value } : { kind: 'current' },
)
const filteredGraph = computed(() => filterGraphByVersion(graph.value, viewMode.value))
const types = computed(() => presentTypes(filteredGraph.value, styles.value))
const allGroups = computed(() => buildTypeTree(filteredGraph.value, styles.value))
const sidebarGroups = computed(() => allGroups.value.filter((g) => !PINNED_TYPES.has(g.type)))
const visibleSelectedId = computed(() => {
  const id = selectedNode.value?.id
  if (!id) return null
  return filteredGraph.value.nodes.some((node) => node.id === id) ? id : null
})

// The filter leaf keys belonging to a group: its subtype leaves, or the bare
// type when the group has no subtypes.
function groupLeafKeys(type: string): string[] {
  const group = allGroups.value.find((g) => g.type === type)
  if (!group) return []
  return group.subgroups.length ? group.subgroups.map((s) => s.key) : [group.key]
}

function toggleKey(key: string): void {
  const next = new Set(activeKeys.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  activeKeys.value = next
}

// Show every type. Deselect all still keeps the pinned aircraft active, since it
// is always shown and anchors the rest of the graph.
function selectAll(): void {
  activeKeys.value = new Set(graph.value.nodes.map(nodeFilterKey))
}

function deselectAll(): void {
  activeKeys.value = new Set(
    graph.value.nodes.filter((n) => PINNED_TYPES.has(n.type)).map(nodeFilterKey),
  )
}

// Toggling a top-level group flips all its leaves together: if every leaf is
// already on, turn them all off, otherwise turn them all on.
function toggleGroup(type: string): void {
  if (PINNED_TYPES.has(type)) return
  const keys = groupLeafKeys(type)
  if (!keys.length) return
  const next = new Set(activeKeys.value)
  const allOn = keys.every((k) => next.has(k))
  for (const k of keys) {
    if (allOn) next.delete(k)
    else next.add(k)
  }
  activeKeys.value = next
}

function reset(): void {
  graphRef.value?.resetView()
}

function setLayout(mode: LayoutMode): void {
  layout.value = mode
}

// Click a node to focus it; click the same node again (or the background)
// clears the selection.
function onNodeSelected(node: GraphNode): void {
  selectedNode.value = selectedNode.value?.id === node.id ? null : node
}

// Load optional style overrides. Failure is non-fatal: the resolver falls back
// to procedurally generated styling for every type.
async function loadStyles(): Promise<void> {
  try {
    const res = await fetch('/data/node-styles.json')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as { nodeStyles?: NodeStyles }
    styles.value = data.nodeStyles ?? {}
  } catch (err) {
    console.warn('Could not load node-styles.json; using procedural styling only:', err)
  }
}

async function loadGraph(): Promise<void> {
  await loadStyles()
  try {
    const res = await fetch('/data/knowledge-graph.json')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as RawGraph
    graph.value = adaptNeo4j(data)
    activeKeys.value = new Set(graph.value.nodes.map(nodeFilterKey))
  } catch (err) {
    console.error('Failed to load knowledge-graph.json:', err)
    error.value = 'Failed to load /data/knowledge-graph.json. Is the dev server running?'
  }
}

onMounted(loadGraph)
</script>

<template>
  <GraphHeader
    :node-count="filteredGraph.nodes.length"
    :edge-count="filteredGraph.links.length"
    :layout="layout"
    :view-kind="viewKind"
    :as-of-date="asOfDate"
    @reset="reset"
    @set-layout="setLayout"
    @set-view-kind="viewKind = $event"
    @set-as-of-date="asOfDate = $event"
  />

  <SidebarFilters
    :groups="sidebarGroups"
    :active-keys="activeKeys"
    :style-for="styleFor"
    @toggle="toggleKey"
    @toggle-group="toggleGroup"
    @select-all="selectAll"
    @deselect-all="deselectAll"
  />

  <KnowledgeGraph
    ref="graphRef"
    :graph="filteredGraph"
    :types="types"
    :active-keys="activeKeys"
    :pinned-types="PINNED_TYPES"
    :style-for="styleFor"
    :layout="layout"
    :selected-id="visibleSelectedId"
    @node-selected="onNodeSelected"
    @background="selectedNode = null"
  />

  <InfoPanel
    :node="selectedNode"
    :graph="filteredGraph"
    :full-graph="graph"
    :style-for="styleFor"
    @select-node="selectedNode = $event"
    @close="selectedNode = null"
  />

  <div class="hint">Drag to pan · Scroll to zoom · Click a node to focus</div>

  <div v-if="error" class="load-error">{{ error }}</div>
</template>

<style scoped>
.hint {
  position: fixed;
  bottom: 12px;
  left: 232px;
  z-index: 10;
  font-size: 11px;
  color: var(--text-dim);
  pointer-events: none;
}

.load-error {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 24px;
  color: var(--red);
  font-size: 14px;
  z-index: 200;
}
</style>

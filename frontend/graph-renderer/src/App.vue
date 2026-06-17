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
  mergeGraphs,
  nodeFilterKey,
  presentTypes,
} from './graph'
import type { Domain, Graph, GraphNode, LayoutMode, NodeStyles, RawGraph } from './types'
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

// Filter keys spanned by the current selection's focus lineage, surfaced by the
// graph so the sidebar can highlight which node types took part. Empty when
// nothing is selected.
const focusKeys = ref<Set<string>>(new Set())

// The aircraft is the root the whole graph hangs off, so it is always shown and
// is excluded from the sidebar — hiding it would orphan everything.
const PINNED_TYPES = new Set(['Aircraft'])

// Display metadata for each domain, used by the sidebar domain toggle. Order
// here drives the order the domain toggles appear in.
const DOMAIN_META: Record<Domain, { label: string; color: string }> = {
  aircraft: { label: 'Aircraft (operational)', color: '#4ab8f0' },
  sdlc: { label: 'SDLC (assurance)', color: '#c98bff' },
}

const graphRef = useTemplateRef<InstanceType<typeof KnowledgeGraph>>('graphRef')

const styleFor = computed(() => createStyleResolver(styles.value))
const viewMode = computed<GraphViewMode>(() =>
  viewKind.value === 'as-of' ? { kind: 'as-of', date: asOfDate.value } : { kind: 'current' },
)
const filteredGraph = computed(() => filterGraphByVersion(graph.value, viewMode.value))
const types = computed(() => presentTypes(filteredGraph.value, styles.value))
const allGroups = computed(() => buildTypeTree(filteredGraph.value, styles.value))
const sidebarGroups = computed(() => allGroups.value.filter((g) => !PINNED_TYPES.has(g.type)))

// The toggleable domains present in the data, each with the filter leaf keys of
// its node types. The domain toggle is the one place the pinned aircraft root is
// included, so toggling the operational domain off hides the aircraft entirely
// and leaves the SDLC overlay on its own; the granular per-type list below still
// excludes the root so an individual toggle can't orphan the aircraft graph.
const domainFilters = computed(() => {
  const byDomain = new Map<Domain, Set<string>>()
  for (const n of filteredGraph.value.nodes) {
    const keys = byDomain.get(n.domain) ?? new Set<string>()
    keys.add(nodeFilterKey(n))
    byDomain.set(n.domain, keys)
  }
  return (Object.keys(DOMAIN_META) as Domain[])
    .filter((d) => byDomain.has(d))
    .map((d) => ({
      key: d,
      label: DOMAIN_META[d].label,
      color: DOMAIN_META[d].color,
      leafKeys: [...byDomain.get(d)!],
    }))
})

// The sidebar grouped by domain: each present domain becomes a section whose
// heading is the domain toggle (leafKeys + colour from domainFilters) and whose
// body is the type groups belonging to that domain. Makes it clear which node
// types are operational and which are SDLC.
const sidebarSections = computed(() => {
  const typeDomain = new Map<string, Domain>()
  for (const n of filteredGraph.value.nodes) {
    if (!typeDomain.has(n.type)) typeDomain.set(n.type, n.domain)
  }
  const groupsByDomain = new Map<Domain, typeof sidebarGroups.value>()
  for (const g of sidebarGroups.value) {
    const d = typeDomain.get(g.type) ?? 'aircraft'
    const arr = groupsByDomain.get(d) ?? []
    arr.push(g)
    groupsByDomain.set(d, arr)
  }
  return domainFilters.value
    .filter((df) => groupsByDomain.has(df.key))
    .map((df) => ({ ...df, groups: groupsByDomain.get(df.key)! }))
})
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

// Toggling a domain flips every leaf belonging to it together: if all are on,
// turn them off, otherwise turn them all on. Mirrors toggleGroup across types.
function toggleDomain(domain: Domain): void {
  const filter = domainFilters.value.find((d) => d.key === domain)
  if (!filter || !filter.leafKeys.length) return
  const next = new Set(activeKeys.value)
  const allOn = filter.leafKeys.every((k) => next.has(k))
  for (const k of filter.leafKeys) {
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

function onFocusKeys(keys: string[]): void {
  // Surface which node types belong to the current selection's lineage so the
  // sidebar can highlight them. Selecting a node never mutates the active filter
  // — inspecting a node must not undo the types the user has filtered out. If a
  // lineage type is hidden, the highlight tells the user it exists; revealing it
  // is their explicit choice via the sidebar.
  focusKeys.value = new Set(keys)
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

// Fetch one Neo4j/APOC export and adapt it, tagging every node with its domain.
// A missing optional graph (e.g. the SDLC overlay) is non-fatal: it just yields
// an empty graph so the aircraft graph still renders on its own.
async function fetchGraph(path: string, domain: Domain, optional = false): Promise<Graph> {
  try {
    const res = await fetch(path)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as RawGraph
    return adaptNeo4j(data, domain)
  } catch (err) {
    if (optional) {
      console.warn(`Optional graph ${path} not loaded; continuing without it:`, err)
      return { nodes: [], links: [] }
    }
    throw err
  }
}

async function loadGraph(): Promise<void> {
  await loadStyles()
  try {
    const [aircraft, sdlc] = await Promise.all([
      fetchGraph('/data/aircraft-knowledge-graph.json', 'aircraft'),
      fetchGraph('/data/sdlc-knowledge-graph.json', 'sdlc', true),
    ])
    graph.value = mergeGraphs(aircraft, sdlc)
    activeKeys.value = new Set(graph.value.nodes.map(nodeFilterKey))
  } catch (err) {
    console.error('Failed to load aircraft-knowledge-graph.json:', err)
    error.value = 'Failed to load /data/aircraft-knowledge-graph.json. Is the dev server running?'
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
    :sections="sidebarSections"
    :active-keys="activeKeys"
    :highlight-keys="focusKeys"
    :style-for="styleFor"
    @toggle="toggleKey"
    @toggle-group="toggleGroup"
    @toggle-domain="toggleDomain"
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
    @focus-keys="onFocusKeys"
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
  left: 292px;
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

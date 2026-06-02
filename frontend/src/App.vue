<script setup lang="ts">
import { computed, onMounted, ref, useTemplateRef } from 'vue'
import GraphHeader from './components/GraphHeader.vue'
import SidebarFilters from './components/SidebarFilters.vue'
import InfoPanel from './components/InfoPanel.vue'
import KnowledgeGraph from './components/KnowledgeGraph.vue'
import { adaptNeo4j, createStyleResolver, presentTypes } from './graph'
import type { Graph, GraphNode, NodeStyles, RawGraph } from './types'

const graph = ref<Graph>({ nodes: [], links: [] })
const styles = ref<NodeStyles>({})
const activeTypes = ref<Set<string>>(new Set())
const selectedNode = ref<GraphNode | null>(null)
const error = ref<string | null>(null)

const graphRef = useTemplateRef<InstanceType<typeof KnowledgeGraph>>('graphRef')

const styleFor = computed(() => createStyleResolver(styles.value))
const types = computed(() => presentTypes(graph.value, styles.value))

function toggleType(type: string): void {
  const next = new Set(activeTypes.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  activeTypes.value = next
}

function reset(): void {
  graphRef.value?.resetView()
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
    activeTypes.value = new Set(presentTypes(graph.value, styles.value))
  } catch (err) {
    console.error('Failed to load knowledge-graph.json:', err)
    error.value = 'Failed to load /data/knowledge-graph.json. Is the dev server running?'
  }
}

onMounted(loadGraph)
</script>

<template>
  <div class="scan-line"></div>

  <GraphHeader :node-count="graph.nodes.length" :edge-count="graph.links.length" @reset="reset" />

  <SidebarFilters
    :types="types"
    :graph="graph"
    :active-types="activeTypes"
    :style-for="styleFor"
    @toggle="toggleType"
  />

  <KnowledgeGraph
    ref="graphRef"
    :graph="graph"
    :types="types"
    :active-types="activeTypes"
    :style-for="styleFor"
    @node-selected="selectedNode = $event"
  />

  <InfoPanel
    :node="selectedNode"
    :graph="graph"
    :style-for="styleFor"
    @close="selectedNode = null"
  />

  <div class="corner bl">DRAG TO PAN · SCROLL TO ZOOM · CLICK NODE FOR DETAIL</div>
  <div class="corner br">SPO:// v1.0.0</div>

  <div v-if="error" class="load-error">{{ error }}</div>
</template>

<style scoped>
.load-error {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 24px;
  color: var(--red);
  font-size: 12px;
  z-index: 200;
}
</style>

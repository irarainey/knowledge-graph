<script setup lang="ts">
import { computed } from 'vue'
import type { Graph, GraphNode } from '../types'
import type { StyleResolver } from '../graph'
import { getVersionInfo, versionHistory } from '../version'

const props = defineProps<{
  node: GraphNode | null
  graph: Graph
  fullGraph: Graph
  styleFor: StyleResolver
}>()

defineEmits<{
  close: []
  'select-node': [node: GraphNode]
}>()

const labelsById = computed(() => {
  const map = new Map<string, string>()
  for (const n of props.fullGraph.nodes) map.set(n.id, n.label)
  return map
})
const labelFor = (id: string): string => labelsById.value.get(id) ?? id

// Index links by endpoint once per graph so selecting a node is O(degree) rather
// than scanning every link (O(edges)) for both the outgoing and incoming lists.
const linksBySource = computed(() => {
  const map = new Map<string, Graph['links']>()
  for (const l of props.graph.links) {
    const list = map.get(l.source)
    if (list) list.push(l)
    else map.set(l.source, [l])
  }
  return map
})
const linksByTarget = computed(() => {
  const map = new Map<string, Graph['links']>()
  for (const l of props.graph.links) {
    const list = map.get(l.target)
    if (list) list.push(l)
    else map.set(l.target, [l])
  }
  return map
})

const outgoing = computed(() => (props.node ? (linksBySource.value.get(props.node.id) ?? []) : []))
const incoming = computed(() => (props.node ? (linksByTarget.value.get(props.node.id) ?? []) : []))
const propEntries = computed(() => (props.node ? Object.entries(props.node.props) : []))
const selectedVersion = computed(() => (props.node ? getVersionInfo(props.node) : null))
const history = computed(() =>
  selectedVersion.value ? versionHistory(props.fullGraph, selectedVersion.value.logicalId) : [],
)

function validityWindow(node: GraphNode): string {
  const version = getVersionInfo(node)
  if (!version) return ''
  return `${version.validFrom}–${version.validTo ?? 'current'}`
}
</script>

<template>
  <div id="info" :class="{ open: node !== null }">
    <button class="close-btn" @click="$emit('close')">✕</button>
    <template v-if="node">
      <div class="info-type">{{ styleFor(node.type).label }}</div>
      <div class="info-label">{{ node.label }}</div>

      <div v-if="selectedVersion" class="info-section">
        <div class="info-section-title">VERSION</div>
        <div class="info-row">
          <span class="arrow">·</span><span>version:&nbsp;</span
          ><span class="target">{{ selectedVersion.version }}</span>
        </div>
        <div class="info-row">
          <span class="arrow">·</span><span>validFrom:&nbsp;</span
          ><span class="target">{{ selectedVersion.validFrom }}</span>
        </div>
        <div class="info-row">
          <span class="arrow">·</span><span>validTo:&nbsp;</span
          ><span class="target">{{ selectedVersion.validTo ?? 'current' }}</span>
        </div>
        <div class="info-row">
          <span class="arrow">·</span><span>current:&nbsp;</span
          ><span class="target">{{ selectedVersion.current }}</span>
        </div>
      </div>

      <div v-if="history.length" class="info-section">
        <div class="info-section-title">VERSION HISTORY</div>
        <button
          v-for="historyNode in history"
          :key="historyNode.id"
          class="history-row"
          :class="{ active: historyNode.id === node.id }"
          @click="$emit('select-node', historyNode)"
        >
          <span>v{{ getVersionInfo(historyNode)?.version }}</span>
          <span class="history-window">{{ validityWindow(historyNode) }}</span>
          <span v-if="getVersionInfo(historyNode)?.current" class="history-current">current</span>
        </button>
      </div>

      <div v-if="propEntries.length" class="info-section">
        <div class="info-section-title">PROPERTIES</div>
        <div v-for="[k, v] in propEntries" :key="k" class="info-row">
          <span class="arrow">·</span><span>{{ k }}:&nbsp;</span><span class="target">{{ v }}</span>
        </div>
      </div>

      <div v-if="node.sub" class="info-section">
        <div class="info-section-title">CLASS</div>
        <div class="info-row">
          <span class="arrow">·</span><span class="target">spo:{{ node.sub }}</span>
        </div>
      </div>

      <div v-if="outgoing.length" class="info-section">
        <div class="info-section-title">OUTGOING ({{ outgoing.length }})</div>
        <div v-for="(l, i) in outgoing" :key="i" class="info-row">
          <span class="arrow">→</span><span class="rel">{{ l.rel }}</span
          >&nbsp;<span class="target">{{ labelFor(l.target) }}</span>
        </div>
      </div>

      <div v-if="incoming.length" class="info-section">
        <div class="info-section-title">INCOMING ({{ incoming.length }})</div>
        <div v-for="(l, i) in incoming" :key="i" class="info-row">
          <span class="arrow">←</span><span class="target">{{ labelFor(l.source) }}</span
          >&nbsp;<span class="rel">{{ l.rel }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
#info {
  position: fixed;
  right: 0;
  top: 56px;
  bottom: 0;
  width: 340px;
  z-index: 50;
  background: var(--panel);
  border-left: 1px solid var(--border);
  padding: 20px 18px;
  overflow-y: auto;
  transform: translateX(100%);
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
#info.open {
  transform: translateX(0);
}

.info-type {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.info-label {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 14px;
  line-height: 1.3;
}
.info-section {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.info-section-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.info-row {
  display: flex;
  gap: 6px;
  font-size: 13px;
  color: var(--text);
  margin-bottom: 6px;
  align-items: flex-start;
  line-height: 1.4;
}
.info-row .arrow {
  color: var(--text-dim);
  flex-shrink: 0;
}
.info-row .target {
  color: var(--text);
}
.info-row .rel {
  color: var(--accent);
}

.history-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text);
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
  margin-bottom: 4px;
  padding: 7px 8px;
  text-align: left;
}
.history-row:hover {
  background: rgba(255, 255, 255, 0.04);
}
.history-row.active {
  background: var(--panel-2);
  border-color: var(--border);
}
.history-window {
  color: var(--text-dim);
  flex: 1;
}
.history-current {
  color: var(--accent);
  font-size: 11px;
}

.close-btn {
  position: absolute;
  top: 14px;
  right: 14px;
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 16px;
  cursor: pointer;
  line-height: 1;
  padding: 4px;
}
.close-btn:hover {
  color: var(--text);
}
</style>

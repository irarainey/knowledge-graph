<script setup lang="ts">
import { computed } from 'vue'
import type { Graph, GraphNode } from '../types'
import type { StyleResolver } from '../graph'

const props = defineProps<{
  node: GraphNode | null
  graph: Graph
  styleFor: StyleResolver
}>()

defineEmits<{
  close: []
}>()

const labelsById = computed(() => {
  const map = new Map<string, string>()
  for (const n of props.graph.nodes) map.set(n.id, n.label)
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
</script>

<template>
  <div id="info" :class="{ open: node !== null }">
    <button class="close-btn" @click="$emit('close')">✕</button>
    <template v-if="node">
      <div class="info-type">{{ styleFor(node.type).label }}</div>
      <div class="info-label">{{ node.label }}</div>

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
  width: 280px;
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

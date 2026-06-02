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

const labelFor = (id: string): string => props.graph.nodes.find((n) => n.id === id)?.label ?? id

const outgoing = computed(() =>
  props.node ? props.graph.links.filter((l) => l.source === props.node!.id) : [],
)
const incoming = computed(() =>
  props.node ? props.graph.links.filter((l) => l.target === props.node!.id) : [],
)
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
  width: 260px;
  z-index: 50;
  background: rgba(8, 12, 16, 0.95);
  border-left: 1px solid var(--border);
  padding: 20px 16px;
  backdrop-filter: blur(6px);
  overflow-y: auto;
  transform: translateX(100%);
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
#info.open {
  transform: translateX(0);
}

.info-type {
  font-size: 9px;
  letter-spacing: 0.2em;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.info-label {
  font-family: 'Orbitron', sans-serif;
  font-size: 14px;
  font-weight: 700;
  color: var(--amber);
  text-shadow: 0 0 16px rgba(232, 160, 32, 0.4);
  margin-bottom: 12px;
  line-height: 1.3;
}
.info-section {
  margin-top: 14px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  padding-top: 10px;
}
.info-section-title {
  font-size: 9px;
  letter-spacing: 0.15em;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.info-row {
  display: flex;
  gap: 6px;
  font-size: 10px;
  color: var(--text);
  margin-bottom: 4px;
  align-items: flex-start;
}
.info-row .arrow {
  color: var(--amber-dim);
  flex-shrink: 0;
}
.info-row .target {
  color: var(--green);
}
.info-row .rel {
  color: var(--amber-dim);
}

.close-btn {
  position: absolute;
  top: 12px;
  right: 12px;
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 16px;
  cursor: pointer;
  line-height: 1;
  padding: 4px;
}
.close-btn:hover {
  color: var(--amber);
}
</style>

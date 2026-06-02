<script setup lang="ts">
import type { Graph } from '../types'
import type { StyleResolver } from '../graph'

const props = defineProps<{
  types: string[]
  graph: Graph
  activeTypes: Set<string>
  styleFor: StyleResolver
}>()

defineEmits<{
  toggle: [type: string]
}>()

function countFor(type: string): number {
  return props.graph.nodes.filter((n) => n.type === type).length
}
</script>

<template>
  <div id="sidebar">
    <div class="sidebar-heading">NODE TYPES</div>
    <button
      v-for="type in types"
      :key="type"
      class="filter-btn"
      :class="{ active: activeTypes.has(type) }"
      @click="$emit('toggle', type)"
    >
      <span
        class="dot"
        :style="{ background: styleFor(type).color, color: styleFor(type).color }"
      ></span>
      {{ styleFor(type).label }}
      <span class="count">{{ countFor(type) }}</span>
    </button>
  </div>
</template>

<style scoped>
#sidebar {
  position: fixed;
  left: 0;
  top: 56px;
  bottom: 0;
  width: 220px;
  z-index: 50;
  background: rgba(8, 12, 16, 0.92);
  border-right: 1px solid var(--border);
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  backdrop-filter: blur(6px);
  overflow-y: auto;
}

.sidebar-heading {
  font-size: 9px;
  letter-spacing: 0.2em;
  color: var(--text-dim);
  margin: 8px 4px 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  padding-bottom: 4px;
}

.filter-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: 1px solid transparent;
  color: var(--text);
  font-family: 'Share Tech Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.05em;
  padding: 6px 8px;
  cursor: pointer;
  transition: all 0.15s;
  width: 100%;
  text-align: left;
}
.filter-btn .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  box-shadow: 0 0 6px currentColor;
}
.filter-btn.active {
  background: rgba(255, 255, 255, 0.04);
  border-color: rgba(255, 255, 255, 0.08);
}
.filter-btn:hover {
  background: rgba(255, 255, 255, 0.05);
}
.filter-btn .count {
  margin-left: auto;
  font-size: 9px;
  color: var(--text-dim);
}
</style>

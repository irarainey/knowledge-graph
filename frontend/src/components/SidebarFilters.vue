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
    <div class="sidebar-heading">Node types</div>
    <button
      v-for="type in types"
      :key="type"
      class="filter-btn"
      :class="{ active: activeTypes.has(type) }"
      @click="$emit('toggle', type)"
    >
      <span class="dot" :style="{ background: styleFor(type).color }"></span>
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
  background: var(--panel);
  border-right: 1px solid var(--border);
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}

.sidebar-heading {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  margin: 4px 6px 10px;
}

.filter-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  padding: 8px 10px;
  cursor: pointer;
  transition: all 0.15s;
  width: 100%;
  text-align: left;
}
.filter-btn .dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}
.filter-btn.active {
  background: var(--panel-2);
  border-color: var(--border);
}
.filter-btn:hover {
  background: rgba(255, 255, 255, 0.04);
}
.filter-btn .count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
}
</style>

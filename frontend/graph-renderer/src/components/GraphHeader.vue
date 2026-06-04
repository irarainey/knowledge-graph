<script setup lang="ts">
import type { LayoutMode } from '../types'

defineProps<{
  nodeCount: number
  edgeCount: number
  layout: LayoutMode
}>()

defineEmits<{
  reset: []
  'set-layout': [mode: LayoutMode]
}>()
</script>

<template>
  <header>
    <div class="brand">
      <span class="brand-title">✈ Cessna 172S Skyhawk</span>
      <span class="brand-sub">G-ECHO · Knowledge Graph</span>
    </div>

    <div class="stats">
      <span class="stat"
        ><strong>{{ nodeCount }}</strong> nodes</span
      >
      <span class="stat-divider"></span>
      <span class="stat"
        ><strong>{{ edgeCount }}</strong> edges</span
      >
    </div>

    <div class="header-right">
      <div class="layout-toggle">
        <button
          class="btn-layout"
          :class="{ active: layout === 'force' }"
          @click="$emit('set-layout', 'force')"
        >
          Force
        </button>
        <button
          class="btn-layout"
          :class="{ active: layout === 'radial' }"
          @click="$emit('set-layout', 'radial')"
        >
          Radial
        </button>
      </div>
      <button class="btn-reset" @click="$emit('reset')">Reset view</button>
    </div>
  </header>
</template>

<style scoped>
header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 24px;
}

.brand {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}
.brand-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
}
.brand-sub {
  font-size: 11px;
  color: var(--text-dim);
}

.stats {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--text-dim);
}
.stats strong {
  color: var(--text);
  font-weight: 600;
}
.stat-divider {
  width: 1px;
  height: 18px;
  background: var(--border);
}

.header-right {
  margin-left: auto;
  display: flex;
  gap: 8px;
  align-items: center;
}

.layout-toggle {
  display: flex;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.btn-layout {
  background: transparent;
  border: none;
  border-right: 1px solid var(--border);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 13px;
  padding: 6px 14px;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-layout:last-child {
  border-right: none;
}
.btn-layout:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.04);
}
.btn-layout.active {
  color: var(--accent);
  background: var(--accent-weak);
}

.btn-reset {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  padding: 6px 14px;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-reset:hover {
  border-color: var(--accent);
  color: var(--accent);
}
</style>

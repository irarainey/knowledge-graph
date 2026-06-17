<script setup lang="ts">
import type { LayoutMode } from '../types'
import type { GraphViewMode } from '../version'

defineProps<{
  nodeCount: number
  edgeCount: number
  layout: LayoutMode
  viewKind: GraphViewMode['kind']
  asOfDate: string
}>()

const emit = defineEmits<{
  reset: []
  'set-layout': [mode: LayoutMode]
  'set-view-kind': [kind: GraphViewMode['kind']]
  'set-as-of-date': [date: string]
}>()

function setAsOfDate(event: Event): void {
  const target = event.target as HTMLInputElement | null
  if (target) emit('set-as-of-date', target.value)
}
</script>

<template>
  <header>
    <div class="brand">
      <span class="brand-title">⬡ Knowledge Graph Explorer</span>
      <span class="brand-sub">Operational + SDLC domains</span>
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
      <div class="view-mode">
        <div class="layout-toggle">
          <button
            class="btn-layout"
            :class="{ active: viewKind === 'current' }"
            @click="$emit('set-view-kind', 'current')"
          >
            Current
          </button>
          <button
            class="btn-layout"
            :class="{ active: viewKind === 'as-of' }"
            @click="$emit('set-view-kind', 'as-of')"
          >
            As-of
          </button>
        </div>
        <input
          v-if="viewKind === 'as-of'"
          class="date-input"
          type="date"
          :value="asOfDate"
          @input="setAsOfDate"
        />
      </div>
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

.view-mode {
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

.date-input {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  color-scheme: dark;
  font-family: inherit;
  font-size: 13px;
  padding: 5px 8px;
}
.date-input:focus {
  border-color: var(--accent);
  outline: none;
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

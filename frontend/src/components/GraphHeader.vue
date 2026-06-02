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
    <div class="logo">ONTOLOGY</div>
    <div class="header-divider"></div>
    <div class="stat-pill">
      NODES <strong>{{ nodeCount }}</strong>
    </div>
    <div class="header-divider"></div>
    <div class="stat-pill">
      EDGES <strong>{{ edgeCount }}</strong>
    </div>
    <div class="header-divider"></div>
    <div class="stat-pill">EXAMPLE <strong>G-ECHO · C172S</strong></div>
    <div class="header-right">
      <div class="layout-toggle">
        <button
          class="btn-layout"
          :class="{ active: layout === 'force' }"
          @click="$emit('set-layout', 'force')"
        >
          ✦ FORCE
        </button>
        <button
          class="btn-layout"
          :class="{ active: layout === 'radial' }"
          @click="$emit('set-layout', 'radial')"
        >
          ◎ RADIAL
        </button>
      </div>
      <button class="btn-reset" @click="$emit('reset')">⟳ RESET</button>
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
  background: linear-gradient(180deg, rgba(8, 12, 16, 0.98) 0%, rgba(8, 12, 16, 0.85) 100%);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 24px;
  backdrop-filter: blur(8px);
}

.logo {
  font-family: 'Orbitron', sans-serif;
  font-weight: 900;
  font-size: 13px;
  letter-spacing: 0.15em;
  color: var(--amber);
  text-shadow: 0 0 20px rgba(232, 160, 32, 0.5);
  white-space: nowrap;
}

.logo span {
  color: var(--text-dim);
  font-weight: 400;
}

.header-divider {
  width: 1px;
  height: 28px;
  background: var(--border);
  flex-shrink: 0;
}

.stat-pill {
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 6px;
}
.stat-pill strong {
  color: var(--amber);
  font-size: 13px;
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
}
.btn-layout {
  background: transparent;
  border: none;
  border-right: 1px solid var(--border);
  color: var(--text-dim);
  font-family: 'Share Tech Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.1em;
  padding: 5px 10px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-layout:last-child {
  border-right: none;
}
.btn-layout:hover {
  color: var(--amber);
  background: rgba(232, 160, 32, 0.08);
}
.btn-layout.active {
  color: var(--amber);
  background: rgba(232, 160, 32, 0.15);
  text-shadow: 0 0 8px rgba(232, 160, 32, 0.4);
}

.btn-reset {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--amber);
  font-family: 'Share Tech Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.1em;
  padding: 5px 12px;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-reset:hover {
  background: rgba(232, 160, 32, 0.1);
  border-color: var(--amber);
  box-shadow: 0 0 12px rgba(232, 160, 32, 0.2);
}
</style>

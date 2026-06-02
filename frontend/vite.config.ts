import { fileURLToPath, URL } from 'node:url'
import { readFile } from 'node:fs/promises'
import { resolve, normalize } from 'node:path'
import { cpSync, existsSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'

// Absolute path to the repo-level data directory that holds the knowledge graph
// JSON and the optional node-style overrides.
const dataDir = fileURLToPath(new URL('../data', import.meta.url))

// Serve files from ../data under the /data/ URL during dev, and copy them into
// the build output so the same fetch('/data/...') calls work in production.
function dataDirPlugin(): Plugin {
  const contentType = (file: string): string =>
    file.endsWith('.json')
      ? 'application/json'
      : file.endsWith('.ttl')
        ? 'text/turtle'
        : 'application/octet-stream'

  return {
    name: 'serve-data-dir',
    configureServer(server) {
      server.middlewares.use('/data', async (req, res, next) => {
        try {
          const rel = decodeURIComponent((req.url ?? '/').split('?')[0])
          const target = normalize(resolve(dataDir, '.' + rel))
          // Reject any path that escapes the data directory.
          if (target !== dataDir && !target.startsWith(dataDir + '/')) {
            res.statusCode = 403
            res.end('Forbidden')
            return
          }
          const body = await readFile(target)
          res.setHeader('Content-Type', contentType(target))
          res.end(body)
        } catch {
          next()
        }
      })
    },
    closeBundle() {
      const out = fileURLToPath(new URL('./dist/data', import.meta.url))
      if (existsSync(dataDir)) {
        cpSync(dataDir, out, { recursive: true })
      }
    },
  }
}

export default defineConfig({
  plugins: [vue(), dataDirPlugin()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
  },
})

import { readFile } from 'node:fs/promises'
import test from 'node:test'
import assert from 'node:assert/strict'

const source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
let sequence = 0
async function fixture(overrides = {}) {
  const calls = []
  const state = { connection: 'local-device', profile: 'default' }
  const job = { id: 'a'.repeat(32), session_id: 'bot-target', state: 'pending', admission_id: null }
  const gateway = {
    async request(method, params) {
      calls.push({ method, params })
      if (overrides.request) {
        const value = await overrides.request(method, params, state)
        if (value !== undefined) return value
      }
      if (method === 'session.list') return { sessions: [{ id: 'authorized-anchor', source: 'local' }] }
      if (method.endsWith('.poll')) return { backend_id: 'backend-one', jobs: [job] }
      if (method.endsWith('.claim')) return { ok: true, token: 'claim-token' }
      if (method.endsWith('.begin')) return { ok: true, session_id: job.session_id, input_id: job.id, text: 'Private wake-up instructions', title_preview: 'Follow-up: outline' }
      if (method === 'prompt.submit') return { admission_id: 'admission-one', ref: { session_id: job.session_id }, status: 'queued' }
      if (method.endsWith('.ack')) return { ok: true, cancel: false }
      if (method.endsWith('.failed')) return { ok: true }
      throw new Error('Unexpected request: ' + method)
    },
  }
  state.gateway = gateway
  globalThis.__commitmentsTestHost = {
    getGateway: () => state.gateway,
    state: { connectionId: { get: () => state.connection }, profile: { get: () => state.profile } },
  }
  const program = source.replace("import { host } from '@hermes/plugin-sdk'", 'const host = globalThis.__commitmentsTestHost') + `\n// test module ${++sequence}`
  const { default: plugin } = await import('data:text/javascript;base64,' + Buffer.from(program).toString('base64'))
  let tick, dispose
  plugin.register({ source: 'plugin:commitments', onDispose(fn) { dispose = fn }, setTimeout() {}, setInterval(fn) { tick = fn } })
  return { calls, state, job, tick, dispose }
}

test('submits a hidden queued turn to the backend-selected session, not the foreground anchor', async () => {
  const f = await fixture(); await f.tick()
  const sent = f.calls.filter(c => c.method === 'prompt.submit')
  assert.equal(sent.length, 1)
  assert.deepEqual(sent[0].params, { session_id: 'bot-target', input_id: 'a'.repeat(32), text: 'Private wake-up instructions', display_kind: 'hidden', title_preview: 'Follow-up: outline', queued: true, profile: 'default' })
  assert.equal(f.calls.find(c => c.method.endsWith('.poll')).params.session_id, 'authorized-anchor')
  assert.ok(f.calls.some(c => c.method.endsWith('.ack')))
  f.dispose()
})

test('a second window losing the claim never submits', async () => {
  const f = await fixture({ request(method) { if (method.endsWith('.claim')) return { ok: false, reason: 'claimed' } } })
  await f.tick(); assert.equal(f.calls.filter(c => c.method === 'prompt.submit').length, 0); f.dispose()
})

test('profile switch during discovery does not send to either profile', async () => {
  const f = await fixture({ request(method, params, state) { if (method === 'session.list') { state.profile = 'other'; return { sessions: [{ id: 'anchor', source: 'local' }] } } } })
  await f.tick(); assert.equal(f.calls.length, 1); f.dispose()
})

test('profile switch after begin records uncertainty on the original connection without sending', async () => {
  const f = await fixture({ request(method, params, state) { if (method.endsWith('.begin')) state.profile = 'other' } })
  await f.tick()
  assert.equal(f.calls.filter(c => c.method === 'prompt.submit').length, 0)
  const failed = f.calls.find(c => c.method.endsWith('.failed'))
  assert.equal(failed.params.profile, 'default'); f.dispose()
})

test('ambiguous submission is not retried and never falls back to visible input', async () => {
  const f = await fixture({ request(method) { if (method === 'prompt.submit') throw new Error('connection lost') } })
  await f.tick(); await f.tick()
  assert.equal(f.calls.filter(c => c.method === 'prompt.submit').length, 1)
  assert.ok(f.calls.some(c => c.method.endsWith('.failed')))
  assert.equal(f.calls.find(c => c.method === 'prompt.submit').params.display_kind, 'hidden'); f.dispose()
})

test('invalid delivery envelope is refused before submission', async () => {
  const f = await fixture({ request(method) { if (method.endsWith('.begin')) return { ok: true, input_id: 'a'.repeat(32), session_id: 'wrong-chat', text: 'no' } } })
  await f.tick(); assert.equal(f.calls.filter(c => c.method === 'prompt.submit').length, 0); f.dispose()
})

test('disabling the desktop component stops polling', async () => {
  const f = await fixture(); f.dispose(); await f.tick(); assert.equal(f.calls.length, 0)
})

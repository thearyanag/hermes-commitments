import { host } from '@hermes/plugin-sdk'

// One package, two halves: Python owns policy/state; Desktop transports hidden turns.
// No DOM/CSS changes, transcript edits, credentials, or direct network requests.
export default {
  id: 'commitments',
  name: 'Commitments',
  description: 'Deliver conversational follow-ups without showing internal wake-up instructions.',
  defaultEnabled: false,
  register(ctx) {
    const namespace = ctx.source.slice('plugin:'.length)
    const clientId = crypto.randomUUID()
    let stopped = false
    let busy = false
    let retryAfter = 0
    ctx.onDispose(() => { stopped = true })

    const capture = () => ({
      gateway: host.getGateway(),
      connection: host.state.connectionId.get(),
      profile: host.state.profile.get(),
    })
    const current = stamp => !stopped && stamp.gateway === host.getGateway()
      && stamp.connection === host.state.connectionId.get()
      && stamp.profile === host.state.profile.get()

    async function tick() {
      if (stopped || busy || Date.now() < retryAfter) return
      const stamp = capture()
      if (!stamp.gateway || !stamp.profile) return
      busy = true
      // Keep this gateway object and profile for the entire transaction. Never
      // resolve the ambient active gateway again after an await.
      const request = (method, params) => stamp.gateway.request(method, { ...params, profile: stamp.profile })
      const rpc = (method, params) => request(`plugin.${namespace}.desktop.${method}`, params)
      try {
        const inventory = await request('session.list', { scope: 'stored', include_hidden: true, limit: 20 })
        if (!current(stamp)) return
        const anchor = (inventory.sessions || []).find(row => ['local', 'gui', 'desktop', 'cli', 'tui'].includes(row.source))
        const anchorId = anchor?.resolved_id || anchor?.session_id || anchor?.id
        if (!anchorId) return
        const batch = await rpc('poll', { session_id: anchorId })
        if (!current(stamp) || typeof batch.backend_id !== 'string' || !Array.isArray(batch.jobs)) return
        for (const job of batch.jobs) {
          if (!current(stamp)) break
          if (typeof job.id !== 'string' || typeof job.session_id !== 'string') continue
          const base = { id: job.id, session_id: job.session_id, backend_id: batch.backend_id, client_id: clientId }
          if (job.state === 'accepted' && job.admission_id) {
            const receipt = await request('prompt.receipt', { session_id: job.session_id, admission_id: job.admission_id })
            const result = await rpc('receipt', { ...base, receipt })
            if (result.cancel && receipt.status !== 'terminal') {
              await request('prompt.cancel', { session_id: job.session_id, admission_id: job.admission_id })
            }
            continue
          }
          const claim = await rpc('claim', base)
          if (!claim.ok || !current(stamp)) continue
          const claimed = { ...base, token: claim.token }
          let begun = false
          try {
            const payload = await rpc('begin', claimed)
            if (!payload.ok) continue
            begun = true
            if (!current(stamp)) throw new Error('Desktop route changed before submission')
            if (payload.session_id !== job.session_id || payload.input_id !== job.id || typeof payload.text !== 'string') {
              throw new Error('Desktop dispatch envelope mismatch')
            }
            const receipt = await request('prompt.submit', {
              session_id: payload.session_id,
              input_id: payload.input_id,
              text: payload.text,
              display_kind: 'hidden',
              title_preview: payload.title_preview,
              queued: true,
            })
            if (receipt.ref?.session_id !== job.session_id || typeof receipt.admission_id !== 'string') {
              throw new Error('Desktop admission receipt mismatch')
            }
            const ack = await rpc('ack', { ...claimed, receipt })
            if (ack.cancel && receipt.status !== 'terminal') {
              await request('prompt.cancel', { session_id: job.session_id, admission_id: receipt.admission_id })
            }
          } catch (error) {
            // No visible-input fallback and no blind resend on ambiguous errors.
            if (begun) {
              try { await rpc('failed', claimed) } catch { /* backend keeps the unknown occurrence */ }
            }
            retryAfter = Date.now() + 10000
            console.warn('[Commitments] Hidden delivery deferred; inspect /commitments status.', error?.name || 'Error')
          }
        }
      } catch (error) {
        retryAfter = Date.now() + 10000
        console.warn('[Commitments] Desktop bridge unavailable.', error?.name || 'Error')
      } finally {
        busy = false
      }
    }
    ctx.setTimeout(tick, 0)
    ctx.setInterval(tick, 2500)
  },
}

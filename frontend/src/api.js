async function parseError(r) {
  const text = await r.text()
  try {
    const body = JSON.parse(text)
    if (body && body.detail) return body.detail
  } catch (_) { /* not JSON */ }
  return `${r.status} ${text}`
}

export async function getJSON(url) {
  const r = await fetch(url)
  if (!r.ok) throw new Error(await parseError(r))
  return r.json()
}

export async function postJSON(url, body = {}) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await parseError(r))
  return r.json()
}

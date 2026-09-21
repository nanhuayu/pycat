export class Api {
  constructor() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    this.token = fragment.get('token') || sessionStorage.getItem('pycat-token') || '';
    if (fragment.has('token')) history.replaceState(null, '', location.pathname);
    if (this.token) sessionStorage.setItem('pycat-token', this.token);
  }
  async request(path, {method = 'GET', body, raw = false, signal} = {}) {
    const headers = {Authorization: `Bearer ${this.token}`, 'X-Request-ID': crypto.randomUUID()};
    if (body !== undefined && !raw) headers['Content-Type'] = 'application/json';
    const response = await fetch('/api' + path, {method, headers, body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal, credentials: 'omit'});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(typeof data.detail === 'string' ? data.detail : data.error || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return response;
  }
  async json(path, options) { return (await this.request(path, options)).json(); }
  operation(name, args = {}) { return this.json('/operations/' + encodeURIComponent(name), {method: 'POST', body: args}); }
  input(request) { return this.json('/input', {method: 'POST', body: request}); }
  async upload(session, file) {
    return this.json(`/sessions/${encodeURIComponent(session)}/attachments?name=${encodeURIComponent(file.name)}`, {method: 'POST', body: file, raw: true});
  }
  async download(session, path, filename) {
    const response = await this.request(`/sessions/${encodeURIComponent(session)}/${path}`);
    const address = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = address; link.download = filename;
    link.click(); setTimeout(() => URL.revokeObjectURL(address), 1000);
  }
  async *events(run, {after = 0, signal} = {}) {
    let cursor = after, failures = 0;
    while (!signal?.aborted) {
      try {
        const response = await this.request(`/runs/${run}/events?after=${cursor}`, {signal});
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffered = '';
        try {
          while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffered += decoder.decode(chunk.value, {stream: true});
            let boundary;
            while ((boundary = buffered.indexOf('\n\n')) >= 0) {
              const frame = buffered.slice(0, boundary); buffered = buffered.slice(boundary + 2);
              const data = frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
              if (!data) continue;
              const event = JSON.parse(data); cursor = event.cursor ?? cursor; failures = 0;
              yield event;
              if (event.type === 'final' || event.type === 'reset' && event.done) return;
            }
          }
        } finally { reader.releaseLock(); }
        const state = await this.json(`/runs/${run}`, {signal});
        if (state.done) { yield {type: 'final', ...state.final, cursor: state.cursor}; return; }
      } catch (error) {
        if (signal?.aborted) return;
        if (error.status === 401 || error.status === 422 || ++failures > 5) throw error;
        await new Promise(resolve => setTimeout(resolve, Math.min(5000, 250 * 2 ** failures)));
      }
    }
  }
}

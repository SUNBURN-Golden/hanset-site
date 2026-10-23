/* Persistent protocol. Restart never turns an ambiguous start into a second DOM read. */
(function (root) {
  class Clipper {
    constructor(io) { this.io = io; }
    async save(job) { await this.io.save(job); return job; }
    async deliver(job) {
      if (!job.data) return job;
      try {
        const ack = await this.io.request('/result', {reservation: job.reservation, data: job.data});
        job.ack = ack;
        job.state = 'acked';
        delete job.data;
        await this.save(job);
      } catch (e) {
        job.reason = e.message === 'offline' ? 'offline' : 'result_rejected';
        if (e.message !== 'offline') { job.state = 'expired'; delete job.data; }
        await this.save(job);
      }
      return job;
    }
    async start(job) {
      if (job.state !== 'intent') return this.resume(job);
      try {
        job.reservation = await this.io.request('/reserve', {clip_id: job.clip_id, url: job.url, document_id: job.document_id});
        job.state = 'reserved';
        await this.save(job);
        job.state = 'begin_requested';
        await this.save(job);
        job.reservation = await this.io.request('/begin', {reservation: job.reservation});
        // The durable marker precedes every actual DOM extraction.
        job.state = 'extracting';
        await this.save(job);
        job.data = await this.io.extract(job);
        job.state = 'ready';
        await this.save(job);
        return this.deliver(job);
      } catch (e) {
        if (job.state !== 'intent') job.state = 'uncertain';
        job.reason = e.message === 'offline' ? 'offline' : 'collection_stopped';
        await this.save(job);
        return job;
      }
    }
    async resume(job) {
      if (job.state === 'ready') return this.deliver(job);
      if (['extracting', 'begin_requested', 'reserved', 'uncertain'].includes(job.state)) {
        // No automatic begin/extract, including when status says 'running'.
        job.state = 'uncertain';
        try { await this.io.request('/revoke', {reservation: job.reservation}); } catch { /* retain checkpoint */ }
        await this.save(job);
      }
      return job;
    }
  }
  root.HANSETClipper = Clipper;
  if (typeof module !== 'undefined') module.exports = Clipper;
})(globalThis);

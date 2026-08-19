import { randomUUID } from 'node:crypto';
/** Suspends Cursor MCP calls while Agent Virtualization and DSH execute them. */
export class GenericToolBroker {
    send;
    pending = new Map();
    closed;
    constructor(send) {
        this.send = send;
    }
    async invoke(name, arguments_, signal) {
        if (this.closed !== undefined)
            throw this.closed;
        if (this.pending.size > 0)
            throw new Error('concurrent Cursor MCP calls are not allowed');
        const id = randomUUID();
        const result = new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
        });
        const onAbort = () => {
            const pending = this.pending.get(id);
            if (pending === undefined)
                return;
            this.pending.delete(id);
            pending.reject(signal?.reason instanceof Error ? signal.reason : new Error('tool call aborted'));
        };
        signal?.addEventListener('abort', onAbort, { once: true });
        try {
            await this.send({ type: 'tool.call', id, name, arguments: arguments_ });
            return await result;
        }
        catch (error) {
            this.pending.delete(id);
            throw error;
        }
        finally {
            signal?.removeEventListener('abort', onAbort);
        }
    }
    resolve(message) {
        if (message.id === undefined)
            throw new Error('generic tool result is missing id');
        const pending = this.pending.get(message.id);
        if (pending === undefined)
            throw new Error(`no pending generic tool call ${JSON.stringify(message.id)}`);
        this.pending.delete(message.id);
        pending.resolve(message);
    }
    close(error) {
        if (this.closed !== undefined)
            return;
        this.closed = error;
        for (const pending of this.pending.values())
            pending.reject(error);
        this.pending.clear();
    }
    get size() {
        return this.pending.size;
    }
}
//# sourceMappingURL=generic-tool-broker.js.map
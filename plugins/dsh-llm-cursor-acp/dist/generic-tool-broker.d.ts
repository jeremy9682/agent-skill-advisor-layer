import type { GenericToolResult } from './types.js';
/** Suspends Cursor MCP calls while Agent Virtualization and DSH execute them. */
export declare class GenericToolBroker {
    private readonly send;
    private readonly pending;
    private closed;
    constructor(send: (message: unknown) => Promise<void>);
    invoke(name: string, arguments_: unknown, signal?: AbortSignal): Promise<GenericToolResult>;
    resolve(message: GenericToolResult): void;
    close(error: Error): void;
    get size(): number;
}
//# sourceMappingURL=generic-tool-broker.d.ts.map
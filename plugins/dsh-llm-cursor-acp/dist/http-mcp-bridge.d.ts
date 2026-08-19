import { GenericToolBroker } from './generic-tool-broker.js';
export interface McpToolDefinition {
    readonly name: string;
    readonly description: string;
    readonly inputSchema: Record<string, unknown>;
}
/** Private stateless Streamable HTTP MCP server for one Cursor ACP session. */
export declare class HttpMcpBridge {
    private readonly broker;
    private readonly maxBodyBytes;
    readonly token: string;
    readonly serverName: string;
    private readonly path;
    private readonly server;
    private started;
    private closed;
    private endpointValue;
    private hostValue;
    private readonly toolsByName;
    constructor(tools: readonly McpToolDefinition[], broker: GenericToolBroker, maxBodyBytes: number);
    start(): Promise<void>;
    get endpoint(): string;
    close(): Promise<void>;
    private handle;
    private dispatch;
}
//# sourceMappingURL=http-mcp-bridge.d.ts.map
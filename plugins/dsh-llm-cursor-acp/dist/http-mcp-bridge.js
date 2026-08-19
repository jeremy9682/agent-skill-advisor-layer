import { randomBytes, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { GenericToolBroker } from './generic-tool-broker.js';
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function authorized(expected, actual) {
    if (actual === undefined || !actual.startsWith('Bearer '))
        return false;
    const supplied = actual.slice('Bearer '.length);
    const left = Buffer.from(expected);
    const right = Buffer.from(supplied);
    return left.length === right.length && timingSafeEqual(left, right);
}
function response(res, status, value) {
    if (value === undefined) {
        res.writeHead(status, { 'cache-control': 'no-store' });
        res.end();
        return;
    }
    const body = JSON.stringify(value);
    res.writeHead(status, {
        'cache-control': 'no-store',
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body),
    });
    res.end(body);
}
function errorResponse(id, code, message) {
    return { jsonrpc: '2.0', id: id ?? null, error: { code, message } };
}
async function readBody(req, maxBytes) {
    const chunks = [];
    let size = 0;
    for await (const value of req) {
        const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
        size += chunk.byteLength;
        if (size > maxBytes)
            throw new Error('MCP request body exceeds configured limit');
        chunks.push(chunk);
    }
    if (size === 0)
        throw new Error('MCP request body is empty');
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
/** Private stateless Streamable HTTP MCP server for one Cursor ACP session. */
export class HttpMcpBridge {
    broker;
    maxBodyBytes;
    token = randomBytes(32).toString('hex');
    serverName = `dsh-${randomBytes(8).toString('hex')}`;
    path = `/mcp/${randomBytes(16).toString('hex')}`;
    server;
    started = false;
    closed = false;
    endpointValue;
    hostValue;
    toolsByName;
    constructor(tools, broker, maxBodyBytes) {
        this.broker = broker;
        this.maxBodyBytes = maxBodyBytes;
        if (!Number.isSafeInteger(maxBodyBytes) || maxBodyBytes <= 0)
            throw new Error('maxBodyBytes must be positive');
        this.toolsByName = new Map(tools.map(tool => [tool.name, tool]));
        if (this.toolsByName.size !== tools.length)
            throw new Error('MCP tool names must be unique');
        this.server = createServer((req, res) => { void this.handle(req, res); });
    }
    async start() {
        if (this.closed)
            throw new Error('MCP bridge is closed');
        if (this.started)
            return;
        await new Promise((resolve, reject) => {
            const onError = (error) => reject(error);
            this.server.once('error', onError);
            this.server.listen(0, '127.0.0.1', () => {
                this.server.off('error', onError);
                resolve();
            });
        });
        const address = this.server.address();
        this.hostValue = `127.0.0.1:${String(address.port)}`;
        this.endpointValue = `http://${this.hostValue}${this.path}`;
        this.started = true;
    }
    get endpoint() {
        if (this.endpointValue === undefined)
            throw new Error('MCP bridge is not started');
        return this.endpointValue;
    }
    async close() {
        if (this.closed)
            return;
        this.closed = true;
        if (!this.started)
            return;
        await new Promise(resolve => this.server.close(() => resolve()));
    }
    async handle(req, res) {
        if (req.url !== this.path) {
            response(res, 404);
            return;
        }
        if (this.hostValue === undefined || req.headers.host !== this.hostValue) {
            response(res, 400);
            return;
        }
        if (!authorized(this.token, req.headers.authorization)) {
            response(res, 401);
            return;
        }
        if (req.method === 'GET') {
            response(res, 405);
            return;
        }
        if (req.method !== 'POST') {
            response(res, 405);
            return;
        }
        const contentType = req.headers['content-type'];
        if (typeof contentType !== 'string' || !/^application\/json(?:\s*;|$)/iu.test(contentType)) {
            response(res, 415);
            return;
        }
        let raw;
        try {
            raw = await readBody(req, this.maxBodyBytes);
        }
        catch (error) {
            response(res, error instanceof SyntaxError ? 400 : 413, errorResponse(null, -32700, 'invalid MCP request body'));
            return;
        }
        if (!isRecord(raw) || raw.jsonrpc !== '2.0' || typeof raw.method !== 'string') {
            response(res, 400, errorResponse(isRecord(raw) ? raw.id : null, -32600, 'invalid MCP JSON-RPC request'));
            return;
        }
        const request = raw;
        if (request.id === undefined) {
            response(res, 202);
            return;
        }
        try {
            const result = await this.dispatch(request.method, request.params, req);
            response(res, 200, { jsonrpc: '2.0', id: request.id, result });
        }
        catch (error) {
            response(res, 200, errorResponse(request.id, -32000, error instanceof Error ? error.message : 'MCP call failed'));
        }
    }
    async dispatch(method, params, req) {
        switch (method) {
            case 'initialize': {
                const object = isRecord(params) ? params : {};
                return {
                    protocolVersion: typeof object.protocolVersion === 'string' ? object.protocolVersion : '2025-11-25',
                    capabilities: { tools: {} },
                    serverInfo: { name: this.serverName, version: '0.1.0' },
                };
            }
            case 'ping': return {};
            case 'tools/list':
                return { tools: [...this.toolsByName.values()] };
            case 'tools/call': {
                if (!isRecord(params) || typeof params.name !== 'string')
                    throw new Error('invalid MCP tools/call parameters');
                if (!this.toolsByName.has(params.name))
                    throw new Error(`unknown MCP tool ${JSON.stringify(params.name)}`);
                const controller = new AbortController();
                req.once('aborted', () => controller.abort(new Error('Cursor disconnected from MCP call')));
                const result = await this.broker.invoke(params.name, params.arguments, controller.signal);
                return {
                    content: [{ type: 'text', text: result.content }],
                    isError: !result.success,
                };
            }
            default: throw new Error(`unsupported MCP request ${method}`);
        }
    }
}
//# sourceMappingURL=http-mcp-bridge.js.map
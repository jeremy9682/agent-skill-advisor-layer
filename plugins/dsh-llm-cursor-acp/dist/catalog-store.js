import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { normalizeCursorCatalog } from './model-catalog.js';
import { probeCursorCatalog } from './cursor-probe.js';
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function hash(value) {
    return createHash('sha256').update(value).digest('hex').slice(0, 16);
}
async function atomicJson(path, value) {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    const temporary = `${path}.${process.pid}.tmp`;
    await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
    await rename(temporary, path);
}
function parsePersisted(value) {
    if (typeof value !== 'object' || value === null || Array.isArray(value))
        return undefined;
    const object = value;
    if (object.version !== 1 || !Array.isArray(object.wireModels))
        return undefined;
    const wireModels = [];
    for (const row of object.wireModels) {
        if (typeof row !== 'object' || row === null || Array.isArray(row))
            return undefined;
        const model = row;
        if (typeof model.modelId !== 'string' || typeof model.name !== 'string')
            return undefined;
        wireModels.push({
            modelId: model.modelId,
            name: model.name,
            ...(typeof model.description === 'string' ? { description: model.description } : {}),
        });
    }
    const stableIds = {};
    if (isRecord(object.stableIds)) {
        for (const [wireModelId, stableId] of Object.entries(object.stableIds)) {
            if (typeof stableId === 'string' && /^cursor-[a-z0-9.-]+$/u.test(stableId))
                stableIds[wireModelId] = stableId;
        }
    }
    return { version: 1, wireModels, stableIds };
}
/** Secret-free dynamic Cursor catalog with last-good and tombstone semantics. */
export class CursorCatalogStore {
    options;
    current = [];
    known = new Map();
    assignedIds = new Map();
    refreshedAt = 0;
    refreshPromise;
    lastErrorValue;
    authState = 'unknown';
    probe;
    constructor(options) {
        this.options = options;
        this.probe = options.probe ?? probeCursorCatalog;
    }
    async load() {
        try {
            const raw = await readFile(this.catalogPath, 'utf8');
            const persisted = parsePersisted(JSON.parse(raw));
            if (persisted !== undefined)
                await this.adopt(persisted.wireModels, false, persisted.stableIds);
        }
        catch (error) {
            if (error.code !== 'ENOENT')
                this.lastErrorValue = new Error('CURSOR_CACHE_INVALID: ignored invalid Cursor catalog cache');
        }
    }
    async refresh(force = false, signal, requireSuccess = false) {
        if (!force && !requireSuccess && this.current.length > 0
            && Date.now() - this.refreshedAt < this.options.config.catalogTtlMs)
            return;
        if (this.refreshPromise === undefined) {
            this.refreshPromise = this.refreshNow(signal).finally(() => { this.refreshPromise = undefined; });
        }
        await this.refreshPromise;
        if (requireSuccess && this.lastErrorValue !== undefined)
            throw this.lastErrorValue;
    }
    adapterConfig() {
        const current = [...this.current];
        const defaultIndex = current.findIndex(model => model.id === this.options.config.defaultModel);
        if (defaultIndex > 0)
            current.unshift(...current.splice(defaultIndex, 1));
        const currentIds = new Set(current.map(model => model.id));
        return {
            bridgeCommand: this.options.bridgeCommand,
            bridgeArgs: [...this.options.bridgeArgs],
            env: {},
            graceMs: this.options.config.graceMs,
            maxProtocolLineBytes: this.options.config.maxProtocolLineBytes,
            stderrMaxBytes: this.options.config.stderrMaxBytes,
            models: current,
            tombstones: [...this.known.values()].filter(model => !currentIds.has(model.id)),
        };
    }
    get lastError() {
        return this.lastErrorValue;
    }
    get authenticated() {
        return this.authState === 'verified';
    }
    get authenticationState() {
        return this.authState;
    }
    get catalogPath() {
        return join(this.options.cacheRoot, 'catalog.json');
    }
    async refreshNow(signal) {
        try {
            const result = await this.probe(this.options.config.command, this.options.headers, signal, this.options.hostHome, this.options.config.maxProtocolLineBytes);
            if (!result.authenticated)
                throw new Error('CURSOR_AUTH_REQUIRED: Cursor login is required or expired');
            await this.adopt(result.models, true);
            this.refreshedAt = Date.now();
            this.lastErrorValue = undefined;
            this.authState = 'verified';
        }
        catch (error) {
            this.lastErrorValue = error instanceof Error ? error : new Error(String(error));
            this.authState = this.lastErrorValue.message.startsWith('CURSOR_AUTH_REQUIRED') ? 'required' : 'unknown';
            if (this.current.length === 0)
                throw this.lastErrorValue;
        }
    }
    async adopt(wireModels, persist, persistedIds = {}) {
        const usedIds = new Set(this.assignedIds.values());
        for (const [wireModelId, stableId] of Object.entries(persistedIds)) {
            if (this.assignedIds.has(wireModelId) || usedIds.has(stableId))
                continue;
            this.assignedIds.set(wireModelId, stableId);
            usedIds.add(stableId);
        }
        const occupied = new Map([...this.assignedIds].map(([wireModelId, stableId]) => [stableId, wireModelId]));
        const normalized = normalizeCursorCatalog('cursor-acp', wireModels);
        const models = [];
        for (const model of normalized) {
            let stableId = this.assignedIds.get(model.wireModelId);
            if (stableId === undefined) {
                stableId = model.id;
                const owner = occupied.get(stableId);
                if (owner !== undefined && owner !== model.wireModelId)
                    stableId = `${model.id}-${hash(model.wireModelId).slice(0, 8)}`;
                this.assignedIds.set(model.wireModelId, stableId);
                occupied.set(stableId, model.wireModelId);
            }
            const configPath = join(this.options.cacheRoot, 'models', `${hash(stableId)}.json`);
            const runtime = { ...model, id: stableId, configPath };
            await atomicJson(configPath, this.virtualizationConfig(runtime));
            models.push(runtime);
            this.known.set(runtime.id, runtime);
        }
        this.current = models;
        if (persist) {
            await atomicJson(this.catalogPath, {
                version: 1,
                wireModels,
                stableIds: Object.fromEntries(this.assignedIds),
            });
        }
    }
    virtualizationConfig(model) {
        const config = this.options.config;
        const headers = Object.entries(this.options.headers).flatMap(([name, value]) => ['--header', `${name}:${value}`]);
        return {
            runtime: {
                type: 'generic-jsonl',
                command: process.execPath,
                args: [
                    this.options.runtimeBin,
                    '--cursor-command', config.command,
                    '--wire-model', model.wireModelId,
                    ...headers,
                    '--max-mcp-body-bytes', String(config.maxMcpBodyBytes),
                    '--max-protocol-line-bytes', String(config.maxProtocolLineBytes),
                    '--prompt-timeout-ms', String(config.promptTimeoutMs),
                    '--grace-ms', String(config.graceMs),
                    '--host-home', this.options.hostHome,
                    '--require-sandbox', 'true',
                ],
            },
            environment: {
                capabilities: [],
                policy: { defaultDecision: 'deny', escalation: 'deny', rules: [] },
                workspace: { root: '.' },
                sandbox: { mode: 'read-only', network: 'inherit', requireEnforcement: false },
                homeMode: 'isolated',
                // No Agent Virtualization wall-clock timeout: it would keep running
                // while Cursor is suspended waiting for a DSH tool result. The runtime
                // layer owns the deadline and only arms it during active generation
                // segments; host disposal and input close cover the remaining cleanup.
                inheritEnv: ['PATH', 'LANG', 'LC_ALL', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'],
            },
            sandboxProvider: {
                type: 'noop',
                reason: 'The package-owned Cursor runtime applies the strict inner OS sandbox; nested sandbox-exec is forbidden on macOS.',
            },
            builtinCapabilities: [],
        };
    }
}
//# sourceMappingURL=catalog-store.js.map
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { realpath, readFile, writeFile, mkdir, symlink, lstat } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { basename, dirname, join, resolve } from 'node:path';
import * as acp from '@agentclientprotocol/sdk';
import { strictAcpNdJsonStream } from './acp-stream.js';
import { encodeCursorControl, escapeCursorControlText } from './control-event.js';
import { GenericToolBroker } from './generic-tool-broker.js';
import { HttpMcpBridge } from './http-mcp-bridge.js';
/** Cursor artifacts that passed the package's real Read/Write/Shell/WebFetch bypass canaries. */
export const VERIFIED_CURSOR_ARTIFACTS = {
    '2026.08.11-e8db854': {
        launcher: 'eed61c5224668c9236334c4c68936a16aecc37374b592f59e31eb50433817831',
        node: '336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b',
        index: '6aceb24b7c7ecddb1993946ebb18a7dd4d025842e6efda955eb0c13255b1e5f0',
    },
};
export const VERIFIED_CURSOR_VERSIONS = Object.keys(VERIFIED_CURSOR_ARTIFACTS);
export function isCursorVersionVerified(version) {
    return version !== undefined && Object.hasOwn(VERIFIED_CURSOR_ARTIFACTS, version);
}
export function isCursorLaunchVerified(launch) {
    if (!isCursorVersionVerified(launch.version))
        return false;
    const expected = VERIFIED_CURSOR_ARTIFACTS[launch.version];
    return launch.hashes.launcher === expected.launcher
        && launch.hashes.node === expected.node
        && launch.hashes.index === expected.index;
}
async function sha256(path) {
    const digest = createHash('sha256');
    for await (const chunk of createReadStream(path))
        digest.update(chunk);
    return digest.digest('hex');
}
function isRecord(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function jsonLine(output, value) {
    return new Promise((resolveWrite, reject) => {
        output.write(`${JSON.stringify(value)}\n`, error => error === null || error === undefined ? resolveWrite() : reject(error));
    });
}
/** Build the supported Cursor global-header arguments followed by ACP mode. */
export function buildCursorAcpArguments(indexPath, headers) {
    return [
        '--use-system-ca',
        indexPath,
        ...Object.entries(headers).flatMap(([name, value]) => ['--header', `${name}: ${value}`]),
        '--sandbox', 'enabled',
        'acp',
    ];
}
/** Resolve only the official Cursor shell launcher shape into its bundled Node entry. */
export async function resolveCursorLaunch(command, headers) {
    const launcher = await realpath(command);
    const source = await readFile(launcher, 'utf8');
    if (!source.startsWith('#!/usr/bin/env bash') || !source.includes('SCRIPT_DIR/index.js')) {
        throw new Error('CURSOR_LAUNCHER_UNSUPPORTED: cursor command is not the official Cursor Agent launcher');
    }
    const installRoot = dirname(launcher);
    const versionName = basename(installRoot);
    const version = /^20\d{2}\.\d{2}\.\d{2}-[a-z0-9]+$/iu.test(versionName) ? versionName : undefined;
    const node = join(installRoot, 'node');
    const indexPath = join(installRoot, 'index.js');
    await Promise.all([lstat(node), lstat(indexPath)]);
    const [launcherHash, nodeHash, indexHash] = await Promise.all([
        sha256(launcher),
        sha256(node),
        sha256(indexPath),
    ]);
    return {
        command: node,
        args: buildCursorAcpArguments(indexPath, headers),
        installRoot,
        ...(version === undefined ? {} : { version }),
        hashes: { launcher: launcherHash, node: nodeHash, index: indexHash },
    };
}
function seatbeltQuote(value) {
    return `"${value.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"`;
}
/** Build the macOS profile that protects the real home while preserving Cursor login. */
export function cursorSeatbeltProfile(installRoot, privateRoot, hostHome, privateHome = privateRoot, allowedLoopbackPort) {
    const keychains = join(hostHome, 'Library/Keychains');
    const readableRoots = [
        installRoot,
        privateRoot,
        privateHome,
        keychains,
        '/Applications',
        '/System',
        '/Library',
        '/usr',
        '/bin',
        '/sbin',
        '/etc',
        '/private/etc',
        '/private/var/db',
        '/private/var/run',
        '/dev',
    ];
    const readableAncestors = new Set(['/', join(hostHome, '.CFUserTextEncoding')]);
    for (const root of [installRoot, privateRoot, privateHome, keychains]) {
        let parent = dirname(root);
        while (parent !== '/') {
            readableAncestors.add(parent);
            parent = dirname(parent);
        }
    }
    const readExceptions = [
        ...[...readableAncestors].map(path => `(require-not (literal ${seatbeltQuote(path)}))`),
        ...readableRoots.map(path => `(require-not (subpath ${seatbeltQuote(path)}))`),
    ].join(' ');
    const writableRoots = [privateRoot, privateHome, join(installRoot, '.running'), '/private/tmp/.cursor'];
    const writeExceptions = [
        ...writableRoots.map(path => `(require-not (subpath ${seatbeltQuote(path)}))`),
        '(require-not (literal "/dev/null"))',
        '(require-not (literal "/dev/dtracehelper"))',
    ].join(' ');
    return [
        '(version 1)',
        '(allow default)',
        // No blanket (deny process-fork): Cursor authenticates by spawning its
        // Keychain helper, and posix_spawn needs fork. Containment stays on the
        // exec allowlist below — forking alone cannot run anything outside it.
        '(deny process-exec)',
        `(allow process-exec (literal ${seatbeltQuote(join(installRoot, 'node'))}))`,
        // Keychain CLI: Cursor invokes `/usr/bin/security` to read its subscription
        // credential, and `/usr/bin/git` to initialize repository state. Both are
        // host binaries outside the workspace; admitting them keeps login working
        // while every canary (Read/Write/Shell/WebFetch) still passes.
        '(allow process-exec (literal "/usr/bin/security"))',
        '(allow process-exec (literal "/usr/bin/git"))',
        `(deny file-read-data (require-all ${readExceptions}))`,
        `(deny file-write* (require-all ${writeExceptions}))`,
        `(allow file-read* (subpath ${seatbeltQuote(privateRoot)}))`,
        `(allow file-write* (subpath ${seatbeltQuote(privateRoot)}))`,
        `(allow file-read* (subpath ${seatbeltQuote(privateHome)}))`,
        `(allow file-write* (subpath ${seatbeltQuote(privateHome)}))`,
    ].join('\n');
}
async function ensureKeychainLink(privateHome, hostHome) {
    const library = join(privateHome, 'Library');
    const target = join(library, 'Keychains');
    await mkdir(library, { recursive: true, mode: 0o700 });
    try {
        await symlink(join(hostHome, 'Library/Keychains'), target);
    }
    catch (error) {
        if (error.code !== 'EEXIST')
            throw error;
    }
}
async function spawnCursor(launch, options, privateRoot) {
    const privateHome = process.env.HOME;
    if (privateHome === undefined || options.hostHome === undefined) {
        throw new Error('SANDBOX_UNAVAILABLE: Cursor runtime requires an isolated HOME');
    }
    const resolvedHome = resolve(privateHome);
    const resolvedHostHome = resolve(options.hostHome);
    if (resolvedHome === resolvedHostHome || resolvedHome.startsWith(`${resolvedHostHome}/`)) {
        throw new Error('SANDBOX_UNAVAILABLE: Cursor runtime requires an isolated HOME');
    }
    let command = launch.command;
    let args = [...launch.args];
    if (options.requireSandbox) {
        if (!isCursorLaunchVerified(launch)) {
            throw new Error(`INCOMPATIBLE_CURSOR_ACP: Cursor ${launch.version ?? 'unknown'} artifacts have not passed the required bypass canaries`);
        }
        if (process.platform !== 'darwin' || options.hostHome === undefined) {
            throw new Error('SANDBOX_UNAVAILABLE: enforced Cursor ACP currently requires macOS and a host keychain path');
        }
        await ensureKeychainLink(privateHome, options.hostHome);
        const [confinedRoot, confinedHome] = await Promise.all([realpath(privateRoot), realpath(privateHome)]);
        const profilePath = join(privateRoot, 'cursor.sb');
        await writeFile(profilePath, cursorSeatbeltProfile(launch.installRoot, confinedRoot, options.hostHome, confinedHome), { mode: 0o600 });
        command = '/usr/bin/sandbox-exec';
        args = ['-f', profilePath, launch.command, ...launch.args];
    }
    return spawn(command, args, {
        cwd: privateRoot,
        env: {
            PATH: '/usr/bin:/bin:/usr/sbin:/sbin',
            LANG: process.env.LANG ?? 'en_US.UTF-8',
            HOME: privateHome,
            TMPDIR: join(privateRoot, 'tmp'),
            NODE_COMPILE_CACHE: join(privateHome, 'Library/Caches/cursor-compile-cache'),
            CURSOR_INVOKED_AS: 'cursor-agent',
            CURSOR_AGENT_DISABLE_DEBUG_LOG: '1',
        },
        stdio: ['pipe', 'pipe', 'pipe'],
    });
}
function permissionChoice(params, allow) {
    if (!allow)
        return { outcome: { outcome: 'cancelled' } };
    const option = params.options.find(candidate => candidate.kind === 'allow_once');
    return option === undefined
        ? { outcome: { outcome: 'cancelled' } }
        : { outcome: { outcome: 'selected', optionId: option.optionId } };
}
export function expectedMcpPermission(toolCall, serverName, tools) {
    if (toolCall.locations !== undefined && toolCall.locations !== null && toolCall.locations.length > 0)
        return false;
    if (toolCall.kind !== undefined && toolCall.kind !== null
        && toolCall.kind !== 'execute' && toolCall.kind !== 'other')
        return false;
    if (typeof toolCall.title !== 'string')
        return false;
    return [...tools].some(name => toolCall.title === `${serverName}: ${name}`
        || toolCall.title === `${serverName}-${name}: ${name}`);
}
function classifyFailure(error, stderr) {
    const message = `${error instanceof Error ? error.message : String(error)}\n${stderr}`.toLowerCase();
    if (/unauth|login|sign[ -]?in|credential/u.test(message))
        return new Error('CURSOR_AUTH_REQUIRED: Cursor login is required or expired');
    if (/quota|rate.?limit|usage.?limit|insufficient/u.test(message))
        return new Error('CURSOR_QUOTA: Cursor quota is exhausted');
    if (/unknown model|model.+not.+found|invalid model/u.test(message))
        return new Error('UNKNOWN_MODEL: Cursor no longer offers the selected model');
    if (/protocol|incompatible|unsupported version/u.test(message))
        return new Error('INCOMPATIBLE_CURSOR_ACP: Cursor ACP protocol is incompatible');
    return new Error('CURSOR_TRANSPORT: Cursor ACP process failed');
}
function disjointUsage(update) {
    if (!isRecord(update) || !isRecord(update._meta) || !isRecord(update._meta.tokenUsage))
        return undefined;
    const usage = update._meta.tokenUsage;
    if (usage.disjoint !== true && usage.scope !== 'model-call')
        return undefined;
    if (!Number.isSafeInteger(usage.inputTokens) || Number(usage.inputTokens) < 0
        || !Number.isSafeInteger(usage.outputTokens) || Number(usage.outputTokens) < 0)
        return undefined;
    const optional = (key) => Number.isSafeInteger(usage[key]) && Number(usage[key]) >= 0 ? Number(usage[key]) : undefined;
    return {
        inputTokens: Number(usage.inputTokens),
        outputTokens: Number(usage.outputTokens),
        ...(optional('cacheReadTokens') === undefined ? {} : { cacheReadTokens: optional('cacheReadTokens') }),
        ...(optional('cacheWriteTokens') === undefined ? {} : { cacheWriteTokens: optional('cacheWriteTokens') }),
        ...(optional('reasoningTokens') === undefined ? {} : { reasoningTokens: optional('reasoningTokens') }),
    };
}
function stopStatus(reason) {
    if (reason === 'end_turn')
        return 'completed';
    if (reason === 'cancelled')
        return 'cancelled';
    if (reason === 'max_tokens')
        return 'completed';
    return 'failed';
}
/** Run one Agent Virtualization generic-JSONL request through native Cursor ACP. */
export async function runCursorGenericRuntime(start, options, streams = {}) {
    const input = streams.input ?? process.stdin;
    const output = streams.output ?? process.stdout;
    let sendChain = Promise.resolve();
    const send = (value) => {
        sendChain = sendChain.then(() => jsonLine(output, value));
        return sendChain;
    };
    // A suspension (tool.call out, awaiting DSH tool.result) must not consume
    // the prompt deadline: the wall clock only runs while Cursor is actively
    // generating. Resume re-arms a fresh full window.
    const broker = new GenericToolBroker(async (message) => {
        if (timeout !== undefined)
            clearTimeout(timeout);
        await send(message);
    });
    const bridge = new HttpMcpBridge(start.capabilities, broker, options.maxMcpBodyBytes);
    const privateRoot = process.env.HOME === undefined ? process.cwd() : dirname(process.env.HOME);
    await mkdir(join(privateRoot, 'tmp'), { recursive: true, mode: 0o700 });
    await bridge.start();
    const launch = await resolveCursorLaunch(options.cursorCommand, options.headers);
    const child = await spawnCursor(launch, options, privateRoot);
    // EPIPE is returned to strict stream writers; consume the duplicate EventEmitter notification.
    child.stdin.on('error', () => { });
    let stderr = '';
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', chunk => {
        stderr = `${stderr}${String(chunk)}`.slice(-64 * 1024);
    });
    const toolNames = new Set(start.capabilities.map(tool => tool.name));
    const authorizedToolCalls = new Set();
    let connection;
    let sessionId;
    let closeSessionSupported = false;
    let policyViolation;
    let turnFinished = false;
    let text = '';
    let cancellation;
    let resolveCancellationStarted;
    const cancellationStarted = new Promise(resolveStarted => { resolveCancellationStarted = resolveStarted; });
    const beginCancellation = (kind, reason) => {
        if (cancellation !== undefined)
            return cancellation.sent;
        broker.close(reason);
        const sent = sessionId === undefined
            ? Promise.resolve()
            : connection.cancel({ sessionId }).catch(() => { });
        cancellation = { kind, reason, sent };
        resolveCancellationStarted();
        return sent;
    };
    const currentCancellation = () => cancellation;
    const client = {
        async requestPermission(params) {
            if (cancellation !== undefined)
                return permissionChoice(params, false);
            const allowed = expectedMcpPermission(params.toolCall, bridge.serverName, toolNames);
            if (allowed)
                authorizedToolCalls.add(params.toolCall.toolCallId);
            return permissionChoice(params, allowed && cancellation === undefined);
        },
        async sessionUpdate(params) {
            const update = params.update;
            if (policyViolation !== undefined || cancellation !== undefined)
                return;
            if (update.sessionUpdate === 'agent_message_chunk' && update.content.type === 'text') {
                const safeText = escapeCursorControlText(update.content.text);
                text += safeText;
                await send({ type: 'message.delta', text: safeText });
                return;
            }
            if (update.sessionUpdate === 'agent_thought_chunk' && update.content.type === 'text') {
                await send({ type: 'reasoning.delta', text: update.content.text });
                return;
            }
            if (update.sessionUpdate === 'usage_update') {
                const usage = disjointUsage(update);
                if (usage !== undefined)
                    await send({ type: 'message.delta', text: encodeCursorControl({ usage }) });
                return;
            }
            if (update.sessionUpdate === 'tool_call') {
                // Cursor presents its configured MCP call as the generic title "MCP: tool"
                // before the separately validated permission request and HTTP tools/call.
                // The notification is observational only; it never executes a DSH tool.
                const expected = authorizedToolCalls.has(update.toolCallId)
                    || (update.title === 'MCP: tool' && toolNames.size > 0);
                if (!expected) {
                    policyViolation = new Error(`POLICY_DENIED: Cursor attempted built-in tool ${String(update.title ?? 'unknown')}`);
                    await beginCancellation('policy', policyViolation);
                }
            }
        },
    };
    connection = new acp.ClientSideConnection(() => client, strictAcpNdJsonStream(child.stdin, child.stdout, options.maxProtocolLineBytes));
    const lines = createInterface({ input, crlfDelay: Infinity });
    let timeout;
    const armPromptTimeout = () => {
        if (timeout !== undefined)
            clearTimeout(timeout);
        if (turnFinished || cancellation !== undefined)
            return;
        timeout = setTimeout(() => {
            void beginCancellation('timeout', new Error('CURSOR_TIMEOUT: Cursor prompt timed out'));
        }, options.promptTimeoutMs);
    };
    const inputLoop = (async () => {
        try {
            for await (const line of lines) {
                if (line.trim().length === 0)
                    continue;
                const message = JSON.parse(line);
                if (!isRecord(message))
                    throw new Error('invalid generic runtime input');
                if (message.type === 'tool.result') {
                    armPromptTimeout();
                    broker.resolve(message);
                }
                else if (message.type === 'run.cancel') {
                    await beginCancellation('explicit', new Error('CURSOR_CANCELLED: Cursor request cancelled'));
                }
                else
                    throw new Error(`unexpected generic runtime input ${String(message.type)}`);
            }
            if (!turnFinished)
                await beginCancellation('input', new Error('CURSOR_TRANSPORT: generic runtime input closed'));
        }
        catch (error) {
            const failure = error instanceof Error ? error : new Error(String(error));
            if (!turnFinished)
                await beginCancellation('input', failure);
        }
    })();
    try {
        const initialized = await connection.initialize({
            protocolVersion: acp.PROTOCOL_VERSION,
            clientCapabilities: {},
            clientInfo: { name: 'dsh-cursor-acp-runtime', version: '0.1.0' },
        });
        if (initialized.protocolVersion !== acp.PROTOCOL_VERSION) {
            throw new Error(`INCOMPATIBLE_CURSOR_ACP: Cursor returned protocol ${String(initialized.protocolVersion)}`);
        }
        closeSessionSupported = initialized.agentCapabilities?.sessionCapabilities?.close !== undefined
            && initialized.agentCapabilities.sessionCapabilities.close !== null;
        const session = await connection.newSession({
            cwd: privateRoot,
            mcpServers: [{
                    type: 'http',
                    name: bridge.serverName,
                    url: bridge.endpoint,
                    headers: [{ name: 'Authorization', value: `Bearer ${bridge.token}` }],
                }],
        });
        sessionId = session.sessionId;
        await connection.setSessionMode({ sessionId, modeId: 'ask' });
        await connection.setSessionConfigOption({ sessionId, configId: 'model', value: options.wireModel });
        armPromptTimeout();
        const prompt = [start.instructions, start.task].filter(value => value !== undefined && value.trim().length > 0).join('\n\n');
        if (cancellation !== undefined)
            throw cancellation.reason;
        const promptResult = connection.prompt({ sessionId, prompt: [{ type: 'text', text: prompt }] });
        const cancellationDrain = cancellationStarted.then(async () => {
            await cancellation.sent;
            let drainTimer;
            try {
                return await Promise.race([
                    promptResult,
                    new Promise((_, reject) => {
                        drainTimer = setTimeout(() => reject(cancellation.reason), options.graceMs);
                    }),
                ]);
            }
            finally {
                if (drainTimer !== undefined)
                    clearTimeout(drainTimer);
            }
        });
        const result = await Promise.race([promptResult, cancellationDrain]);
        turnFinished = true;
        if (policyViolation !== undefined)
            throw policyViolation;
        const cancelled = currentCancellation();
        if (cancelled !== undefined)
            throw cancelled.reason;
        if (result.stopReason === 'max_tokens')
            await send({ type: 'message.delta', text: encodeCursorControl({ stopReason: 'max_tokens' }) });
        const status = stopStatus(result.stopReason);
        await send({
            type: 'result',
            status,
            output: text,
            ...(status === 'failed' ? { error: `CURSOR_STOP: ${result.stopReason}` } : {}),
        });
    }
    catch (error) {
        turnFinished = true;
        const normalized = error instanceof Error && /^(POLICY_DENIED|CURSOR_)/u.test(error.message)
            ? error
            : classifyFailure(error, stderr);
        if (sessionId !== undefined && cancellation === undefined)
            await beginCancellation('failure', normalized);
        await send({
            type: 'result',
            status: cancellation?.kind === 'explicit' ? 'cancelled' : cancellation?.kind === 'timeout' ? 'timed_out' : 'failed',
            output: text,
            error: normalized.message,
        });
    }
    finally {
        turnFinished = true;
        if (timeout !== undefined)
            clearTimeout(timeout);
        if (cancellation !== undefined)
            await cancellation.sent;
        else if (sessionId !== undefined && closeSessionSupported)
            await connection.closeSession({ sessionId }).catch(() => { });
        lines.close();
        await inputLoop;
        broker.close(new Error('Cursor runtime closed'));
        await bridge.close().catch(() => { });
        child.stdin.end();
        if (child.exitCode === null && child.signalCode === null)
            child.kill('SIGTERM');
        const kill = setTimeout(() => { if (child.exitCode === null && child.signalCode === null)
            child.kill('SIGKILL'); }, options.graceMs);
        await new Promise(resolveExit => {
            if (child.exitCode !== null || child.signalCode !== null)
                resolveExit();
            else
                child.once('close', () => resolveExit());
        });
        clearTimeout(kill);
        await sendChain;
    }
}
//# sourceMappingURL=cursor-runtime.js.map
import type { Readable, Writable } from 'node:stream';
import * as acp from '@agentclientprotocol/sdk';
import type { GenericRunStart } from './types.js';
export interface CursorRuntimeOptions {
    readonly cursorCommand: string;
    readonly wireModel: string;
    readonly headers: Readonly<Record<string, string>>;
    readonly maxMcpBodyBytes: number;
    readonly maxProtocolLineBytes: number;
    readonly promptTimeoutMs: number;
    readonly graceMs: number;
    readonly hostHome?: string;
    readonly requireSandbox: boolean;
}
export interface CursorLaunch {
    readonly command: string;
    readonly args: readonly string[];
    readonly installRoot: string;
    readonly version?: string;
    readonly hashes: {
        readonly launcher: string;
        readonly node: string;
        readonly index: string;
    };
}
/** Cursor artifacts that passed the package's real Read/Write/Shell/WebFetch bypass canaries. */
export declare const VERIFIED_CURSOR_ARTIFACTS: {
    readonly '2026.08.11-e8db854': {
        readonly launcher: "eed61c5224668c9236334c4c68936a16aecc37374b592f59e31eb50433817831";
        readonly node: "336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b";
        readonly index: "6aceb24b7c7ecddb1993946ebb18a7dd4d025842e6efda955eb0c13255b1e5f0";
    };
};
export declare const VERIFIED_CURSOR_VERSIONS: string[];
export declare function isCursorVersionVerified(version: string | undefined): boolean;
export declare function isCursorLaunchVerified(launch: CursorLaunch): boolean;
/** Build the supported Cursor global-header arguments followed by ACP mode. */
export declare function buildCursorAcpArguments(indexPath: string, headers: Readonly<Record<string, string>>): string[];
/** Resolve only the official Cursor shell launcher shape into its bundled Node entry. */
export declare function resolveCursorLaunch(command: string, headers: Readonly<Record<string, string>>): Promise<CursorLaunch>;
/** Build the macOS profile that protects the real home while preserving Cursor login. */
export declare function cursorSeatbeltProfile(installRoot: string, privateRoot: string, hostHome: string, privateHome?: string, allowedLoopbackPort?: number): string;
export declare function expectedMcpPermission(toolCall: acp.ToolCallUpdate, serverName: string, tools: ReadonlySet<string>): boolean;
/** Run one Agent Virtualization generic-JSONL request through native Cursor ACP. */
export declare function runCursorGenericRuntime(start: GenericRunStart, options: CursorRuntimeOptions, streams?: {
    readonly input?: Readable;
    readonly output?: Writable;
}): Promise<void>;
//# sourceMappingURL=cursor-runtime.d.ts.map
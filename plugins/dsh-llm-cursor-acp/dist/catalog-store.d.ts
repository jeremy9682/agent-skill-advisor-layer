import { type CursorProbeResult } from './cursor-probe.js';
import type { CursorAdapterConfig, ResolvedCursorProviderConfig } from './types.js';
export interface CursorCatalogStoreOptions {
    readonly cacheRoot: string;
    readonly runtimeBin: string;
    readonly bridgeCommand: string;
    readonly bridgeArgs: readonly string[];
    readonly hostHome: string;
    readonly headers: Readonly<Record<string, string>>;
    readonly config: ResolvedCursorProviderConfig;
    readonly probe?: (command: string, headers: Readonly<Record<string, string>>, signal?: AbortSignal, hostHome?: string, maxLineBytes?: number) => Promise<CursorProbeResult>;
}
/** Secret-free dynamic Cursor catalog with last-good and tombstone semantics. */
export declare class CursorCatalogStore {
    private readonly options;
    private current;
    private readonly known;
    private readonly assignedIds;
    private refreshedAt;
    private refreshPromise;
    private lastErrorValue;
    private authState;
    private readonly probe;
    constructor(options: CursorCatalogStoreOptions);
    load(): Promise<void>;
    refresh(force?: boolean, signal?: AbortSignal, requireSuccess?: boolean): Promise<void>;
    adapterConfig(): CursorAdapterConfig;
    get lastError(): Error | undefined;
    get authenticated(): boolean;
    get authenticationState(): 'verified' | 'required' | 'unknown';
    private get catalogPath();
    private refreshNow;
    private adopt;
    private virtualizationConfig;
}
//# sourceMappingURL=catalog-store.d.ts.map
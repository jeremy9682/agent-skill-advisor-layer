import type { CursorWireModel } from './types.js';
export interface CursorProbeResult {
    readonly authenticated: boolean;
    readonly models: readonly CursorWireModel[];
}
/** Discover the live Cursor model catalog without exposing account fields. */
export declare function probeCursorCatalog(command: string, headers: Readonly<Record<string, string>>, signal?: AbortSignal, hostHome?: string, maxLineBytes?: number): Promise<CursorProbeResult>;
//# sourceMappingURL=cursor-probe.d.ts.map
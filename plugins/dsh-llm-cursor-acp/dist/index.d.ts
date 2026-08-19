import type { Context } from '@deepseek-ai/cordis';
import z from '@deepseek-ai/schemastery';
import { CursorCatalogStore } from './catalog-store.js';
import { type CursorProviderConfig, type ResolvedCursorProviderConfig } from './types.js';
export { CursorAcpAdapter, renderCursorTask } from './adapter.js';
export { CursorCatalogStore } from './catalog-store.js';
export { normalizeCursorCatalog, parseCursorWireModel } from './model-catalog.js';
export type * from './types.js';
export declare const name = "llm-cursor-acp";
export declare const inject: string[];
export declare const PROVIDER: "cursor-acp";
export declare const SETTINGS_NAMESPACE: import("@deepseek-ai/dsh-settings").SettingsNamespace;
export declare const DEFAULT_CATALOG_TTL_MS: number;
export declare const DEFAULT_PROMPT_TIMEOUT_MS: number;
export declare const DEFAULT_GRACE_MS = 5000;
export declare const DEFAULT_MAX_PROTOCOL_LINE_BYTES: number;
export declare const DEFAULT_MAX_MCP_BODY_BYTES: number;
export declare const DEFAULT_STDERR_MAX_BYTES: number;
export declare const DEFAULT_RUNTIME_BIN: string;
export declare const DEFAULT_BRIDGE_CLI: string;
export declare const Config: z<CursorProviderConfig>;
export declare function resolveConfig(config: CursorProviderConfig): ResolvedCursorProviderConfig;
export declare function providerCacheRoot(): string;
export declare function createCatalogStore(config: ResolvedCursorProviderConfig): CursorCatalogStore;
/** Register Cursor ACP as a native configurable DSH model provider. */
export declare function apply(ctx: Context, config: CursorProviderConfig): Promise<void>;
//# sourceMappingURL=index.d.ts.map
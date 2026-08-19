import type { Context } from '@deepseek-ai/cordis';
import { LlmAdapter, type GenerateOptions, type LlmModelInfo, type LlmProviderInfo, type LlmResolvedModelInfo, type ResolvedRetryPolicy, type StreamChunk } from '@deepseek-ai/dsh-llm';
import { type CursorAdapterConfig } from './types.js';
/** Serialize the complete DSH context; it is also the restart recovery record. */
export declare function renderCursorTask(options: GenerateOptions, toolsAllowed: boolean): string;
/** Native DSH adapter backed by Agent Virtualization's model-provider bridge. */
export declare class CursorAcpAdapter extends LlmAdapter {
    private readonly ctx;
    private readonly config;
    private readonly refresh?;
    private readonly active;
    private readonly live;
    private disposing;
    constructor(ctx: Context, config: () => CursorAdapterConfig, refresh?: ((force: boolean, signal?: AbortSignal, requireSuccess?: boolean) => Promise<void>) | undefined);
    providerInfo(provider: string): LlmProviderInfo;
    providerRetryPolicy(_provider: string): ResolvedRetryPolicy;
    listModels(_provider: string): Promise<readonly LlmModelInfo[]>;
    resolveModel(_provider: string, modelId: string, signal?: AbortSignal): Promise<LlmResolvedModelInfo>;
    stream(options: GenerateOptions): AsyncIterable<StreamChunk>;
    dispose(): Promise<void>;
    reset(reason?: string): Promise<void>;
    disposeSession(sessionId: string): Promise<void>;
    private resolveConfiguredModel;
    private startBridge;
    private assertOutputOwner;
    private closeBridge;
}
//# sourceMappingURL=adapter.d.ts.map
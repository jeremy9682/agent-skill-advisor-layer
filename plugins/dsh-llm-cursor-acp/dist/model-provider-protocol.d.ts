import type { Readable, Writable } from 'node:stream';
import { type ModelProviderInput, type ModelProviderOutput } from './types.js';
/** Parse and validate one untrusted Agent Virtualization output line. */
export declare function parseModelProviderOutput(line: string): ModelProviderOutput;
/** Bounded ordered NDJSON mailbox for one persistent model-provider bridge. */
export declare class ModelProviderMailbox {
    private readonly maxLineBytes;
    private bytes;
    private readonly values;
    private queuedBytes;
    private readonly waiters;
    private closed;
    constructor(input: Readable, maxLineBytes: number);
    next(signal?: AbortSignal): Promise<ModelProviderOutput>;
    close(error: Error): void;
    private push;
    private fail;
    private pushValue;
}
/** Write one host message without closing the bridge stdin. */
export declare function writeModelProviderInput(output: Writable, message: ModelProviderInput): Promise<void>;
//# sourceMappingURL=model-provider-protocol.d.ts.map
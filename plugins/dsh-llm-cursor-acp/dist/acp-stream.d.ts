import type { Readable, Writable } from 'node:stream';
import type { Stream } from '@agentclientprotocol/sdk';
/** Strict bounded ACP NDJSON framing that never logs raw protocol content. */
export declare function strictAcpNdJsonStream(output: Writable, input: Readable, maxLineBytes: number): Stream;
//# sourceMappingURL=acp-stream.d.ts.map
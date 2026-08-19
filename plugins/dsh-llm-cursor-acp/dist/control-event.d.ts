import type { TokenUsage } from '@deepseek-ai/dsh-llm';
export declare const CURSOR_CONTROL_PREFIX = "\u001Edsh-cursor:";
export type CursorControlEvent = {
    readonly stopReason: 'max_tokens';
} | {
    readonly usage: TokenUsage;
};
export declare function escapeCursorControlText(text: string): string;
export declare function encodeCursorControl(event: CursorControlEvent): string;
/** Parse only package-owned bridge controls; ordinary model text is untouched. */
export declare function parseCursorControl(message: string): CursorControlEvent | undefined;
//# sourceMappingURL=control-event.d.ts.map
#!/usr/bin/env node
export declare const CLI_ATTRIBUTION_HEADERS: {
    readonly 'user-agent': "deepseek-harness/0.1.0-rc.6 (+https://github.com/deepseek-ai/deepseek-harness)";
};
interface CliOptions {
    readonly command: 'doctor' | 'models' | 'probe';
    readonly cursorCommand?: string;
    readonly json: boolean;
    readonly refresh: boolean;
}
export declare function parseCliArguments(argv: readonly string[]): CliOptions;
export {};
//# sourceMappingURL=cli.d.ts.map
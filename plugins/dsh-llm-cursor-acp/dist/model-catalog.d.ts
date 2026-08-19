import type { CursorCatalogModel, CursorWireModel } from './types.js';
interface ParsedWireModel {
    readonly base: string;
    readonly parameters: Readonly<Record<string, string>>;
}
/** Parse Cursor's parameterized model spelling without interpreting unknown keys. */
export declare function parseCursorWireModel(modelId: string): ParsedWireModel;
/** Normalize one live Cursor catalog into deterministic DSH ids. */
export declare function normalizeCursorCatalog(provider: string, wireModels: readonly CursorWireModel[]): CursorCatalogModel[];
/** Resolve an exact stable model id without silently falling back. */
export declare function requireCatalogModel(catalog: readonly CursorCatalogModel[], modelId: string): CursorCatalogModel;
export {};
//# sourceMappingURL=model-catalog.d.ts.map
window.__ModuleLoader__.load({
	id: "@jeremy9682/dsh-llm-cursor-acp",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		//#region \0rolldown/runtime.js
		var __create = Object.create;
		var __defProp = Object.defineProperty;
		var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
		var __getOwnPropNames = Object.getOwnPropertyNames;
		var __getProtoOf = Object.getPrototypeOf;
		var __hasOwnProp = Object.prototype.hasOwnProperty;
		var __copyProps = (to, from, except, desc) => {
			if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
				key = keys[i];
				if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
					get: ((k) => from[k]).bind(null, key),
					enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
				});
			}
			return to;
		};
		var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", {
			value: mod,
			enumerable: true
		}) : target, mod));
		//#endregion
		let react = require("react");
		react = __toESM(react, 1);
		//#region src/client.ts
		const inject = [
			"slots",
			"locale",
			"connection"
		];
		const NS = "settings.cursorAcp";
		const CHANNEL = "/cursor-acp";
		const STYLE = `
.cursorAcp{display:flex;flex-direction:column;gap:12px;max-width:720px;color:var(--dsw-alias-label-primary)}
.cursorAcp h2,.cursorAcp p{margin:0}.cursorAcp h2{font-size:16px;line-height:24px;font-weight:500}
.cursorAcpCard{display:flex;flex-direction:column;gap:12px;padding:14px 16px;border:1px solid var(--dsw-alias-border-l2);border-radius:12px;background:var(--dsw-alias-bg-layer-1)}
.cursorAcpGrid{display:grid;grid-template-columns:140px minmax(0,1fr);gap:10px 12px;align-items:center}.cursorAcpGrid label{font-size:12px;color:var(--dsw-alias-label-secondary)}
.cursorAcpGrid input,.cursorAcpGrid select{width:100%;box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;padding:8px 10px;background:var(--dsw-alias-bg-module-platform);color:inherit}
.cursorAcpRow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.cursorAcpStatus{font-size:13px;color:var(--dsw-alias-label-secondary)}.cursorAcpError{font-size:13px;color:var(--dsw-alias-state-error-primary)}
.cursorAcp button{border:1px solid var(--dsw-alias-border-l2);border-radius:8px;padding:7px 12px;background:var(--dsw-alias-bg-module-platform);color:inherit;cursor:pointer}.cursorAcp button:disabled{opacity:.5;cursor:not-allowed}
@media(max-width:640px){.cursorAcpGrid{grid-template-columns:1fr}}
`;
		const copy = {
			en: {
				nav: "Cursor ACP",
				title: "Cursor ACP subscription",
				intro: "Uses the signed-in Cursor Agent subscription. Credentials and account identity never cross into this page.",
				path: "Cursor Agent path",
				defaultModel: "Preferred default",
				refresh: "Refresh models",
				save: "Save settings",
				signedIn: "Signed in",
				signedOut: "Sign-in unavailable",
				verified: "Verified",
				unknown: "Unknown",
				required: "Login required",
				loading: "Loading…",
				models: "models",
				saved: "Settings saved.",
				failed: "Cursor ACP request failed."
			},
			zh: {
				nav: "Cursor ACP",
				title: "Cursor ACP 订阅",
				intro: "使用已登录的 Cursor Agent 订阅；凭据和账户身份不会传到此页面。",
				path: "Cursor Agent 路径",
				defaultModel: "首选默认模型",
				refresh: "刷新模型",
				save: "保存设置",
				signedIn: "已登录",
				signedOut: "登录不可用",
				verified: "已验证",
				unknown: "未知",
				required: "需要登录",
				loading: "加载中…",
				models: "个模型",
				saved: "设置已保存。",
				failed: "Cursor ACP 请求失败。"
			}
		};
		function healthValue(result) {
			if (!result.ok || typeof result.value !== "object" || result.value === null) throw new Error(result.error?.message ?? "Cursor ACP RPC failed");
			return result.value;
		}
		function CursorSection({ rpc, t }) {
			const [health, setHealth] = (0, react.useState)();
			const [command, setCommand] = (0, react.useState)("");
			const [defaultModel, setDefaultModel] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [message, setMessage] = (0, react.useState)();
			const load = (endpoint) => {
				setBusy(true);
				setMessage(void 0);
				return rpc.call(CHANNEL, endpoint, {}).then(healthValue).then((next) => {
					setHealth(next);
					setCommand(next.command);
					setDefaultModel(next.defaultModel);
				}).catch((error) => setMessage(error instanceof Error ? error.message : t("failed"))).finally(() => setBusy(false));
			};
			(0, react.useEffect)(() => {
				load("status");
			}, []);
			const save = () => {
				setBusy(true);
				setMessage(void 0);
				rpc.call(CHANNEL, "configure", {
					command,
					defaultModel
				}).then((result) => {
					if (!result.ok) throw new Error(result.error?.message ?? t("failed"));
					return load("status").then(() => {
						setMessage(t("saved"));
					});
				}).catch((error) => setMessage(error instanceof Error ? error.message : t("failed"))).finally(() => setBusy(false));
			};
			const status = health === void 0 ? t("loading") : `${health.authStatus === "verified" ? t("signedIn") : t("signedOut")} · ${t(health.authStatus)} · ${String(health.models.length)} ${t("models")}${health.version === void 0 ? "" : ` · ${health.version}`}`;
			return react.default.createElement("div", { className: "cursorAcp" }, react.default.createElement("h2", null, t("title")), react.default.createElement("p", { className: "cursorAcpStatus" }, t("intro")), react.default.createElement("div", { className: "cursorAcpCard" }, react.default.createElement("div", { className: "cursorAcpStatus" }, status), react.default.createElement("div", { className: "cursorAcpGrid" }, react.default.createElement("label", { htmlFor: "cursor-acp-command" }, t("path")), react.default.createElement("input", {
				id: "cursor-acp-command",
				value: command,
				onChange: (event) => setCommand(event.currentTarget.value),
				disabled: busy
			}), react.default.createElement("label", { htmlFor: "cursor-acp-default" }, t("defaultModel")), react.default.createElement("select", {
				id: "cursor-acp-default",
				value: defaultModel,
				onChange: (event) => setDefaultModel(event.currentTarget.value),
				disabled: busy || health === void 0
			}, ...(health?.models ?? []).map((model) => react.default.createElement("option", {
				key: model.id,
				value: model.id
			}, model.name)))), react.default.createElement("div", { className: "cursorAcpRow" }, react.default.createElement("button", {
				type: "button",
				disabled: busy,
				onClick: () => {
					load("refresh");
				}
			}, t("refresh")), react.default.createElement("button", {
				type: "button",
				disabled: busy || command.trim() === "" || defaultModel.trim() === "",
				onClick: save
			}, t("save"))), message === void 0 ? null : react.default.createElement("p", { className: health?.error === void 0 ? "cursorAcpStatus" : "cursorAcpError" }, message), health?.error === void 0 ? null : react.default.createElement("p", { className: "cursorAcpError" }, health.error)));
		}
		function apply(ctx) {
			ctx.effect(() => ctx.locale.register(NS, copy), "cursor-acp: locale");
			ctx.effect(() => {
				const tag = document.createElement("style");
				tag.dataset.plugin = "@jeremy9682/dsh-llm-cursor-acp";
				tag.textContent = STYLE;
				document.head.append(tag);
				return () => tag.remove();
			}, "cursor-acp: style");
			const connection = ctx.get("connection");
			const t = ctx.locale.bind(NS);
			ctx.slots.inject("settings.section", () => ctx.slots.register({
				name: "settings.section",
				id: "cursor-acp",
				order: 16,
				label: () => t("nav"),
				locale: NS,
				inject: () => ({
					rpc: connection.rpc,
					t
				})
			}, CursorSection));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map
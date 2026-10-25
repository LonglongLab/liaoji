import { createReadStream, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import type { Plugin } from "vite";
import { defineConfig } from "vite";

import { collectUiCopy } from "./scripts/uiCopy.mjs";
import { versionForTarget } from "./scripts/releaseVersions.mjs";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));

const readJson = (relativePath: string): Record<string, unknown> =>
  JSON.parse(readFileSync(new URL(relativePath, import.meta.url), "utf8")) as Record<
    string,
    unknown
  >;

const packageJson = readJson("./package.json");
const referenceProjects = JSON.parse(
  readFileSync(new URL("../third_party/REFERENCES.json", import.meta.url), "utf8"),
) as Array<{
  name: string;
  upstream_url: string | null;
  role: string;
  license: string;
  source_path: string | null;
  commit?: string;
  version?: string;
  retrieved_on?: string;
}>;
const chatlogKeeperSource = readJson("../third_party/chatlog-keeper/SOURCE.json");
const chatlogKeeperLicense = readFileSync(
  new URL("../third_party/chatlog-keeper/LICENSE", import.meta.url),
  "utf8",
);
const contractVersion =
  /^\s*version:\s*(\S+)\s*$/m.exec(
    readFileSync(new URL("../contracts/openapi.yaml", import.meta.url), "utf8"),
  )?.[1] ?? "unknown";
const frontendDependencies = Object.entries(
  (packageJson.dependencies ?? {}) as Record<string, string>,
).map(([name, version]) => ({ name, version }));

/**
 * pdf.js 在运行时按需去取的四样东西。默认地址是空的，不给就掉字掉图：
 *   cmaps           中文 PDF 用预定义 CMap 编码、字体没内嵌时，靠它才认得出字。
 *   standard_fonts  没内嵌 base-14 字体时的替身。
 *   wasm / iccs     扫描件里的 JBIG2 / JPEG2000 解码与 ICC 色彩。
 *
 * 挂到我们自己的 `/pdfjs/` 下，**不从 CDN 取**——「解析全在本机、文件不出这台
 * 机器」那句话在 PDF 上也得成立，不能改成「顺手向 CDN 报一声用了哪些中文字体」。
 * 开发时从 node_modules 直读，构建时随产物发出去，两边同一份文件。
 */
const PDFJS_ASSET_DIRECTORIES = ["cmaps", "standard_fonts", "wasm", "iccs"];
const PDFJS_URL_PREFIX = "/pdfjs/";
const pdfjsPackageRoot = fileURLToPath(
  new URL("./node_modules/pdfjs-dist/", import.meta.url),
);

/** 开发服务器要自己报类型：wasm 的 MIME 不对时浏览器会拒绝流式编译。 */
const PDFJS_CONTENT_TYPES: Record<string, string> = {
  ".wasm": "application/wasm",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".icc": "application/vnd.iccprofile",
};

/**
 * 把 `/pdfjs/<目录>/<文件>` 解析成 node_modules 里的真实路径。
 * 只认那四个目录里的真实文件；`..` 在 `resolve` 之后再判一次，
 * 不然这就成了一个能读整个磁盘的接口。
 */
const resolvePdfjsAsset = (urlPath: string): string | null => {
  const relative = urlPath.slice(PDFJS_URL_PREFIX.length);
  const directory = relative.split("/")[0] ?? "";
  if (!PDFJS_ASSET_DIRECTORIES.includes(directory)) return null;
  const candidate = path.resolve(pdfjsPackageRoot, relative);
  const root = path.resolve(pdfjsPackageRoot, directory) + path.sep;
  if (!candidate.startsWith(root)) return null;
  try {
    return statSync(candidate).isFile() ? candidate : null;
  } catch {
    return null;
  }
};

const pdfjsRuntimeAssets = (): Plugin => ({
  name: "pdfjs-runtime-assets",
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const url = request.url ?? "";
      if (!url.startsWith(PDFJS_URL_PREFIX)) {
        next();
        return;
      }
      const file = resolvePdfjsAsset(url.split("?")[0] ?? "");
      if (file === null) {
        next();
        return;
      }
      const type = PDFJS_CONTENT_TYPES[path.extname(file)] ?? "application/octet-stream";
      response.setHeader("Content-Type", type);
      createReadStream(file).pipe(response);
    });
  },
  generateBundle() {
    for (const directory of PDFJS_ASSET_DIRECTORIES) {
      const source = path.join(pdfjsPackageRoot, directory);
      for (const entry of readdirSync(source, { withFileTypes: true })) {
        if (!entry.isFile()) continue;
        this.emitFile({
          type: "asset",
          fileName: `pdfjs/${directory}/${entry.name}`,
          source: readFileSync(path.join(source, entry.name)),
        });
      }
    }
  },
});

/**
 * 反馈录屏的**静态界面文案表**，经平台 widget 的宿主选项 `uiCopy` 交出去
 * （widget 0.1.6，用户 2026-09-29 拍板录屏默认「结构档」）。
 *
 * 这一端的页面上绝大多数文本就是敏感数据（消息正文、昵称、群名、文件名），
 * 所以录屏按 allowlist 走：默认全遮，整个文本节点 trim 后**精确命中**这张表的才
 * 原样显示。表是**产品知识**（只有本仓的模板里才有），按「配置平台下发、产品知识
 * 宿主交」的划线归宿主交；脱敏策略本身（含平台键 `staticCopy`）仍归平台下发。
 *
 * 做成**虚拟模块**而不是落一份 `ui-copy.json`：落文件就会有「文件与模板不同步」
 * 这种状态，而不同步的方向一半是「白名单里有模板已经删掉的句子」（无害），
 * 另一半是「新加的界面文案在回放里变成『字字字』」（有害但不报错，只会让人
 * 以为脱敏坏了）。虚拟模块每次构建现抽，两者不可能不同步；产物里带着这张表，
 * 离线照样可用。
 *
 * dev 与 build 走的是同一个 `load`；dev 下模板改了要重启开发服务器才更新——
 * 文案表是构建期事实，不做 HMR 追踪。
 */
const VIRTUAL_UI_COPY_ID = "virtual:feedback-ui-copy";
const RESOLVED_UI_COPY_ID = `\0${VIRTUAL_UI_COPY_ID}`;

const feedbackUiCopy = (): Plugin => ({
  name: "feedback-ui-copy",
  resolveId(id) {
    return id === VIRTUAL_UI_COPY_ID ? RESOLVED_UI_COPY_ID : null;
  },
  load(id) {
    if (id !== RESOLVED_UI_COPY_ID) return null;
    const copy = collectUiCopy(path.join(frontendRoot, "src"));
    return `export default ${JSON.stringify(copy)};\n`;
  },
});

export default defineConfig(({ mode }) => {
  const target = mode === "desktop" ? "local" : mode === "remote" ? "remote" : "remote";
  const buildBoundary = (): Plugin => ({
    name: "build-boundary",
    generateBundle(_options, bundle) {
      const modules = [...new Set(Object.values(bundle).flatMap((item) =>
        item.type === "chunk" ? Object.keys(item.modules).filter((id) => id.includes("/src/")).map((id) => path.relative(frontendRoot, id).replaceAll("\\", "/")) : [],
      ))].sort();
      const forbidden = target === "local" ? /src\/views\/(Remote(?:Conversations|Search|Settings|Setup|SignIn)|PublicShare)View\.vue/ : target === "remote" ? /src\/views\/(Onboarding|Overview|Pairing|Settings|SyncHealth|WechatCleanupHint|BoundRemoteWeb|Conversations|Search)View\.vue/ : null;
      if (forbidden !== null && modules.some((id) => forbidden.test(id))) this.error(`Unexpected page in ${target} build`);
      this.emitFile({ type: "asset", fileName: "build-target.json", source: JSON.stringify({ target, version: versionForTarget(target), modules }, null, 2) });
    },
  });
  return {
    root: frontendRoot,
    plugins: [vue(), pdfjsRuntimeAssets(), feedbackUiCopy(), buildBoundary()],
    define: {
      __APP_TARGET__: JSON.stringify(target),
      __APP_VERSION__: JSON.stringify(versionForTarget(target)),
      __CONTRACT_VERSION__: JSON.stringify(contractVersion),
      __CHATLOG_KEEPER__: JSON.stringify(chatlogKeeperSource),
      __CHATLOG_KEEPER_LICENSE__: JSON.stringify(chatlogKeeperLicense),
      __FRONTEND_DEPENDENCIES__: JSON.stringify(frontendDependencies),
      __REFERENCE_PROJECTS__: JSON.stringify(referenceProjects),
    },
    resolve: {
      alias: {
        "@workspace-routes": fileURLToPath(new URL(`./src/routes/${target}.ts`, import.meta.url)),
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      host: "127.0.0.1",
      port: 4173,
      strictPort: true,
      proxy: {
        "/api": "http://127.0.0.1:8000",
      },
    },
  };
});

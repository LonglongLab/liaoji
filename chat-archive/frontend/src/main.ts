import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { createPinia } from "pinia";
import { createApp } from "vue";
import PrimeVue from "primevue/config";
import { uiOptions } from "./lib/uiTheme";

import App from "./App.vue";
import { type ArchiveApi, createArchiveApi } from "./api/archiveApi";
import { archiveApiKey } from "./api/injection";
import { registerPrivateQueryClient } from "./lib/privateCaches";
import { browserReauthChannel } from "./lib/reauthChannel";
import { installReauthSync } from "./lib/reauthSync";
import { installWorkspaceGuard, router } from "./router";
import { useCapabilityStore } from "./stores/capabilities";
import "./styles/main.css";

const app = createApp(App);
app.use(createPinia());
app.use(PrimeVue, uiOptions);
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});
registerPrivateQueryClient(queryClient);
app.use(VueQueryPlugin, { queryClient });
// 会话失效时把端类型与能力清单作废，再让守卫按认证轴把人送回登录页。
// 只有这一处拦截 401：散在各视图里判会漏，而漏一处就是一条走不出去的死路。
// 423（日常解锁到期，PSU-2.213）同理：会话还在，但要先去锁定页，并清掉已拉到的私人数据。
const api: ArchiveApi = createArchiveApi({
  onUnauthenticated: () => {
    useCapabilityStore().loaded = false;
    void router.replace("/login");
  },
  onReauthRequired: () => {
    reauthSync.onReauthRequired();
  },
});
// 423、其他标签页的广播、bfcache 恢复，都由这一处处理。本机端没有日常解锁，不开广播通道。
const reauthSync = installReauthSync({
  router,
  api,
  channel: __APP_TARGET__ === "remote" ? browserReauthChannel() : null,
});
app.use(router);
app.provide(archiveApiKey, api);
installWorkspaceGuard(router, api);
// 从往返缓存（bfcache）恢复的页面不经过路由守卫：锁定后按「返回」可能把旧页面原样
// 带回来。恢复时重新问一次端状态；公开分享页不受影响。
window.addEventListener("pageshow", (event) => {
  if (event.persisted) void reauthSync.recheck();
});
app.mount("#app");

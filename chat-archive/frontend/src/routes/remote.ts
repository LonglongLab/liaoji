import type { RouteRecordRaw } from "vue-router";
const ExportView = () => import("@/views/ExportView.vue");
const RemoteConversationsView = () => import("@/views/RemoteConversationsView.vue");
const RemoteSearchView = () => import("@/views/RemoteSearchView.vue");
const RemoteSettingsView = () => import("@/views/RemoteSettingsView.vue");
const RemoteSetupView = () => import("@/views/RemoteSetupView.vue");
const RemoteSignInView = () => import("@/views/RemoteSignInView.vue");
const RemoteUnlockView = () => import("@/views/RemoteUnlockView.vue");
const PublicShareView = () => import("@/views/PublicShareView.vue");
const SharesView = () => import("@/views/SharesView.vue");
const StatsView = () => import("@/views/StatsView.vue");

export const routes: RouteRecordRaw[] = [
    { path: "/login", name: "login", component: RemoteSignInView, meta: { workspace: "public" } },
    { path: "/setup", name: "setup", component: RemoteSetupView, meta: { workspace: "public" } },
    { path: "/unlock", name: "unlock", component: RemoteUnlockView, meta: { workspace: "public" } },
    {
      path: "/remote/shares",
      name: "remote-shares",
      component: SharesView,
      props: { mode: "remote" },
      meta: { workspace: "remote" },
    },
    { path: "/remote/stats", name: "remote-stats", component: StatsView, props: { mode: "remote" }, meta: { workspace: "remote" } },
    { path: "/remote/export", name: "remote-export", component: ExportView, props: { mode: "remote" }, meta: { workspace: "remote", canvas: true } },
    {
      path: "/remote/conversations",
      name: "remote-conversations",
      component: RemoteConversationsView,
      meta: { workspace: "remote", canvas: true },
    },
    {
      path: "/remote/search",
      name: "remote-search",
      component: RemoteSearchView,
      meta: { workspace: "remote", canvas: true },
    },
    {
      path: "/remote/settings",
      name: "remote-settings",
      component: RemoteSettingsView,
      meta: { workspace: "remote" },
    },
    {
      path: "/s/:token",
      name: "public-share",
      component: PublicShareView,
      meta: { workspace: "share" },
    },

];

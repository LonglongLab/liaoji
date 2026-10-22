<script setup lang="ts">
// 重定向判定不在这里了：端能力与 onboarding 合成一条，由 `router.beforeEach`
// 独家执行（`installWorkspaceGuard`）。此前挂在 `watch(() => store.step)` 上的
// 那份只在 step 变化时判，直接改地址栏进得去。
</script>

<template>
  <RouterView />
</template>
